"""HTTP proxy-response execution behind the ``RouterHandler`` compatibility facade."""
from __future__ import annotations

import errno
import time
from typing import Any, Dict

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

# 仅改变 socket 写入边界，完整 SSE chunk 的语义仍由上游运行时负责。
MAX_DOWNSTREAM_WRITE_BYTES = 16 * 1024
MAX_DOWNSTREAM_DRAIN_SECONDS = 15
MAX_DOWNSTREAM_DRAIN_BYTES = 1 * 1024 * 1024

_DOWNSTREAM_FAILURE_EVENTS = {
    "downstream_broken_pipe",
    "downstream_connection_reset",
    "downstream_write_os_error",
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


def _terminal_signal(chunk: bytes) -> str:
    """只从完整上游 chunk 提取匿名终态，不能按下游分片判断。"""
    if b"response.failed" in chunk:
        return "response.failed"
    if b"response.incomplete" in chunk:
        return "response.incomplete"
    if b"response.completed" in chunk:
        return "response.completed"
    if b"[DONE]" in chunk:
        return "[DONE]"
    return ""


def _dashboard_cancellation_requested(router: Any, request_id: str) -> bool:
    check = getattr(router, "is_live_request_cancelled", None)
    return bool(request_id and callable(check) and check(request_id))


def _downstream_failure_event(error: OSError) -> str:
    if isinstance(error, BrokenPipeError):
        return "downstream_broken_pipe"
    if isinstance(error, ConnectionResetError):
        return "downstream_connection_reset"
    return "downstream_write_os_error"


def _record_transport_event(router: Any, request_id: str, event: str, **evidence: int | str | bool) -> None:
    """兼容旧 facade，同时让新运行时获取精确匿名归因。"""
    if not request_id:
        return
    record = getattr(router, "record_stream_transport_event", None)
    if not callable(record):
        return
    try:
        record(request_id, event, **evidence)
    except TypeError:
        # 既有 handler 契约替身只有两个参数；下游异常保留其旧事件名。
        fallback = "downstream_write_failed" if event in _DOWNSTREAM_FAILURE_EVENTS else event
        record(request_id, fallback)


def _record_delivery_evidence(router: Any, request_id: str, **evidence: int | str | bool) -> None:
    record = getattr(router, "record_stream_delivery_evidence", None)
    if callable(record) and request_id:
        record(request_id, **evidence)


def _write_downstream_chunk(handler: Any, chunk: bytes) -> int:
    """按固定上限写下游；返回实际成功 flush 的分片数量。"""
    written_chunks = 0
    for offset in range(0, len(chunk), MAX_DOWNSTREAM_WRITE_BYTES):
        fragment = chunk[offset : offset + MAX_DOWNSTREAM_WRITE_BYTES]
        written = handler.wfile.write(fragment)
        if written is not None and int(written) != len(fragment):
            raise OSError(errno.EIO, "partial downstream write")
        handler.wfile.flush()
        written_chunks += 1
    return written_chunks


def _drain_after_downstream_disconnect(iterator: Any, router: Any, request_id: str) -> Dict[str, int | str]:
    """断开后只保留有限匿名终态证据，绝不再触碰下游连接。"""
    started_at = time.monotonic()
    deadline = started_at + MAX_DOWNSTREAM_DRAIN_SECONDS
    drained_bytes = 0
    drained_chunks = 0
    max_chunk_bytes = 0
    terminal = ""
    stop_reason = "time_budget"

    while drained_bytes < MAX_DOWNSTREAM_DRAIN_BYTES and time.monotonic() < deadline:
        try:
            chunk = next(iterator)
        except StopIteration:
            stop_reason = "upstream_eof"
            break
        except Exception:
            # 下游断开仍是主因；这里仅留下匿名的上游终态观察。
            stop_reason = "upstream_iterator_error_after_headers"
            break
        if not isinstance(chunk, bytes):
            stop_reason = "upstream_iterator_error_after_headers"
            break
        if not chunk:
            stop_reason = "upstream_eof"
            break

        max_chunk_bytes = max(max_chunk_bytes, len(chunk))
        remaining = MAX_DOWNSTREAM_DRAIN_BYTES - drained_bytes
        if len(chunk) > remaining:
            # iterator 的每个值都是不可拆回的完整上游 chunk；不继续读取下一块。
            drained_bytes += len(chunk)
            drained_chunks += 1
            stop_reason = "byte_budget"
            break

        drained_bytes += len(chunk)
        drained_chunks += 1
        terminal = _terminal_signal(chunk)
        if terminal:
            stop_reason = "upstream_terminal"
            break

    if drained_bytes >= MAX_DOWNSTREAM_DRAIN_BYTES and stop_reason == "time_budget":
        stop_reason = "byte_budget"
    elapsed_ms = max(0, int((time.monotonic() - started_at) * 1000))
    return {
        "downstream_drain_bytes": drained_bytes,
        "downstream_drain_chunks": drained_chunks,
        "downstream_drain_elapsed_ms": elapsed_ms,
        "downstream_drain_stop_reason": stop_reason,
        "downstream_drain_terminal": terminal,
        "downstream_drain_max_upstream_chunk_bytes": max_chunk_bytes,
    }


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
            max_upstream_chunk_bytes = 0
            downstream_write_chunks = 0
            downstream_bytes_written = 0
            terminal_forwarded = False
            drain_evidence: Dict[str, int | str] = {}
            try:
                upstream_iterator = iter(iterator)
                while True:
                    try:
                        chunk = next(upstream_iterator)
                    except StopIteration:
                        break
                    except Exception:
                        _record_transport_event(
                            handler.router,
                            request_id,
                            "upstream_iterator_error_after_headers",
                            response_headers_emitted=True,
                        )
                        break

                    if not isinstance(chunk, bytes):
                        _record_transport_event(
                            handler.router,
                            request_id,
                            "upstream_iterator_error_after_headers",
                            response_headers_emitted=True,
                        )
                        break
                    max_upstream_chunk_bytes = max(max_upstream_chunk_bytes, len(chunk))
                    try:
                        written = _write_downstream_chunk(handler, chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError) as error:
                        if not _dashboard_cancellation_requested(handler.router, request_id):
                            _record_transport_event(
                                handler.router,
                                request_id,
                                _downstream_failure_event(error),
                                response_headers_emitted=True,
                                downstream_os_error_code=int(error.errno or 0),
                                max_upstream_chunk_bytes=max_upstream_chunk_bytes,
                                downstream_write_chunks=downstream_write_chunks,
                                downstream_bytes_written=downstream_bytes_written,
                                downstream_terminal_forwarded=terminal_forwarded,
                            )
                            current_terminal = _terminal_signal(chunk)
                            if current_terminal:
                                # 当前完整上游 chunk 已在内存中，可保留匿名终态；
                                # 但它没有完整下发，绝不能记为 downstream_terminal_forwarded。
                                drain_evidence = {
                                    "downstream_drain_bytes": 0,
                                    "downstream_drain_chunks": 0,
                                    "downstream_drain_elapsed_ms": 0,
                                    "downstream_drain_stop_reason": "upstream_terminal",
                                    "downstream_drain_terminal": current_terminal,
                                    "downstream_drain_max_upstream_chunk_bytes": 0,
                                }
                            else:
                                drain_evidence = _drain_after_downstream_disconnect(
                                    upstream_iterator,
                                    handler.router,
                                    request_id,
                                )
                        break
                    except Exception:
                        _record_transport_event(
                            handler.router,
                            request_id,
                            "local_stream_handler_error",
                            response_headers_emitted=True,
                            max_upstream_chunk_bytes=max_upstream_chunk_bytes,
                            downstream_write_chunks=downstream_write_chunks,
                            downstream_bytes_written=downstream_bytes_written,
                            downstream_terminal_forwarded=terminal_forwarded,
                        )
                        break

                    downstream_write_chunks += written
                    downstream_bytes_written += len(chunk)
                    if not first_flush_recorded:
                        _record_transport_event(handler.router, request_id, "downstream_first_flush")
                        first_flush_recorded = True
                    if _is_terminal_chunk(chunk):
                        _record_transport_event(handler.router, request_id, "downstream_terminal_forwarded")
                        terminal_forwarded = True
            finally:
                _record_delivery_evidence(
                    handler.router,
                    request_id,
                    max_upstream_chunk_bytes=max(
                        max_upstream_chunk_bytes,
                        int(drain_evidence.get("downstream_drain_max_upstream_chunk_bytes", 0) or 0),
                    ),
                    downstream_write_chunks=downstream_write_chunks,
                    downstream_bytes_written=downstream_bytes_written,
                    downstream_terminal_forwarded=terminal_forwarded,
                    **drain_evidence,
                )
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
            _record_transport_event(
                handler.router,
                request_id,
                "local_stream_handler_error" if stream else "downstream_write_failed",
                response_headers_emitted=True,
            )
            return
        handler._send_all_models_failed_error(err)
    except Exception as err:
        if response_started:
            _record_transport_event(
                handler.router,
                request_id,
                "local_stream_handler_error" if stream else "downstream_write_failed",
                response_headers_emitted=True,
            )
            return
        handler._send_json({
            "error": {
                "message": f"服务器内部错误: {err}",
                "type": "internal_server_error",
                "code": "internal_error",
            }
        }, status=500)
    return
