"""PRD v1.0 P0-4：下游交付韧性的 handler mock 验收。"""
from __future__ import annotations

import errno
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from app import AllModelsFailedError, ArkProxyRouter
from linrouter_core.runtime import handler_runtime


class TrackingIterator:
    def __init__(self, values: list[bytes | Exception]) -> None:
        self._values = iter(values)
        self.next_count = 0
        self.closed = False

    def __iter__(self) -> "TrackingIterator":
        return self

    def __next__(self) -> bytes:
        self.next_count += 1
        value = next(self._values)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        self.closed = True


class TrackingWriteBuffer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        if self.error is not None:
            raise self.error
        return len(value)

    def flush(self) -> None:
        return None


class FlushFailureBuffer(TrackingWriteBuffer):
    def flush(self) -> None:
        raise RuntimeError("local flush failure")


@dataclass
class FakeRouter:
    iterator: TrackingIterator
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    finalized: list[str] = field(default_factory=list)
    stream_calls: int = 0

    def stream(self, *_args: Any) -> tuple[int, dict[str, str], TrackingIterator, str]:
        self.stream_calls += 1
        return 200, {"Content-Type": "text/event-stream"}, self.iterator, "request-1"

    def finalize_stream_if_needed(self, request_id: str) -> None:
        self.finalized.append(request_id)

    def record_stream_transport_event(self, _request_id: str, event: str, **evidence: Any) -> None:
        self.events.append((event, evidence))

    def record_stream_delivery_evidence(self, _request_id: str, **evidence: Any) -> None:
        self.evidence.append(evidence)

    def is_live_request_cancelled(self, _request_id: str) -> bool:
        return False


class FakeHandler:
    _all_models_failed_error_type = AllModelsFailedError

    def __init__(self, router: FakeRouter, wfile: TrackingWriteBuffer) -> None:
        self.router = router
        self.wfile = wfile
        self.headers: dict[str, str] = {}
        self.response_statuses: list[int] = []
        self.headers_sent: list[tuple[str, str]] = []

    def send_response(self, status: int) -> None:
        self.response_statuses.append(status)

    def send_header(self, name: str, value: str) -> None:
        self.headers_sent.append((name, value))

    def end_headers(self) -> None:
        return None

    def _send_all_models_failed_error(self, error: Exception) -> None:
        raise AssertionError(error)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raise AssertionError((payload, status))


def _run(iterator: TrackingIterator, wfile: TrackingWriteBuffer) -> tuple[FakeRouter, FakeHandler]:
    router = FakeRouter(iterator)
    handler = FakeHandler(router, wfile)
    handler_runtime.handle_proxy_request(
        handler,
        "/v1/chat/completions",
        {"stream": True},
        object(),
        b"{}",
    )
    return router, handler


@pytest.mark.parametrize(
    ("buffer", "expected"),
    [
        (TrackingWriteBuffer(BrokenPipeError(errno.EPIPE, "ignored")), "downstream_broken_pipe"),
        (TrackingWriteBuffer(ConnectionResetError(errno.ECONNRESET, "ignored")), "downstream_connection_reset"),
        (TrackingWriteBuffer(OSError(errno.EIO, "ignored")), "downstream_write_os_error"),
        (TrackingWriteBuffer(), "upstream_iterator_error_after_headers"),
        (FlushFailureBuffer(), "local_stream_handler_error"),
    ],
)
def test_post_header_failures_have_exact_anonymous_categories(
    buffer: TrackingWriteBuffer,
    expected: str,
) -> None:
    values: list[bytes | Exception] = [b"data: first\n\n"]
    if expected == "upstream_iterator_error_after_headers":
        values.append(RuntimeError("upstream iterator content must not be logged"))
    router, _handler = _run(TrackingIterator(values), buffer)

    assert expected in [event for event, _evidence in router.events]
    assert router.finalized == ["request-1"]
    if expected.startswith("downstream_"):
        event_evidence = next(evidence for event, evidence in router.events if event == expected)
        assert event_evidence["response_headers_emitted"] is True
        assert "ignored" not in str(event_evidence)


def test_168k_chunk_is_split_without_byte_or_terminal_regression() -> None:
    # [DONE] 刻意跨越 16 KiB 分片边界，证明终态判断使用完整上游 chunk。
    prefix = b"x" * (handler_runtime.MAX_DOWNSTREAM_WRITE_BYTES - 2)
    size = 168 * 1024
    chunk = prefix + b"[DONE]" + b"z" * (size - len(prefix) - len(b"[DONE]"))
    router, handler = _run(TrackingIterator([chunk]), TrackingWriteBuffer())

    assert b"".join(handler.wfile.writes) == chunk
    assert all(len(part) <= handler_runtime.MAX_DOWNSTREAM_WRITE_BYTES for part in handler.wfile.writes)
    assert "downstream_terminal_forwarded" in [event for event, _evidence in router.events]
    assert router.evidence[-1]["max_upstream_chunk_bytes"] == len(chunk)
    assert router.evidence[-1]["downstream_write_chunks"] == 11


@pytest.mark.parametrize("terminal", (b"response.failed", b"response.incomplete"))
def test_split_terminal_categories_remain_visible_for_failed_and_incomplete_streams(terminal: bytes) -> None:
    chunk = b"x" * (handler_runtime.MAX_DOWNSTREAM_WRITE_BYTES - 4) + terminal + b"\n\n"
    router, handler = _run(TrackingIterator([chunk]), TrackingWriteBuffer())

    assert b"".join(handler.wfile.writes) == chunk
    assert "downstream_terminal_forwarded" in [event for event, _evidence in router.events]


def test_downstream_disconnect_stops_writes_and_drains_at_one_mib_without_fallback() -> None:
    first = b"data: first\n\n"
    iterator = TrackingIterator([
        first,
        b"a" * (512 * 1024),
        b"b" * (512 * 1024),
        b"must-not-be-read",
    ])
    router, handler = _run(iterator, TrackingWriteBuffer(BrokenPipeError(errno.EPIPE, "ignored")))

    # 首次下游写失败后，不得继续写下游或触发第二个候选。
    assert handler.wfile.writes == [first]
    assert router.stream_calls == 1
    assert iterator.next_count == 3
    evidence = router.evidence[-1]
    assert evidence["downstream_drain_bytes"] == 1024 * 1024
    assert evidence["downstream_drain_stop_reason"] == "byte_budget"
    assert evidence["downstream_drain_terminal"] == ""


def test_downstream_disconnect_retains_terminal_from_the_current_complete_chunk() -> None:
    terminal = b'data: {"type":"response.failed"}\n\n'
    iterator = TrackingIterator([terminal, b"must-not-be-read"])
    router, handler = _run(iterator, TrackingWriteBuffer(BrokenPipeError(errno.EPIPE, "ignored")))

    assert handler.wfile.writes == [terminal]
    assert iterator.next_count == 1
    evidence = router.evidence[-1]
    assert evidence["downstream_drain_terminal"] == "response.failed"
    assert evidence["downstream_drain_stop_reason"] == "upstream_terminal"
    assert "downstream_terminal_forwarded" not in [event for event, _evidence in router.events]


def test_downstream_disconnect_stops_drain_after_fifteen_seconds() -> None:
    first = b"data: first\n\n"
    iterator = TrackingIterator([first, b"safe-terminal-observation", b"must-not-be-read"])
    with patch.object(handler_runtime.time, "monotonic", side_effect=[0.0, 0.0, 16.0, 16.0]):
        router, _handler = _run(iterator, TrackingWriteBuffer(BrokenPipeError(errno.EPIPE, "ignored")))

    evidence = router.evidence[-1]
    assert iterator.next_count == 2
    assert evidence["downstream_drain_stop_reason"] == "time_budget"
    assert evidence["downstream_drain_elapsed_ms"] == 16000
    assert handler_runtime.MAX_DOWNSTREAM_DRAIN_SECONDS == 15
    assert handler_runtime.MAX_DOWNSTREAM_DRAIN_BYTES == 1024 * 1024


@pytest.mark.parametrize(
    ("event", "scope"),
    [
        ("downstream_broken_pipe", "downstream"),
        ("downstream_connection_reset", "downstream"),
        ("downstream_write_os_error", "downstream"),
        ("upstream_iterator_error_after_headers", "upstream"),
        ("local_stream_handler_error", "local"),
    ],
)
def test_real_observability_persists_exact_post_header_category_without_error_text(
    tmp_path: Any,
    event: str,
    scope: str,
) -> None:
    class Store:
        groups: list[Any] = []
        models: list[Any] = []

    router = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    router.add_log(
        "/v1/chat/completions",
        "demo",
        "streaming",
        "stream_started_at_ms=1; final_result=streaming",
        request_id="request-1",
        event="stream_ok",
    )
    router.record_stream_transport_event(
        "request-1",
        event,
        response_headers_emitted=True,
        downstream_os_error_code=5,
        secret_error_text="MUST_NOT_PERSIST",
    )
    item = router.logs[0]
    assert item.event == event
    assert item.failure_scope == scope
    assert f"failure_category={event}" in item.detail
    assert "MUST_NOT_PERSIST" not in item.detail

    if scope == "downstream":
        assert router.observability.downstream_failure_category("request-1") == event
        router.finalize_stream_if_needed("request-1")
        assert f"final_result={event}" in item.detail
