#!/usr/bin/env python3
"""
验证聚合成员进入 cooldown 后可以通过 clear-cooldown 恢复，
以及底层真实模型不可用时 UI 提示所需的状态判断。
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


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(handler, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def ok_body():
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FailOnce500Handler(BaseHTTPRequestHandler):
    """第一次请求返回 500，之后返回 200。"""

    request_count = 0

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        self.__class__.request_count += 1
        if self.__class__.request_count == 1:
            body = json.dumps({"error": {"type": "server_error", "message": "down"}}).encode("utf-8")
            self.send_response(500)
        else:
            body = json.dumps(ok_body()).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_config(port):
    group_id = uuid.uuid4().hex
    model_id = uuid.uuid4().hex
    aggregate_id = uuid.uuid4().hex
    member_id = uuid.uuid4().hex
    return {
        "groups": [
            {
                "id": group_id,
                "name": "relay-single",
                "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "route_key": "lr-single",
                "auto_model_name": "lin-router-auto",
                "auto_model_cooldown_minutes": 5,
            }
        ],
        "models": [
            {
                "id": model_id,
                "name": "model-single",
                "ep_id": "gpt-test",
                "group_id": group_id,
                "upstream_model": "gpt-test",
                "api_key": "sk-test",
                "usable": True,
            }
        ],
        "aggregate_models": [
            {
                "id": aggregate_id,
                "name": "agg-single",
                "route_key": "lr-ag-single",
                "enabled": True,
                "strategy": "priority",
                "cooldown_minutes": 5,
            }
        ],
        "aggregate_members": [
            {
                "id": member_id,
                "aggregate_id": aggregate_id,
                "group_id": group_id,
                "model_id": model_id,
                "priority": 1,
                "enabled": True,
            }
        ],
    }


def aggregate_route_ctx(store, route_key):
    aggregate = store.find_aggregate_by_route_key(route_key)
    assert aggregate is not None
    return RouteContext(
        client_key=route_key,
        group=None,
        group_id=f"__aggregate__{aggregate.id}",
        provider_type="aggregate",
        base_url="",
        display_name=aggregate.display_name or aggregate.name,
        passthrough=False,
        is_global=False,
        aggregate=aggregate,
    )


def test_clear_cooldown_restores_member():
    FailOnce500Handler.request_count = 0
    port = get_free_port()
    server = start_server(FailOnce500Handler, port)

    member_id = None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_config(port)
        # clear-cooldown 只验证显式固定冷却的恢复动作，不能依赖 smart_breaker 首错冷却。
        cfg["aggregate_models"][0]["routing_policy"] = "fixed_cooldown"
        member_id = cfg["aggregate_members"][0]["id"]
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        store = ConfigStore(config_path)
        router = Router(store, settings_store=None)
        ctx = aggregate_route_ctx(store, "lr-ag-single")
        payload = {"model": "agg-single", "messages": [{"role": "user", "content": "hi"}]}

        # 第一次请求：500 -> 成员进入 cooldown
        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("第一次请求应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "aggregate_members_unavailable", f"期望 aggregate_members_unavailable，实际 {err}"

        member = store.find_aggregate_member(member_id)
        assert member.health_state == "cooling", "成员应立即进入冷却态"
        assert member.consecutive_failures == 0
        assert member.last_error != "", "成员应有 last_error"
        assert member.cooldown_until > int(time.time())
        assert member.cooldown_reason != "", "冷却态应保留冷却原因"

        # 调用清冷却 API
        now_str = router._now()
        assert store.clear_aggregate_member_cooldown(member_id, now_str) is True

        member = store.find_aggregate_member(member_id)
        assert member.enabled is True
        assert member.cooldown_until == 0
        assert member.cooldown_reason == ""
        assert member.last_error == ""
        assert member.last_checked_at == now_str

        # 第二次正常请求应成功
        status, _headers, data = router.call("/v1/chat/completions", payload, ctx)
        assert status == 200, f"第二次请求期望 200，实际 {status}"
        resp = json.loads(data)
        assert resp["choices"][0]["message"]["content"] == "ok"
        print("PASS: clear-cooldown 后聚合成员恢复并可路由")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_underlying_model_unusable():
    port = get_free_port()
    server = start_server(FailOnce500Handler, port)

    member_id = None
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_config(port)
        member_id = cfg["aggregate_members"][0]["id"]
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        store = ConfigStore(config_path)
        router = Router(store, settings_store=None)
        member = store.find_aggregate_member(member_id)
        model = store.find_model(member.model_id)

        # 真实模型被用户禁用时，聚合成员应被视为不可用
        model.usable = False
        model.disabled_by_user = True
        store.save()
        assert router._aggregate_member_usable(member) is False, "底层模型被禁用时成员应不可用"

        # 底层模型自动 cooldown 不跨域阻断聚合成员；聚合成员只看自己的健康状态。
        model.usable = False
        model.disabled_by_user = False
        model.cooldown_until = int(time.time()) + 300
        store.save()
        assert router._aggregate_member_usable(member) is True, "底层模型自动 cooldown 不应阻断聚合成员"

        # 恢复正常后可用
        model.usable = True
        model.cooldown_until = 0
        store.save()
        assert router._aggregate_member_usable(member) is True, "底层模型正常时成员应可用"
        print("PASS: 底层模型不可用状态正确影响聚合成员可用性")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_aggregate_member_skip_reason_is_not_written_to_request_logs():
    port = get_free_port()
    server = start_server(FailOnce500Handler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_config(port)
        member_id = cfg["aggregate_members"][0]["id"]
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        store = ConfigStore(config_path)
        router = Router(
            store,
            settings_store=None,
            log_file=Path(config_path).with_suffix(".logs.jsonl"),
        )
        ctx = aggregate_route_ctx(store, "lr-ag-single")
        payload = {"model": "agg-single", "messages": [{"role": "user", "content": "hi"}]}

        member = store.find_aggregate_member(member_id)
        member.enabled = False
        store.save()
        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("成员停用时应无可用候选")
        except Exception as err:
            assert getattr(err, "error_code", "") == "aggregate_members_unavailable"
        assert not any(log.event == "skip" for log in router.logs)

        member.enabled = True
        model = store.find_model(member.model_id)
        model.usable = False
        model.disabled_by_user = True
        store.save()
        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("底层模型停用时应无可用候选")
        except Exception as err:
            assert getattr(err, "error_code", "") == "aggregate_members_unavailable"
        assert not any(log.event == "skip" for log in router.logs)
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def main():
    test_clear_cooldown_restores_member()
    test_underlying_model_unusable()
    test_aggregate_member_skip_reason_is_not_written_to_request_logs()
    print("\nAll aggregate clear-cooldown tests passed.")


if __name__ == "__main__":
    main()
