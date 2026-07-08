#!/usr/bin/env python3
"""
验证聚合模型流式请求在首包输出后不会无感 fallback 到其他成员。

场景：
- 聚合模型有两个 relay 成员。
- 第一个上游在发送一个 SSE chunk 后立即关闭连接（模拟首包后失败）。
- 第二个上游如果收到请求，则标记 fallback 发生。
- 期望：客户端只收到第一个 chunk，第二个上游不会被请求。
"""

import json
import socket
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ArkProxyRouter as Router, ConfigStore, RouteContext

fallback_happened = False
first_chunk_content = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'


class FirstUpstreamHandler(BaseHTTPRequestHandler):
    """发送首包后不再发送任何数据，使 lin-router 因流空闲超时而终止当前流。"""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(first_chunk_content)
        self.wfile.flush()
        # 模拟首包后上游无响应；保持连接打开但不继续输出，
        # lin-router 会在 stream_idle_timeout 后中断当前流，且不做无感 fallback。


class SecondUpstreamHandler(BaseHTTPRequestHandler):
    """如果收到请求，说明发生了 fallback。"""

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        global fallback_happened
        fallback_happened = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"from_second"}}]}\n\n')
        self.wfile.write(b'data: [DONE]\n\n')


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(handler, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def build_config(port1, port2):
    group1_id = uuid.uuid4().hex
    group2_id = uuid.uuid4().hex
    model1_id = uuid.uuid4().hex
    model2_id = uuid.uuid4().hex
    aggregate_id = uuid.uuid4().hex
    member1_id = uuid.uuid4().hex
    member2_id = uuid.uuid4().hex
    return {
        "groups": [
            {
                "id": group1_id,
                "name": "relay-1",
                "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{port1}/v1",
                "route_key": "lr-test-1",
                "auto_model_name": "lin-router-auto",
                "stream_idle_timeout": 2,
            },
            {
                "id": group2_id,
                "name": "relay-2",
                "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{port2}/v1",
                "route_key": "lr-test-2",
                "auto_model_name": "lin-router-auto",
                "stream_idle_timeout": 2,
            },
        ],
        "models": [
            {
                "id": model1_id,
                "name": "model-1",
                "ep_id": "gpt-test",
                "group_id": group1_id,
                "upstream_model": "gpt-test",
                "api_key": "sk-test-1",
                "usable": True,
            },
            {
                "id": model2_id,
                "name": "model-2",
                "ep_id": "gpt-test",
                "group_id": group2_id,
                "upstream_model": "gpt-test",
                "api_key": "sk-test-2",
                "usable": True,
            },
        ],
        "aggregate_models": [
            {
                "id": aggregate_id,
                "name": "agg-test",
                "route_key": "lr-ag-test",
                "enabled": True,
                "strategy": "priority",
                "cooldown_minutes": 5,
            }
        ],
        "aggregate_members": [
            {
                "id": member1_id,
                "aggregate_id": aggregate_id,
                "group_id": group1_id,
                "model_id": model1_id,
                "priority": 1,
                "enabled": True,
            },
            {
                "id": member2_id,
                "aggregate_id": aggregate_id,
                "group_id": group2_id,
                "model_id": model2_id,
                "priority": 2,
                "enabled": True,
            },
        ],
    }


def main():
    port1 = get_free_port()
    port2 = get_free_port()
    server1 = start_server(FirstUpstreamHandler, port1)
    server2 = start_server(SecondUpstreamHandler, port2)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        json.dump(build_config(port1, port2), f, ensure_ascii=False, indent=2)

    try:
        store = ConfigStore(config_path)
        router = Router(store, settings_store=None)
        aggregate = store.find_aggregate_by_route_key("lr-ag-test")
        ctx = RouteContext(
            client_key="lr-ag-test",
            group=None,
            group_id=f"__aggregate__{aggregate.id}",
            provider_type="aggregate",
            base_url="",
            display_name=aggregate.display_name or aggregate.name,
            passthrough=False,
            is_global=False,
            aggregate=aggregate,
        )

        payload = {
            "model": "agg-test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        status, headers, iterator, request_id = router.stream("/v1/chat/completions", payload, ctx)
        assert status == 200, f"期望 200，实际 {status}"

        chunks = list(iterator)
        # 第一个 chunk 被成功发送；后续上游断开，不应 fallback 到第二个成员
        assert len(chunks) >= 1, "至少应收到首包"
        assert first_chunk_content in b"".join(chunks), "首包内容应包含 hello"
        assert not fallback_happened, "首包后不应 fallback 到第二个上游"
        print("PASS: 首包后未发生无感 fallback")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
