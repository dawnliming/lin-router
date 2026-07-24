"""v0.6.5：聚合连接失败隔离、首帧预算与快速熔断回归。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

from app import ArkProxyRouter, ConfigStore, RouteContext
from linrouter_core.config.models import AggregateMember, ModelConfig
from linrouter_core.contracts import AllModelsFailedError
from linrouter_core.runtime import router_runtime
from linrouter_core.runtime.router_runtime import (
    FIRST_FRAME_TIMEOUT_SECONDS,
    UPSTREAM_INITIAL_RESPONSE_TIMEOUT_SECONDS,
)
from settings_store import SettingsStore


def _router(tmp_path: Path) -> tuple[ArkProxyRouter, RouteContext]:
    config = {
        "groups": [
            {
                "id": "g1",
                "name": "relay-one",
                "provider_type": "relay",
                "base_url": "http://127.0.0.1:19991/v1",
                "route_key": "group-key-one",
                "auto_model_cooldown_minutes": 5,
            },
            {
                "id": "g2",
                "name": "relay-two",
                "provider_type": "relay",
                "base_url": "http://127.0.0.1:19992/v1",
                "route_key": "group-key-two",
                "auto_model_cooldown_minutes": 5,
            },
        ],
        "models": [
            {
                "id": "m1",
                "name": "model-one",
                "ep_id": "upstream-one",
                "group_id": "g1",
                "api_key": "test-key-one",
            },
            {
                "id": "m2",
                "name": "model-two",
                "ep_id": "upstream-two",
                "group_id": "g2",
                "api_key": "test-key-two",
            },
        ],
        "aggregate_models": [
            {
                "id": "a1",
                "name": "aggregate-one",
                "route_key": "aggregate-key",
                "cooldown_minutes": 5,
            },
        ],
        "aggregate_members": [
            {
                "id": "am1",
                "aggregate_id": "a1",
                "group_id": "g1",
                "model_id": "m1",
                "priority": 0,
            },
            {
                "id": "am2",
                "aggregate_id": "a1",
                "group_id": "g2",
                "model_id": "m2",
                "priority": 1,
            },
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    router = ArkProxyRouter(ConfigStore(path), SettingsStore(path), tmp_path / "logs.jsonl")
    aggregate = router.store.find_aggregate("a1")
    assert aggregate is not None
    return router, RouteContext(
        client_key=aggregate.route_key,
        group=None,
        group_id="__aggregate__a1",
        provider_type="aggregate",
        base_url="",
        display_name=aggregate.name,
        passthrough=False,
        aggregate=aggregate,
    )


def _payload() -> dict[str, Any]:
    return {
        "model": "aggregate-one",
        "stream": True,
        "messages": [{"role": "user", "content": "test"}],
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now


class _CompletedStreamResponse:
    status = 200
    headers = {"Content-Type": "text/event-stream"}
    http_version = "HTTP/1.1"
    transport = "test"

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self._lines = [
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
            b"\n",
            b'data: {"type":"response.completed"}\n',
            b"\n",
        ]

    def readline(self, timeout_seconds: float = 0) -> bytes:
        self.timeouts.append(timeout_seconds)
        return self._lines.pop(0) if self._lines else b""

    def close(self) -> None:
        return


class _DelayedFirstFrameTimeoutResponse:
    status = 200
    headers = {"Content-Type": "text/event-stream"}
    http_version = "HTTP/1.1"
    transport = "test"

    def __init__(self, clock: _Clock, timeout_type: type[Exception]) -> None:
        self.clock = clock
        self.timeout_type = timeout_type
        self.timeouts: list[float] = []

    def readline(self, timeout_seconds: float = 0) -> bytes:
        self.timeouts.append(timeout_seconds)
        self.clock.now += 50
        raise self.timeout_type("stream_idle_timeout")

    def close(self) -> None:
        return


class _ConnectFailureThenSuccessClient:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.requests: list[tuple[str, float]] = []
        self.success_response: _CompletedStreamResponse | None = None

    def request(
        self,
        _method: str,
        _url: str,
        _headers: dict[str, str],
        body: bytes,
        *,
        stream: bool,
        timeout: float,
        stream_idle_timeout: int | None = None,
    ) -> Any:
        assert stream is True
        model = json.loads(body)["model"]
        self.requests.append((model, timeout))
        if model == "upstream-one":
            self.clock.now += 8
            raise URLError("connect failed")
        self.success_response = _CompletedStreamResponse()
        return self.success_response


class _FirstFrameBudgetClient:
    def __init__(self, clock: _Clock, timeout_type: type[Exception]) -> None:
        self.clock = clock
        self.timeout_type = timeout_type
        self.requests: list[tuple[str, float]] = []
        self.second_response: _CompletedStreamResponse | None = None

    def request(
        self,
        _method: str,
        _url: str,
        _headers: dict[str, str],
        body: bytes,
        *,
        stream: bool,
        timeout: float,
        stream_idle_timeout: int | None = None,
    ) -> Any:
        assert stream is True
        model = json.loads(body)["model"]
        self.requests.append((model, timeout))
        if model == "upstream-one":
            # TCP/TLS + headers consume 8 seconds, but must not enter the shared
            # first-frame budget.  The response then spends 50 seconds waiting.
            self.clock.now += 8
            return _DelayedFirstFrameTimeoutResponse(self.clock, self.timeout_type)
        # 后续成员的连接阶段同样必须完全排除在聚合首帧预算之外。
        self.clock.now += 8
        self.second_response = _CompletedStreamResponse()
        return self.second_response


def test_aggregate_connect_failure_does_not_consume_first_frame_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, aggregate_context = _router(tmp_path)
    clock = _Clock()
    monkeypatch.setattr(router_runtime.time, "perf_counter", clock.perf_counter)
    client = _ConnectFailureThenSuccessClient(clock)
    router.runtime.upstream = client

    status, _headers, chunks, _request_id = router.stream(
        "/v1/chat/completions",
        _payload(),
        aggregate_context,
    )

    assert status == 200
    assert b"ok" in b"".join(chunks)
    assert client.requests == [
        ("upstream-one", UPSTREAM_INITIAL_RESPONSE_TIMEOUT_SECONDS),
        ("upstream-two", UPSTREAM_INITIAL_RESPONSE_TIMEOUT_SECONDS),
    ]
    assert client.success_response is not None
    assert client.success_response.timeouts[0] == FIRST_FRAME_TIMEOUT_SECONDS
    failed_member = router.store.find_aggregate_member("am1")
    assert failed_member is not None
    assert failed_member.health_state == "observing"
    assert failed_member.cooldown_until == 0
    assert failed_member.consecutive_failures == 1
    assert any(
        log.event == "network" and "connect_phase=true" in log.detail
        for log in router.logs
    )


def test_aggregate_first_frame_budget_only_counts_first_frame_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, aggregate_context = _router(tmp_path)
    clock = _Clock()
    monkeypatch.setattr(router_runtime.time, "perf_counter", clock.perf_counter)
    client = _FirstFrameBudgetClient(clock, router.runtime.faults.stream_idle_timeout)
    router.runtime.upstream = client

    status, _headers, chunks, _request_id = router.stream(
        "/v1/chat/completions",
        _payload(),
        aggregate_context,
    )

    assert status == 200
    assert b"ok" in b"".join(chunks)
    assert client.requests == [
        ("upstream-one", UPSTREAM_INITIAL_RESPONSE_TIMEOUT_SECONDS),
        ("upstream-two", UPSTREAM_INITIAL_RESPONSE_TIMEOUT_SECONDS),
    ]
    assert client.second_response is not None
    # 两个成员各自的 8 秒连接阶段均不计入预算；成员一等待首帧 50 秒后，
    # 成员二仍须得到完整的剩余 10 秒，而不是被其连接时间压缩为 2 秒。
    assert client.second_response.timeouts[0] == 10.0


def test_single_network_failure_only_enters_observation(tmp_path: Path) -> None:
    router, _aggregate_context = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None

    router.candidate_health.record_qualified_failure(
        0,
        "connect failed",
        0,
        "network",
        "network",
    )

    assert model.health_state == "observing"
    assert model.cooldown_until == 0
    assert model.usable is True
    assert model.consecutive_failures == 1


def test_two_network_failures_remain_observing_until_standard_threshold(tmp_path: Path) -> None:
    router, _aggregate_context = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None

    for _ in range(2):
        router.candidate_health.record_qualified_failure(
            0,
            "connect failed",
            0,
            "network",
            "network",
        )

    assert model.health_state == "observing"
    assert model.breaker_level == 0
    assert model.breaker_until == 0

    router.candidate_health.record_qualified_failure(
        0, "connect failed", 0, "network", "network"
    )
    assert model.health_state == "breaker_open"
    assert model.breaker_level == 1
    assert model.breaker_until > int(time.time())


def test_failure_timestamp_window_expires_after_300_seconds(tmp_path: Path) -> None:
    router, _aggregate_context = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None
    model.qualified_failure_timestamps = [int(time.time()) - 302, int(time.time()) - 301]
    model.consecutive_failures = 2
    model.last_failure_at = int(time.time()) - 301

    router.candidate_health.record_qualified_failure(
        0,
        "upstream unavailable",
        0,
        "server_error_500",
        "server_error",
    )

    assert len(model.qualified_failure_timestamps) == 1
    assert model.consecutive_failures == 1
    assert model.health_state == "observing"


def test_server_error_still_uses_standard_breaker_threshold(tmp_path: Path) -> None:
    router, _aggregate_context = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None

    for _ in range(2):
        router.candidate_health.record_qualified_failure(
            0,
            "upstream unavailable",
            0,
            "server_error_500",
            "server_error",
        )
    assert model.health_state == "observing"
    assert model.breaker_until == 0

    router.candidate_health.record_qualified_failure(
        0,
        "upstream unavailable",
        0,
        "server_error_500",
        "server_error",
    )
    assert model.health_state == "breaker_open"
    assert model.breaker_until > int(time.time())


def test_fixed_cooldown_keeps_first_failure_cooldown_semantics(tmp_path: Path) -> None:
    router, _aggregate_context = _router(tmp_path)
    group = router.store.find_group("g1")
    model = router.store.find_model("m1")
    assert group is not None and model is not None
    group.routing_policy = "fixed_cooldown"

    applied = router.candidate_health.record_qualified_failure(
        0, "connect failed", 0, "upstream_connect_failure", "upstream_connect_failure"
    )

    assert applied is True
    assert model.health_state == "cooling"
    assert model.cooldown_until > int(time.time())
    assert model.usable is False


class _AllInitialResponseTimeoutClient:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def request(
        self,
        _method: str,
        _url: str,
        _headers: dict[str, str],
        body: bytes,
        *,
        stream: bool,
        timeout: float,
        stream_idle_timeout: int | None = None,
    ) -> Any:
        assert stream is True
        assert timeout == UPSTREAM_INITIAL_RESPONSE_TIMEOUT_SECONDS
        self.requests.append(str(json.loads(body)["model"]))
        raise TimeoutError("initial response timed out")


def test_four_aggregate_initial_response_timeouts_do_not_persist_cooling(tmp_path: Path) -> None:
    router, aggregate_context = _router(tmp_path)
    for suffix in ("3", "4"):
        model = ModelConfig(
            id=f"m{suffix}",
            name=f"model-{suffix}",
            ep_id=f"upstream-{suffix}",
            group_id="g1",
            api_key=f"test-key-{suffix}",
        )
        router.store.models.append(model)
        router.store.aggregate_members.append(AggregateMember(
            id=f"am{suffix}",
            aggregate_id="a1",
            group_id="g1",
            model_id=model.id,
            priority=int(suffix) - 1,
        ))
    router.store.save()
    client = _AllInitialResponseTimeoutClient()
    router.runtime.upstream = client

    with pytest.raises(AllModelsFailedError):
        router.stream("/v1/chat/completions", _payload(), aggregate_context)

    assert client.requests == ["upstream-one", "upstream-two", "upstream-3", "upstream-4"]
    members = router.store.get_aggregate_members("a1")
    assert all(member.health_state == "observing" for member in members)
    assert all(member.cooldown_until == 0 for member in members)
    assert len(list(router._iter_aggregate_candidates(router.store.find_aggregate("a1")))) == 4
    timeout_logs = [log for log in router.logs if "initial_response_timeout" in log.detail]
    assert len(timeout_logs) == 4
    assert all(log.cooldown_applied is False for log in timeout_logs)
