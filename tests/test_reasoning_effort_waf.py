#!/usr/bin/env python3
import json
import socket
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ArkProxyRouter, ConnectionGroup, create_server


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CaptureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        self.server.captures.append({
            "path": self.path,
            "raw": raw,
            "payload": payload,
            "headers": {key.lower(): value for key, value in self.headers.items()},
        })
        if payload.get("model") in getattr(self.server, "fail_models", set()):
            body = b'{"error":{"message":"forced fallback"}}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        effort = ((payload.get("reasoning") or {}).get("effort") or payload.get("reasoning_effort") or "unset")
        reasoning_tokens = 12 if effort == "low" else 96 if effort == "high" else 0
        body = json.dumps({
            "id": f"resp-{effort}",
            "object": "response",
            "output": [],
            "usage": {
                "input_tokens": 4,
                "output_tokens": 2,
                "total_tokens": 6,
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            },
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def post_responses(port, effort, model="client-model", route_key="lr-reasoning-test", extra_headers=None):
    raw = (
        f'{{ "model" : "{model}", "input" : "ping", '
        f'"reasoning" : {{ "effort" : "{effort}", "summary" : "auto" }}, '
        '"stream" : false }'
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {route_key}",
        "Content-Type": "application/json",
        "User-Agent": "Hermes-Test/1.0",
    }
    headers.update(extra_headers or {})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=raw,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def post_chat_completions(port, effort, model="client-model", route_key="lr-reasoning-test", extra_headers=None):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    }
    if effort is not None:
        payload["reasoning_effort"] = effort
    headers = {
        "Authorization": f"Bearer {route_key}",
        "Content-Type": "application/json",
        "User-Agent": "Hermes-Test/1.0",
    }
    headers.update(extra_headers or {})
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def write_config(path, upstream_port):
    path.write_text(json.dumps({
        "groups": [{
            "id": "g1",
            "name": "reasoning-relay",
            "provider_type": "relay",
            "base_url": f"http://127.0.0.1:{upstream_port}/v1",
            "route_key": "lr-reasoning-test",
            "waf_compatible": False,
            "reasoning_support": "unknown",
        }],
        "models": [{
            "id": "m1",
            "name": "client-model",
            "ep_id": "target-model",
            "upstream_model": "target-model",
            "group_id": "g1",
            "api_key": "sk-test-reasoning",
            "usable": True,
        }],
        "aggregate_models": [],
        "aggregate_members": [],
    }), encoding="utf-8")


def test_reasoning_effort_ab_preserved_with_and_without_waf():
    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), CaptureHandler)
    upstream.captures = []
    upstream.fail_models = set()
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path, upstream_port)
        server, port, _ = create_server("127.0.0.1", free_port(), config_path)
        server.router.logs = []
        server.router.log_file = Path(tmp) / "test-logs.jsonl"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            observed_tokens = {}
            for waf_enabled in (False, True):
                group = server.store.find_group("g1")
                group.waf_compatible = waf_enabled
                server.store.save()
                for effort in ("low", "high"):
                    response = post_responses(port, effort)
                    observed_tokens[(waf_enabled, effort)] = response["usage"]["output_tokens_details"]["reasoning_tokens"]

            assert len(upstream.captures) == 4
            for capture, (waf_enabled, effort) in zip(
                upstream.captures,
                [(False, "low"), (False, "high"), (True, "low"), (True, "high")],
            ):
                assert capture["path"] == "/v1/responses"
                assert capture["payload"]["model"] == "target-model"
                assert capture["payload"]["reasoning"]["effort"] == effort
                assert capture["payload"]["reasoning"]["summary"] == "auto"
                assert "reasoning_effort" not in capture["payload"]
                assert ("mozilla" in capture["headers"].get("user-agent", "").lower()) is waf_enabled

            assert observed_tokens[(False, "low")] == observed_tokens[(True, "low")] == 12
            assert observed_tokens[(False, "high")] == observed_tokens[(True, "high")] == 96
            assert observed_tokens[(False, "low")] != observed_tokens[(False, "high")]

            success_logs = [item for item in server.router.logs if item.event == "ok"]
            assert len(success_logs) == 4
            details = [item.detail for item in success_logs]
            for effort in ("low", "high"):
                assert sum(f"requested_reasoning_effort={effort}" in detail for detail in details) == 2
            assert all("request_api=responses" in detail for detail in details)
            assert all("reasoning_field_source=reasoning.effort" in detail for detail in details)
            assert all("reasoning_preserved=true" in detail for detail in details)
            assert all("upstream_reasoning_support=unknown" in detail for detail in details)
            assert all("body_mode=raw-model-patch" in detail for detail in details)
            assert sum("waf_compatible=true" in detail for detail in details) == 2
            assert sum("waf_compatible=false" in detail for detail in details) == 2
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()


def test_reasoning_field_selection_and_raw_model_patch_bytes():
    group = ConnectionGroup(id="g1", name="relay", provider_type="relay", base_url="https://relay.example")
    responses_raw = b'{ "model" : "client-model", "reasoning" : { "effort" : "high", "summary" : "auto" }, "input" : "ping" }'
    responses_payload = json.loads(responses_raw)
    responses_body, responses_mode = ArkProxyRouter._body_for_upstream(responses_payload, responses_raw, "client-model", "target-model")
    assert responses_mode == "raw-model-patch"
    assert responses_body.replace(b'"target-model"', b'"client-model"', 1) == responses_raw
    responses_fields = ArkProxyRouter._reasoning_log_fields("/v1/responses", responses_payload, responses_body, responses_mode, group)
    assert "requested_reasoning_effort=high" in responses_fields
    assert "reasoning_field_source=reasoning.effort" in responses_fields
    assert "reasoning_preserved=true" in responses_fields

    chat_payload = {"model": "client-model", "messages": [], "reasoning_effort": "low"}
    chat_body, chat_mode = ArkProxyRouter._body_for_upstream(chat_payload, None, "client-model", "target-model")
    chat_fields = ArkProxyRouter._reasoning_log_fields("/v1/chat/completions", chat_payload, chat_body, chat_mode, group)
    assert "requested_reasoning_effort=low" in chat_fields
    assert "reasoning_field_source=reasoning_effort" in chat_fields
    assert "reasoning_value_status=recognized" in chat_fields
    assert "reasoning_preserved=true" in chat_fields
    assert "\"reasoning\"" not in chat_body.decode("utf-8")

    for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
        raw = json.dumps({"model": "client-model", "reasoning": {"effort": effort}}, separators=(",", ":")).encode("utf-8")
        payload = json.loads(raw)
        body, mode = ArkProxyRouter._body_for_upstream(payload, raw, "client-model", "target-model")
        fields = ArkProxyRouter._reasoning_log_fields("/v1/responses", payload, body, mode, group)
        assert f"requested_reasoning_effort={effort}" in fields
        assert "reasoning_value_status=recognized" in fields
        assert "reasoning_preserved=true" in fields

    future_payload = {"model": "client-model", "reasoning": {"effort": "future_level"}}
    future_body, future_mode = ArkProxyRouter._body_for_upstream(future_payload, None, "client-model", "target-model")
    future_fields = ArkProxyRouter._reasoning_log_fields("/v1/responses", future_payload, future_body, future_mode, group)
    assert "requested_reasoning_effort=unrecognized" in future_fields
    assert "future_level" not in future_fields
    assert "reasoning_value_status=unrecognized" in future_fields
    assert "reasoning_effort_sha256=" in future_fields
    assert "reasoning_preserved=true" in future_fields

    absent_payload = {"model": "client-model", "input": "ping"}
    absent_body, absent_mode = ArkProxyRouter._body_for_upstream(absent_payload, None, "client-model", "target-model")
    absent_fields = ArkProxyRouter._reasoning_log_fields("/v1/responses", absent_payload, absent_body, absent_mode, group)
    assert "requested_reasoning_effort=unset" in absent_fields
    assert "reasoning_value_status=absent" in absent_fields
    assert "reasoning_preserved=n/a" in absent_fields

    for effort in ("max", "ultra", "future_level"):
        payload = {"model": "client-model", "messages": [], "reasoning_effort": effort}
        body, mode = ArkProxyRouter._body_for_upstream(payload, None, "client-model", "target-model")
        fields = ArkProxyRouter._reasoning_log_fields("/v1/chat/completions", payload, body, mode, group)
        expected_effort = effort if effort != "future_level" else "unrecognized"
        assert f"requested_reasoning_effort={expected_effort}" in fields
        assert f"reasoning_value_status={'recognized' if effort != 'future_level' else 'unrecognized'}" in fields
        if effort == "future_level":
            assert effort not in fields
            assert "reasoning_effort_sha256=" in fields
        assert "reasoning_preserved=true" in fields


def test_chat_completions_reasoning_effort_is_preserved_through_waf_and_logged():
    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), CaptureHandler)
    upstream.captures = []
    upstream.fail_models = set()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path, upstream_port)
        server, port, _ = create_server("127.0.0.1", free_port(), config_path)
        server.router.logs = []
        server.router.log_file = Path(tmp) / "test-logs.jsonl"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            group = server.store.find_group("g1")
            assert group is not None
            group.waf_compatible = True
            server.store.save()

            for effort in ("max", "ultra", "future_level", None):
                post_chat_completions(port, effort)

            assert [capture["payload"].get("reasoning_effort") for capture in upstream.captures] == [
                "max", "ultra", "future_level", None,
            ]
            assert all("reasoning" not in capture["payload"] for capture in upstream.captures)
            success_logs = [item for item in server.router.logs if item.event == "ok"]
            assert len(success_logs) == 4
            expected_details = [
                ("max", "recognized", "true"),
                ("ultra", "recognized", "true"),
                ("unrecognized", "unrecognized", "true"),
                ("unset", "absent", "n/a"),
            ]
            for item, (effort, status, preserved) in zip(reversed(success_logs), expected_details):
                assert "request_api=chat_completions" in item.detail
                assert f"requested_reasoning_effort={effort}" in item.detail
                field_source = "none" if effort == "unset" else "reasoning_effort"
                assert f"reasoning_field_source={field_source}" in item.detail
                assert f"reasoning_value_status={status}" in item.detail
                assert f"reasoning_preserved={preserved}" in item.detail
            assert all("future_level" not in item.detail for item in success_logs)
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()


def test_aggregate_fallback_preserves_same_reasoning_effort():
    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), CaptureHandler)
    upstream.captures = []
    upstream.fail_models = {"target-one"}
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{
                "id": "g1", "name": "relay", "provider_type": "relay",
                "base_url": f"http://127.0.0.1:{upstream_port}/v1",
                "route_key": "lr-group", "waf_compatible": True,
                "reasoning_support": "supported",
            }],
            "models": [
                {"id": "m1", "name": "one", "ep_id": "target-one", "upstream_model": "target-one", "group_id": "g1", "api_key": "sk-one", "usable": True},
                {"id": "m2", "name": "two", "ep_id": "target-two", "upstream_model": "target-two", "group_id": "g1", "api_key": "sk-two", "usable": True},
            ],
            "aggregate_models": [{"id": "ag1", "name": "agg-reasoning", "route_key": "lr-ag-reasoning", "client_model_aliases": ["gpt-5.5"], "enabled": True, "strategy": "priority"}],
            "aggregate_members": [
                {"id": "am1", "aggregate_id": "ag1", "group_id": "g1", "model_id": "m1", "priority": 1, "enabled": True},
                {"id": "am2", "aggregate_id": "ag1", "group_id": "g1", "model_id": "m2", "priority": 2, "enabled": True},
            ],
        }), encoding="utf-8")
        server, port, _ = create_server("127.0.0.1", free_port(), config_path)
        server.router.logs = []
        server.router.log_file = Path(tmp) / "test-logs.jsonl"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            response = post_responses(port, "ultra", model="gpt-5.5", route_key="lr-ag-reasoning")
            assert response["usage"]["output_tokens_details"]["reasoning_tokens"] == 0
            assert [capture["payload"]["model"] for capture in upstream.captures] == ["target-one", "target-two"]
            assert all(capture["payload"]["reasoning"]["effort"] == "ultra" for capture in upstream.captures)
            request_logs = [item for item in server.router.logs if item.event in {"fallback", "cooldown", "ok"}]
            assert len(request_logs) >= 2
            assert all("requested_reasoning_effort=ultra" in item.detail for item in request_logs[:2])
            assert all("reasoning_value_status=recognized" in item.detail for item in request_logs[:2])
            assert all("reasoning_preserved=true" in item.detail for item in request_logs[:2])
            assert any("requested=gpt-5.5" in item.detail and "resolved_as=aggregate_alias" in item.detail for item in request_logs)
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()


def test_codex_max_and_ultra_are_preserved_through_codex_direct_path():
    upstream_port = free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), CaptureHandler)
    upstream.captures = []
    upstream.fail_models = set()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path, upstream_port)
        server, port, _ = create_server("127.0.0.1", free_port(), config_path)
        server.router.logs = []
        server.router.log_file = Path(tmp) / "test-logs.jsonl"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            group = server.store.find_group("g1")
            assert group is not None
            group.waf_compatible = True
            group.waf_client_mode = "auto_bypass_codex"
            server.store.save()
            codex_headers = {"User-Agent": "Codex/1.0", "X-Codex-Beta-Features": "responses"}

            for effort in ("max", "ultra"):
                post_responses(port, effort, extra_headers=codex_headers)

            assert [capture["payload"]["reasoning"]["effort"] for capture in upstream.captures] == ["max", "ultra"]
            assert all(capture["headers"]["user-agent"] == "Codex/1.0" for capture in upstream.captures)
            request_logs = [item for item in server.router.logs if item.event == "ok"]
            assert len(request_logs) == 2
            for effort, item in zip(("max", "ultra"), reversed(request_logs)):
                assert f"request_api=responses" in item.detail
                assert f"requested_reasoning_effort={effort}" in item.detail
                assert "reasoning_field_source=reasoning.effort" in item.detail
                assert "reasoning_value_status=recognized" in item.detail
                assert "reasoning_preserved=true" in item.detail
                assert "waf_decision=codex_direct" in item.detail
                assert "body_mode=raw-model-patch" in item.detail
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()

def test_waf_smart_mode_keeps_headers_independent_from_serial_protection():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path, free_port())
        server, _, _ = create_server("127.0.0.1", free_port(), config_path)
        try:
            router = server.router
            group = server.store.find_group("g1")
            model = server.store.find_model("m1")
            assert group is not None and model is not None
            group.waf_compatible = True
            group.waf_client_mode = "auto_bypass_codex"
            candidate = router._candidate_from_model(0, model, group)
            codex_headers = {
                "User-Agent": "Codex/1.0",
                "X-Codex-Beta-Features": "responses",
                "Content-Type": "application/json",
            }
            hermes_headers = {"User-Agent": "Hermes/1.0", "Content-Type": "application/json"}

            codex_out = router._headers_for(group, "sk-test", codex_headers, stream=True)
            hermes_out = router._headers_for(group, "sk-test", hermes_headers, stream=True)
            assert router._waf_decision(group, codex_headers) == "codex_direct"
            assert router._candidate_lock_enabled(candidate, codex_headers) is False
            assert codex_out["User-Agent"] == "Codex/1.0"
            assert codex_out["X-Codex-Beta-Features"] == "responses"
            assert router._waf_decision(group, hermes_headers) == "waf_compatible"
            assert router._candidate_lock_enabled(candidate, hermes_headers) is False
            assert hermes_out["user-agent"] != "Hermes/1.0"
            assert "Mozilla/5.0" in hermes_out["user-agent"]

            group.serial_protection = True
            assert router._candidate_lock_enabled(candidate, codex_headers) is True
            assert router._candidate_lock_enabled(candidate, hermes_headers) is True

            body = b'{"model":"target-model","reasoning":{"effort":"high"}}'
            detail = router._debug_detail(candidate, "client-model", "https://relay.example/v1/responses", "raw", body, {"model": "target-model", "reasoning": {"effort": "high"}}, codex_out, "ok")
            assert "waf_client_mode=auto_bypass_codex" in detail
            assert "waf_applied=false" in detail
            assert "waf_decision=codex_direct" in detail
            assert "client_family=codex" in detail
            assert "request_concurrency=serial_protection" in detail
        finally:
            server.server_close()
