"""前端运行态 scope 刷新、增量合并与日志轮询的回归契约。"""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_node(script: str) -> str:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout.strip()


def test_runtime_state_api_preserves_legacy_silent_call_and_serializes_scope_cursor() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/api.js', 'utf8') + '\nthis.API = API;';
const urls = [];
const context = {
  console,
  URLSearchParams,
  FormData,
  fetch: async url => {
    urls.push(url);
    return { ok: true, headers: { get: () => 'application/json' }, json: async () => ({ ok: true }) };
  },
  document: { getElementById: () => null },
};
vm.runInNewContext(source, context);
(async () => {
  await context.API.getRuntimeState({ silent: true });
  await context.API.getRuntimeState({ scope: 'dashboard', revision: 'r 1', activity_cursor: '42' }, { silent: true });
  if (urls[0] !== '/api/runtime-state') throw new Error('legacy silent call changed URL');
  if (urls[1] !== '/api/runtime-state?scope=dashboard&revision=r+1&activity_cursor=42') throw new Error('scope parameters were not serialized');
  console.log('API_SCOPE_OK');
})();
'''
    assert run_node(script) == "API_SCOPE_OK"


def test_app_runtime_scope_merges_incrementally_and_does_not_write_logs_from_config() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/app.js', 'utf8') + '\nthis.App = App;';
const calls = [];
const responses = [];
const Store = {
  state: {
    models: [{ id: 'm1', usable: true }],
    aggregate_members: [{ id: 'am1', model_id: 'm1' }],
    logs: [{ request_id: 'old', attempt: 1, time: 't0', model: 'm1' }],
    live_requests: [],
  },
  update(patch) { this.state = { ...this.state, ...patch }; },
};
const context = {
  console,
  document: { hidden: false, addEventListener() {}, getElementById: () => null },
  Tabs: { current: 'dashboard' },
  Store,
  API: { getRuntimeState(params, opts) { calls.push({ params, opts }); return Promise.resolve(responses.shift()); } },
  setInterval() { return 1; }, clearInterval() {}, Date, Map, Set, Object, Array,
};
vm.runInNewContext(source, context);
const app = context.App;
(async () => {
  responses.push({
    scope: 'dashboard', runtime_revision: 'r1', changed: true, next_poll_ms: 5000,
    models: [{ model_id: 'm1', cooldown_until: 10 }],
    aggregate_members: [{ member_id: 'am1', derived_status: 'healthy' }],
    live_requests: [], log_write_error: '',
    activity: { cursor: '7', changed: true, mode: 'snapshot', logs: [{ request_id: 'first', attempt: 1, time: 't1', model: 'm1' }] },
  });
  await app.refreshRuntimeState('dashboard', { background: false });
  if (calls[0].params.scope !== 'dashboard' || calls[0].params.revision || calls[0].params.activity_cursor) throw new Error('initial dashboard scope request is wrong');
  if (Store.state.models[0].cooldown_until !== 10 || Store.state.logs[0].request_id !== 'first') throw new Error('dashboard snapshot was not applied');

  responses.push({
    scope: 'dashboard', runtime_revision: 'r1', changed: false, next_poll_ms: 5000,
    live_requests: [], activity: { cursor: '8', changed: true, mode: 'snapshot', logs: [] },
  });
  await app.refreshRuntimeState('dashboard', { background: false });
  if (calls[1].params.revision !== 'r1' || calls[1].params.activity_cursor !== '7') throw new Error('revision or activity cursor was not reused');
  if (Store.state.models[0].cooldown_until !== 10 || Store.state.logs.length !== 0) throw new Error('changed=false must preserve model state but allow activity snapshot reset');

  responses.push({
    scope: 'config', runtime_revision: 'c1', changed: true, next_poll_ms: 1000,
    models: [{ model_id: 'm1', cooldown_until: 20 }], live_requests: [],
    logs: [{ request_id: 'must-not-leak' }],
  });
  await app.refreshRuntimeState('config', { background: false });
  if (calls[2].params.scope !== 'config' || calls[2].params.activity_cursor) throw new Error('config request used dashboard cursor');
  if (Store.state.models[0].cooldown_until !== 20 || Store.state.logs.length !== 0) throw new Error('config runtime update wrote logs');
  if (app._runtimePollDelay('dashboard', { live_requests: [{ request_id: 'live' }], next_poll_ms: 1000 }) !== 1000) throw new Error('live dashboard interval is not 1 second');
  if (app._runtimePollDelay('config', { live_requests: [{ request_id: 'live' }], next_poll_ms: 1000 }) !== 5000) throw new Error('config interval must remain 5 seconds');
  const history = Array.from({ length: 40 }, (_, index) => ({ request_id: `old-${index}`, time: String(index) }));
  const bounded = app._mergeRuntimeActivity(history, { mode: 'delta', logs: [{ request_id: 'new', time: 'new' }] });
  if (bounded.length !== 30 || bounded[0].request_id !== 'new') throw new Error('dashboard activity delta was not bounded to 30');

  let resolveRequest;
  context.API.getRuntimeState = params => {
    calls.push({ params });
    return new Promise(resolve => { resolveRequest = resolve; });
  };
  const first = app.refreshRuntimeState('dashboard', { background: false });
  const second = app.refreshRuntimeState('dashboard', { background: false });
  if (calls.length !== 4) throw new Error('same scope started duplicate in-flight request');
  resolveRequest({ scope: 'dashboard', runtime_revision: 'r2', changed: false, next_poll_ms: 5000, activity: { cursor: '9', changed: false, mode: 'delta', logs: [] } });
  await Promise.all([first, second]);

  context.API.getRuntimeState = () => Promise.reject(new Error('offline'));
  const before = Date.now();
  const fallback = await app.refreshRuntimeState('dashboard', { background: true });
  if (fallback !== null || app._runtimeRefreshStates.dashboard.failures !== 1) throw new Error('background failure did not enter retry state');
  if (app._runtimeRefreshStates.dashboard.nextPollAt < before + 4900) throw new Error('first failure did not apply 5 second backoff');
  if (app._runtimeBackoffDelay(1) !== 5000 || app._runtimeBackoffDelay(2) !== 10000 || app._runtimeBackoffDelay(3) !== 30000 || app._runtimeBackoffDelay(4) !== 30000) throw new Error('runtime backoff sequence is not 5/10/30');

  const callsBeforeLogs = calls.length;
  context.Tabs.current = 'logs';
  app._runtimeRefreshTick(true);
  if (calls.length !== callsBeforeLogs) throw new Error('logs scope requested runtime-state');
  context.API.getRuntimeState = params => {
    calls.push({ params });
    return Promise.resolve({ scope: 'dashboard', runtime_revision: 'r3', changed: false, next_poll_ms: 5000, activity: { cursor: '10', changed: false, mode: 'delta', logs: [] } });
  };
  context.Tabs.current = 'dashboard';
  context.document.hidden = true;
  app._onRuntimeVisibilityChange();
  app._runtimeRefreshTick(true);
  if (calls.length !== callsBeforeLogs) throw new Error('hidden dashboard requested runtime-state');
  context.document.hidden = false;
  app._onRuntimeVisibilityChange();
  await Promise.resolve();
  await Promise.resolve();
  if (calls.length !== callsBeforeLogs + 1) throw new Error('visible dashboard did not make exactly one catch-up refresh');
  console.log('APP_RUNTIME_SCOPE_OK');
})();
'''
    assert run_node(script) == "APP_RUNTIME_SCOPE_OK"


def test_config_manual_refresh_and_logs_auto_refresh_are_scope_safe_and_single_flight() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const runtimeSource = fs.readFileSync('static/js/config-tab-runtime.js', 'utf8') + '\nthis.runtime = ConfigTabRuntimeView;';
const logsSource = fs.readFileSync('static/js/logs-tab.js', 'utf8') + '\nthis.logs = LogsTab;';
const runtimeCalls = [];
const configStore = {
  state: { models: [{ id: 'm1' }], aggregate_members: [], logs: [{ request_id: 'keep' }], live_requests: [] },
  selected: { type: 'model', id: 'm1' },
  update(patch) { this.state = { ...this.state, ...patch }; },
  getAggregateMembers: () => [],
  getModel(id) { return this.state.models.find(item => item.id === id) || null; },
};
const runtimeContext = {
  console, Map, Array, Object, Date, setInterval() { return 1; }, clearInterval() {}, clearTimeout() {},
  document: { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] },
  Tabs: { current: 'config' }, Store: configStore,
  API: { getRuntimeState(params) { runtimeCalls.push(params); return Promise.resolve({ models: [{ model_id: 'm1', cooldown_until: 30 }], logs: [] }); } },
  Toast: { success() {}, error() {} }, Utils: { escapeHtml: value => value, formatDate: value => String(value) },
};
vm.runInNewContext(runtimeSource, runtimeContext);
const controller = { onRuntimeStateUpdate() {}, updateCooldownDisplay() {}, patchVisibleRuntimeStatus() {}, aggregateMemberStatus() { return { class: '', text: '', title: '' }; } };

const listeners = {};
let logCalls = 0;
const logStore = { state: { logs: [] }, selected: {}, update(patch) { this.state = { ...this.state, ...patch }; } };
const logContext = {
  console, Date, Map, Set, Object, Array, CSS: { escape: value => value },
  document: { hidden: false, addEventListener(type, handler) { listeners[type] = handler; }, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] },
  Tabs: { current: 'logs' }, Store: logStore,
  API: { async getLogs() { logCalls += 1; return { total: 0, logs: [] }; } }, Toast: { error() {} }, Utils: { escapeHtml: value => String(value || '') },
};
vm.runInNewContext(logsSource, logContext);
(async () => {
  await runtimeContext.runtime.refreshRuntimeNow(controller);
  if (runtimeCalls[0].scope !== 'config' || configStore.state.logs[0].request_id !== 'keep') throw new Error('config manual refresh was not logs-safe');
  configStore.state.models[0].cooldown_until = Math.floor(Date.now() / 1000) - 1;
  runtimeContext.runtime.updateCooldownDisplay(controller);
  runtimeContext.runtime.updateCooldownDisplay(controller);
  await Promise.resolve();
  await Promise.resolve();
  if (runtimeCalls.length !== 2 || runtimeCalls[1].scope !== 'config') throw new Error('expired cooldown did not make exactly one config refresh');

  const logs = logContext.logs;
  logs.syncCurrentOnlySelection = () => false;
  logs.hasCurrentOnlyGroupConflict = () => false;
  logs.shouldUseLocalCurrentOnlyPagination = () => false;
  logs.syncPageToTotal = () => false;
  logs.renderRows = () => {};
  logs.renderPagination = () => {};
  const first = logs.manualRefresh(true);
  const second = logs.manualRefresh(true);
  await Promise.all([first, second]);
  if (logCalls !== 1) throw new Error('logs refresh did not single-flight');
  logContext.document.hidden = true;
  await logs.autoRefreshTick();
  if (logCalls !== 1) throw new Error('hidden logs tab still requested data');
  logContext.document.hidden = false;
  logs.bindVisibility();
  listeners.visibilitychange();
  await Promise.resolve();
  await Promise.resolve();
  if (logCalls !== 2) throw new Error('visible logs tab did not refresh exactly once');
  console.log('CONFIG_AND_LOGS_REFRESH_OK');
})();
'''
    assert run_node(script) == "CONFIG_AND_LOGS_REFRESH_OK"


def test_dashboard_cancel_uses_scope_refresh_instead_of_legacy_runtime_endpoint() -> None:
    dashboard_js = (ROOT / "static" / "js" / "dashboard-tab.js").read_text(encoding="utf-8")

    assert "App.refreshRuntimeState('dashboard', { background: false, silent: true })" in dashboard_js
    assert "API.getRuntimeState({ silent: true }).then" not in dashboard_js
