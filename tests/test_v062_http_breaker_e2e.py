from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from app import ArkProxyRouter, ConfigStore, RouteContext
from linrouter_core.contracts import AllModelsFailedError
from settings_store import SettingsStore


class LocalMockUpstream:
    """A real local HTTP upstream with per-model responses and call evidence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self._default_response: tuple[int, bytes] = (500, _error_body("server_error", "upstream unavailable"))
        self._responses: dict[str, tuple[int, bytes]] = {}
        self.wait_before_response = False
        self.request_started = threading.Event()
        self.release_response = threading.Event()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                model = str(payload.get("model") or "") if isinstance(payload, dict) else ""
                with outer._lock:
                    outer.calls.append({"path": self.path, "payload": payload, "model": model})
                    status, body = outer._responses.get(model, outer._default_response)
                    wait_before_response = outer.wait_before_response
                if wait_before_response:
                    outer.request_started.set()
                    outer.release_response.wait(timeout=5)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.calls)

    def set_default(self, status: int, body: bytes | None = None) -> None:
        with self._lock:
            self._default_response = (status, body if body is not None else _error_body("server_error", f"HTTP {status}"))

    def set_response(self, model: str, status: int, body: bytes | None = None) -> None:
        with self._lock:
            self._responses[model] = (status, body if body is not None else _error_body("server_error", f"HTTP {status}"))

    def clear_calls(self) -> None:
        with self._lock:
            self.calls.clear()

    def close(self) -> None:
        self.release_response.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self) -> "LocalMockUpstream":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _error_body(error_type: str, message: str) -> bytes:
    return json.dumps({"error": {"type": error_type, "message": message}}).encode("utf-8")


def _ok_body() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode("utf-8")


def _config(base_url: str, *, serial_protection: bool = False, two_models: bool = False, aggregate: bool = False) -> dict[str, Any]:
    models = [
        {
            "id": "m1",
            "name": "explicit-model",
            "ep_id": "upstream-one",
            "group_id": "g1",
            "api_key": "upstream-key-one",
            "usable": True,
        }
    ]
    if two_models:
        models.append(
            {
                "id": "m2",
                "name": "fallback-model",
                "ep_id": "upstream-two",
                "group_id": "g1",
                "api_key": "upstream-key-two",
                "usable": True,
            }
        )
    config: dict[str, Any] = {
        "groups": [
            {
                "id": "g1",
                "name": "local-relay",
                "provider_type": "relay",
                "base_url": base_url,
                "route_key": "group-key",
                "auto_model_name": "lin-router-auto",
                "auto_model_cooldown_minutes": 5,
                "serial_protection": serial_protection,
            }
        ],
        "models": models,
    }
    if aggregate:
        config["aggregate_models"] = [
            {"id": "a1", "name": "aggregate-model", "route_key": "aggregate-key", "strategy": "priority", "cooldown_minutes": 5}
        ]
        config["aggregate_members"] = [
            {"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "priority": 1, "enabled": True},
            {"id": "am2", "aggregate_id": "a1", "group_id": "g1", "model_id": "m2", "priority": 2, "enabled": True},
        ]
    return config


def _router(tmp_path: Any, upstream: LocalMockUpstream, *, breaker: bool, serial_protection: bool = False, two_models: bool = False, aggregate: bool = False) -> tuple[ArkProxyRouter, RouteContext]:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config(upstream.base_url, serial_protection=serial_protection, two_models=two_models, aggregate=aggregate)), encoding="utf-8")
    settings = SettingsStore(path)
    settings.update({"smart_breaker_enabled": breaker})
    router = ArkProxyRouter(ConfigStore(path), settings, tmp_path / "logs.jsonl")
    group = router.store.find_group("g1")
    assert group is not None
    return router, RouteContext(
        client_key="group-key",
        group=group,
        group_id=group.id,
        provider_type=group.provider_type,
        base_url=group.base_url,
        display_name=group.name,
        passthrough=False,
    )


def _aggregate_context(router: ArkProxyRouter) -> RouteContext:
    aggregate = router.store.find_aggregate("a1")
    assert aggregate is not None
    return RouteContext(
        client_key="aggregate-key",
        group=None,
        group_id=f"__aggregate__{aggregate.id}",
        provider_type="aggregate",
        base_url="",
        display_name=aggregate.name,
        passthrough=False,
        aggregate=aggregate,
    )


def _payload(model: str, *, stream: bool = False) -> dict[str, Any]:
    return {"model": model, "stream": stream, "messages": [{"role": "user", "content": "test"}]}


def _assert_no_live_requests(router: ArkProxyRouter) -> None:
    assert router.live_requests_payload()["count"] == 0


def _assert_all_models_failed(call: Any) -> AllModelsFailedError:
    with pytest.raises(AllModelsFailedError) as captured:
        call()
    return captured.value


def _assert_clean_breaker(router: ArkProxyRouter) -> None:
    model = router.store.models[0]
    assert model.health_state == "normal"
    assert model.consecutive_failures == 0
    assert model.breaker_until == 0


def test_breaker_disabled_explicit_http_5xx_never_opens(tmp_path: Any) -> None:
    with LocalMockUpstream() as upstream:
        router, context = _router(tmp_path, upstream, breaker=False)

        for _ in range(3):
            error = _assert_all_models_failed(lambda: router.call("/v1/chat/completions", _payload("explicit-model"), context))
            assert error.error_code == "all_models_failed"

        assert upstream.call_count == 3
        assert [call["model"] for call in upstream.calls] == ["upstream-one"] * 3
        _assert_clean_breaker(router)
        _assert_no_live_requests(router)


def test_breaker_opens_for_explicit_non_stream_http_5xx_then_manual_recover(tmp_path: Any) -> None:
    with LocalMockUpstream() as upstream:
        router, context = _router(tmp_path, upstream, breaker=True)

        for expected_count in range(1, 4):
            error = _assert_all_models_failed(lambda: router.call("/v1/chat/completions", _payload("explicit-model"), context))
            assert error.error_code == "all_models_failed"
            assert upstream.call_count == expected_count

        model = router.store.models[0]
        assert model.health_state == "breaker_open"
        assert model.consecutive_failures == 3
        assert model.breaker_until > int(time.time())

        no_candidate = _assert_all_models_failed(lambda: router.call("/v1/chat/completions", _payload("explicit-model"), context))
        assert no_candidate.error_code == "no_usable_models"
        assert upstream.call_count == 3
        _assert_no_live_requests(router)

        upstream.set_response("upstream-one", 200, _ok_body())
        recovered = router.recover_model(model.id)
        assert recovered["ok"] is True
        assert upstream.call_count == 4
        assert model.health_state == "normal"
        assert model.consecutive_failures == 0
        assert model.breaker_until == 0
        assert model.usable is True
        _assert_no_live_requests(router)


def test_breaker_opens_for_explicit_stream_http_5xx_and_skips_fourth(tmp_path: Any) -> None:
    with LocalMockUpstream() as upstream:
        router, context = _router(tmp_path, upstream, breaker=True)

        for expected_count in range(1, 4):
            error = _assert_all_models_failed(lambda: router.stream("/v1/chat/completions", _payload("explicit-model", stream=True), context))
            assert error.error_code == "all_models_failed"
            assert upstream.call_count == expected_count

        model = router.store.models[0]
        assert model.health_state == "breaker_open"
        assert model.consecutive_failures == 3
        assert model.breaker_until > int(time.time())

        no_candidate = _assert_all_models_failed(lambda: router.stream("/v1/chat/completions", _payload("explicit-model", stream=True), context))
        assert no_candidate.error_code == "no_usable_models"
        assert upstream.call_count == 3
        _assert_no_live_requests(router)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, _error_body("invalid_request_error", "bad request")),
        (401, _error_body("authentication_error", "unauthorized")),
        (403, _error_body("permission_error", "forbidden")),
        (403, _error_body("permission_error", "your request was blocked by WAF")),
    ],
    ids=["400", "401", "403", "waf"],
)
def test_request_level_and_waf_http_failures_do_not_count_toward_breaker(tmp_path: Any, status: int, body: bytes) -> None:
    with LocalMockUpstream() as upstream:
        upstream.set_default(status, body)
        router, context = _router(tmp_path, upstream, breaker=True)

        for _ in range(3):
            response_status, _headers, _data = router.call("/v1/chat/completions", _payload("explicit-model"), context)
            assert response_status == status

        assert upstream.call_count == 3
        _assert_clean_breaker(router)
        _assert_no_live_requests(router)


def test_model_not_found_and_serial_protection_busy_do_not_count_toward_breaker(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    with LocalMockUpstream() as upstream:
        router, context = _router(tmp_path, upstream, breaker=True, serial_protection=True)

        missing = _assert_all_models_failed(lambda: router.call("/v1/chat/completions", _payload("not-configured"), context))
        assert missing.error_code == "model_not_found"
        assert upstream.call_count == 0
        _assert_clean_breaker(router)

        monkeypatch.setattr(router.runtime.concurrency, "_acquire", lambda _lock, **_kwargs: (False, 0))
        response_status, _headers, _data = router.call("/v1/chat/completions", _payload("explicit-model"), context)
        assert response_status == 503
        assert upstream.call_count == 0
        _assert_clean_breaker(router)
        _assert_no_live_requests(router)


def test_cancelled_http_request_and_health_check_failure_do_not_count_toward_breaker(tmp_path: Any) -> None:
    with LocalMockUpstream() as upstream:
        upstream.set_default(200, _ok_body())
        upstream.wait_before_response = True
        router, context = _router(tmp_path, upstream, breaker=True)
        outcome: list[Any] = []

        def run_request() -> None:
            try:
                outcome.append(router.call("/v1/chat/completions", _payload("explicit-model"), context))
            except BaseException as exc:  # Keep the thread failure visible in the assertion below.
                outcome.append(exc)

        request_thread = threading.Thread(target=run_request, daemon=True)
        request_thread.start()
        assert upstream.request_started.wait(timeout=2)
        live = router.live_requests_payload()["requests"]
        assert len(live) == 1
        assert router.cancel_live_request(live[0]["request_id"])["ok"] is True
        upstream.release_response.set()
        request_thread.join(timeout=3)
        assert not request_thread.is_alive()
        assert len(outcome) == 1
        assert not isinstance(outcome[0], BaseException)
        assert outcome[0][0] == 499
        _assert_clean_breaker(router)
        _assert_no_live_requests(router)

    with LocalMockUpstream() as upstream:
        upstream.set_default(500, _error_body("server_error", "probe failure"))
        router, _context = _router(tmp_path, upstream, breaker=True)
        speed = router.speed_test_group("g1")
        assert speed["ok"] is False
        assert speed["source"] == "health_check"
        assert upstream.call_count == 1
        _assert_clean_breaker(router)
        _assert_no_live_requests(router)


def test_auto_and_aggregate_http_fallback_keep_existing_health_paths(tmp_path: Any) -> None:
    with LocalMockUpstream() as upstream:
        upstream.set_response("upstream-one", 500, _error_body("server_error", "first candidate failed"))
        upstream.set_response("upstream-two", 200, _ok_body())
        router, context = _router(tmp_path, upstream, breaker=True, two_models=True)

        status, _headers, _data = router.call("/v1/chat/completions", _payload("lin-router-auto"), context)
        assert status == 200
        assert [call["model"] for call in upstream.calls] == ["upstream-one", "upstream-two"]
        assert router.store.models[0].health_state == "cooling"
        assert router.store.models[0].consecutive_failures == 1
        assert router.store.models[1].health_state == "normal"
        _assert_no_live_requests(router)

    with LocalMockUpstream() as upstream:
        upstream.set_response("upstream-one", 500, _error_body("server_error", "first member failed"))
        upstream.set_response("upstream-two", 200, _ok_body())
        router, _context = _router(tmp_path, upstream, breaker=True, two_models=True, aggregate=True)

        status, _headers, _data = router.call("/v1/chat/completions", _payload("aggregate-model"), _aggregate_context(router))
        assert status == 200
        assert [call["model"] for call in upstream.calls] == ["upstream-one", "upstream-two"]
        first_member = router.store.find_aggregate_member("am1")
        second_member = router.store.find_aggregate_member("am2")
        assert first_member is not None and first_member.health_state == "cooling" and first_member.consecutive_failures == 1
        assert second_member is not None and second_member.health_state == "normal" and second_member.consecutive_failures == 0
        _assert_no_live_requests(router)
