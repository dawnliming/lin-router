from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app import ArkProxyRouter, ConfigStore, RouteContext


SESSION_SENTINEL = "session-secret-must-not-persist"


class _SessionPrivacyUpstream(BaseHTTPRequestHandler):
    received_payloads: list[dict[str, Any]] = []

    @classmethod
    def reset(cls) -> None:
        cls.received_payloads = []

    def do_POST(self) -> None:
        payload = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
        )
        type(self).received_payloads.append(payload)
        if payload.get("stream"):
            response = b'data: {"type":"response.completed"}\n\n'
            content_type = "text/event-stream; charset=utf-8"
        else:
            response = b'{"id":"non-stream-ok"}'
            content_type = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_upstream() -> ThreadingHTTPServer:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = ThreadingHTTPServer(("127.0.0.1", port), _SessionPrivacyUpstream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _router(tmp_path: Path, port: int) -> tuple[ArkProxyRouter, RouteContext]:
    config_path = tmp_path / "config.json"
    group_id = "group-session-privacy"
    config_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": group_id,
                        "name": "session-privacy-relay",
                        "provider_type": "relay",
                        "base_url": f"http://127.0.0.1:{port}/v1",
                        "route_key": "session-privacy-route",
                        "waf_compatible": True,
                    }
                ],
                "models": [
                    {
                        "id": "model-session-privacy",
                        "name": "session-privacy-model",
                        "ep_id": "session-privacy-model",
                        "upstream_model": "session-privacy-model",
                        "group_id": group_id,
                        "api_key": "test-key",
                        "usable": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = ConfigStore(config_path)
    router = ArkProxyRouter(store, log_file=tmp_path / "logs.jsonl")
    group = store.groups[0]
    context = RouteContext(
        client_key=group.route_key,
        group=group,
        group_id=group.id,
        provider_type=group.provider_type,
        base_url=group.base_url,
        display_name=group.name,
        passthrough=False,
    )
    return router, context


def _assert_diagnostics_are_session_safe(
    tmp_path: Path,
    *,
    stream: bool,
) -> None:
    _SessionPrivacyUpstream.reset()
    server = _start_upstream()
    captures: list[dict[str, Any]] = []
    try:
        router, context = _router(tmp_path, server.server_address[1])
        router.runtime.debug_capture._capture = lambda **kwargs: captures.append(kwargs)
        payload = {
            "model": "session-privacy-model",
            "messages": [{"role": "user", "content": "privacy regression"}],
            "session_id": SESSION_SENTINEL,
            "stream": stream,
        }

        if stream:
            status, _headers, iterator, _request_id = router.stream(
                "/v1/chat/completions",
                payload,
                context,
                raw_body=json.dumps(payload).encode("utf-8"),
            )
            assert status == 200
            assert b"response.completed" in b"".join(iterator)
        else:
            status, _headers, _body = router.call(
                "/v1/chat/completions",
                payload,
                context,
                raw_body=json.dumps(payload).encode("utf-8"),
            )
            assert status == 200

        # payload.session_id 仍是兼容上游输入，不能为了脱敏破坏真实请求体。
        assert _SessionPrivacyUpstream.received_payloads == [payload]
        assert len(captures) == 1
        capture = captures[0]
        assert SESSION_SENTINEL not in capture["body"].decode("utf-8")
        assert SESSION_SENTINEL not in capture["fingerprint"]
        assert "session_id" not in json.loads(capture["body"])
        assert all(SESSION_SENTINEL not in item.detail for item in router.logs)
        # 可观测性只保留匿名枚举，不能泄露会话原文或摘要。
        assert any("routing_policy=smart_breaker" in item.detail for item in router.logs)
        assert any("sticky_status=available" in item.detail for item in router.logs)
        assert all("session_digest" not in item.detail for item in router.logs)
    finally:
        server.shutdown()
        server.server_close()


def test_non_stream_session_id_reaches_upstream_but_not_diagnostics(tmp_path: Path) -> None:
    _assert_diagnostics_are_session_safe(tmp_path, stream=False)


def test_stream_session_id_reaches_upstream_but_not_diagnostics(tmp_path: Path) -> None:
    _assert_diagnostics_are_session_safe(tmp_path, stream=True)
