#!/usr/bin/env python3
"""
验证 403 WAF 拦截类错误的中文提示、failure_scope 分类以及不写入 cooldown。
"""

import json
import socket
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ArkProxyRouter as Router, ConfigStore, RouteContext


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(handler, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class WafBlocked403Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.dumps({"error": {"type": "auth_error", "message": "Your request was blocked."}}).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_config(port, waf_compatible=False):
    group_id = uuid.uuid4().hex
    model_id = uuid.uuid4().hex
    return {
        "groups": [
            {
                "id": group_id,
                "name": "relay-group",
                "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "route_key": "lr-group",
                "auto_model_name": "lin-router-auto",
                "auto_model_cooldown_minutes": 5,
                "waf_compatible": waf_compatible,
            }
        ],
        "models": [
            {
                "id": model_id,
                "name": "model-1",
                "ep_id": "gpt-test",
                "group_id": group_id,
                "upstream_model": "gpt-test",
                "api_key": "sk-test",
                "usable": True,
            },
        ],
    }


def make_router(config_path):
    store = ConfigStore(config_path)
    return Router(store, settings_store=None)


def group_route_ctx(store, route_key):
    group = store.find_group_by_route_key(route_key)
    assert group is not None
    return RouteContext(
        client_key=route_key,
        group=group,
        group_id=group.id,
        provider_type=group.provider_type,
        base_url=group.base_url,
        display_name=group.name,
        passthrough=False,
        is_global=False,
    )


def test_waf_blocked_waf_off_suggests_enable():
    port = get_free_port()
    server = start_server(WafBlocked403Handler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        json.dump(build_config(port, waf_compatible=False), f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = group_route_ctx(store, "lr-group")
        payload = {"model": "lin-router-auto", "messages": [{"role": "user", "content": "hi"}]}

        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("应当失败")
        except Exception as err:
            msg = str(err)
            assert "Your request was blocked" in msg, f"错误消息应包含原始提示：{msg}"
            assert "未开启 WAF 兼容" in msg, f"错误消息应建议开启 WAF：{msg}"
            assert getattr(err, "error_code", "") == "upstream_request_rejected"

        # 模型不应被 cooldown
        model = store.models[0]
        assert model.usable is True, "WAF 拦截不应写入 cooldown"
        assert model.cooldown_until == 0, "WAF 拦截不应写入 cooldown"

        logs = [log for log in router.logs if log.status == "403" and log.group_id == store.groups[0].id]
        assert len(logs) == 1, f"期望 1 条 403 日志，实际 {len(logs)}"
        log = logs[0]
        assert log.failure_scope == "candidate", f"failure_scope 应为 candidate，实际 {log.failure_scope}"
        assert log.cooldown_applied is False, "cooldown_applied 应为 false"
        assert "waf_blocked=true" in log.detail, f"日志应标记 waf_blocked=true：{log.detail}"
        assert "上游中转站拦截了请求" in log.detail, f"日志应包含中文原因：{log.detail}"
        assert "开启「仅中转站 WAF 兼容」" in log.detail, f"未开启 WAF 时应建议开启 WAF：{log.detail}"
        print("PASS: WAF 未开启时提示开启 WAF 兼容")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_waf_blocked_waf_on_suggests_check_upstream():
    port = get_free_port()
    server = start_server(WafBlocked403Handler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        json.dump(build_config(port, waf_compatible=True), f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = group_route_ctx(store, "lr-group")
        payload = {"model": "lin-router-auto", "messages": [{"role": "user", "content": "hi"}]}

        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("应当失败")
        except Exception as err:
            msg = str(err)
            assert "已开启 WAF" in msg, f"错误消息应提示已开启 WAF：{msg}"
            assert "检查中转站后台" in msg, f"错误消息应建议检查后台：{msg}"
            assert "未开启 WAF 兼容" not in msg, f"已开启 WAF 时不应再建议开启 WAF：{msg}"

        logs = [log for log in router.logs if log.status == "403" and log.group_id == store.groups[0].id]
        assert len(logs) == 1
        log = logs[0]
        assert "已开启 WAF，仍被拦截" in log.detail, f"日志应提示已开启 WAF：{log.detail}"
        assert "检查中转站后台" in log.detail, f"日志应建议检查后台：{log.detail}"
        print("PASS: WAF 已开启时提示检查中转站后台")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


if __name__ == "__main__":
    test_waf_blocked_waf_off_suggests_enable()
    test_waf_blocked_waf_on_suggests_check_upstream()
    print("All WAF blocked hint tests passed.")
