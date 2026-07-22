"""配置页自动保存失败时的服务端基线回滚契约。"""
from __future__ import annotations

import subprocess
from pathlib import Path


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


def test_existing_form_autosave_failure_restores_server_baseline_for_all_types() -> None:
    """连接组、模型、聚合保存失败后均应丢弃失败草稿并重渲染 Store 基线。"""
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = [
  fs.readFileSync('static/js/config-tab-form.js', 'utf8'),
  'this.form = ConfigTabForm;',
  fs.readFileSync('static/js/config-tab-actions.js', 'utf8'),
  'this.actions = ConfigTabActions;',
].join('\n');
const Store = {
  selected: { type: 'group', id: 'g1' },
  getGroup(id) { return id === 'g1' ? { id, name: '服务端组基线' } : null; },
  getModel(id) { return id === 'm1' ? { id, name: '服务端模型基线', group_id: 'g1' } : null; },
  getAggregate(id) { return id === 'a1' ? { id, name: '服务端聚合基线' } : null; },
};
const elements = new Map();
const document = {
  getElementById(id) { return elements.get(id) || null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
const context = {
  Store,
  document,
  Tabs: { current: 'config' },
  API: {
    saveGroup: async () => { throw new Error('server 500'); },
    saveModel: async () => { throw new Error('server 500'); },
    saveAggregate: async () => { throw new Error('server 500'); },
  },
  Toast: { error() {}, success() {} },
  Modal: {},
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(source, context);

function input(value = '', checked = false) { return { value, checked }; }
function makeController(type, id) {
  const selection = { type, id };
  const key = `${type}:${id}`;
  const controller = {
    _drafts: new Map([[key, { stale: '失败草稿' }]]),
    _draftDirty: new Set([key]),
    _draftBaselines: new Map([[key, { complete: '服务端最后成功基线' }]]),
    _autoSaveRevision: 7,
    _autoSaveSavingRevision: 7,
    statuses: [],
    renders: [],
    setSaveStatus(status, message) { this.statuses.push({ status, message }); },
    captureDraft() { throw new Error('同 generation 的失败不应保留失败草稿'); },
    clearDraft(selectionArg) { return context.form.clearDraft(this, selectionArg); },
    restoreFailedAutoSaveBaseline(selectionArg, generation) {
      return context.form.restoreFailedAutoSaveBaseline(this, selectionArg, generation);
    },
    render() {
      const baseline = type === 'group' ? Store.getGroup(id)
        : type === 'model' ? Store.getModel(id) : Store.getAggregate(id);
      this.renders.push(baseline);
    },
    validateGroupForm() { return { ok: true }; },
    validateModelForm() { return { ok: true }; },
    validateAggregateForm() { return { ok: true }; },
    isNewGroupDraft() { return false; },
  };
  return { controller, key };
}

async function verify(type, id, submit, fields) {
  elements.clear();
  Object.entries(fields).forEach(([key, value]) => elements.set(key, value));
  Store.selected = { type, id };
  const { controller, key } = makeController(type, id);
  await context.actions[submit](controller, { preventDefault() {}, autoSave: true });
  if (controller._drafts.has(key) || controller._draftDirty.has(key) || controller._draftBaselines.has(key)) {
    throw new Error(`${type} failed draft was not cleared`);
  }
  if (controller.renders.length !== 1 || controller.renders[0].name !== `服务端${type === 'group' ? '组' : type === 'model' ? '模型' : '聚合'}基线`) {
    throw new Error(`${type} did not restore complete server baseline`);
  }
  if (controller.statuses.at(-1).status !== 'error') throw new Error(`${type} did not retain error status`);
}

(async () => {
  await verify('group', 'g1', 'onGroupSubmit', {
    'group-id': input('g1'), 'group-provider': input('relay'), 'group-key': input(''),
    'group-name': input('失败组名'), 'group-base': input('https://failed.example/v1'),
    'group-auto-model-name': input(''), 'group-cooldown': input('5'), 'group-stream-timeout': input('120'),
    'group-routing-policy': input('smart_breaker'), 'group-waf': input('', false),
  });
  await verify('model', 'm1', 'onModelSubmit', {
    'model-id': input('m1'), 'model-group': input('g1'), 'model-upstream': input('failed-model'),
    'model-ep': input('failed-model'), 'model-name': input('失败模型名'), 'model-key': input(''),
    'model-price': input(''), 'model-price-input': input('0'), 'model-price-output': input('0'),
    'model-usable': input('', true),
  });
  await verify('aggregate', 'a1', 'onAggregateSubmit', {
    'aggregate-id': input('a1'), 'aggregate-name': input('失败聚合名'), 'aggregate-display-name': input(''),
    'aggregate-client-model-aliases': input(''), 'aggregate-enabled': input('', true),
    'aggregate-routing-policy': input('smart_breaker'), 'aggregate-cooldown': input('0'),
  });
  console.log('AUTOSAVE_FAILURE_BASELINE_ROLLBACK_OK');
})();
'''
    assert _run_node(script) == "AUTOSAVE_FAILURE_BASELINE_ROLLBACK_OK"


def test_newer_edit_generation_is_never_rolled_back_by_failed_autosave() -> None:
    """保存期间发生新编辑时，旧请求失败只能报错，不能清理或覆盖新草稿。"""
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab-form.js', 'utf8') + '\nthis.form = ConfigTabForm;';
const Store = { selected: { type: 'model', id: 'm1' } };
const context = { Store, document: {}, setTimeout, clearTimeout };
vm.runInNewContext(source, context);
const key = 'model:m1';
const controller = {
  _drafts: new Map([[key, { 'model-name': '保存期间的新编辑' }]]),
  _draftDirty: new Set([key]),
  _draftBaselines: new Map([[key, { 'model-name': '服务端成功基线' }]]),
  _autoSaveRevision: 9,
  _autoSaveSavingRevision: 8,
  renders: 0,
  render() { this.renders += 1; },
};
const rolledBack = context.form.restoreFailedAutoSaveBaseline(
  controller,
  Store.selected,
  controller._autoSaveSavingRevision,
);
if (rolledBack) throw new Error('newer edit must prevent rollback');
if (controller.renders !== 0) throw new Error('newer edit must not re-render over the form');
if (controller._drafts.get(key)?.['model-name'] !== '保存期间的新编辑') throw new Error('newer draft was lost');
if (!controller._draftDirty.has(key)) throw new Error('newer draft lost dirty marker');
if (!controller._draftBaselines.has(key)) throw new Error('server baseline should remain for newer edit');
console.log('AUTOSAVE_FAILURE_NEWER_EDIT_PROTECTED_OK');
'''
    assert _run_node(script) == "AUTOSAVE_FAILURE_NEWER_EDIT_PROTECTED_OK"


def test_failed_autosave_uses_the_latest_successful_server_baseline() -> None:
    """一次成功保存后，下一次失败必须回退到更新后的服务端基线而非首次渲染旧值。"""
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = [
  fs.readFileSync('static/js/config-tab-form.js', 'utf8'),
  'this.form = ConfigTabForm;',
  fs.readFileSync('static/js/config-tab-actions.js', 'utf8'),
  'this.actions = ConfigTabActions;',
].join('\n');
let serverModel = { id: 'm1', name: '首次服务端基线', group_id: 'g1' };
let requestNumber = 0;
const elements = new Map();
const Store = {
  selected: { type: 'model', id: 'm1' },
  getGroup() { return { id: 'g1', provider_type: 'relay' }; },
  getModel() { return serverModel; },
  async load() {},
};
const context = {
  Store,
  document: {
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  },
  Tabs: { current: 'config' },
  API: {
    async saveModel(_id, payload) {
      requestNumber += 1;
      if (requestNumber === 1) {
        serverModel = { ...serverModel, name: payload.name };
        return {};
      }
      throw new Error('server 500');
    },
  },
  Toast: { error() {}, success() {} },
  Modal: {},
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(source, context);
function input(value = '', checked = false) { return { value, checked }; }
function setFields(name) {
  elements.clear();
  [
    ['model-id', input('m1')], ['model-group', input('g1')], ['model-upstream', input('upstream')],
    ['model-ep', input('upstream')], ['model-name', input(name)], ['model-key', input('')],
    ['model-price', input('')], ['model-price-input', input('0')], ['model-price-output', input('0')],
    ['model-usable', input('', true)],
  ].forEach(([key, value]) => elements.set(key, value));
}
const key = 'model:m1';
const controller = {
  _drafts: new Map(), _draftDirty: new Set(), _draftBaselines: new Map(),
  _autoSaveRevision: 1, _autoSaveSavingRevision: 1,
  setSaveStatus() {},
  clearDraft(selection) { return context.form.clearDraft(this, selection); },
  captureDraft() {},
  restoreFailedAutoSaveBaseline(selection, generation) {
    return context.form.restoreFailedAutoSaveBaseline(this, selection, generation);
  },
  validateModelForm() { return { ok: true }; },
  render() { this.renderedName = Store.getModel('m1').name; },
};
(async () => {
  setFields('成功后服务端基线');
  await context.actions.onModelSubmit(controller, { preventDefault() {}, autoSave: true });
  if (serverModel.name !== '成功后服务端基线') throw new Error('first automatic save did not persist server baseline');
  controller._drafts.set(key, { 'model-name': '失败草稿' });
  controller._draftDirty.add(key);
  controller._draftBaselines.set(key, { 'model-name': '成功后服务端基线' });
  setFields('失败草稿');
  await context.actions.onModelSubmit(controller, { preventDefault() {}, autoSave: true });
  if (controller.renderedName !== '成功后服务端基线') throw new Error('failure did not restore latest successful server baseline');
  console.log('AUTOSAVE_LATEST_SERVER_BASELINE_OK');
})();
'''
    assert _run_node(script) == "AUTOSAVE_LATEST_SERVER_BASELINE_OK"


def test_autosave_generation_advances_while_prior_request_is_in_flight() -> None:
    """新编辑必须递增 generation，finally 才能安排后续 500ms 合并保存。"""
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab-form.js', 'utf8') + '\nthis.form = ConfigTabForm;';
const context = {
  Store: { selected: { type: 'aggregate', id: 'a1' } },
  document: { querySelector() { return null; } },
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(source, context);
const controller = {
  _autoSaveInFlight: true,
  _autoSaveRevision: 4,
  setSaveStatus() { throw new Error('in-flight edit must not overwrite error state'); },
};
context.form.scheduleAutoSave(controller, { isConnected: true });
if (controller._autoSaveRevision !== 5) throw new Error('new edit did not advance generation during in-flight save');
console.log('AUTOSAVE_INFLIGHT_GENERATION_OK');
'''
    assert _run_node(script) == "AUTOSAVE_INFLIGHT_GENERATION_OK"
