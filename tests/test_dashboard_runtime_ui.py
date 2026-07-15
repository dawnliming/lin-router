"""首页运行态差分渲染与可扩展接入区的静态/渲染契约。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_dashboard(state: dict, access_filter: str = "") -> dict:
    """在无浏览器依赖的 Node VM 中执行首页纯渲染逻辑。"""
    script = r"""
const fs = require('fs');
const vm = require('vm');
const state = JSON.parse(process.argv[1]);
const accessFilter = process.argv[2] || '';
const context = {
  Store: { state },
  URL,
  Date,
  window: { location: { origin: 'http://127.0.0.1:8748' } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/utils.js', 'utf8') + '\nthis.ConnectionStatus = ConnectionStatus; this.Utils = Utils;', context);
vm.runInContext(fs.readFileSync('static/js/dashboard-tab.js', 'utf8') + '\nthis.DashboardTab = DashboardTab;', context);
context.DashboardTab._accessFilter = accessFilter;
const structureBefore = context.DashboardTab.structureSignature(state);
const runtimeBefore = context.DashboardTab.runtimeSignature(state);
const changedRuntime = JSON.parse(JSON.stringify(state));
changedRuntime.live_requests = [{ request_id: 'runtime-only', requested_model: 'runtime-model' }];
changedRuntime.logs = [{ request_id: 'runtime-only', status: '200', event: 'ok', detail: '' }];
const changedHealth = JSON.parse(JSON.stringify(state));
if (changedHealth.models && changedHealth.models[0]) {
  changedHealth.models[0].usable = false;
  changedHealth.models[0].cooldown_until = 9999999999;
  changedHealth.models[0].last_success_at = '';
}
const flow = context.ConnectionStatus.derive(state);
console.log(JSON.stringify({
  html: context.DashboardTab.render(),
  metricsHtml: context.DashboardTab.renderMetrics(state, flow, state.logs || []),
  structureBefore,
  structureAfterRuntimeOnly: context.DashboardTab.structureSignature(changedRuntime),
  structureAfterHealthOnly: context.DashboardTab.structureSignature(changedHealth),
  runtimeBefore,
  runtimeAfter: context.DashboardTab.runtimeSignature(changedRuntime),
  runtimeAfterHealthOnly: context.DashboardTab.runtimeSignature(changedHealth),
}));
"""
    completed = subprocess.run(
        ["node", "-e", script, json.dumps(state, ensure_ascii=False), access_filter],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


def run_onboarding(state: dict, selection: dict | None = None) -> dict:
    """执行接入引导纯渲染，验证选择不会混用不同连接组的数据。"""
    script = r"""
const fs = require('fs');
const vm = require('vm');
const state = JSON.parse(process.argv[1]);
const initialSelection = JSON.parse(process.argv[2]);
const context = {
  Store: { state },
  URL,
  Date,
  window: { location: { origin: 'http://127.0.0.1:18400' } },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/utils.js', 'utf8') + '\nthis.ConnectionStatus = ConnectionStatus; this.Utils = Utils;', context);
vm.runInContext(fs.readFileSync('static/js/dashboard-tab.js', 'utf8') + '\nthis.DashboardTab = DashboardTab;', context);
if (initialSelection) context.DashboardTab._onboardingSelection = initialSelection;
const flow = context.ConnectionStatus.derive(state);
const selected = context.DashboardTab.selectedOnboarding(flow);
console.log(JSON.stringify({
  flowCode: flow.code,
  relayGroupIds: context.DashboardTab.onboardingRelayGroups(flow).map(item => item.group.id),
  onboardingModelIds: selected ? selected.models.map(item => item.id) : [],
  selected: selected ? {
    groupId: selected.group.id,
    modelId: selected.model?.id || '',
    client: selected.client,
  } : null,
  accessText: selected ? context.DashboardTab.onboardingClientText(selected, 'http://127.0.0.1:18400/v1') : '',
  html: context.DashboardTab.renderSelfServiceOnboarding(flow, 'http://127.0.0.1:18400/v1'),
}));
"""
    completed = subprocess.run(
        ["node", "-e", script, json.dumps(state, ensure_ascii=False), json.dumps(selection, ensure_ascii=False)],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


def group(group_id: str, name: str, *, provider_type: str = "relay") -> dict:
    return {
        "id": group_id,
        "name": name,
        "provider_type": provider_type,
        "base_url": "https://relay.example/v1",
        "route_key": f"lr-{group_id}",
    }


def model(
    model_id: str,
    group_id: str,
    *,
    verified: bool = False,
    usable: bool = True,
    cooldown_until: int = 0,
) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "group_id": group_id,
        "upstream_model": model_id,
        "api_key": "sk-test",
        "usable": usable,
        "cooldown_until": cooldown_until,
        "last_success_at": "2026-07-14 12:00:00" if verified else "",
    }


def primary_button_count(html: str) -> int:
    return html.count('class="btn-primary"')


def test_runtime_fields_do_not_change_dashboard_structure_signature():
    state = {
        "groups": [group("g1", "主连接组")],
        "models": [model("m1", "g1", verified=True)],
        "aggregate_models": [],
        "aggregate_members": [],
        "logs": [],
        "live_requests": [],
    }

    rendered = run_dashboard(state)

    assert rendered["structureBefore"] == rendered["structureAfterRuntimeOnly"]
    assert rendered["structureBefore"] == rendered["structureAfterHealthOnly"]
    assert rendered["runtimeBefore"] != rendered["runtimeAfter"]
    assert rendered["runtimeBefore"] != rendered["runtimeAfterHealthOnly"]


def test_s2_keeps_next_step_expanded_and_always_renders_live_requests():
    state = {
        "groups": [group("g1", "待验证连接组")],
        "models": [model("m1", "g1")],
        "aggregate_models": [],
        "aggregate_members": [],
        "logs": [],
        "live_requests": [{
            "request_id": "req-live",
            "request_id_short": "req-live",
            "requested_model": "m1",
            "group": "待验证连接组",
            "stage": "waiting_first_byte",
            "elapsed_ms": 1000,
        }],
    }

    rendered = run_dashboard(state)
    html = rendered["html"]

    assert 'data-dashboard-live-requests' in html
    assert 'data-dashboard-live-request="req-live"' in html
    assert 'data-dashboard-flow-summary="S2"' not in html
    assert primary_button_count(html) == 1


def test_s0_s1_and_e1_keep_next_step_expanded_with_one_primary_action():
    cases = [
        {
            "groups": [],
            "models": [],
            "aggregate_models": [],
            "aggregate_members": [],
            "logs": [],
            "live_requests": [],
        },
        {
            "groups": [group("g1", "待添加模型")],
            "models": [],
            "aggregate_models": [],
            "aggregate_members": [],
            "logs": [],
            "live_requests": [],
        },
        {
            "groups": [group("g1", "需处理连接组")],
            "models": [{**model("m1", "g1"), "usable": False}],
            "aggregate_models": [],
            "aggregate_members": [],
            "logs": [],
            "live_requests": [],
        },
    ]

    for state in cases:
        html = run_dashboard(state)["html"]
        assert 'data-dashboard-flow-summary=' not in html
        assert primary_button_count(html) == 1
        assert 'data-dashboard-live-requests' in html


def test_s3_shows_all_aggregates_and_keeps_single_primary_action():
    aggregates = [
        {
            "id": f"a{index}",
            "name": f"aggregate-{index}",
            "display_name": f"聚合 {index}",
            "route_key": f"lr-ag-{index}",
            "enabled": index != 5,
        }
        for index in range(1, 6)
    ]
    state = {
        "groups": [group("g1", "主连接组")],
        "models": [model("m1", "g1", verified=True)],
        "aggregate_models": aggregates,
        "aggregate_members": [],
        "logs": [],
        "live_requests": [],
    }

    rendered = run_dashboard(state)
    html = rendered["html"]

    assert 'dashboard-hero-operational' in html
    assert 'data-dashboard-flow-summary="S3"' not in html
    assert html.count('data-dashboard-flow-card') == 1
    assert '可用连接组' in html
    assert '已验证模型' in html
    assert '已验证模型' not in rendered["metricsHtml"]
    assert 'data-dashboard-access-filter' in html
    assert '中转站' in html
    assert '可用模型 1 / 1' in html
    assert 'data-dashboard-access-group="g1" open' in html
    assert html.count('class="dashboard-card dashboard-aggregate-card') == 5
    assert '聚合 5' in html
    assert '已停用' in html
    assert 'dashboard-aggregate-access-card is-disabled' not in html
    assert primary_button_count(html) == 1


def test_s4_filters_collapsible_direct_access_groups_without_hiding_live_region():
    state = {
        "groups": [group("g1", "主连接组"), group("g2", "备用连接组")],
        "models": [model("m1", "g1", verified=True), model("m2", "g2", verified=True)],
        "aggregate_models": [],
        "aggregate_members": [],
        "logs": [],
        "live_requests": [],
    }

    html = run_dashboard(state, "备用")["html"]

    assert 'dashboard-hero-operational' in html
    assert 'data-dashboard-flow-summary="S4"' not in html
    assert html.count('data-dashboard-flow-card') == 1
    assert html.count('data-dashboard-access-group=') == 1
    # 引导的连接组下拉框会保留全部可接入 relay；这里仅校验原有直连接入区仍被筛选。
    assert 'data-dashboard-access-group="g2"' in html
    assert 'data-dashboard-access-group="g1"' not in html
    assert 'data-dashboard-access-group="g2" open' not in html
    assert 'data-dashboard-live-requests' in html
    assert primary_button_count(html) == 1


def test_v063_onboarding_is_not_rendered_before_a_ready_relay_exists():
    states = [
        {
            "groups": [], "models": [], "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
        },
        {
            "groups": [group("g1", "待添加模型")], "models": [], "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
        },
        {
            "groups": [group("g1", "待验证连接组")], "models": [model("m1", "g1")], "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
        },
        {
            "groups": [group("g1", "异常连接组")], "models": [model("m1", "g1", usable=False)], "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
        },
    ]

    for state in states:
        html = run_dashboard(state)["html"]
        assert "data-onboarding-group" not in html
        assert "data-onboarding-model" not in html
        assert 'data-onboarding-client="codex"' not in html

    proxy_only = {
        "groups": [{**group("proxy", "通用代理", provider_type="proxy"), "api_key": "sk-proxy"}],
        "models": [model("proxy-model", "proxy", verified=True)],
        "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
    }
    proxy_onboarding = run_onboarding(proxy_only)
    assert proxy_onboarding["flowCode"] == "S3"
    assert proxy_onboarding["selected"] is None
    html = run_dashboard(proxy_only)["html"]
    assert "data-onboarding-group" not in html
    assert "data-onboarding-model" not in html


def test_v063_onboarding_only_uses_ready_relay_and_verified_models():
    state = {
        "groups": [
            group("relay", "可接入中转站"),
            {**group("proxy", "可接入代理", provider_type="proxy"), "api_key": "sk-proxy"},
        ],
        "models": [
            model("relay-good", "relay", verified=True),
            model("relay-pending", "relay"),
            model("relay-disabled", "relay", verified=True, usable=False),
            model("relay-cooling", "relay", verified=True, cooldown_until=9_999_999_999),
            model("proxy-good", "proxy", verified=True),
        ],
        "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
    }

    rendered = run_onboarding(state)

    assert rendered["flowCode"] == "S4"
    assert rendered["relayGroupIds"] == ["relay"]
    assert rendered["onboardingModelIds"] == ["relay-good"]
    assert rendered["selected"] == {"groupId": "relay", "modelId": "relay-good", "client": "codex"}
    assert 'value="relay" selected' in rendered["html"]
    assert "data-onboarding-collapse" in rendered["html"]
    assert 'data-onboarding-output="base-url"' in rendered["html"]
    assert 'data-onboarding-output="route-key"' in rendered["html"]
    assert 'data-onboarding-output="model"' in rendered["html"]
    assert "不是上游 API Key" in rendered["html"]
    assert "2026-07-14 12:00:00" in rendered["html"]
    assert "https://relay.example/v1" not in rendered["html"]
    assert "lr-relay" in rendered["html"]
    assert "lr-proxy" not in rendered["html"]
    assert "relay-pending" not in rendered["html"]
    assert "relay-disabled" not in rendered["html"]
    assert "relay-cooling" not in rendered["html"]


def test_v063_onboarding_quick_copy_follows_the_selected_group_and_model():
    state = {
        "groups": [group("g1", "主连接组"), group("g2", "备用连接组")],
        "models": [
            {**model("m1", "g1", verified=True), "name": "model-aurora"},
            {**model("m2", "g2", verified=True), "name": "model-borealis"},
        ],
        "aggregate_models": [], "aggregate_members": [], "logs": [], "live_requests": [],
    }

    default_render = run_onboarding(state)
    assert default_render["selected"] == {"groupId": "g1", "modelId": "m1", "client": "codex"}
    assert "lr-g1" in default_render["html"]
    assert "model-aurora" in default_render["html"]
    assert default_render["accessText"] == "客户端: Codex\nBase URL: http://127.0.0.1:18400/v1\nroute key: lr-g1\nModel: model-aurora"
    assert "一键复制接入信息" in default_render["html"]
    assert "复制下方接入信息，在目标客户端中按其已有方式填写即可。" in default_render["html"]
    assert "lr-g2" not in default_render["html"]
    assert "model-borealis" not in default_render["html"]

    hermes_render = run_onboarding(state, {"groupId": "g2", "modelId": "m2", "client": "hermes"})
    assert hermes_render["selected"] == {"groupId": "g2", "modelId": "m2", "client": "hermes"}
    assert "lr-g2" in hermes_render["html"]
    assert "model-borealis" in hermes_render["html"]
    assert hermes_render["accessText"] == "客户端: Hermes\nBase URL: http://127.0.0.1:18400/v1\nroute key: lr-g2\nModel: model-borealis"
    assert 'data-onboarding-client="hermes" aria-pressed="true"' in hermes_render["html"]
    assert "lr-g1" not in hermes_render["html"]
    assert "model-aurora" not in hermes_render["html"]


def test_v063_onboarding_stays_information_only_and_readme_matches_contract():
    dashboard_js = (ROOT / "static/js/dashboard-tab.js").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_onboarding = readme[readme.index("## 自助接入 Codex / Hermes"):readme.index("## 预览 / 调试")]
    onboarding_start = dashboard_js.index("  onboardingRelayGroups(")
    onboarding_end = dashboard_js.index("  formatElapsed(", onboarding_start)
    onboarding_source = dashboard_js[onboarding_start:onboarding_end]

    for forbidden in (
        "client_kind",
        "clientKind",
        "data-onboarding-confirm",
        "data-onboarding-detect",
        "data-onboarding-listen",
    ):
        assert forbidden not in dashboard_js
    assert "live_requests" not in onboarding_source
    assert "logs" not in onboarding_source
    assert "API." not in onboarding_source
    assert "接入信息已准备好，请在客户端中使用" in dashboard_js
    assert "一键复制接入信息" in dashboard_js
    assert "复制下方接入信息，在目标客户端中按其已有方式填写即可。" in dashboard_js
    for forbidden in ("$env:", "config.toml", "model_providers", "hermes model", "Custom endpoint", "chat_completions"):
        assert forbidden not in onboarding_source
        assert forbidden not in readme_onboarding
    assert "Lin Router 是本地 OpenAI 兼容中转站" in readme
    assert "上游 API Key" in readme
    assert "route key 是客户端到本机 Lin Router 的认证信息" in readme
    assert "一键复制接入信息" in readme
    assert "推理强度支持" not in readme


def test_dashboard_runtime_source_uses_stable_slots_and_delegated_events():
    dashboard_js = (ROOT / "static/js/dashboard-tab.js").read_text(encoding="utf-8")
    dashboard_css = (ROOT / "static/css/dashboard-tab.css").read_text(encoding="utf-8")

    assert "patchRuntime(panel)" in dashboard_js
    assert "data-dashboard-live-requests" in dashboard_js
    assert "data-dashboard-metrics" in dashboard_js
    assert "data-dashboard-access-filter" in dashboard_js
    assert "panel.addEventListener('click'" in dashboard_js
    assert ".slice(0, 4)" not in dashboard_js
    assert "dashboard-access-group" in dashboard_css
    assert "dashboard-flow-details" in dashboard_css
    assert "dashboard-aggregate-card" in dashboard_css
