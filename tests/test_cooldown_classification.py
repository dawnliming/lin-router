#!/usr/bin/env python3
"""
验证请求级错误（400 invalid_request_error、401 等）不会污染连接组/聚合模型状态；
只有 5xx / network / 429 / stream timeout 类错误才会写入 cooldown。

覆盖：
- 连接组 auto 非流式 400 -> 不 cooldown，第二次正常请求成功
- 聚合模型非流式 400 -> 不 cooldown，返回 upstream_request_rejected
- 聚合模型流式 400 -> 不 cooldown
- 聚合模型 500 -> 第一个成员 cooldown，fallback 到第二个成员成功
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
    """前 N 次请求返回 400 invalid_request_error，之后返回 200。"""

    request_count = 0
    max_bad = 1
    err_type = "invalid_request_error"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.end_headers()
            return
        cls = self.__class__
        cls.request_count += 1
        if cls.request_count <= cls.max_bad:
            body = json.dumps(err_body(cls.err_type)).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(ok_body()).encode("utf-8")
        self.send_response(200)
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


class Conditional500ThenOkHandler(BaseHTTPRequestHandler):
    """根据请求体里的 model 字段返回 500 或 200，用于同一组内不同 upstream_model 的差异化响应。"""

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


def build_one_relay_group_two_models(port):
    """组级 auto fallback：两个模型同属一个 relay 组。"""
    group_id = uuid.uuid4().hex
    model1_id = uuid.uuid4().hex
    model2_id = uuid.uuid4().hex
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


def build_two_relay_groups_one_model_each(port1, port2):
    """聚合模型：两个成员分别来自两个 relay 组。"""
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


def test_group_auto_400_no_cooldown():
    BadRequest400Handler.request_count = 0
    BadRequest400Handler.max_bad = 2  # 同一组内两个候选各一次 400
    port = get_free_port()
    server = start_server(BadRequest400Handler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        json.dump(build_one_relay_group_two_models(port), f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = group_route_ctx(store, "lr-group")
        payload = {"model": "lin-router-auto", "messages": [{"role": "user", "content": "hi"}]}

        try:
            router.call("/v1/chat/completions", payload, ctx)
            raise AssertionError("第一次请求应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "upstream_request_rejected", f"期望 upstream_request_rejected，实际 {err}"

        # 两个模型都不应被 cooldown
        for m in store.models:
            assert m.usable is True, f"模型 {m.name} 不应被置为 unusable"
            assert m.cooldown_until == 0, f"模型 {m.name} 不应有 cooldown"

        # 第二次正常请求应成功
        status, _headers, data = router.call("/v1/chat/completions", payload, ctx)
        assert status == 200, f"第二次请求期望 200，实际 {status}"
        resp = json.loads(data)
        assert resp["choices"][0]["message"]["content"] == "ok"
        print("PASS: 连接组 auto 400 不污染状态，第二次请求成功")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_group_auto_500_cooldown_and_fallback():
    """组级 auto：第一个模型 500 cooldown，fallback 到第二个模型成功。"""
    port = get_free_port()
    server = start_server(Conditional500ThenOkHandler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        config_path = f.name
        cfg = build_one_relay_group_two_models(port)
        # relay 模式下上游请求体里的 model 取 ep_id，因此用 ep_id 区分 500/200
        cfg["models"][0]["ep_id"] = "gpt-bad"
        cfg["models"][1]["ep_id"] = "gpt-ok"
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    try:
        router = make_router(config_path)
        store = router.store
        ctx = group_route_ctx(store, "lr-group")
        payload = {"model": "lin-router-auto", "messages": [{"role": "user", "content": "hi"}]}

        status, _headers, data = router.call("/v1/chat/completions", payload, ctx)
        assert status == 200, f"期望 fallback 后 200，实际 {status}"
        resp = json.loads(data)
        assert resp["choices"][0]["message"]["content"] == "ok"

        model1, model2 = store.models[0], store.models[1]
        assert model1.health_state == "observing", "第一个模型应进入观察态"
        assert model1.consecutive_failures == 1
        assert model2.cooldown_until == 0, "第二个模型不应 cooldown"
        print("PASS: 连接组 auto 500 进入 cooldown 并成功 fallback")
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_aggregate_400_no_cooldown():
    BadRequest400Handler.request_count = 0
    BadRequest400Handler.max_bad = 2
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
            raise AssertionError("第一次请求应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "upstream_request_rejected", f"期望 upstream_request_rejected，实际 {err}"
            chain = getattr(err, "fallback_chain", []) or []
            assert len(chain) == 2, f"期望 fallback_chain 长度 2，实际 {len(chain)}"
            for item in chain:
                assert item.get("cooldown_applied") is False, f"请求级错误不应 cooldown，实际 {item}"

        for member in store.aggregate_members:
            assert member.cooldown_until == 0, f"成员 {member.id} 不应 cooldown"

        # 第二次请求应成功（第一个成员现在返回 200）
        status, _headers, data = router.call("/v1/chat/completions", payload, ctx)
        assert status == 200, f"第二次请求期望 200，实际 {status}"
        resp = json.loads(data)
        assert resp["choices"][0]["message"]["content"] == "ok"
        print("PASS: 聚合模型非流式 400 不污染状态，第二次请求成功")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_aggregate_stream_400_no_cooldown():
    BadRequest400Handler.request_count = 0
    BadRequest400Handler.max_bad = 2
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
        payload = {"model": "agg-test", "messages": [{"role": "user", "content": "hi"}], "stream": True}

        try:
            router.stream("/v1/chat/completions", payload, ctx)
            raise AssertionError("第一次流式请求应当失败")
        except Exception as err:
            assert getattr(err, "error_code", "") == "upstream_request_rejected", f"期望 upstream_request_rejected，实际 {err}"

        for member in store.aggregate_members:
            assert member.cooldown_until == 0, f"流式场景成员不应 cooldown"

        # 第二次流式请求应成功
        status, _headers, iterator, _request_id = router.stream("/v1/chat/completions", payload, ctx)
        assert status == 200, f"第二次流式请求期望 200，实际 {status}"
        chunks = list(iterator)
        assert any(b"ok" in c for c in chunks), "第二次流式响应应包含 ok"
        print("PASS: 聚合模型流式 400 不污染状态，第二次请求成功")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_aggregate_500_cooldown_and_fallback():
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
        assert status == 200, f"期望 fallback 到第二个成员后 200，实际 {status}"
        resp = json.loads(data)
        assert resp["choices"][0]["message"]["content"] == "ok"

        member1 = next(m for m in store.aggregate_members if m.id == member1_id)
        member2 = next(m for m in store.aggregate_members if m.id == member2_id)
        assert member1.health_state == "observing", "第一个成员应进入观察态"
        assert member1.consecutive_failures == 1
        assert member2.cooldown_until == 0, "第二个成员不应 cooldown"
        print("PASS: 聚合模型 500 进入 cooldown 并成功 fallback")
    finally:
        server1.shutdown()
        server2.shutdown()
        Path(config_path).unlink(missing_ok=True)


def main():
    test_group_auto_400_no_cooldown()
    test_group_auto_500_cooldown_and_fallback()
    test_aggregate_400_no_cooldown()
    test_aggregate_stream_400_no_cooldown()
    test_aggregate_500_cooldown_and_fallback()
    print("\nAll cooldown classification tests passed.")


if __name__ == "__main__":
    main()
