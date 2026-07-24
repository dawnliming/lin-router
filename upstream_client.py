from __future__ import annotations

import queue
import ssl
import threading
import zlib
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class StreamIdleTimeoutError(TimeoutError):
    pass


class UpstreamResponse:
    """统一上游响应包装，兼容 urllib 与 httpx。

    提供 readline(timeout_seconds) 方法以支持流式空闲超时检测。
    """

    def __init__(
        self,
        status: int,
        headers: Dict[str, str],
        http_version: str,
        line_reader: Optional[Any] = None,
        body_bytes: Optional[bytes] = None,
        close_callback: Optional[Any] = None,
        transport: str = "urllib",
        content_decoded: bool = False,
        opaque_stream: bool = False,
        stream_read_timeout_preconfigured: bool = False,
    ) -> None:
        self.status = status
        self.headers = headers
        self.http_version = http_version
        # This is the transport that actually served this response, rather
        # than the configured preference.  It keeps transport A/B evidence
        # honest when httpx is unavailable during client initialization.
        self.transport = transport
        self.content_decoded = bool(content_decoded)
        self.opaque_stream = bool(opaque_stream)
        self._line_reader = line_reader
        self._body_bytes = body_bytes
        self._close_callback = close_callback
        self._stream_read_timeout_preconfigured = bool(stream_read_timeout_preconfigured)
        self._closed = False
        self._body_consumed = False

    def readline(self, timeout_seconds: int = 0) -> bytes:
        """读取一行 SSE 数据；timeout_seconds <= 0 表示无限等待。"""
        if self._line_reader is not None:
            return self._line_reader.readline(timeout_seconds)
        if self._body_bytes is not None and not self._body_consumed:
            self._body_consumed = True
            return self._body_bytes
        return b""

    def read(self) -> bytes:
        if self._body_bytes is not None:
            return self._body_bytes
        if self._line_reader is not None:
            chunks = []
            while True:
                chunk = self.readline()
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        return b""

    def read_chunk(self, timeout_seconds: int = 0) -> bytes:
        if self._line_reader is not None:
            read_chunk = getattr(self._line_reader, "read_chunk", None)
            if callable(read_chunk):
                return read_chunk(timeout_seconds)
            return self._line_reader.readline(timeout_seconds)
        return self.readline(timeout_seconds)

    def set_stream_idle_timeout(self, timeout_seconds: int) -> bool:
        """响应头到达后切换流读取 socket 超时，避免沿用初始响应上限。"""
        if self._line_reader is None:
            return self._stream_read_timeout_preconfigured
        configure = getattr(self._line_reader, "set_stream_idle_timeout", None)
        if callable(configure) and configure(timeout_seconds):
            return True
        # httpx 在构建请求时已把 read 超时写入传输配置，响应头后无需再次
        # 修改底层对象；该标记使运行时观测仍能反映真实配置已生效。
        return self._stream_read_timeout_preconfigured

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._line_reader is not None:
            try:
                self._line_reader.close()
            except Exception:
                pass
        if self._close_callback is not None:
            try:
                self._close_callback()
            except Exception:
                pass

    def __enter__(self) -> "UpstreamResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class _LineReader:
    """把 urllib/httpx 原始响应包装成支持超时 readline 的迭代器。"""

    def __init__(
        self,
        raw: Any,
        is_httpx: bool = False,
        raw_bytes: bool = False,
        content_encoding: str = "",
        opaque: bool = False,
    ) -> None:
        self._raw = raw
        self._is_httpx = is_httpx
        self._raw_bytes = bool(raw_bytes)
        self._opaque = bool(opaque)
        self._buffer: bytes = b""
        self._closed = False
        self._decoded_eof = False
        self._content_decoder = self._urllib_content_decoder(content_encoding) if not is_httpx else None
        if is_httpx:
            self._iter = raw.iter_raw() if self._raw_bytes else raw.iter_lines()
        else:
            self._iter = None

    @staticmethod
    def _urllib_content_decoder(content_encoding: str) -> Any:
        normalized = str(content_encoding or "").strip().lower()
        if normalized == "gzip":
            return zlib.decompressobj(16 + zlib.MAX_WBITS)
        if normalized == "deflate":
            return zlib.decompressobj()
        return None

    def _read_decoded_urllib_line(self) -> bytes:
        """Split known decoded encodings into logical SSE lines without EOF buffering."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = self._buffer[: newline + 1]
                self._buffer = self._buffer[newline + 1 :]
                return line
            if self._decoded_eof:
                line, self._buffer = self._buffer, b""
                return line
            read1 = getattr(self._raw, "read1", None)
            raw_chunk = read1(8192) if callable(read1) else self._raw.read(8192)
            if raw_chunk:
                try:
                    self._buffer += self._content_decoder.decompress(bytes(raw_chunk))
                except zlib.error as exc:
                    raise URLError("upstream content decoding failed") from exc
                continue
            try:
                self._buffer += self._content_decoder.flush()
            except zlib.error as exc:
                raise URLError("upstream content decoding failed") from exc
            if not self._content_decoder.eof:
                raise URLError("upstream content decoding failed: truncated stream")
            self._decoded_eof = True

    @staticmethod
    def _as_bytes(chunk: Any) -> bytes:
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        if isinstance(chunk, bytearray):
            return bytes(chunk)
        return chunk if isinstance(chunk, bytes) else bytes(chunk)

    def _read_raw_chunk_once(self) -> bytes:
        if self._is_httpx:
            try:
                return self._as_bytes(next(self._iter))
            except StopIteration:
                return b""
            except Exception as exc:
                self._raise_normalized_httpx_error(exc)
        read1 = getattr(self._raw, "read1", None)
        chunk = read1(8192) if callable(read1) else self._raw.read(8192)
        return self._as_bytes(chunk) if chunk else b""

    def _read_once(self) -> bytes:
        if self._is_httpx:
            if self._raw_bytes:
                while True:
                    newline = self._buffer.find(b"\n")
                    if newline >= 0:
                        line = self._buffer[: newline + 1]
                        self._buffer = self._buffer[newline + 1 :]
                        return line
                    try:
                        chunk = next(self._iter)
                    except StopIteration:
                        line, self._buffer = self._buffer, b""
                        return line
                    except Exception as exc:
                        self._raise_normalized_httpx_error(exc)
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    elif isinstance(chunk, bytearray):
                        chunk = bytes(chunk)
                    elif not isinstance(chunk, bytes):
                        chunk = bytes(chunk)
                    self._buffer += chunk
            try:
                line = next(self._iter)
            except StopIteration:
                return b""
            except Exception as exc:
                self._raise_normalized_httpx_error(exc)
            # httpx.iter_lines() yields text lines without their line ending.
            # Keep SSE blank lines: they delimit an event and are not EOF.
            if isinstance(line, str):
                line = line.encode("utf-8")
            elif isinstance(line, bytearray):
                line = bytes(line)
            elif not isinstance(line, bytes):
                line = bytes(line)
            if not line.endswith(b"\n"):
                line += b"\n"
            return line
        if self._content_decoder is not None:
            return self._read_decoded_urllib_line()
        return self._raw.readline()

    def _read_with_timeout(self, reader: Any, timeout_seconds: int) -> bytes:
        if timeout_seconds <= 0:
            return reader()
        result: queue.Queue[Any] = queue.Queue(maxsize=1)

        def _read() -> None:
            try:
                result.put(reader())
            except Exception as exc:
                result.put(exc)

        worker = threading.Thread(target=_read, daemon=True)
        worker.start()
        try:
            item = result.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise StreamIdleTimeoutError("stream_idle_timeout") from exc
        if isinstance(item, Exception):
            # urllib 在 socket 已设置读取超时时直接抛出 socket.timeout。
            # 对上层而言这与线程等待到期同属流空闲超时，必须统一归因。
            if isinstance(item, TimeoutError):
                raise StreamIdleTimeoutError("stream_idle_timeout") from item
            raise item
        return item

    @staticmethod
    def _raise_normalized_httpx_error(exc: Exception) -> None:
        # Router runtime already handles urllib-style transport errors.
        # Normalize late httpx stream failures so post-first-frame disconnects
        # and decoding faults never masquerade as client disconnects.
        try:
            import httpx
            if isinstance(exc, httpx.TimeoutException):
                raise TimeoutError(str(exc)) from exc
            if isinstance(exc, httpx.RequestError):
                raise URLError(str(exc)) from exc
        except ImportError:
            pass
        raise exc

    def readline(self, timeout_seconds: int = 0) -> bytes:
        if self._closed:
            return b""
        return self._read_with_timeout(self._read_once, timeout_seconds)

    def read_chunk(self, timeout_seconds: int = 0) -> bytes:
        if self._closed:
            return b""
        return self._read_with_timeout(self._read_raw_chunk_once, timeout_seconds)

    def set_stream_idle_timeout(self, timeout_seconds: int) -> bool:
        """仅 urllib 可在 HTTP 响应头后安全更新 socket 的读取超时。"""
        if self._is_httpx:
            return False
        timeout: float | None = None if timeout_seconds <= 0 else float(timeout_seconds)
        pending = [self._raw]
        visited: set[int] = set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in visited:
                continue
            visited.add(id(current))
            settimeout = getattr(current, "settimeout", None)
            if callable(settimeout):
                try:
                    settimeout(timeout)
                    return True
                except (OSError, ValueError):
                    pass
            # urllib 的标准层级是 HTTPResponse.fp.raw._sock；保留其他常见
            # 包装层可兼容 SSL 与不同 Python 小版本，而不依赖私有类型判断。
            for attribute in ("_sock", "sock", "socket", "raw", "fp"):
                child = getattr(current, attribute, None)
                if child is not None:
                    pending.append(child)
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._raw.close()
        except Exception:
            pass


class UpstreamClient:
    """上游 HTTP 客户端抽象，支持 urllib 与 httpx，失败时自动降级。"""

    def __init__(
        self,
        client_type: str = "urllib",
        http2: bool = False,
        keepalive: bool = False,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self.client_type = client_type if client_type in ("urllib", "httpx") else "urllib"
        self.http2 = bool(http2)
        self.keepalive = bool(keepalive)
        self.ssl_context = ssl_context
        self._httpx_client: Optional[Any] = None
        self._httpx_available: Optional[bool] = None
        self._init_error: Optional[str] = None
        if self.client_type == "httpx":
            self._ensure_httpx_client()

    def _ensure_httpx_client(self) -> bool:
        if self._httpx_client is not None:
            return True
        if self._httpx_available is False:
            return False
        try:
            import httpx
        except Exception as exc:
            self._httpx_available = False
            self._init_error = f"httpx import failed: {exc}"
            return False
        try:
            # ``None`` is not a valid value for httpx.Client(limits=...), and
            # the defaults would retain idle connections even when the caller
            # explicitly disabled keepalive.  Use one explicit pool policy for
            # every httpx variant so A/B configuration maps to real behavior.
            limits = httpx.Limits(
                max_keepalive_connections=20 if self.keepalive else 0,
                max_connections=100,
            )
            self._httpx_client = httpx.Client(
                http2=self.http2,
                limits=limits,
                verify=self.ssl_context if self.ssl_context is not None else True,
            )
            self._httpx_available = True
            return True
        except Exception as exc:
            self._httpx_available = False
            self._init_error = f"httpx client init failed: {exc}"
            return False

    def _detect_http_version(self, resp: Any, is_httpx: bool) -> str:
        version = getattr(resp, "http_version", None)
        if version:
            return str(version)
        if not is_httpx:
            raw_version = getattr(resp, "version", None)
            if raw_version == 10:
                return "HTTP/1.0"
            if raw_version == 11:
                return "HTTP/1.1"
        return "unknown"

    @staticmethod
    def _content_encoding_tokens(headers: Dict[str, str]) -> list[str]:
        content_encoding = next(
            (str(value) for name, value in headers.items() if str(name).lower() == "content-encoding"),
            "",
        )
        return [item.strip().lower() for item in content_encoding.split(",") if item.strip()]

    @staticmethod
    def _httpx_decodes_content(headers: Dict[str, str]) -> bool:
        """Return true only when every advertised encoding has a live decoder."""
        encodings = UpstreamClient._content_encoding_tokens(headers)
        if not encodings or encodings == ["identity"]:
            return False
        try:
            from httpx._decoders import SUPPORTED_DECODERS
            return all(encoding in SUPPORTED_DECODERS for encoding in encodings)
        except Exception:
            # gzip and deflate are built-in httpx decoders across supported
            # versions.  Unknown encodings must keep their response header.
            return all(encoding in {"gzip", "deflate"} for encoding in encodings)

    @staticmethod
    def _urllib_stream_content_encoding(headers: Dict[str, str]) -> str:
        encodings = UpstreamClient._content_encoding_tokens(headers)
        if len(encodings) == 1 and encodings[0] in {"gzip", "deflate"}:
            return encodings[0]
        return ""

    def _urllib_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        stream: bool,
        timeout: float,
        stream_idle_timeout: float | None = None,
    ) -> UpstreamResponse:
        request = Request(url, data=body, headers=headers, method=method)
        kwargs: Dict[str, Any] = {"timeout": timeout}
        if self.ssl_context is not None:
            kwargs["context"] = self.ssl_context
        resp = urlopen(request, **kwargs)
        response_headers = dict(resp.headers.items())
        status = getattr(resp, "status", getattr(resp, "code", 200))
        http_version = self._detect_http_version(resp, is_httpx=False)
        if stream:
            content_encoding = self._urllib_stream_content_encoding(response_headers)
            encoding_tokens = self._content_encoding_tokens(response_headers)
            opaque_stream = bool(encoding_tokens and encoding_tokens != ["identity"] and not content_encoding)
            return UpstreamResponse(
                status=status,
                headers=response_headers,
                http_version=http_version,
                line_reader=_LineReader(resp, is_httpx=False, content_encoding=content_encoding, opaque=opaque_stream),
                close_callback=resp.close,
                transport="urllib",
                content_decoded=bool(content_encoding),
                opaque_stream=opaque_stream,
            )
        return UpstreamResponse(
            status=status,
            headers=response_headers,
            http_version=http_version,
            body_bytes=resp.read(),
            close_callback=resp.close,
            transport="urllib",
        )

    def _httpx_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        stream: bool,
        timeout: float,
        stream_idle_timeout: float | None = None,
    ) -> UpstreamResponse:
        import httpx

        if not self._ensure_httpx_client():
            return self._urllib_request(method, url, headers, body, stream, timeout, stream_idle_timeout)
        client = self._httpx_client
        assert client is not None
        # httpx 的单个标量 timeout 会同时限制连接和后续流读取。将其拆开：
        # 连接、写入和连接池仍受初始响应保护；流读取由连接组空闲上限控制。
        read_timeout: float | None = timeout
        if stream and stream_idle_timeout is not None:
            read_timeout = None if stream_idle_timeout <= 0 else float(stream_idle_timeout)
        request_timeout = httpx.Timeout(
            connect=float(timeout),
            write=float(timeout),
            pool=float(timeout),
            read=read_timeout,
        )
        request = client.build_request(method, url, headers=headers, content=body, timeout=request_timeout)
        if stream:
            resp = client.send(request, stream=True)
            resp.raise_for_status()
            response_headers = dict(resp.headers.items())
            status = resp.status_code
            http_version = self._detect_http_version(resp, is_httpx=True)
            content_decoded = self._httpx_decodes_content(response_headers)
            encoding_tokens = self._content_encoding_tokens(response_headers)
            opaque_stream = bool(encoding_tokens and encoding_tokens != ["identity"] and not content_decoded)
            return UpstreamResponse(
                status=status,
                headers=response_headers,
                http_version=http_version,
                line_reader=_LineReader(resp, is_httpx=True, raw_bytes=not content_decoded, opaque=opaque_stream),
                close_callback=resp.close,
                transport="httpx",
                content_decoded=content_decoded,
                opaque_stream=opaque_stream,
                stream_read_timeout_preconfigured=stream_idle_timeout is not None,
            )
        resp = client.send(request)
        resp.raise_for_status()
        response_headers = dict(resp.headers.items())
        status = resp.status_code
        http_version = self._detect_http_version(resp, is_httpx=True)
        return UpstreamResponse(
            status=status,
            headers=response_headers,
            http_version=http_version,
            body_bytes=resp.content,
            close_callback=resp.close,
            transport="httpx",
            content_decoded=self._httpx_decodes_content(response_headers),
        )

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        stream: bool = False,
        timeout: float = 120.0,
        stream_idle_timeout: float | None = None,
    ) -> UpstreamResponse:
        if self.client_type == "httpx":
            if not self._ensure_httpx_client():
                return self._urllib_request(method, url, headers, body, stream, timeout, stream_idle_timeout)
            try:
                return self._httpx_request(
                    method,
                    url,
                    headers,
                    body,
                    stream,
                    timeout,
                    stream_idle_timeout,
                )
            except HTTPError:
                raise
            except Exception as exc:
                # Keep status and transport errors compatible with the router
                # without silently reissuing a possibly already-sent request
                # through urllib.  Retrying here makes transport A/B timings
                # and upstream call counts untrustworthy.
                httpx_status_error = self._try_extract_httpx_status_error(exc)
                if httpx_status_error is not None:
                    raise httpx_status_error
                try:
                    import httpx
                    if isinstance(exc, httpx.TimeoutException):
                        raise TimeoutError(str(exc)) from exc
                    if isinstance(exc, httpx.RequestError):
                        raise URLError(str(exc)) from exc
                except ImportError:
                    pass
                raise
        return self._urllib_request(method, url, headers, body, stream, timeout, stream_idle_timeout)

    @staticmethod
    def _try_extract_httpx_status_error(exc: Exception) -> Optional[HTTPError]:
        try:
            import httpx
        except Exception:
            return None
        if not isinstance(exc, httpx.HTTPStatusError):
            return None
        response = getattr(exc, "response", None)
        if response is None:
            return None
        from io import BytesIO

        code = int(response.status_code)
        try:
            body = response.content or b""
        except Exception:
            try:
                body = response.read() or b""
            except Exception:
                body = b""
        try:
            response.close()
        except Exception:
            pass
        if isinstance(body, str):
            body = body.encode("utf-8")
        fp = BytesIO(body)
        url = str(exc.request.url) if hasattr(exc, "request") and exc.request else ""
        return HTTPError(url, code, str(exc), dict(response.headers), fp)

    def close(self) -> None:
        if self._httpx_client is not None:
            try:
                self._httpx_client.close()
            except Exception:
                pass
            self._httpx_client = None

    def __del__(self) -> None:
        self.close()
