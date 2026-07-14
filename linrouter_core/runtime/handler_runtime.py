"""HTTP proxy-response execution behind the ``RouterHandler`` compatibility facade."""
from __future__ import annotations

from typing import Any, Dict

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def _is_json_compat_response(headers: Dict[str, str]) -> bool:
    content_type = next((str(value) for key, value in headers.items() if str(key).lower() == "content-type"), "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _forward_response_headers(handler: Any, headers: Dict[str, str], *, stream: bool, data_length: int = 0) -> None:
    connection_header = next((value for key, value in headers.items() if key.lower() == "connection"), "")
    connection_tokens = {
        token.strip().lower()
        for value in connection_header.split(",")
        for token in [value]
        if token.strip()
    }
    sent_content_type = False
    json_compat = bool(stream and _is_json_compat_response(headers))
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP_HEADERS or lowered in connection_tokens:
            continue
        if lowered == "content-length" and not (stream and json_compat):
            continue
        # Normal streaming routes are normalized to SSE.  A fully buffered
        # JSON compatibility response keeps its JSON media type so clients do
        # not mistake one body for an incremental SSE stream.
        if stream and lowered in {"cache-control", "x-accel-buffering"}:
            continue
        if stream and lowered == "content-type" and not json_compat:
            continue
        if lowered == "content-type":
            if sent_content_type:
                continue
            sent_content_type = True
        handler.send_header(key, value)
    if stream:
        if not sent_content_type:
            handler.send_header("Content-Type", "application/json; charset=utf-8" if json_compat else "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
    elif not sent_content_type:
        handler.send_header("Content-Type", "application/json; charset=utf-8")
    if not stream:
        handler.send_header("Content-Length", str(data_length))


def _is_terminal_chunk(chunk: bytes) -> bool:
    return b"[DONE]" in chunk or any(signal in chunk for signal in (b"response.completed", b"response.failed", b"response.incomplete"))


def _dashboard_cancellation_requested(router: Any, request_id: str) -> bool:
    check = getattr(router, "is_live_request_cancelled", None)
    return bool(request_id and callable(check) and check(request_id))


def handle_proxy_request(
    handler: Any,
    path: str,
    payload: Dict[str, Any],
    route: Any,
    raw_body: bytes,
) -> None:
    """Execute the existing ``/v1`` and ``/chat`` POST response path via a handler facade."""
    stream = bool(payload.get("stream"))
    response_started = False
    request_id = ""
    try:
        if stream:
            status, headers, iterator, request_id = handler.router.stream(path, payload, route, dict(handler.headers.items()), raw_body)
            handler.send_response(status)
            _forward_response_headers(handler, headers, stream=True)
            handler.end_headers()
            response_started = True
            first_flush_recorded = False
            try:
                for chunk in iterator:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    if not first_flush_recorded and hasattr(handler.router, "record_stream_transport_event"):
                        handler.router.record_stream_transport_event(request_id, "downstream_first_flush")
                        first_flush_recorded = True
                    if _is_terminal_chunk(chunk) and hasattr(handler.router, "record_stream_transport_event"):
                        handler.router.record_stream_transport_event(request_id, "downstream_terminal_forwarded")
            except (BrokenPipeError, ConnectionResetError, OSError):
                if not _dashboard_cancellation_requested(handler.router, request_id) and hasattr(handler.router, "record_stream_transport_event"):
                    handler.router.record_stream_transport_event(request_id, "downstream_write_failed")
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
                handler.router.finalize_stream_if_needed(request_id)
            return
        status, headers, data = handler.router.call(path, payload, route, dict(handler.headers.items()), raw_body)
        handler.send_response(status)
        _forward_response_headers(handler, headers, stream=False, data_length=len(data))
        handler.end_headers()
        response_started = True
        handler.wfile.write(data)
    except handler._all_models_failed_error_type as err:
        if response_started:
            if request_id and hasattr(handler.router, "record_stream_transport_event"):
                handler.router.record_stream_transport_event(request_id, "downstream_write_failed")
            return
        handler._send_all_models_failed_error(err)
    except Exception as err:
        if response_started:
            if request_id and hasattr(handler.router, "record_stream_transport_event"):
                handler.router.record_stream_transport_event(request_id, "downstream_write_failed")
            return
        handler._send_json({
            "error": {
                "message": f"服务器内部错误: {err}",
                "type": "internal_server_error",
                "code": "internal_error",
            }
        }, status=500)
    return
