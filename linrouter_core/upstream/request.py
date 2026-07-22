"""Existing upstream request construction rules, kept behavior-compatible."""
from __future__ import annotations

from typing import Dict
from urllib.parse import urlparse

BLOCKED_FORWARD_HEADERS = {
    "authorization", "connection", "content-length", "transfer-encoding", "host",
    "openai-organization", "openai-project", "x-request-id", "x-linrouter-session",
}
WAF_STRIP_PREFIXES = ("x-stainless-",)
WAF_STRIP_EXACT = {
    "host", "connection", "content-length", "user-agent", "cache-control", "pragma",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "authorization", "openai-organization",
    "openai-project", "x-request-id", "x-linrouter-session",
}
PASSTHROUGH_STRIP_EXACT = {
    "host", "connection", "content-length", "transfer-encoding", "authorization",
    # 仅供本地路由选择使用，任何上游路径都不得收到该 Header。
    "x-linrouter-session",
}
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def build_upstream_headers(api_key: str, *, stream: bool) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def build_waf_compatible_headers(incoming_headers: Dict[str, str], upstream_host: str, *, stream: bool) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in incoming_headers.items():
        lower = name.strip().lower()
        if not lower or lower in WAF_STRIP_EXACT or any(lower.startswith(prefix) for prefix in WAF_STRIP_PREFIXES):
            continue
        headers[name] = value
    headers["host"] = upstream_host
    headers["user-agent"] = BROWSER_UA
    if not any(key.lower() == "accept" for key in headers):
        headers["accept"] = "application/json, text/event-stream, */*"
    if not any(key.lower() == "accept-language" for key in headers):
        headers["accept-language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    return headers


def build_passthrough_headers(api_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in incoming_headers.items():
        lower = name.strip().lower()
        if not lower or lower in PASSTHROUGH_STRIP_EXACT:
            continue
        headers[name] = value
    headers["Authorization"] = f"Bearer {api_key}"
    headers["Content-Type"] = headers.get("Content-Type") or headers.get("content-type") or "application/json"
    if stream and not any(key.lower() == "accept" for key in headers):
        headers["Accept"] = "text/event-stream"
    elif not stream and not any(key.lower() == "accept" for key in headers):
        headers["Accept"] = "application/json"
    return headers


def build_model_fetch_headers(auth_key: str) -> Dict[str, str]:
    return {
        "authorization": f"Bearer {auth_key}",
        "user-agent": BROWSER_UA,
        "accept": "application/json, text/event-stream, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
    }


def can_forward_header(name: str) -> bool:
    normalized = name.strip().lower()
    return bool(normalized) and normalized not in BLOCKED_FORWARD_HEADERS and not normalized.startswith("x-stainless-")


def build_request_headers(
    *, base_url: str, auth_key: str, incoming_headers: Dict[str, str], stream: bool,
    waf_compatible: bool, waf_accept_policy: str,
) -> Dict[str, str]:
    """Build the pre-existing header result; caller owns WAF decision semantics."""
    if waf_compatible:
        headers = build_waf_compatible_headers(incoming_headers, urlparse(base_url).netloc, stream=stream)
        headers["authorization"] = f"Bearer {auth_key}"
        if not any(key.lower() == "content-type" for key in headers):
            headers["content-type"] = "application/json"
        # A streaming request must advertise SSE even when the client sent a
        # browser-style Accept header such as application/json.  Keep the
        # explicit passthrough mode available for compatibility/debugging.
        if stream and waf_accept_policy != "passthrough":
            headers["accept"] = "text/event-stream"
        elif waf_accept_policy == "text_event_stream":
            headers["accept"] = "text/event-stream" if stream else "application/json"
        elif waf_accept_policy == "passthrough":
            incoming_accept = next((value for name, value in incoming_headers.items() if name.strip().lower() == "accept"), None)
            if incoming_accept:
                headers["accept"] = incoming_accept
        return headers
    if incoming_headers:
        return build_passthrough_headers(auth_key, incoming_headers, stream=stream)
    return build_upstream_headers(auth_key, stream=stream)
