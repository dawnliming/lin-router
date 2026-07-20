from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ArkProxyRouter, ConfigStore, RouteContext
from linrouter_core.observability import ObservabilityService
from linrouter_core.runtime.router_runtime import _read_sse_frame
from settings_store import SettingsStore


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_config(port: int, *, serial_protection: bool = False, stream_idle_timeout: int = 5, model_count: int = 1) -> dict:
    group_id = uuid.uuid4().hex
    return {
        "groups": [{
            "id": group_id,
            "name": "parallel-relay",
            "provider_type": "relay",
            "base_url": f"http://127.0.0.1:{port}/v1",
            "route_key": "lr-parallel",
            "waf_compatible": True,
            "serial_protection": serial_protection,
            "stream_idle_timeout": stream_idle_timeout,
        }],
        "models": [{
            "id": uuid.uuid4().hex,
            "name": "same-model" if index == 0 else f"backup-model-{index}",
            "ep_id": "gpt-test",
            "upstream_model": "gpt-test",
            "group_id": group_id,
            "api_key": "sk-test",
            "usable": True,
        } for index in range(model_count)],
    }


def make_router(tmp_path: str, port: int, *, breaker: bool = False, **kwargs: object) -> tuple[ArkProxyRouter, RouteContext]:
    config_path = Path(tmp_path) / "config.json"
    config_path.write_text(json.dumps(build_config(port, **kwargs), ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(config_path)
    settings = None
    if breaker:
        settings = SettingsStore(config_path)
        settings.update({"smart_breaker_enabled": True})
    router = ArkProxyRouter(store, settings_store=settings, log_file=Path(tmp_path) / "logs.jsonl")
    group = store.groups[0]
    return router, RouteContext(
        client_key=group.route_key,
        group=group,
        group_id=group.id,
        provider_type=group.provider_type,
        base_url=group.base_url,
        display_name=group.name,
        passthrough=False,
    )


class ConcurrentTerminalHandler(BaseHTTPRequestHandler):
    release = threading.Event()
    hold_open = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    request_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.release = threading.Event()
        cls.hold_open = threading.Event()
        cls.started = threading.Event()
        cls.request_count = 0

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        content = str(((payload.get("messages") or [{}])[0] or {}).get("content") or "")
        terminal = b"data: [DONE]\n\n" if content == "done" else b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":2}}}\n\n'
        with type(self).lock:
            type(self).request_count += 1
            if type(self).request_count >= 2:
                type(self).started.set()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"working"}\n\n')
            self.wfile.flush()
            type(self).release.wait(5)
            self.wfile.write(terminal)
            self.wfile.flush()
            type(self).hold_open.wait(5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class BlockingNonStreamHandler(BaseHTTPRequestHandler):
    release = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    request_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.release = threading.Event()
        cls.started = threading.Event()
        cls.request_count = 0

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with type(self).lock:
            type(self).request_count += 1
            type(self).started.set()
        type(self).release.wait(5)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"id":"first-response"}')
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ReceivingResponseHandler(BaseHTTPRequestHandler):
    body_release = threading.Event()
    response_started = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.body_release = threading.Event()
        cls.response_started = threading.Event()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b'{\"id\":\"cancelled-before-success\"}')))
            self.end_headers()
            self.wfile.flush()
            type(self).response_started.set()
            type(self).body_release.wait(5)
            self.wfile.write(b'{"id":"cancelled-before-success"}')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ImmediateTerminalHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(content_length) or b"{}")
        signal = str(((payload.get("messages") or [{}])[0] or {}).get("content") or "response.completed")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"working"}\n\n')
        if signal != "eof":
            self.wfile.write(f'data: {{"type":"{signal}"}}\n\n'.encode("utf-8"))
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class IdleAfterFirstChunkHandler(BaseHTTPRequestHandler):
    release = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.release = threading.Event()

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"working"}\n\n')
            self.wfile.flush()
            type(self).release.wait(5)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class DelayedFirstChunkHandler(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    request_count = 0

    @classmethod
    def reset(cls) -> None:
        cls.started = threading.Event()
        cls.release = threading.Event()
        cls.request_count = 0

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with type(self).lock:
            type(self).request_count += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        self.wfile.flush()
        type(self).started.set()
        try:
            type(self).release.wait(5)
            self.wfile.write(b'data: {"type":"response.completed"}\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


def start_server(handler_type: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", get_free_port()), handler_type)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stream_payload(content: str, model: str = "same-model") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}], "stream": True}


def test_sse_reader_forwards_only_complete_event_frames() -> None:
    lines = iter([
        b"event: response.created\n",
        b"data: {\"type\":\"response.created\"}\n",
        b"\n",
    ])

    frame = _read_sse_frame(lambda _timeout: next(lines, b""), 5)

    assert frame == (
        b"event: response.created\n"
        b"data: {\"type\":\"response.created\"}\n\n"
    )
    assert frame.endswith(b"\n\n")


def test_sse_reader_skips_leading_blank_lines_and_preserves_eof_buffer() -> None:
    lines = iter([b"\n", b"data: partial\n"])

    assert _read_sse_frame(lambda _timeout: next(lines, b""), 5) == b"data: partial\n"


def test_waf_compatible_same_candidate_streams_run_in_parallel_and_finalize_before_eof() -> None:
    ConcurrentTerminalHandler.reset()
    upstream = start_server(ConcurrentTerminalHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1])
            status_a, _headers_a, stream_a, request_a = router.stream("/v1/chat/completions", stream_payload("completed"), context)
            assert status_a == 200
            assert b"working" in next(stream_a)

            status_b, _headers_b, stream_b, request_b = router.stream("/v1/chat/completions", stream_payload("done"), context)
            assert status_b == 200
            assert b"working" in next(stream_b)
            assert ConcurrentTerminalHandler.started.wait(1)
            assert ConcurrentTerminalHandler.request_count == 2
            live = router.live_requests_payload()
            assert live["count"] == 2
            assert {item["request_id"] for item in live["requests"]} == {request_a, request_b}
            assert all(item["stage"] == "streaming" for item in live["requests"])

            ConcurrentTerminalHandler.release.set()
            started_at = time.perf_counter()
            assert b"response.completed" in b"".join(stream_a)
            assert time.perf_counter() - started_at < 1
            assert router.live_requests_payload()["count"] == 1

            assert b"[DONE]" in b"".join(stream_b)
            assert router.live_requests_payload()["count"] == 0

            logs_by_request = {item.request_id: item for item in router.logs if item.request_id in {request_a, request_b}}
            assert "completion_signal=response.completed" in logs_by_request[request_a].detail
            assert "completion_signal=[DONE]" in logs_by_request[request_b].detail
            assert all("upstream_terminal_received=true" in item.detail for item in logs_by_request.values())
            assert all("lifecycle=stream_done" in item.detail for item in logs_by_request.values())
            assert all("request_concurrency=parallel" in item.detail for item in logs_by_request.values())
            assert not any(item.event in {"waf_lock_timeout", "serial_protection_timeout"} for item in router.logs)
    finally:
        ConcurrentTerminalHandler.release.set()
        ConcurrentTerminalHandler.hold_open.set()
        upstream.shutdown()
        upstream.server_close()


def test_response_failed_and_incomplete_have_distinct_stream_lifecycles() -> None:
    upstream = start_server(ImmediateTerminalHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1], breaker=True)
            for expected_failures, (signal, lifecycle) in enumerate((("response.failed", "stream_failed"), ("response.incomplete", "stream_incomplete")), start=1):
                status, _headers, stream, request_id = router.stream("/v1/responses", stream_payload(signal), context)
                assert status == 200
                assert signal.encode("utf-8") in b"".join(stream)
                log = next(item for item in router.logs if item.request_id == request_id)
                assert f"completion_signal={signal}" in log.detail
                assert f"lifecycle={lifecycle}" in log.detail
                assert log.failure_scope == "upstream"
                assert log.cooldown_applied is True
                assert router.store.models[0].health_state == "observing"
                assert router.store.models[0].consecutive_failures == expected_failures
                assert router.live_requests_payload()["count"] == 0

            status, _headers, stream, _request_id = router.stream("/v1/responses", stream_payload("response.completed"), context)
            assert status == 200
            assert b"response.completed" in b"".join(stream)
            assert router.store.models[0].health_state == "normal"
            assert router.store.models[0].consecutive_failures == 0
            assert router.store.models[0].usable is True
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_stream_completion_signal_parses_done_and_structured_response_events() -> None:
    assert ArkProxyRouter._stream_completion_signal(b"data: [DONE]\n") == "[DONE]"
    assert ArkProxyRouter._stream_completion_signal(b'data: {"type":"response.completed"}\n') == "response.completed"
    assert ArkProxyRouter._stream_completion_signal(b'data: {"event":"response.failed"}\n') == "response.failed"
    assert ArkProxyRouter._stream_completion_signal(b'event: response.incomplete\n') == "event:response.incomplete"


def test_stream_eof_remains_a_compatible_completion_signal() -> None:
    upstream = start_server(ImmediateTerminalHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1])
            status, _headers, stream, request_id = router.stream("/v1/chat/completions", stream_payload("eof"), context)
            assert status == 200
            assert b"working" in b"".join(stream)
            log = next(item for item in router.logs if item.request_id == request_id)
            assert "lifecycle=stream_done" in log.detail
            assert "completion_signal=eof" in log.detail
            assert "upstream_terminal_missing=true" in log.detail
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_post_first_byte_idle_timeout_is_not_recorded_as_client_disconnect() -> None:
    IdleAfterFirstChunkHandler.reset()
    upstream = start_server(IdleAfterFirstChunkHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1], breaker=True, stream_idle_timeout=1)
            status, _headers, stream, request_id = router.stream("/v1/chat/completions", stream_payload("idle"), context)
            assert status == 200
            assert b"working" in next(stream)
            list(stream)
            log = next(item for item in router.logs if item.request_id == request_id)
            assert log.status == "timeout"
            assert "lifecycle=stream_idle_timeout" in log.detail
            assert "lifecycle=client_disconnected" not in log.detail
            assert log.cooldown_applied is True
            assert router.store.models[0].health_state == "observing"
            assert router.store.models[0].consecutive_failures == 1
    finally:
        IdleAfterFirstChunkHandler.release.set()
        upstream.shutdown()
        upstream.server_close()


def test_post_first_byte_network_error_counts_as_upstream_failure() -> None:
    class BrokenStreamResponse:
        status = 200
        headers = {}

        def __init__(self) -> None:
            self._lines = [
                b'data: {"type":"response.output_text.delta","delta":"working"}\n',
                b"\n",
            ]

        def readline(self, _timeout: int = 0) -> bytes:
            if self._lines:
                return self._lines.pop(0)
            raise URLError("upstream connection reset after first chunk")

        def close(self) -> None:
            return None

    class BrokenStreamClient:
        def request(self, _method, _url, _headers, _body, **kwargs):
            assert kwargs.get("stream") is True
            return BrokenStreamResponse()

    with tempfile.TemporaryDirectory() as tmp:
        router, context = make_router(tmp, get_free_port(), breaker=True)
        client = BrokenStreamClient()
        router.runtime.upstream = client
        router.stream_execution._candidates.upstream = client

        status, _headers, stream, request_id = router.stream("/v1/chat/completions", stream_payload("network"), context)
        assert status == 200
        assert b"working" in next(stream)
        assert list(stream) == []

        log = next(item for item in router.logs if item.request_id == request_id)
        assert log.status == "network"
        assert "lifecycle=stream_incomplete" in log.detail
        assert "completion_signal=network_error" in log.detail
        assert log.failure_scope == "upstream"
        assert log.cooldown_applied is True
        assert router.store.models[0].health_state == "observing"
        assert router.store.models[0].consecutive_failures == 1
        assert router.live_requests_payload()["count"] == 0


def test_dashboard_cancel_while_waiting_first_byte_isolated_from_health_and_fallback() -> None:
    DelayedFirstChunkHandler.reset()
    upstream = start_server(DelayedFirstChunkHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1], serial_protection=True, model_count=2)
            for model in ("same-model", "lin-router-auto"):
                results: dict[str, tuple[int, dict[str, str], object, str]] = {}
                request_thread = threading.Thread(
                    target=lambda: results.setdefault("result", router.stream("/v1/chat/completions", stream_payload("delayed", model), context))
                )
                request_thread.start()
                assert DelayedFirstChunkHandler.started.wait(1)
                deadline = time.monotonic() + 1
                request_id = ""
                while time.monotonic() < deadline:
                    waiting = [item for item in router.live_requests_payload()["requests"] if item["stage"] == "waiting_first_byte"]
                    if waiting:
                        request_id = waiting[0]["request_id"]
                        break
                    time.sleep(0.01)
                assert request_id
                assert router.cancel_live_request(request_id)["state"] == "cancellation_requested"
                request_thread.join(1)
                assert not request_thread.is_alive()

                status, _headers, body, returned_request_id = results["result"]
                assert status == 499
                assert returned_request_id == request_id
                assert json.loads(b"".join(body)) == {"error": {
                    "message": "请求已由用户终止",
                    "type": "request_cancelled",
                    "code": "manual_cancelled",
                    "request_id": request_id,
                }}
                cancelled = [item for item in router.logs if item.request_id == request_id]
                assert len(cancelled) == 1
                assert cancelled[0].event == "request_cancelled"
                assert cancelled[0].failure_scope == "client_cancelled"
                assert cancelled[0].cooldown_applied is False
                assert "lock_released=true" in cancelled[0].detail
                assert not any(item.request_id == request_id and item.event in {"network", "fallback", "cooldown", "serial_protection_timeout", "stream_timeout"} for item in router.logs)
                assert all(model_item.cooldown_until == 0 for model_item in router.store.models)
                assert router.live_requests_payload()["count"] == 0
                assert DelayedFirstChunkHandler.request_count == 1
                DelayedFirstChunkHandler.release.set()
                DelayedFirstChunkHandler.reset()
    finally:
        DelayedFirstChunkHandler.release.set()
        upstream.shutdown()
        upstream.server_close()


def test_dashboard_cancel_closes_only_target_stream_without_cooldown() -> None:
    ConcurrentTerminalHandler.reset()
    upstream = start_server(ConcurrentTerminalHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1])
            status, _headers, stream, request_id = router.stream("/v1/chat/completions", stream_payload("completed"), context)
            assert status == 200
            assert b"working" in next(stream)
            assert router.cancel_live_request(request_id) == {
                "ok": True,
                "request_id": request_id,
                "state": "cancellation_requested",
                "message": "已发送终止指令，正在释放本地请求资源。",
            }
            assert router.cancel_live_request(request_id)["state"] == "cancellation_already_requested"
            stream.close()
            log = next(item for item in router.logs if item.request_id == request_id)
            assert log.event == "request_cancelled"
            assert log.failure_scope == "client_cancelled"
            assert log.cooldown_applied is False
            assert "lifecycle=manual_cancelled" in log.detail
            assert router.live_requests_payload()["count"] == 0
    finally:
        ConcurrentTerminalHandler.release.set()
        ConcurrentTerminalHandler.hold_open.set()
        upstream.shutdown()
        upstream.server_close()


def test_dashboard_cancel_while_non_stream_receives_response_is_not_success_or_network_failure() -> None:
    ReceivingResponseHandler.reset()
    upstream = start_server(ReceivingResponseHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1])
            payload = {"model": "same-model", "messages": [{"role": "user", "content": "non-stream"}]}
            result: dict[str, tuple[int, dict[str, str], bytes]] = {}
            request_thread = threading.Thread(
                target=lambda: result.setdefault("response", router.call("/v1/chat/completions", payload, context))
            )
            request_thread.start()
            assert ReceivingResponseHandler.response_started.wait(1)
            # The server has sent headers, but the client worker still has to
            # return from urllib setup and publish its live-request entry.
            deadline = time.monotonic() + 3
            request_id = ""
            while time.monotonic() < deadline:
                active = router.live_requests_payload()["requests"]
                if active:
                    request_id = active[0]["request_id"]
                    break
                time.sleep(0.01)
            assert request_id
            assert router.cancel_live_request(request_id)["state"] == "cancellation_requested"
            ReceivingResponseHandler.body_release.set()
            request_thread.join(1)
            assert not request_thread.is_alive()

            status, _headers, body = result["response"]
            assert status == 499
            assert json.loads(body)["error"]["code"] == "manual_cancelled"
            cancelled = [item for item in router.logs if item.request_id == request_id]
            assert len(cancelled) == 1
            assert cancelled[0].event == "request_cancelled"
            assert cancelled[0].failure_scope == "client_cancelled"
            assert cancelled[0].cooldown_applied is False
            assert not any(item.request_id == request_id and item.event in {"ok", "network", "fallback", "cooldown"} for item in router.logs)
            assert router.store.models[0].cooldown_until == 0
            assert router.live_requests_payload()["count"] == 0
    finally:
        ReceivingResponseHandler.body_release.set()
        upstream.shutdown()
        upstream.server_close()


def test_dashboard_cancel_while_non_stream_waits_for_serial_protection() -> None:
    BlockingNonStreamHandler.reset()
    upstream = start_server(BlockingNonStreamHandler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, context = make_router(tmp, upstream.server_address[1], serial_protection=True)
            payload = {"model": "same-model", "messages": [{"role": "user", "content": "non-stream"}]}
            results: dict[str, tuple[int, dict[str, str], bytes]] = {}
            first = threading.Thread(target=lambda: results.setdefault("first", router.call("/v1/chat/completions", payload, context)))
            first.start()
            assert BlockingNonStreamHandler.started.wait(1)

            second = threading.Thread(target=lambda: results.setdefault("second", router.call("/v1/chat/completions", payload, context)))
            second.start()
            deadline = time.monotonic() + 1
            second_request_id = ""
            while time.monotonic() < deadline:
                waiting = [item for item in router.live_requests_payload()["requests"] if item["stage"] == "waiting_serial_protection"]
                if waiting:
                    second_request_id = waiting[0]["request_id"]
                    break
                time.sleep(0.01)
            assert second_request_id
            assert router.cancel_live_request(second_request_id)["state"] == "cancellation_requested"
            second.join(1)
            assert not second.is_alive()

            status, _headers, body = results["second"]
            assert status == 499
            assert json.loads(body) == {"error": {
                "message": "请求已由用户终止",
                "type": "request_cancelled",
                "code": "manual_cancelled",
                "request_id": second_request_id,
            }}
            assert BlockingNonStreamHandler.request_count == 1
            cancelled = next(item for item in router.logs if item.request_id == second_request_id)
            assert cancelled.event == "request_cancelled"
            assert cancelled.failure_scope == "client_cancelled"
            assert cancelled.cooldown_applied is False
            assert "lock_released=false" in cancelled.detail
            assert not any(item.event == "serial_protection_timeout" and item.request_id == second_request_id for item in router.logs)
            assert router.store.models[0].cooldown_until == 0

            BlockingNonStreamHandler.release.set()
            first.join(1)
            assert not first.is_alive()
            assert results["first"][0] == 200
    finally:
        BlockingNonStreamHandler.release.set()
        upstream.shutdown()
        upstream.server_close()


def test_dashboard_cancellation_closes_registered_response_once() -> None:
    class CloseCountingResponse:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    with tempfile.TemporaryDirectory() as tmp:
        observability = ObservabilityService(Path(tmp) / "logs.jsonl", now=time.time, sanitize_detail=lambda value: value)
        response = CloseCountingResponse()
        observability.start_live_request("cancel-once", "/v1/chat/completions", "same-model", stream=True)
        observability.set_live_response("cancel-once", response)
        assert observability.request_cancellation("cancel-once")["state"] == "cancellation_requested"
        assert response.close_calls == 1
        assert observability.close_live_response("cancel-once", response) is False
        assert response.close_calls == 1
