from __future__ import annotations

from upstream_client import _LineReader
from app import ArkProxyRouter


class _FakeHttpxResponse:
    def __init__(self, lines: list[object]) -> None:
        self._lines = iter(lines)
        self.closed = False

    def iter_lines(self):
        return self._lines

    def close(self) -> None:
        self.closed = True


def test_httpx_reader_normalizes_text_lines_and_preserves_sse_blank_lines() -> None:
    raw = _FakeHttpxResponse([
        'data: {"type":"response.completed"}',
        "",
        "data: [DONE]",
        "",
    ])
    reader = _LineReader(raw, is_httpx=True)

    assert reader.readline() == b'data: {"type":"response.completed"}\n'
    assert reader.readline() == b"\n"
    assert reader.readline() == b"data: [DONE]\n"
    assert reader.readline() == b"\n"
    assert reader.readline() == b""


def test_httpx_reader_accepts_bytes_lines_and_only_stop_iteration_is_eof() -> None:
    raw = _FakeHttpxResponse([b"data: first\n", b""])
    reader = _LineReader(raw, is_httpx=True)

    assert reader.readline() == b"data: first\n"
    assert reader.readline() == b"\n"
    assert reader.readline() == b""


def test_stream_usage_parser_accepts_multiline_sse_terminal_frame() -> None:
    chunk = (
        b"event: response.completed\n"
        b"data: {\"usage\": {\"prompt_tokens\": 3, \"completion_tokens\": 5, \"total_tokens\": 8}}\n\n"
    )
    assert ArkProxyRouter._usage_from_stream_chunk(chunk) == (3, 5, 8, 0, 0)


def test_stream_usage_parser_preserves_explicit_all_zero_usage_presence() -> None:
    chunk = b'data: {"type":"response.completed","usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}\n\n'

    assert ArkProxyRouter._usage_from_stream_chunk_with_presence(chunk) == ((0, 0, 0, 0, 0), True)
    assert ArkProxyRouter._usage_from_stream_chunk_with_presence(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n') == ((0, 0, 0, 0, 0), False)
