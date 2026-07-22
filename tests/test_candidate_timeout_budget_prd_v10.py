"""冻结 PRD v1.0：超时边界、聚合总预算与聚合成员健康归属。"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from app import ArkProxyRouter, ConfigStore, RouteContext
from linrouter_core.contracts import AllModelsFailedError
from linrouter_core.runtime import router_runtime
from linrouter_core.runtime.router_runtime import (
    CONNECT_TIMEOUT_SECONDS,
    FIRST_FRAME_TIMEOUT_SECONDS,
    LARGE_REQUEST_FIRST_FRAME_TIMEOUT_SECONDS,
    LARGE_REQUEST_RESPONSES_INPUT_BYTES,
    LARGE_REQUEST_TOOLS_BYTES,
    _is_large_request,
)
from settings_store import SettingsStore


def _router(tmp_path: Path) -> tuple[ArkProxyRouter, RouteContext, RouteContext]:
    config = {
        "groups": [
            {
                "id": "g1",
                "name": "test-relay",
                "provider_type": "relay",
                "base_url": "http://127.0.0.1:19999/v1",
                "route_key": "group-key",
                "auto_model_name": "lin-router-auto",
            }
        ],
        "models": [
            {"id": "m1", "name": "model-1", "ep_id": "upstream-1", "group_id": "g1", "api_key": "test-key-1", "usable": True},
            {"id": "m2", "name": "model-2", "ep_id": "upstream-2", "group_id": "g1", "api_key": "test-key-2", "usable": True},
        ],
        "aggregate_models": [{"id": "a1", "name": "aggregate-1", "route_key": "aggregate-key", "cooldown_minutes": 5}],
        "aggregate_members": [
            {"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "enabled": True, "priority": 0},
            {"id": "am2", "aggregate_id": "a1", "group_id": "g1", "model_id": "m2", "enabled": True, "priority": 1},
        ],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    router = ArkProxyRouter(ConfigStore(config_path), SettingsStore(config_path), tmp_path / "logs.jsonl")
    group = router.store.find_group("g1")
    aggregate = router.store.find_aggregate("a1")
    assert group is not None and aggregate is not None
    group_context = RouteContext(
        client_key=group.route_key,
        group=group,
        group_id=group.id,
        provider_type=group.provider_type,
        base_url=group.base_url,
        display_name=group.name,
        passthrough=False,
    )
    aggregate_context = RouteContext(
        client_key=aggregate.route_key,
        group=None,
        group_id=f"__aggregate__{aggregate.id}",
        provider_type="aggregate",
        base_url="",
        display_name=aggregate.name,
        passthrough=False,
        aggregate=aggregate,
    )
    return router, group_context, aggregate_context


def _payload(model: str, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": "test"}],
    }


class _JsonResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self) -> bytes:
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def close(self) -> None:
        return


class _CompletedStreamResponse:
    status = 200
    headers = {"Content-Type": "text/event-stream"}
    http_version = "HTTP/1.1"
    transport = "test"

    def __init__(self) -> None:
        self.lines = [
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
            b"\n",
            b'data: {"type":"response.completed"}\n',
            b"\n",
        ]
        self.first_frame_timeouts: list[float] = []

    def readline(self, timeout_seconds: float = 0) -> bytes:
        self.first_frame_timeouts.append(timeout_seconds)
        return self.lines.pop(0) if self.lines else b""

    def close(self) -> None:
        return


class _RecordingClient:
    def __init__(self) -> None:
        self.requests: list[tuple[bool, float]] = []
        self.stream_response: _CompletedStreamResponse | None = None

    def request(self, _method: str, _url: str, _headers: dict[str, str], _body: bytes, *, stream: bool, timeout: float) -> Any:
        self.requests.append((stream, timeout))
        if not stream:
            return _JsonResponse()
        self.stream_response = _CompletedStreamResponse()
        return self.stream_response


class _TimeoutResponse:
    status = 200
    headers = {"Content-Type": "text/event-stream"}
    http_version = "HTTP/1.1"
    transport = "test"

    def __init__(self, timeout_type: type[Exception], clock: dict[str, bool]) -> None:
        self._timeout_type = timeout_type
        self._clock = clock
        self.timeouts: list[float] = []

    def readline(self, timeout_seconds: float = 0) -> bytes:
        self.timeouts.append(timeout_seconds)
        self._clock["expired"] = True
        raise self._timeout_type("stream_idle_timeout")

    def close(self) -> None:
        return


class _AggregateBudgetClient:
    def __init__(self, timeout_type: type[Exception], clock: dict[str, bool]) -> None:
        self.timeout_type = timeout_type
        self.clock = clock
        self.requests: list[tuple[str, float]] = []
        self.response: _TimeoutResponse | None = None

    def request(self, _method: str, _url: str, _headers: dict[str, str], _body: bytes, *, stream: bool, timeout: float) -> _TimeoutResponse:
        assert stream is True
        self.requests.append(("stream", timeout))
        self.response = _TimeoutResponse(self.timeout_type, self.clock)
        return self.response


class _ServerErrorClient:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, _method: str, url: str, _headers: dict[str, str], _body: bytes, **_kwargs: Any) -> Any:
        self.calls += 1
        raise HTTPError(url, 503, "service unavailable", hdrs={}, fp=io.BytesIO(b'{"error":"unavailable"}'))


def test_timeout_constants_large_request_detection_and_upstream_boundaries(tmp_path: Path) -> None:
    assert CONNECT_TIMEOUT_SECONDS == 8
    assert FIRST_FRAME_TIMEOUT_SECONDS == 25
    assert LARGE_REQUEST_FIRST_FRAME_TIMEOUT_SECONDS == 45
    assert _is_large_request(b"x" * (128 * 1024 + 1), {}) is True
    assert _is_large_request(b"{}", {"tools": [{"name": "x" * (LARGE_REQUEST_TOOLS_BYTES + 1)}]}) is True
    assert _is_large_request(b"{}", {"input": "x" * (LARGE_REQUEST_RESPONSES_INPUT_BYTES + 1)}) is True
    assert _is_large_request(b"{}", {"input": "small", "tools": []}) is False

    router, group_context, _aggregate_context = _router(tmp_path)
    client = _RecordingClient()
    router.runtime.upstream = client

    status, _headers, _body = router.call("/v1/chat/completions", _payload("model-1"), group_context)
    assert status == 200
    stream_status, _stream_headers, chunks, _request_id = router.stream(
        "/v1/chat/completions",
        _payload("model-1", stream=True),
        group_context,
    )
    assert stream_status == 200
    assert b"ok" in b"".join(chunks)
    assert client.requests == [(False, CONNECT_TIMEOUT_SECONDS), (True, CONNECT_TIMEOUT_SECONDS)]
    assert client.stream_response is not None
    assert client.stream_response.first_frame_timeouts[0] == FIRST_FRAME_TIMEOUT_SECONDS


def test_aggregate_first_frame_budget_stops_before_second_upstream_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    router, _group_context, aggregate_context = _router(tmp_path)
    clock = {"expired": False}

    def controlled_perf_counter() -> float:
        return 61.0 if clock["expired"] else 0.0

    # 首帧读失败后将时间推进到 60 秒外；后续候选不得再取得一次完整首帧预算。
    monkeypatch.setattr(router_runtime.time, "perf_counter", controlled_perf_counter)
    client = _AggregateBudgetClient(router.runtime.faults.stream_idle_timeout, clock)
    router.runtime.upstream = client

    with pytest.raises(AllModelsFailedError) as captured:
        router.stream("/v1/chat/completions", _payload("aggregate-1", stream=True), aggregate_context)

    error = captured.value
    assert error.error_code == "aggregate_first_frame_timeout"
    assert error.stream_timeout is True
    assert error.request_id
    assert len(client.requests) == 1
    assert client.requests[0] == ("stream", CONNECT_TIMEOUT_SECONDS)
    assert client.response is not None
    assert client.response.timeouts == [FIRST_FRAME_TIMEOUT_SECONDS]
    assert router.store.find_aggregate_member("am1").attempt_window == ["qualified_failure"]
    assert router.store.find_model("m1").attempt_window == []
    assert any(log.event == "stream_timeout" and "first_frame_timeout" in log.detail for log in router.logs)
    assert router.logs[-1].event in {"stream_timeout", "aggregate_first_frame_timeout"}
    assert router.live_requests_payload()["count"] == 0


def test_aggregate_execution_updates_members_without_mutating_underlying_model_health(tmp_path: Path) -> None:
    router, _group_context, aggregate_context = _router(tmp_path)
    client = _ServerErrorClient()
    router.runtime.upstream = client

    with pytest.raises(AllModelsFailedError) as captured:
        router.call("/v1/chat/completions", _payload("aggregate-1"), aggregate_context)

    assert captured.value.error_code == "aggregate_members_unavailable"
    assert client.calls == 2
    assert router.store.find_aggregate_member("am1").attempt_window == ["qualified_failure"]
    assert router.store.find_aggregate_member("am2").attempt_window == ["qualified_failure"]
    assert router.store.find_model("m1").attempt_window == []
    assert router.store.find_model("m2").attempt_window == []
