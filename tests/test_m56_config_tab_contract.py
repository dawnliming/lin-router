#!/usr/bin/env python3
"""Contracts for the v0.5.6 config-tab responsibility extraction."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_node(script):
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return completed.stdout.strip()


def test_config_tab_module_boundaries_and_script_order():
    index_html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    config_js = (ROOT / "static/js/config-tab.js").read_text(encoding="utf-8")
    form_js = (ROOT / "static/js/config-tab-form.js").read_text(encoding="utf-8")
    runtime_js = (ROOT / "static/js/config-tab-runtime.js").read_text(encoding="utf-8")
    actions_js = (ROOT / "static/js/config-tab-actions.js").read_text(encoding="utf-8")
    tabs_js = (ROOT / "static/js/tabs.js").read_text(encoding="utf-8")

    assert index_html.index('js/config-tab-form.js') < index_html.index('js/config-tab.js')
    assert index_html.index('js/config-tab-runtime.js') < index_html.index('js/config-tab.js')
    assert index_html.index('js/config-tab-actions.js') < index_html.index('js/config-tab.js')
    assert 'const ConfigTabForm' in form_js
    assert 'const ConfigTabRuntimeView' in runtime_js
    assert 'const ConfigTabActions' in actions_js
    assert 'ConfigTabForm.validateGroupForm(this, ...args)' in config_js
    assert 'ConfigTabActions.onGroupSubmit(this, ...args)' in config_js
    assert 'const stillViewingSavedGroup = Tabs.current === \'config\'' in actions_js
    assert 'if (explicitSubmit && stillViewingSavedGroup)' in actions_js
    assert 'ConfigTabRuntimeView.onRuntimeStateUpdate(this, ...args)' in config_js
    assert 'ConfigTabForm.bindGlobalEvents(this)' in config_js
    assert 'ConfigTabRuntimeView.dispose(this)' in config_js
    assert "if (this.current === 'config') ConfigTab.dispose();" in tabs_js


def test_runtime_refresh_is_a_dirty_safe_local_patch():
    runtime_js = (ROOT / "static/js/config-tab-runtime.js").read_text(encoding="utf-8")

    assert 'controller.render()' not in runtime_js
    assert 'panel.innerHTML' not in runtime_js
    assert 'controller.onRuntimeStateUpdate()' in runtime_js

    script = r'''
const fs = require('fs');
const vm = require('vm');
const runtimeSource = fs.readFileSync('static/js/config-tab-runtime.js', 'utf8') + '\nthis.runtime = ConfigTabRuntimeView;';
const cell = { innerHTML: '', className: '', textContent: '', title: '' };
const input = { value: 'dirty https://edited.example/v1', selectionStart: 8, selectionEnd: 8 };
const document = {
  getElementById(id) { return id === 'model-cooldown-display' ? null : null; },
  querySelector(selector) { return selector.includes('data-member-status-cell') ? cell : null; },
  querySelectorAll() { return []; },
};
const Store = {
  selected: { type: 'aggregate', id: 'a1' },
  state: { models: [{ id: 'm1', name: 'model' }], aggregate_members: [{ id: 'am1', model_id: 'm1' }], logs: [] },
  getAggregateMembers() { return this.state.aggregate_members; },
  getModel(id) { return this.state.models.find(item => item.id === id); },
  update(patch) { this.state = { ...this.state, ...patch }; },
};
const context = { console, document, Store, Tabs: { current: 'config' }, API: { async getRuntimeState() { return { models: [{ model_id: 'm1', cooldown_until: 100 }], aggregate_members: [{ member_id: 'am1', derived_status: 'healthy' }], logs: [] }; } }, Toast: { success() {}, error() {} }, Utils: { escapeHtml(v) { return v; }, formatDate(v) { return String(v); } }, Map, Date, setInterval, clearInterval, clearTimeout };
vm.runInNewContext(runtimeSource, context);
let renders = 0;
let runtimeUpdates = 0;
const controller = {
  render() { renders += 1; },
  onRuntimeStateUpdate() { runtimeUpdates += 1; context.runtime.patchVisibleRuntimeStatus(this); },
  updateCooldownDisplay() {},
  patchVisibleRuntimeStatus() { return context.runtime.patchVisibleRuntimeStatus(this); },
  aggregateMemberStatus() { return { class: 'success', text: '正常', title: 'healthy' }; },
};
(async () => {
  await context.runtime.refreshRuntimeNow(controller);
  if (renders !== 0) throw new Error('runtime refresh rebuilt config panel');
  if (runtimeUpdates !== 1) throw new Error('runtime refresh did not request local patch');
  if (input.value !== 'dirty https://edited.example/v1' || input.selectionStart !== 8) throw new Error('dirty input changed');
  if (!cell.innerHTML.includes('正常')) throw new Error('visible status was not patched');
  console.log('M56_DIRTY_REFRESH_OK');
})();
'''
    assert run_node(script) == "M56_DIRTY_REFRESH_OK"


def test_action_orchestration_preserves_existing_api_contracts():
    actions_js = (ROOT / "static/js/config-tab-actions.js").read_text(encoding="utf-8")

    for api_call in [
        "API.saveGroup(id, payload)", "API.createGroup(payload)",
        "API.saveModel(id, payload)", "API.createModel(payload)",
        "API.saveAggregate(id, payload)", "API.createAggregate(payload)",
        "API.fetchUpstreamModels(groupId, apiKey)", "API.importConfig(file)",
        "API.req('/api/models/batch', {",
    ]:
        assert api_call in actions_js
    for payload_field in [
        "provider_type: mode", "base_url:", "ark_api_key:", "api_key:",
        "auto_model_cooldown_minutes:", "stream_idle_timeout:",
        "reasoning_support:", "waf_client_mode:", "waf_compatible:",
        "waf_accept_policy:", "group_id: groupId", "upstream_model:",
        "client_model_aliases:", "cooldown_minutes:", "strategy:",
    ]:
        assert payload_field in actions_js


def test_form_global_listener_and_runtime_dispose_are_idempotent():
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab-form.js', 'utf8') + '\nthis.form = ConfigTabForm;' + fs.readFileSync('static/js/config-tab-runtime.js', 'utf8') + '\nthis.runtime = ConfigTabRuntimeView;';
const listeners = new Map();
const document = {
  addEventListener(type, handler) { listeners.set(type, handler); },
  removeEventListener(type, handler) { if (listeners.get(type) === handler) listeners.delete(type); },
};
const context = { document, clearTimeout() {}, clearInterval() {}, setInterval() { return 1; } };
vm.runInNewContext(source, context);
let outsideCalls = 0;
const controller = { _onUpstreamOutsideClick() { outsideCalls += 1; }, _stopCooldownTimer() { this.timerStopped = true; }, _autoSaveTimer: 7 };
context.form.bindGlobalEvents(controller);
context.form.bindGlobalEvents(controller);
if (listeners.size !== 1) throw new Error('duplicate global listener');
listeners.get('mousedown')({});
if (outsideCalls !== 1) throw new Error('listener did not preserve controller binding');
context.form.dispose(controller);
context.form.dispose(controller);
if (listeners.size !== 0 || controller._upstreamOutsideClickHandler) throw new Error('global listener was not disposed');
context.runtime.dispose(controller);
if (!controller.timerStopped || controller._autoSaveTimer !== null) throw new Error('runtime timers were not disposed');
console.log('M56_LIFECYCLE_OK');
'''
    assert run_node(script) == "M56_LIFECYCLE_OK"
