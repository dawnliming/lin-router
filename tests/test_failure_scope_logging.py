#!/usr/bin/env python3
"""
验证错误分类后的结构化日志字段 failure_scope=request|candidate|upstream
被正确写入 RequestLog 和聚合 fallback_chain。
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


def err_body(err_type, message="bad"):
    return {"error": {"type": err_type, "message": message}}


class BadRequest400Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.dumps(err_body("invalid_request_error")).encode("utf-8")
        self.send_response(400)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Auth403Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.dumps(err_body("auth_error", "forbidden")).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ServerError500Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.dumps(err_body("server_error", "internal")).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OkHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        body = json.dumps(ok_body()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_two_relay_groups_one_model_each(port1, port2):
    group1_id = uuid.uuid4().hex
    group2_id = uuid.uuid4().hex
    model1_id = uuid.uuid4().hex
    model2_id = uuid.uuid4().hex
    return {
        "groups": [
            {
                "id": group1_id,
                "name": "relay-1",
                "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{port1}/v1",
                "route_key": "lr-test-1",
                "auto_model_name": "lin-router-auto",
                "auto_model_cooldown_minutes": 5,
            },
            {
                "id": group2_id,
                "name": "relay-2",
                "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{port2}/v1",
                "route_key": "lr-test-2",
                "auto_model_name": "lin-router-auto",
                "auto_model_cooldown_minutes": 5,
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
    }


def build_aggregate(config, aggregate_id, member1_id, member2_id):
    group_ids = [g["id"] for g in config["groups"]]
    model_ids = [m["id"] for m in config["models"]]
    config["aggregate_models"] = [
        {
            "id": aggregate_id,
            "name": "agg-test",
            "route_key": "lr-ag-test",
            "enabled": True,
            "strategy": "priority",
            "cooldown_minutes": 5,
        }
    ]
    config["aggregate_members"] = [
        {
            "id": member1_id,
            "aggregate_id": aggregate_id,
            "group_id": group_ids[0],
            "model_id": model_ids[0],
            "priority": 1,
            "enabled": True,
        },
        {
            "id": member2_id,
            "aggregate_id": aggregate_id,
            "group_id": group_ids[1],
            "model_id": model_ids[1],
            "priority": 2,
            "enabled": True,
        },
    ]
    return config


def make_router(config_path):
    store = ConfigStore(config_path)
    return Router(store, settings_store=None)


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


def test_aggregate_400_failure_scope_request():
    port1 = get_free_port()
    port2 = get_free_port()
    server1 = start_server(BadRequest400Handler, port1)
    server2 = start_server(BadRequest400Handler, port2)

    aggregate_id = uuid.uuid4().hex
    member1_id = uuid.uuid4().hex
    member2_id = uuid.uuid4().hex

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_two_relay_groups_one_model_each(port1, port2)
        build_aggregate(cfg, aggregate_id, member1_id, member2_id)
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = aggregate_route_ctx(store, "lr-ag-test")
        payload = {"model": "agg-test", "messages": [{"role": "user", "content": "hi"}]}

        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "upstream_request_rejected"
            chain = getattr(err, "fallback_chain", []) or []
            assert len(chain) == 2
            for item in chain:
                assert item.get("failure_scope") == "request", f"fallback_chain 应记录 failure_scope=request: {item}"

        failure_logs = [log for log in router.logs if log.status == "400" and log.aggregate_id == aggregate_id]
        assert len(failure_logs) == 2, f"期望 2 条聚合 400 日志，实际 {len(failure_logs)}"
        for log in failure_logs:
            assert log.failure_scope == "request", f"日志 failure_scope 应为 request，实际 {log.failure_scope}"
            assert "failure_scope=request" in log.detail, f"日志 detail 应包含结构化 failure_scope: {log.detail}"
        print("PASS: 聚合模型 400 日志 failure_scope=request")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_aggregate_403_failure_scope_candidate():
    port1 = get_free_port()
    port2 = get_free_port()
    server1 = start_server(Auth403Handler, port1)
    server2 = start_server(Auth403Handler, port2)

    aggregate_id = uuid.uuid4().hex
    member1_id = uuid.uuid4().hex
    member2_id = uuid.uuid4().hex

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_two_relay_groups_one_model_each(port1, port2)
        build_aggregate(cfg, aggregate_id, member1_id, member2_id)
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = aggregate_route_ctx(store, "lr-ag-test")
        payload = {"model": "agg-test", "messages": [{"role": "user", "content": "hi"}]}

        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "upstream_request_rejected"
            chain = getattr(err, "fallback_chain", []) or []
            for item in chain:
                assert item.get("failure_scope") == "candidate", f"fallback_chain 应记录 failure_scope=candidate: {item}"

        failure_logs = [log for log in router.logs if log.status == "403" and log.aggregate_id == aggregate_id]
        assert len(failure_logs) == 2
        for log in failure_logs:
            assert log.failure_scope == "candidate", f"日志 failure_scope 应为 candidate，实际 {log.failure_scope}"
            assert "failure_scope=candidate" in log.detail
        print("PASS: 聚合模型 403 日志 failure_scope=candidate")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_aggregate_500_failure_scope_upstream():
    port1 = get_free_port()
    port2 = get_free_port()
    server1 = start_server(ServerError500Handler, port1)
    server2 = start_server(OkHandler, port2)

    aggregate_id = uuid.uuid4().hex
    member1_id = uuid.uuid4().hex
    member2_id = uuid.uuid4().hex

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_two_relay_groups_one_model_each(port1, port2)
        build_aggregate(cfg, aggregate_id, member1_id, member2_id)
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = aggregate_route_ctx(store, "lr-ag-test")
        payload = {"model": "agg-test", "messages": [{"role": "user", "content": "hi"}]}

        status, _headers, data = router.call("/v1/chat/completions", payload, ctx)
        assert status == 200
        resp = json.loads(data)
        assert resp["choices"][0]["message"]["content"] == "ok"

        failure_logs = [log for log in router.logs if log.status == "500" and log.aggregate_id == aggregate_id]
        assert len(failure_logs) == 1
        log = failure_logs[0]
        assert log.failure_scope == "upstream", f"日志 failure_scope 应为 upstream，实际 {log.failure_scope}"
        assert "failure_scope=upstream" in log.detail
        assert log.cooldown_applied is True

        chain = [log for log in router.logs if log.aggregate_id == aggregate_id and log.event == "cooldown"]
        # fallback_chain 不直接存在于日志，但可以通过 add_log 写入的 detail 中没有 fallback_chain 来确认
        # 这里只确认日志字段本身
        print("PASS: 聚合模型 500 日志 failure_scope=upstream")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_group_auto_400_failure_scope_request():
    group_id = uuid.uuid4().hex
    model1_id = uuid.uuid4().hex
    model2_id = uuid.uuid4().hex
    port = get_free_port()
    server = start_server(BadRequest400Handler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = {
            "groups": [
                {
                    "id": group_id,
                    "name": "relay-group",
                    "provider_type": "relay",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "route_key": "lr-group",
                    "auto_model_name": "lin-router-auto",
                    "auto_model_cooldown_minutes": 5,
                }
            ],
            "models": [
                {
                    "id": model1_id,
                    "name": "model-1",
                    "ep_id": "gpt-test",
                    "group_id": group_id,
                    "upstream_model": "gpt-test",
                    "api_key": "sk-test",
                    "usable": True,
                },
                {
                    "id": model2_id,
                    "name": "model-2",
                    "ep_id": "gpt-test",
                    "group_id": group_id,
                    "upstream_model": "gpt-test-2",
                    "api_key": "sk-test",
                    "usable": True,
                },
            ],
        }
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = group_route_ctx(store, "lr-group")
        payload = {"model": "lin-router-auto", "messages": [{"role": "user", "content": "hi"}]}

        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "upstream_request_rejected", f"实际 {err}"

        failure_logs = [log for log in router.logs if log.status == "400" and log.group_id == group_id]
        assert len(failure_logs) == 2, f"期望 2 条 400 日志，实际 {len(failure_logs)}"
        for log in failure_logs:
            assert log.failure_scope == "request", f"组级 auto 400 日志 failure_scope 应为 request，实际 {log.failure_scope}"
        print("PASS: 连接组 auto 400 日志 failure_scope=request")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_group_auto_500_failure_scope_upstream():
    """第一个模型 500，第二个模型 200；验证 500 日志 failure_scope=upstream。"""
    class Conditional500ThenOkHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
                model = payload.get("model", "")
            except Exception:
                model = ""
            if model == "gpt-bad":
                body = json.dumps(err_body("server_error", "internal")).encode("utf-8")
                self.send_response(500)
            else:
                body = json.dumps(ok_body()).encode("utf-8")
                self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    group_id = uuid.uuid4().hex
    model1_id = uuid.uuid4().hex
    model2_id = uuid.uuid4().hex
    port = get_free_port()
    server = start_server(Conditional500ThenOkHandler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = {
            "groups": [
                {
                    "id": group_id,
                    "name": "relay-group",
                    "provider_type": "relay",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "route_key": "lr-group",
                    "auto_model_name": "lin-router-auto",
                    "auto_model_cooldown_minutes": 5,
                }
            ],
            "models": [
                {
                    "id": model1_id,
                    "name": "model-1",
                    "ep_id": "gpt-bad",
                    "group_id": group_id,
                    "upstream_model": "gpt-bad",
                    "api_key": "sk-test",
                    "usable": True,
                },
                {
                    "id": model2_id,
                    "name": "model-2",
                    "ep_id": "gpt-ok",
                    "group_id": group_id,
                    "upstream_model": "gpt-ok",
                    "api_key": "sk-test",
                    "usable": True,
                },
            ],
        }
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = group_route_ctx(store, "lr-group")
        payload = {"model": "lin-router-auto", "messages": [{"role": "user", "content": "hi"}]}

        status, _headers, data = router.call("/v1/chat/completions", payload, ctx)
        assert status == 200

        failure_logs = [log for log in router.logs if log.status == "500" and log.group_id == group_id]
        assert len(failure_logs) == 1
        log = failure_logs[0]
        assert log.failure_scope == "upstream", f"组级 auto 500 日志 failure_scope 应为 upstream，实际 {log.failure_scope}"
        assert log.cooldown_applied is True
        print("PASS: 连接组 auto 500 日志 failure_scope=upstream")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


if __name__ == "__main__":
    test_aggregate_400_failure_scope_request()
    test_aggregate_403_failure_scope_candidate()
    test_aggregate_500_failure_scope_upstream()
    test_group_auto_400_failure_scope_request()
    test_group_auto_500_failure_scope_upstream()
    print("All failure_scope logging tests passed.")
