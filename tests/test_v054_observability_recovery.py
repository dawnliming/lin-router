#!/usr/bin/env python3
import json
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_server
from tests.test_v053_stats_preview_runtime import write_config


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post_json(port, path, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def test_v054_live_diagnose_and_recover_contracts():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path)
        server, port, _ = create_server("127.0.0.1", get_free_port(), config_path)
        server.router.logs = []
        server.router.log_file = Path(tmp) / "test-logs.jsonl"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            router = server.router
            group = server.store.find_group("g1")
            assert group is not None

            router._live_request_start("live-123456", "/v1/chat/completions", "agg-cheap", stream=True)
            router._live_request_update(
                "live-123456",
                stage="waiting_waf_lock",
                stage_label="等待 WAF 锁",
                group="relay",
                candidate="cheap",
                possible_reason="候选正在处理大上下文请求",
            )
            status, live = get_json(port, "/api/live-requests")
            assert status == 200
            assert live["count"] == 1
            assert live["requests"][0]["stage"] == "waiting_waf_lock"
            assert live["requests"][0]["request_id_short"] == "live-123"

            status, runtime = get_json(port, "/api/runtime-state")
            assert status == 200
            assert runtime["live_requests"][0]["stage_label"] == "等待 WAF 锁"

            router._live_request_finish("live-123456")
            status, live_after = get_json(port, "/api/live-requests")
            assert status == 200
            assert live_after["count"] == 0

            router.add_log(
                "/v1/chat/completions",
                "agg-cheap",
                "timeout",
                "reason=stream_idle_timeout; cooldown_applied=true; failure_scope=upstream",
                event="stream_timeout",
                request_id="req-timeout",
                group=group,
                cooldown_applied=True,
                failure_scope="upstream",
            )
            status, diagnosis = get_json(port, "/api/diagnose/req-timeout")
            assert status == 200
            assert diagnosis["diagnosis"]["root_cause"] == "stream_idle_timeout"
            assert diagnosis["diagnosis"]["failure_scope"] == "upstream"
            assert diagnosis["diagnosis"]["cooldown_applied"] is True

            router._manual_probe_candidate = lambda candidate: (True, "probe_ok", "ok")
            model = server.store.find_model("m1")
            assert model is not None
            model.cooldown_until = 9999999999
            model.cooldown_reason = "read_timeout"
            model.usable = False
            server.store.save()
            status, recovered = post_json(port, "/api/models/m1/recover")
            assert status == 200
            assert recovered["ok"] is True
            assert server.store.find_model("m1").cooldown_until == 0
            assert server.store.find_model("m1").usable is True

            model.cooldown_until = 9999999999
            model.cooldown_reason = "read_timeout"
            model.usable = False
            server.store.save()
            router._manual_probe_candidate = lambda candidate: (False, "read_timeout", "upstream still timed out")
            status, failed_probe = post_json(port, "/api/models/m1/recover")
            assert status == 400
            assert failed_probe["code"] == "probe_failed"
            assert server.store.find_model("m1").cooldown_until > 0
            assert server.store.find_model("m1").usable is False

            router._manual_probe_candidate = lambda candidate: (True, "probe_ok", "ok")

            member = server.store.find_aggregate_member("am1")
            assert member is not None
            member.cooldown_until = 9999999999
            member.cooldown_reason = "stream_idle_timeout"
            member.enabled = True
            server.store.save()
            status, member_recovered = post_json(port, "/api/aggregate-members/am1/recover")
            assert status == 200
            assert member_recovered["ok"] is True
            assert server.store.find_aggregate_member("am1").cooldown_until == 0

            member = server.store.find_aggregate_member("am1")
            member.enabled = False
            server.store.save()
            status, blocked = post_json(port, "/api/aggregate-members/am1/recover")
            assert status == 400
            assert blocked["code"] == "manual_disabled"
        finally:
            server.shutdown()
            server.server_close()


def test_v054_frontend_contracts():
    root = Path(__file__).resolve().parent.parent
    api_js = (root / "static/js/api.js").read_text(encoding="utf-8")
    app_js = (root / "static/js/app.js").read_text(encoding="utf-8")
    dashboard_js = (root / "static/js/dashboard-tab.js").read_text(encoding="utf-8")
    logs_js = (root / "static/js/logs-tab.js").read_text(encoding="utf-8")
    config_js = (root / "static/js/config-tab.js").read_text(encoding="utf-8")

    assert "getLiveRequests" in api_js
    assert "diagnoseRequest" in api_js
    assert "recoverModel" in api_js
    assert "recoverAggregateMember" in api_js
    assert "live_requests" in app_js
    assert "lin-router-sidebar-collapsed" in app_js
    assert "实时请求观测" in dashboard_js
    assert "waiting_waf_lock" in dashboard_js
    assert "智能诊断" in logs_js
    assert "请求级错误 / 上游拒绝" in logs_js
    assert "formatDetailPreview(item.detail)" not in logs_js
    assert "Utils.redactSensitive(item.detail || '')" not in logs_js
    assert "failureScopeLabel(d.scope)" in logs_js
    assert "最小探测未通过，候选保持冷却" in logs_js
    assert "probe_failed: '探测失败'" in logs_js
    assert "manual_probe:'人工探测'" in logs_js
    assert "API.recoverModel" in config_js
    assert "API.recoverAggregateMember" in config_js
    assert "reloadAfterAggregateMemberChange" in config_js
    assert "previewAggregateMemberSort" not in config_js
    assert "排序变更预览" not in config_js
