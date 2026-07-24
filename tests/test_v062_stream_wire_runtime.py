from __future__ import annotations

import gzip
import io
import json
import socket
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from app import AllModelsFailedError, ArkProxyRouter, ConfigStore, RouteContext
from linrouter_core.runtime.handler_runtime import handle_proxy_request
from linrouter_core.runtime.router_runtime import MAX_SSE_FRAME_BYTES
from upstream_client import UpstreamClient


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _start_upstream(
    headers: dict[str, str],
    chunks: list[tuple[bytes, float]],
    *,
    status: int = 200,
) -> ThreadingHTTPServer:
    calls: list[bool] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            calls.append(True)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            for payload, delay_after in chunks:
                self.wfile.write(payload)
                self.wfile.flush()
                if delay_after:
                    time.sleep(delay_after)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
    server.calls = calls  # type: ignore[attr-defined]
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _router(
    tmp_path: Path,
    upstream: ThreadingHTTPServer,
    *,
    stream_idle_timeout: int = 5,
) -> tuple[ArkProxyRouter, RouteContext]:
    port = int(upstream.server_address[1])
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": "g1",
                        "name": "local-stream-upstream",
                        "provider_type": "relay",
                        "base_url": f"http://127.0.0.1:{port}/v1",
                        "route_key": "group-key",
                        "auto_model_name": "lin-router-auto",
                        "stream_idle_timeout": stream_idle_timeout,
                    }
                ],
                "models": [
                    {
                        "id": "m1",
                        "name": "stream-model",
                        "ep_id": "upstream-stream-model",
                        "group_id": "g1",
                        "api_key": "upstream-secret-key",
                        "usable": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    router = ArkProxyRouter(ConfigStore(config_path), None, tmp_path / "logs.jsonl")
    group = router.store.find_group("g1")
    assert group is not None
    return router, RouteContext(
        client_key=group.route_key,
        group=group,
        group_id=group.id,
        provider_type=group.provider_type,
        base_url=group.base_url,
        display_name=group.name,
        passthrough=False,
    )


def _payload() -> dict[str, Any]:
    return {
        "model": "stream-model",
        "stream": True,
        "messages": [{"role": "user", "content": "stream request sentinel"}],
    }


def _detail_fields(detail: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in detail.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def _stream_all(router: ArkProxyRouter, context: RouteContext) -> tuple[list[bytes], dict[str, str], str]:
    status, _headers, iterator, request_id = router.stream("/v1/chat/completions", _payload(), context)
    assert status == 200
    chunks = list(iterator)
    log = next(item for item in router.logs if item.request_id == request_id)
    return chunks, _detail_fields(log.detail), request_id


def _stop(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _use_upstream_client(router: ArkProxyRouter, client: UpstreamClient) -> None:
    router._upstream_client = client
    router.runtime.upstream = client
    router.stream_execution._candidates.upstream = client


def test_normal_sse_preserves_complete_frames_and_records_stage_timings(tmp_path: Path) -> None:
    created = b'event: response.created\ndata: {"type":"response.created"}\n\n'
    delta_one = b'data: {"type":"response.output_text.delta","delta":"SSE_SENTINEL_ONE"}\n\n'
    delta_two = b'data: {"type":"response.output_text.delta","delta":"two"}\n\n'
    completed = b'data: {"type":"response.completed"}\n\n'
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream; charset=utf-8"},
        [(created, 0.03), (delta_one, 0), (delta_two, 0), (completed, 0)],
    )
    try:
        router, context = _router(tmp_path, upstream)
        chunks, detail, request_id = _stream_all(router, context)

        assert chunks == [created, delta_one, delta_two, completed]
        assert detail["stream_wire_mode"] == "sse"
        assert detail["upstream_content_type"] == "text/event-stream"
        assert detail["upstream_content_encoding"] == "-"
        assert detail["stream_frame_count"] == "4"
        assert detail["initial_frame_bytes"] == str(len(created))
        assert int(detail["candidate_selected_ms"]) <= int(detail["upstream_request_started_ms"])
        assert int(detail["upstream_request_started_ms"]) <= int(detail["upstream_headers_ms"])
        assert int(detail["upstream_headers_ms"]) <= int(detail["first_raw_line_ms"])
        assert int(detail["first_raw_line_ms"]) <= int(detail["first_complete_frame_ms"])
        assert int(detail["first_complete_frame_ms"]) <= int(detail["first_content_delta_ms"])
        assert detail["first_downstream_flush_ms"] == "-1"
        assert detail["first_byte_metric"] == "complete_sse_frame_legacy"
        assert "SSE_SENTINEL_ONE" not in next(item.detail for item in router.logs if item.request_id == request_id)
        assert "SSE_SENTINEL_ONE" not in (tmp_path / "logs.jsonl").read_text(encoding="utf-8")
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)


def test_urllib_initial_response_wait_is_not_capped_at_eight_seconds(tmp_path: Path) -> None:
    frame = b'data: {"type":"response.completed"}\n\n'

    class SlowHeadersHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            # 真实 HTTP 连接在 10 秒后才返回响应头，覆盖旧 8 秒误杀场景。
            time.sleep(10)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            self.wfile.flush()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), SlowHeadersHandler)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        router, context = _router(tmp_path, upstream, stream_idle_timeout=120)
        started_at = time.perf_counter()
        status, _headers, iterator, request_id = router.stream(
            "/v1/chat/completions", _payload(), context
        )
        elapsed = time.perf_counter() - started_at

        assert status == 200
        assert list(iterator) == [frame]
        assert 9 <= elapsed < 20
        log = next(item for item in router.logs if item.request_id == request_id)
        detail = _detail_fields(log.detail)
        assert detail["upstream_transport"] == "urllib"
        assert log.event == "stream_ok"
        assert not any(
            item.request_id == request_id and item.event == "network"
            for item in router.logs
        )
    finally:
        _stop(upstream)


def test_json_and_undelimited_stream_responses_are_classified_without_fake_deltas(tmp_path: Path) -> None:
    json_body = b'{"id":"JSON_SENTINEL","choices":[{"message":{"content":"complete"}}]}'
    upstream = _start_upstream({"Content-Type": "application/json; charset=utf-8"}, [(json_body, 0)])
    try:
        router, context = _router(tmp_path, upstream)
        chunks, detail, _request_id = _stream_all(router, context)
        assert chunks == [json_body]
        assert detail["stream_wire_mode"] == "json_compat"
        assert detail["upstream_content_type"] == "application/json"
        assert detail["stream_frame_count"] == "1"
        assert detail["first_content_delta_ms"] == "-1"
        assert "JSON_SENTINEL" not in next(item.detail for item in router.logs if item.request_id == _request_id)
    finally:
        _stop(upstream)

    undelimited = b'data: {"type":"response.output_text.delta","delta":"NO_DELIMITER"}\n'
    upstream = _start_upstream({"Content-Type": "text/event-stream"}, [(undelimited, 0)])
    try:
        router, context = _router(tmp_path, upstream)
        chunks, detail, _request_id = _stream_all(router, context)
        assert chunks == [undelimited]
        assert detail["stream_wire_mode"] == "buffered_or_non_delimited"
        assert detail["stream_frame_count"] == "1"
        assert detail["first_content_delta_ms"] == "-1"
        assert detail["completion_signal"] == "eof"
    finally:
        _stop(upstream)


def test_event_only_completion_frame_finalizes_without_waiting_for_another_frame(tmp_path: Path) -> None:
    completed = b"event: response.completed\n\n"
    upstream = _start_upstream({"Content-Type": "text/event-stream"}, [(completed, 0.2)])
    try:
        router, context = _router(tmp_path, upstream)
        chunks, detail, _request_id = _stream_all(router, context)
        assert chunks == [completed]
        assert detail["completion_signal"] == "response.completed"
        assert detail["lifecycle"] == "stream_done"
        assert detail["stream_frame_count"] == "1"
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)


def test_urllib_decodes_compressed_sse_into_complete_frames(tmp_path: Path) -> None:
    delta = b'data: {"type":"response.output_text.delta","delta":"COMPRESSED_SENTINEL"}\n\n'
    completed = b'data: {"type":"response.completed"}\n\n'
    compressed = gzip.compress(delta + completed)
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream", "Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
        [(compressed, 0)],
    )
    try:
        router, context = _router(tmp_path, upstream)
        chunks, detail, request_id = _stream_all(router, context)
        assert chunks == [delta, completed]
        assert detail["upstream_content_encoding"] == "gzip"
        assert detail["stream_wire_mode"] == "buffered_or_non_delimited"
        assert detail["stream_frame_count"] == "2"
        assert int(detail["first_content_delta_ms"]) >= 0
        assert "COMPRESSED_SENTINEL" not in next(item.detail for item in router.logs if item.request_id == request_id)
    finally:
        _stop(upstream)


def test_urllib_gzip_sync_flush_releases_the_first_complete_frame_early(tmp_path: Path) -> None:
    delta = b'data: {"type":"response.output_text.delta","delta":"early"}\n\n'
    completed = b'data: {"type":"response.completed"}\n\n'
    compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    first_wire_chunk = compressor.compress(delta) + compressor.flush(zlib.Z_SYNC_FLUSH)
    final_wire_chunk = compressor.compress(completed) + compressor.flush(zlib.Z_FINISH)
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream", "Content-Encoding": "gzip"},
        [(first_wire_chunk, 0.35), (final_wire_chunk, 0)],
    )
    try:
        router, context = _router(tmp_path, upstream)
        started_at = time.perf_counter()
        status, _headers, iterator, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 200
        assert next(iterator) == delta
        assert time.perf_counter() - started_at < 0.25
        assert list(iterator) == [completed]
        detail = _detail_fields(next(item.detail for item in router.logs if item.request_id == request_id))
        assert detail["stream_frame_count"] == "2"
        assert int(detail["first_content_delta_ms"]) >= 0
    finally:
        _stop(upstream)


def test_continuous_undelimited_stream_fails_before_response_commitment(tmp_path: Path) -> None:
    class UndelimitedResponse:
        status = 200
        headers = {"Content-Type": "text/event-stream"}
        http_version = "HTTP/1.1"
        transport = "test"

        def readline(self, _timeout: int = 0) -> bytes:
            return b"data: " + (b"x" * (MAX_SSE_FRAME_BYTES // 2)) + b"\n"

        def close(self) -> None:
            return

    class UndelimitedClient:
        def request(self, *_args: Any, **_kwargs: Any) -> UndelimitedResponse:
            return UndelimitedResponse()

    upstream = _start_upstream({"Content-Type": "application/json"}, [(b"{}", 0)])
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, UndelimitedClient())
        status, headers, chunks, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 502
        assert headers["Content-Type"].startswith("application/json")
        assert b"stream_protocol_error" in b"".join(chunks)
        log = next(item for item in router.logs if item.request_id == request_id)
        detail = _detail_fields(log.detail)
        assert log.event == "stream_protocol_error"
        assert detail["stream_wire_mode"] == "buffered_or_non_delimited"
        assert detail["stream_protocol_error"] == "true"
        assert int(detail["first_raw_line_ms"]) >= 0
        assert detail["first_complete_frame_ms"] == "-1"
        assert detail["stream_frame_count"] == "0"
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)


def test_undelimited_frame_deadline_timeout_is_a_protocol_error(tmp_path: Path) -> None:
    class DelayedUndelimitedResponse:
        status = 200
        headers = {"Content-Type": "text/event-stream"}
        http_version = "HTTP/1.1"
        transport = "test"

        def __init__(self, timeout_type: type[Exception]) -> None:
            self._timeout_type = timeout_type
            self._calls = 0

        def readline(self, _timeout: int = 0) -> bytes:
            self._calls += 1
            if self._calls == 1:
                return b"data: partial-without-delimiter\n"
            raise self._timeout_type("stream_idle_timeout")

        def close(self) -> None:
            return

    class DelayedUndelimitedClient:
        def __init__(self, timeout_type: type[Exception]) -> None:
            self._timeout_type = timeout_type

        def request(self, *_args: Any, **_kwargs: Any) -> DelayedUndelimitedResponse:
            return DelayedUndelimitedResponse(self._timeout_type)

    upstream = _start_upstream({"Content-Type": "application/json"}, [(b"{}", 0)])
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, DelayedUndelimitedClient(router.runtime.faults.stream_idle_timeout))
        status, _headers, chunks, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 502
        assert b"stream_protocol_error" in b"".join(chunks)
        detail = _detail_fields(next(item.detail for item in router.logs if item.request_id == request_id))
        assert detail["stream_protocol_reason"] == "stream_frame_wait_limit"
        assert detail["stream_wire_mode"] == "buffered_or_non_delimited"
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)


def test_pre_first_failures_keep_stage_metrics_for_connection_and_idle_timeout(tmp_path: Path) -> None:
    class ConnectionFailureClient:
        def request(self, *_args: Any, **_kwargs: Any) -> Any:
            from urllib.error import URLError

            raise URLError("local connection failure")

    upstream = _start_upstream({"Content-Type": "application/json"}, [(b"{}", 0)])
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, ConnectionFailureClient())
        with pytest.raises(AllModelsFailedError):
            router.stream("/v1/chat/completions", _payload(), context)
        connection_log = next(item for item in router.logs if item.event == "network")
        connection_detail = _detail_fields(connection_log.detail)
        assert int(connection_detail["candidate_selected_ms"]) >= 0
        assert int(connection_detail["upstream_request_started_ms"]) >= int(connection_detail["candidate_selected_ms"])
        assert connection_detail["upstream_headers_ms"] == "-1"
        assert connection_detail["first_raw_line_ms"] == "-1"
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)

    class IdleResponse:
        status = 200
        headers = {"Content-Type": "text/event-stream"}
        http_version = "HTTP/1.1"
        transport = "test"

        def __init__(self, error_type: type[Exception]) -> None:
            self._error_type = error_type

        def readline(self, _timeout: int = 0) -> bytes:
            raise self._error_type("stream_idle_timeout")

        def close(self) -> None:
            return

    class IdleClient:
        def __init__(self, error_type: type[Exception]) -> None:
            self._error_type = error_type

        def request(self, *_args: Any, **_kwargs: Any) -> IdleResponse:
            return IdleResponse(self._error_type)

    upstream = _start_upstream({"Content-Type": "application/json"}, [(b"{}", 0)])
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, IdleClient(router.runtime.faults.stream_idle_timeout))
        status, _headers, _chunks, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 504
        timeout_log = next(item for item in router.logs if item.request_id == request_id and item.event == "stream_timeout")
        timeout_detail = _detail_fields(timeout_log.detail)
        assert int(timeout_detail["candidate_selected_ms"]) >= 0
        assert int(timeout_detail["upstream_request_started_ms"]) >= int(timeout_detail["candidate_selected_ms"])
        assert int(timeout_detail["upstream_headers_ms"]) >= int(timeout_detail["upstream_request_started_ms"])
        assert timeout_detail["first_raw_line_ms"] == "-1"
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)


def test_handler_records_first_downstream_flush_after_actual_sse_write(tmp_path: Path) -> None:
    delta = b'data: {"type":"response.output_text.delta","delta":"flush evidence"}\n\n'
    completed = b'data: {"type":"response.completed"}\n\n'
    upstream = _start_upstream({"Content-Type": "text/event-stream"}, [(delta, 0), (completed, 0)])
    try:
        router, context = _router(tmp_path, upstream)

        class Handler:
            _all_models_failed_error_type = AllModelsFailedError

            def __init__(self) -> None:
                self.router = router
                self.headers: dict[str, str] = {"Content-Type": "application/json"}
                self.wfile = io.BytesIO()
                self.flush_count = 0
                original_flush = self.wfile.flush

                def flush() -> None:
                    self.flush_count += 1
                    original_flush()

                self.wfile.flush = flush  # type: ignore[method-assign]

            def send_response(self, _status: int) -> None:
                return

            def send_header(self, _name: str, _value: str) -> None:
                return

            def end_headers(self) -> None:
                return

            def _send_all_models_failed_error(self, error: Exception) -> None:
                raise AssertionError(error)

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                raise AssertionError((payload, status))

        handler = Handler()
        payload = _payload()
        handle_proxy_request(handler, "/v1/chat/completions", payload, context, json.dumps(payload).encode("utf-8"))

        log = next(item for item in router.logs if item.event == "stream_ok")
        detail = _detail_fields(log.detail)
        assert handler.flush_count == 2
        assert b"flush evidence" in handler.wfile.getvalue()
        assert int(detail["first_downstream_flush_ms"]) >= 0
        assert int(detail["first_downstream_flush_ms"]) - int(detail["first_complete_frame_ms"]) < 1000
        assert router.live_requests_payload()["count"] == 0
    finally:
        _stop(upstream)


def test_json_compat_handler_does_not_advertise_a_json_body_as_sse(tmp_path: Path) -> None:
    body = b'{"id":"json-compat","output":"complete"}'
    upstream = _start_upstream({"Content-Type": "application/json; charset=utf-8"}, [(body, 0)])
    try:
        router, context = _router(tmp_path, upstream)

        class Handler:
            _all_models_failed_error_type = AllModelsFailedError

            def __init__(self) -> None:
                self.router = router
                self.headers: dict[str, str] = {"Content-Type": "application/json"}
                self.wfile = io.BytesIO()
                self.sent_headers: list[tuple[str, str]] = []

            def send_response(self, _status: int) -> None:
                return

            def send_header(self, name: str, value: str) -> None:
                self.sent_headers.append((name, value))

            def end_headers(self) -> None:
                return

            def _send_all_models_failed_error(self, error: Exception) -> None:
                raise AssertionError(error)

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                raise AssertionError((payload, status))

        handler = Handler()
        payload = _payload()
        handle_proxy_request(handler, "/v1/chat/completions", payload, context, json.dumps(payload).encode("utf-8"))
        assert handler.wfile.getvalue() == body
        assert ("Content-Type", "application/json; charset=utf-8") in handler.sent_headers
        assert ("Content-Length", str(len(body))) in handler.sent_headers
        assert not any(value.startswith("text/event-stream") for name, value in handler.sent_headers if name.lower() == "content-type")
    finally:
        _stop(upstream)


def test_httpx_transport_is_used_and_debug_detail_avoids_full_urls_and_headers(tmp_path: Path) -> None:
    frame = b'data: {"type":"response.completed"}\n\n'
    upstream = _start_upstream({"Content-Type": "text/event-stream"}, [(frame, 0)])
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=True)
    try:
        response = client.request(
            "POST",
            f"http://127.0.0.1:{upstream.server_address[1]}/v1/chat/completions",
            {"Content-Type": "application/json", "Accept": "text/event-stream"},
            b"{}",
            stream=True,
            timeout=5,
        )
        assert response.transport == "httpx"
        assert response.readline() == frame.splitlines(keepends=True)[0]
        response.close()

        router, context = _router(tmp_path, upstream)
        candidate = next(router._iter_upstream_candidates("stream-model", context.group_id))
        detail = router._debug_detail(
            candidate,
            "stream-model",
            "https://relay.example/v1/chat/completions?URL_SENTINEL=secret",
            "raw",
            b'{"model":"stream-model"}',
            {"model": "stream-model"},
            {
                "Authorization": "Bearer HEADER_SENTINEL",
                "Referer": "https://client.example/?HEADER_URL_SENTINEL=secret",
                "User-Agent": "USER_AGENT_SENTINEL",
                "Content-Type": "application/json",
            },
            "stream ok",
        )
        assert "upstream_origin_hash=" in detail
        assert "upstream_endpoint=/v1/chat/completions" in detail
        assert "upstream=" not in detail
        assert "URL_SENTINEL" not in detail
        assert "HEADER_SENTINEL" not in detail
        assert "HEADER_URL_SENTINEL" not in detail
        assert "USER_AGENT_SENTINEL" not in detail

        custom_path_detail = router._debug_detail(
            candidate,
            "stream-model",
            "https://relay.example/tenant/PATH_SENTINEL/chat?query=secret",
            "raw",
            b'{"model":"stream-model"}',
            {"model": "stream-model"},
            {},
            "stream ok",
        )
        assert "PATH_SENTINEL" not in custom_path_detail
        assert "custom_path_sha256:" in custom_path_detail
    finally:
        client.close()
        _stop(upstream)


def test_httpx_stream_read_timeout_uses_group_idle_timeout_after_first_frame(tmp_path: Path) -> None:
    first_frame = b'data: {"type":"response.output_text.delta","delta":"first"}\n\n'
    completed_frame = b'data: {"type":"response.completed"}\n\n'
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream"},
        [(first_frame, 31), (completed_frame, 0)],
    )
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=False)
    try:
        router, context = _router(tmp_path, upstream, stream_idle_timeout=120)
        _use_upstream_client(router, client)

        status, _headers, iterator, request_id = router.stream(
            "/v1/chat/completions", _payload(), context
        )
        assert status == 200
        assert next(iterator) == first_frame
        resumed_at = time.perf_counter()
        assert list(iterator) == [completed_frame]
        assert 30 <= time.perf_counter() - resumed_at < 45

        log = next(item for item in router.logs if item.request_id == request_id)
        detail = _detail_fields(log.detail)
        assert detail["upstream_transport"] == "httpx"
        assert detail["stream_socket_idle_timeout_seconds"] == "120"
        assert detail["stream_socket_idle_timeout_applied"] == "true"
    finally:
        client.close()
        _stop(upstream)


def test_httpx_upstream_failure_is_not_silently_reissued_through_urllib() -> None:
    upstream = _start_upstream(
        {"Content-Type": "application/json"},
        [(b'{"error":"upstream down"}', 0)],
        status=500,
    )
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=False)
    try:
        with pytest.raises(HTTPError) as captured:
            client.request(
                "POST",
                f"http://127.0.0.1:{upstream.server_address[1]}/v1/chat/completions",
                {"Content-Type": "application/json"},
                b"{}",
                stream=True,
                timeout=5,
            )
        assert captured.value.code == 500
        assert len(upstream.calls) == 1  # type: ignore[attr-defined]
    finally:
        client.close()
        _stop(upstream)


def test_httpx_decoded_sse_does_not_forward_a_stale_content_encoding(tmp_path: Path) -> None:
    delta = b'data: {"type":"response.output_text.delta","delta":"decoded"}\n\n'
    completed = b'data: {"type":"response.completed"}\n\n'
    compressed = gzip.compress(delta + completed)
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream", "Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
        [(compressed, 0)],
    )
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=True)
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, client)

        class Handler:
            _all_models_failed_error_type = AllModelsFailedError

            def __init__(self) -> None:
                self.router = router
                self.headers: dict[str, str] = {"Content-Type": "application/json"}
                self.wfile = io.BytesIO()
                self.sent_headers: list[tuple[str, str]] = []

            def send_response(self, _status: int) -> None:
                return

            def send_header(self, name: str, value: str) -> None:
                self.sent_headers.append((name, value))

            def end_headers(self) -> None:
                return

            def _send_all_models_failed_error(self, error: Exception) -> None:
                raise AssertionError(error)

            def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
                raise AssertionError((payload, status))

        handler = Handler()
        payload = _payload()
        handle_proxy_request(handler, "/v1/chat/completions", payload, context, json.dumps(payload).encode("utf-8"))
        assert handler.wfile.getvalue() == delta + completed
        assert not any(name.lower() == "content-encoding" for name, _value in handler.sent_headers)
        assert not any(name.lower() == "content-length" for name, _value in handler.sent_headers)
        log = next(item for item in router.logs if item.event == "stream_ok")
        detail = _detail_fields(log.detail)
        assert detail["upstream_transport"] == "httpx"
        assert detail["upstream_content_encoding"] == "gzip"
        assert detail["stream_frame_count"] == "2"
    finally:
        client.close()
        _stop(upstream)


def test_httpx_post_first_frame_disconnect_is_an_upstream_stream_failure(tmp_path: Path) -> None:
    first_frame = b'data: {"type":"response.output_text.delta","delta":"first"}\n\n'

    class TruncatedHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(first_frame) + 128))
            self.end_headers()
            self.wfile.write(first_frame)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, _format: str, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", _free_port()), TruncatedHandler)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=False)
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, client)
        status, _headers, iterator, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 200
        assert next(iterator) == first_frame
        assert list(iterator) == []
        log = next(item for item in router.logs if item.request_id == request_id)
        assert log.status == "network"
        assert log.failure_scope == "upstream"
        assert "lifecycle=stream_incomplete" in log.detail
        assert "completion_signal=network_error" in log.detail
        assert router.live_requests_payload()["count"] == 0
    finally:
        client.close()
        _stop(upstream)


def test_httpx_bad_gzip_before_first_frame_cleans_up_the_live_request(tmp_path: Path) -> None:
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream", "Content-Encoding": "gzip"},
        [(b"not-a-valid-gzip-stream", 0)],
    )
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=False)
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, client)
        with pytest.raises(AllModelsFailedError):
            router.stream("/v1/chat/completions", _payload(), context)
        assert router.live_requests_payload()["count"] == 0
        log = next(item for item in router.logs if item.event == "network")
        assert log.failure_scope == "upstream"
    finally:
        client.close()
        _stop(upstream)


def test_httpx_unknown_content_encoding_keeps_the_downstream_header(tmp_path: Path) -> None:
    opaque_body = b"opaque-brotli-wire-payload"
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream", "Content-Encoding": "br"},
        [(opaque_body, 0)],
    )
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=False)
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, client)
        status, headers, iterator, _request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 200
        assert next(value for name, value in headers.items() if name.lower() == "content-encoding") == "br"
        assert b"".join(iterator) == opaque_body
    finally:
        client.close()
        _stop(upstream)


def test_unknown_content_encoding_passthrough_does_not_wait_for_eof(tmp_path: Path) -> None:
    first_wire_chunk = b"br-opaque-first"
    final_wire_chunk = b"br-opaque-final"
    upstream = _start_upstream(
        {"Content-Type": "text/event-stream", "Content-Encoding": "br"},
        [(first_wire_chunk, 0.35), (final_wire_chunk, 0)],
    )
    client = UpstreamClient(client_type="httpx", http2=False, keepalive=True)
    try:
        router, context = _router(tmp_path, upstream)
        _use_upstream_client(router, client)
        started_at = time.perf_counter()
        status, headers, iterator, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 200
        assert next(value for name, value in headers.items() if name.lower() == "content-encoding") == "br"
        assert next(iterator) == first_wire_chunk
        assert time.perf_counter() - started_at < 0.25
        assert list(iterator) == [final_wire_chunk]
        log = next(item for item in router.logs if item.request_id == request_id)
        detail = _detail_fields(log.detail)
        assert detail["stream_wire_mode"] == "buffered_or_non_delimited"
        assert detail["stream_frame_count"] == "0"
        assert detail["opaque_chunk_count"] == "2"
        assert router.live_requests_payload()["count"] == 0
    finally:
        client.close()
        _stop(upstream)


def test_upstream_error_bodies_are_not_persisted_in_request_logs(tmp_path: Path) -> None:
    body = b'{"error":"BODY_SENTINEL https://relay.example/?URL_SENTINEL=secret Authorization: Bearer HEADER_SENTINEL"}'
    upstream = _start_upstream({"Content-Type": "application/json"}, [(body, 0)], status=400)
    try:
        router, context = _router(tmp_path, upstream)
        status, _headers, iterator, request_id = router.stream("/v1/chat/completions", _payload(), context)
        assert status == 400
        assert body in b"".join(iterator)  # Preserve the existing client error body.
        log = next(item for item in router.logs if item.request_id == request_id)
        for sentinel in ("BODY_SENTINEL", "URL_SENTINEL", "HEADER_SENTINEL"):
            assert sentinel not in log.detail
            assert sentinel not in (tmp_path / "logs.jsonl").read_text(encoding="utf-8")
        assert "redacted_sha256:" in log.detail
    finally:
        _stop(upstream)


def test_rate_limit_retry_error_text_is_not_persisted_in_request_logs(tmp_path: Path) -> None:
    upstream = _start_upstream({"Content-Type": "application/json"}, [(b'{"error":"unused"}', 0)])

    class RetryFailureUpstream:
        def __init__(self) -> None:
            self.calls = 0

        def request(self, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            return_error = HTTPError(
                "https://relay.example/v1/chat/completions?RETRY_URL_SENTINEL=secret",
                429,
                "RETRY_BODY_SENTINEL",
                {},
                io.BytesIO(b'{"error":"RateLimitExceeded"}'),
            )
            raise return_error

    try:
        router, context = _router(tmp_path, upstream)
        retry_failure = RetryFailureUpstream()
        router.non_stream_execution.upstream = retry_failure
        router.runtime.upstream = retry_failure

        status, _headers, _body = router.call(
            "/v1/chat/completions",
            {**_payload(), "stream": False},
            context,
        )

        assert status == 429
        assert retry_failure.calls == 2
        retry_log = next(item for item in router.logs if item.status == "retry failed")
        persisted = (tmp_path / "logs.jsonl").read_text(encoding="utf-8")
        for sentinel in ("RETRY_BODY_SENTINEL", "RETRY_URL_SENTINEL"):
            assert sentinel not in retry_log.detail
            assert sentinel not in persisted
        assert "retry_failed" in retry_log.detail
        assert "redacted_sha256:" in retry_log.detail
    finally:
        _stop(upstream)
