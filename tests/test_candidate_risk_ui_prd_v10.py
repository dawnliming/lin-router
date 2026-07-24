"""冻结 PRD v1.0：风控隔离的诊断与管理台安全交互契约。"""
from __future__ import annotations

import subprocess
from pathlib import Path

from linrouter_core.observability.contracts import RequestLog
from linrouter_core.observability.diagnostics import diagnose_logs


ROOT = Path(__file__).resolve().parent.parent


def _run_node(script: str) -> str:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return completed.stdout.strip()


def test_risk_diagnosis_uses_safe_actionable_summary() -> None:
    log = RequestLog(
        time="2026-07-19T00:00:00Z",
        path="/v1/chat/completions",
        model="model-a",
        status="403",
        event="fallback",
        request_id="risk-request-id",
        failure_scope="candidate",
        detail=(
            "failure_category=waf_blocked; waf_blocked=true; risk_isolated=true; "
            "risk_cooldown_seconds=900; risk_level=1"
        ),
    )

    diagnosis = diagnose_logs([log], lambda detail: detail)

    assert diagnosis["root_cause"] == "risk_isolated"
    assert diagnosis["failure_scope"] == "candidate"
    assert diagnosis["cooldown_applied"] is False
    assert diagnosis["actions"] == [{"type": "risk_recover", "label": "查看风控恢复指引"}]
    assert "不要连续重试" in diagnosis["suggestion"]
    assert "host" not in diagnosis["technical_summary"]
    assert "credential_digest" not in diagnosis["technical_summary"]


def test_risk_dashboard_group_model_and_diagnosis_actions_are_wired_without_scope_leakage() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const dashboardSource = fs.readFileSync('static/js/dashboard-tab.js', 'utf8') + '\nthis.dashboard = DashboardTab;';
const configSource = fs.readFileSync('static/js/config-tab.js', 'utf8') + '\nthis.config = ConfigTab;';
const actionsSource = fs.readFileSync('static/js/config-tab-actions.js', 'utf8') + '\nthis.actions = ConfigTabActions;';
const models = [{
  id: 'm-risk', group_id: 'g-risk', name: 'risk-model', risk_isolated: true,
  risk_until: 1900000000, risk_level: 2, risk_affected_models: 3,
  api_key: 'SAFE_TEST_CREDENTIAL_MUST_NOT_RENDER',
}];
const selected = { type: null, id: '' };
const Store = {
  state: { models },
  selected,
  getModel(id) { return this.state.models.find(item => item.id === id) || null; },
  select(type, id) { this.selected.type = type; this.selected.id = id; },
};
const Utils = {
  escapeHtml(value) { return String(value); },
  formatDate(value) { return `date:${value}`; },
};
const document = {
  getElementById(id) {
    if (id === 'group-id') return { value: 'g-risk' };
    return null;
  },
};
const LogsTab = {
  filters: { start: '', end: '', group: '', status: '' },
  currentOnly: false,
  _currentOnlySelectionKey: 'old',
  page: 9,
  _openDetailKey: 'old',
};
const Tabs = { switched: '', switch(tab) { this.switched = tab; } };
const Toast = { warning(message) { throw new Error(message); } };
const context = { Store, Utils, document, LogsTab, Tabs, Toast };
vm.runInNewContext(dashboardSource, context);
vm.runInNewContext(configSource, context);
vm.runInNewContext(actionsSource, context);
const dashboardHtml = context.dashboard.renderRiskIsolationAlert(Store.state);
const groupSummary = context.config.groupRiskSummary('g-risk');
const groupHtml = context.config.renderGroupRiskAlert(groupSummary);
if (!dashboardHtml.includes('检测到上游风控拦截')) throw new Error('dashboard alert missing');
if (!dashboardHtml.includes('data-dashboard-action="open-risk-model"')) throw new Error('dashboard action missing');
if (!groupHtml.includes('data-group-action="view-risk-diagnosis"')) throw new Error('group diagnosis action missing');
if (!groupHtml.includes('影响')) throw new Error('group impact missing');
if (dashboardHtml.includes('SAFE_TEST_CREDENTIAL_MUST_NOT_RENDER') || groupHtml.includes('SAFE_TEST_CREDENTIAL_MUST_NOT_RENDER')) {
  throw new Error('credential leaked into risk UI');
}
context.actions.onOpenRiskDiagnosis({});
if (Store.selected.type !== 'model' || Store.selected.id !== 'm-risk') throw new Error('diagnosis did not select risk model');
if (LogsTab.filters.group !== 'g-risk' || !LogsTab.currentOnly || LogsTab.page !== 0 || LogsTab._openDetailKey !== '') {
  throw new Error('diagnosis filters were not initialized safely');
}
if (Tabs.switched !== 'logs') throw new Error('diagnosis did not navigate to logs');
console.log('RISK_UI_ACTIONS_OK');
'''
    assert _run_node(script) == "RISK_UI_ACTIONS_OK"


def test_risk_ui_source_has_dynamic_runtime_patch_and_confirmed_release_contract() -> None:
    config = (ROOT / "static/js/config-tab.js").read_text(encoding="utf-8")
    runtime = (ROOT / "static/js/config-tab-runtime.js").read_text(encoding="utf-8")
    actions = (ROOT / "static/js/config-tab-actions.js").read_text(encoding="utf-8")
    logs = (ROOT / "static/js/logs-tab.js").read_text(encoding="utf-8")
    dashboard = (ROOT / "static/js/dashboard-tab.js").read_text(encoding="utf-8")

    assert "risk_isolated: '风险隔离'" in config
    assert "上游风控保护" in config
    assert "最近5次失败" in config
    assert "[data-group-risk-alert]" in runtime
    assert "patchModelRiskAlert" in runtime
    assert "onOpenRiskDiagnosis" in actions
    assert "解除后后续请求会再次访问上游" in actions
    assert "aggregate_first_frame_timeout" in logs
    assert "risk_isolated=true" in logs
    assert "renderRiskIsolationAlert" in dashboard
    assert "data-dashboard-action=\"open-risk-model\"" in dashboard
