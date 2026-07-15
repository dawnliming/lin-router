#!/usr/bin/env python3
import json
import socket
import subprocess
import tempfile
from pathlib import Path

from app import ConnectionGroup, DEFAULT_BASE_URL, create_server


ROOT = Path(__file__).resolve().parent.parent


def run_connection_status(state):
    script = """
const fs = require('fs');
const vm = require('vm');
const state = JSON.parse(process.argv[1]);
const context = { Store: { state }, URL, Date };
vm.runInNewContext(fs.readFileSync('static/js/utils.js', 'utf8') + '\\nthis.ConnectionStatus = ConnectionStatus;', context);
console.log(JSON.stringify(context.ConnectionStatus.derive(state)));
"""
    completed = subprocess.run(
        ["node", "-e", script, json.dumps(state, ensure_ascii=False)],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


def run_draft_group_status(group_state):
    script = """
const fs = require('fs');
const vm = require('vm');
const group = JSON.parse(process.argv[1]);
const context = { Store: { state: {} }, URL, Date };
vm.runInNewContext(fs.readFileSync('static/js/utils.js', 'utf8') + '\\nthis.ConnectionStatus = ConnectionStatus;', context);
console.log(JSON.stringify(context.ConnectionStatus.draftGroup(group)));
"""
    completed = subprocess.run(
        ["node", "-e", script, json.dumps(group_state, ensure_ascii=False)],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


def group(group_id, name):
    return {
        "id": group_id,
        "name": name,
        "provider_type": "relay",
        "base_url": "https://relay.example/v1",
        "route_key": f"lr-{group_id}",
    }


def model(model_id, group_id):
    return {
        "id": model_id,
        "name": model_id,
        "group_id": group_id,
        "upstream_model": model_id,
        "api_key": "sk-test",
        "usable": True,
    }


def test_connection_status_state_machine_contract():
    assert run_connection_status({"groups": [], "models": [], "aggregate_models": [], "logs": []})["code"] == "S0"

    saved_group = group("g1", "主连接组")
    s1 = run_connection_status({"groups": [saved_group], "models": [], "aggregate_models": [], "logs": []})
    assert s1["code"] == "S1"
    assert s1["groups"][0]["code"] == "saved_no_model"

    pending_model = model("m1", "g1")
    s2 = run_connection_status({"groups": [saved_group], "models": [pending_model], "aggregate_models": [], "logs": []})
    assert s2["code"] == "S2"
    assert s2["groups"][0]["code"] == "pending_verify"

    pending_model["last_success_at"] = "2026-07-14 12:00:00"
    # 日志窗口会滚动淘汰，连接组是否已验证只能依赖持久化成功证据。
    s3 = run_connection_status({"groups": [saved_group], "models": [pending_model], "aggregate_models": [], "logs": []})
    assert s3["code"] == "S3"
    assert s3["groups"][0]["code"] == "ready"

    second_group = group("g2", "备用连接组")
    second_model = model("m2", "g2")
    second_model["last_success_at"] = "2026-07-14 12:01:00"
    s4 = run_connection_status({
        "groups": [saved_group, second_group],
        "models": [pending_model, second_model],
        "aggregate_models": [],
        "logs": [],
    })
    assert s4["code"] == "S4"


def test_new_group_draft_status_uses_current_form_values():
    missing_base = run_draft_group_status({
        "name": "新连接组",
        "provider_type": "relay",
        "base_url": "",
    })
    assert missing_base["code"] == "needs_completion"
    assert "Base URL" in missing_base["missingFields"]

    ready_to_save = run_draft_group_status({
        "name": "新连接组",
        "provider_type": "relay",
        "base_url": "https://www.codeok.cc/v1",
    })
    assert ready_to_save["code"] == "draft_ready"
    assert ready_to_save["label"] == "基础字段已填写，待保存"

    missing_proxy_key = run_draft_group_status({
        "name": "代理连接组",
        "provider_type": "proxy",
        "base_url": "https://proxy.example/v1",
        "api_key": "",
    })
    assert missing_proxy_key["code"] == "needs_completion"
    assert "API Key" in missing_proxy_key["missingFields"]


def test_explicit_empty_group_base_url_remains_incomplete():
    created = ConnectionGroup.from_dict({"id": "g-empty", "name": "新连接组", "provider_type": "relay", "base_url": ""})
    legacy = ConnectionGroup.from_dict({"id": "g-legacy", "name": "旧连接组", "provider_type": "relay"})
    assert created.base_url == ""
    assert legacy.base_url == DEFAULT_BASE_URL


def test_new_config_starts_empty_for_onboarding():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "new-user-config.json"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server, _, _ = create_server("127.0.0.1", port, config_path)
        try:
            assert server.store.groups == []
            assert server.store.models == []
            assert server.store.aggregate_models == []
            assert server.router.log_file == config_path.parent / "lin-router-logs.jsonl"
        finally:
            server.server_close()


def test_v055_frontend_onboarding_contracts():
    app_js = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
    utils_js = (ROOT / "static/js/utils.js").read_text(encoding="utf-8")
    dashboard_js = (ROOT / "static/js/dashboard-tab.js").read_text(encoding="utf-8")
    config_js = (ROOT / "static/js/config-tab.js").read_text(encoding="utf-8")
    tree_js = (ROOT / "static/js/tree.js").read_text(encoding="utf-8")
    layout_css = (ROOT / "static/css/layout.css").read_text(encoding="utf-8")
    tree_css = (ROOT / "static/css/tree.css").read_text(encoding="utf-8")
    test_js = (ROOT / "static/js/test-tab.js").read_text(encoding="utf-8")
    settings_js = (ROOT / "static/js/settings-panel.js").read_text(encoding="utf-8")

    assert "const ConnectionStatus" in utils_js
    assert "ConfigTab.startNewGroup()" in app_js
    assert "API.createGroup" not in app_js
    assert "sidebarCollapsed" not in app_js
    assert "lin-router-sidebar-collapsed" not in app_js
    assert "bindResize" not in app_js
    assert "还没有连接组" in dashboard_js
    assert "https://www.codeok.cc/" in dashboard_js
    assert 'target="_blank" rel="noopener noreferrer"' in dashboard_js
    assert "renderDirectAccessCards" in dashboard_js
    assert "onboardingClientText" in dashboard_js
    assert "一键复制接入信息" in dashboard_js
    assert "renderClientTemplates" not in dashboard_js
    assert "model_providers.lin-router" not in dashboard_js
    assert "通用 OpenAI" in dashboard_js
    assert "_openClientTemplates" not in dashboard_js
    assert "不是上游 API Key" in dashboard_js
    assert "renderGroupWorkflow" in config_js
    assert "ConnectionStatus.draftGroup" in config_js
    assert "startNewGroup()" in config_js
    assert "provider_type: 'relay'" in config_js
    assert "https://www.codeok.cc/v1" in config_js
    assert "系统默认地址，可修改" in config_js
    assert "data-system-default-provider" in config_js
    assert "refreshGroupWorkflowFromDraft" in config_js
    assert "validateGroupForm" in config_js
    assert "fetchModelsForGroup" in config_js
    assert "advanced-config" in config_js
    assert "暂无连接组" in tree_js
    assert "tree-import-config" in tree_js
    assert "sidebar-collapse" not in tree_js
    assert "sidebar-collapsed" not in layout_css
    assert "sidebar-collapsed" not in tree_css
    assert "快速测试" in test_js
    assert "请回复：连接成功" in test_js
    assert "'/v1/chat/completions'" in test_js
    assert "v0.6.3" in settings_js
