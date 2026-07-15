#!/usr/bin/env python3
"""v0.6.3 聚合成员批量管理与价格展示前端契约。"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_node(script: str) -> str:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return completed.stdout.strip()


def test_batch_api_and_manual_priority_source_contract():
    api_js = (ROOT / "static/js/api.js").read_text(encoding="utf-8")
    config_js = (ROOT / "static/js/config-tab.js").read_text(encoding="utf-8")
    actions_js = (ROOT / "static/js/config-tab-actions.js").read_text(encoding="utf-8")
    form_js = (ROOT / "static/js/config-tab-form.js").read_text(encoding="utf-8")
    config_css = (ROOT / "static/css/config-tab.css").read_text(encoding="utf-8")

    assert "createAggregateMembersBatch(aggregateId, data)" in api_js
    assert "/members/batch`" in api_js
    assert "group_id: values['member-batch-group']" in actions_js
    assert "strategy: 'priority'" in actions_js
    assert "price_first" not in config_js
    assert "价格优先" not in config_js
    assert "价格组仅展示模型配置，不参与调度排序" in config_js
    assert "renderAggregatePriceGroup" in config_js
    assert "renderAggregateUnderlyingPrice" not in config_js
    assert "aggregate-member-price" not in config_js
    assert "onUpdateAggregateMemberPrice" not in config_js
    assert "manual_price" not in config_js
    assert "member-price" not in actions_js
    assert "manual_price" not in actions_js
    assert "onUpdateAggregateMemberPrice" not in actions_js
    assert "aggregate-member-price" not in form_js
    assert ".aggregate-member-actions > button" in config_css
    assert "height: 28px" in config_css
    assert "min-height: 28px" in config_css
    assert ".aggregate-member-actions > .btn-icon" in config_css
    assert "applyDefaultPrice" not in actions_js


def test_store_counts_addable_existing_and_unavailable_models_by_group():
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/store.js', 'utf8') + '\nthis.store = Store;';
const context = { API: {}, Toast: {}, Set };
vm.runInNewContext(source, context);
context.store.state = {
  models: [
    { id: 'm-existing', group_id: 'g-relay', usable: true },
    { id: 'm-new', group_id: 'g-relay', usable: true },
    { id: 'm-disabled', group_id: 'g-relay', usable: false },
    { id: 'm-other', group_id: 'g-other', usable: true },
  ],
  aggregate_members: [
    { id: 'am-existing', aggregate_id: 'a1', group_id: 'g-relay', model_id: 'm-existing', priority: 1 },
    { id: 'am-other', aggregate_id: 'a1', group_id: 'g-other', model_id: 'm-other', priority: 2 },
  ],
};
const summary = context.store.getAggregateBatchGroupSummary('a1', 'g-relay');
if (summary.modelCount !== 3) throw new Error('modelCount mismatch');
if (summary.addableCount !== 1) throw new Error('addableCount mismatch');
if (summary.existingCount !== 1) throw new Error('existingCount mismatch');
if (summary.unavailableCount !== 1) throw new Error('unavailableCount mismatch');
console.log('V063_STORE_BATCH_SUMMARY_OK');
'''
    assert run_node(script) == "V063_STORE_BATCH_SUMMARY_OK"


def test_batch_ui_uses_one_request_one_refresh_and_reports_success_duplicate_error():
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab-actions.js', 'utf8') + '\nthis.actions = ConfigTabActions;';
const notices = [];
let mode = 'success';
let requests = [];
let reloads = 0;
const Store = {
  state: { groups: [{ id: 'g1', name: '中转一组', provider_type: 'relay' }] },
  getAggregateBatchGroupSummary() { return { addableCount: 2, existingCount: 1, unavailableCount: 0 }; },
};
const document = {
  getElementById(id) { return id === 'aggregate-id' ? { value: 'a1' } : null; },
};
const Modal = {
  async form(options) {
    if (!options.html.includes('可添加 2 / 已存在 1')) throw new Error('group counts missing');
    return { 'member-batch-group': 'g1' };
  },
};
const API = {
  async createAggregateMembersBatch(aggregateId, payload) {
    requests.push({ aggregateId, payload });
    if (mode === 'error') throw new Error('mock upstream failure');
    if (mode === 'duplicate') {
      return { message: '所选连接组模型均已存在', added_count: 0, skipped_count: 3, failed_count: 0 };
    }
    return { added_count: 2, skipped_count: 1, failed_count: 0 };
  },
};
const Toast = {
  success(message) { notices.push(['success', message]); },
  warning(message) { notices.push(['warning', message]); },
  error(message) { notices.push(['error', message]); },
};
const Utils = { escapeHtml(value) { return String(value); } };
const context = { console, Store, document, Modal, API, Toast, Utils, Array, Number };
vm.runInNewContext(source, context);
const controller = { async reloadAfterAggregateMemberChange() { reloads += 1; } };
(async () => {
  await context.actions.onAddAggregateMembersByGroup(controller);
  if (requests.length !== 1 || reloads !== 1) throw new Error('success must request and refresh once');
  if (requests[0].aggregateId !== 'a1' || requests[0].payload.group_id !== 'g1') throw new Error('batch payload mismatch');
  if (notices[0][0] !== 'success' || !notices[0][1].includes('新增 2 个，跳过 1 个，失败 0 个')) throw new Error('success summary mismatch');

  mode = 'duplicate';
  notices.length = 0;
  await context.actions.onAddAggregateMembersByGroup(controller);
  if (requests.length !== 2 || reloads !== 2) throw new Error('duplicate result must request and refresh once');
  if (notices[0][0] !== 'warning' || !notices[0][1].includes('新增 0 个，跳过 3 个，失败 0 个')) throw new Error('duplicate summary mismatch');

  mode = 'error';
  notices.length = 0;
  await context.actions.onAddAggregateMembersByGroup(controller);
  if (requests.length !== 3 || reloads !== 2) throw new Error('failed request must not refresh stale state');
  if (notices[0][0] !== 'error' || !notices[0][1].includes('批量添加失败')) throw new Error('error feedback mismatch');
  console.log('V063_BATCH_UI_FLOW_OK');
})();
'''
    assert run_node(script) == "V063_BATCH_UI_FLOW_OK"


def test_single_member_add_does_not_create_or_overwrite_manual_price():
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab-actions.js', 'utf8') + '\nthis.actions = ConfigTabActions;';
const requests = [];
let reloads = 0;
const Store = {
  state: { groups: [{ id: 'g1', name: '中转一组', provider_type: 'relay' }] },
};
const document = {
  getElementById(id) { return id === 'aggregate-id' ? { value: 'a1' } : null; },
};
const Modal = {
  async form() { return { 'member-group': 'g1', 'member-model': 'm1' }; },
};
const API = {
  async createAggregateMember(aggregateId, payload) {
    requests.push({ aggregateId, payload });
  },
};
const Toast = { success() {}, warning() {}, error() {} };
const Utils = { escapeHtml(value) { return String(value); } };
const context = { console, Store, document, Modal, API, Toast, Utils, Set, String };
vm.runInNewContext(source, context);
(async () => {
  await context.actions.onAddAggregateMember({
    _updateMemberPreview() {},
    async reloadAfterAggregateMemberChange() { reloads += 1; },
  });
  if (requests.length !== 1 || reloads !== 1) throw new Error('member add did not complete');
  const request = requests[0];
  if (request.aggregateId !== 'a1') throw new Error('aggregate id mismatch');
  if (request.payload.group_id !== 'g1' || request.payload.model_id !== 'm1') throw new Error('member payload mismatch');
  if ('manual_price' in request.payload) throw new Error('manual price must not be written by the aggregate UI');
  console.log('V063_SINGLE_MEMBER_NO_MANUAL_PRICE_OK');
})();
'''
    assert run_node(script) == "V063_SINGLE_MEMBER_NO_MANUAL_PRICE_OK"


def test_member_table_renders_price_groups_without_editing_legacy_manual_price():
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab.js', 'utf8') + '\nthis.config = ConfigTab;';
const now = Date.now();
const members = [
  { id: 'am-normal', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-cheap', priority: 1, enabled: true, manual_price: 9 },
  { id: 'am-cooling', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-unset', priority: 2, enabled: true, cooldown_until: Math.floor(now / 1000) + 300 },
  { id: 'am-disabled', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-premium', priority: 3, enabled: false },
  { id: 'am-missing', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-missing', priority: 4, enabled: true },
];
const models = {
  'm-cheap': { id: 'm-cheap', name: 'cheap', upstream_model: 'up-cheap', usable: true, price_group: 'cheap', price_input: 0.0012, price_output: 0.0048 },
  'm-unset': { id: 'm-unset', name: 'unset', upstream_model: 'up-unset', usable: true, price_input: 0, price_output: 0 },
  'm-premium': { id: 'm-premium', name: 'premium', upstream_model: 'up-premium', usable: true, price_group: 'premium' },
};
const Store = {
  getAggregateMembers() { return members; },
  getGroup() { return { id: 'g1', name: '中转一组' }; },
  getModel(id) { return models[id]; },
};
const Utils = { escapeHtml(value) { return value == null ? '' : String(value); } };
const context = { console, Store, Utils, Date, Map, Number, String };
vm.runInNewContext(source, context);
const html = context.config.renderAggregateMembers({ id: 'a1' });
if (!html.includes('<th class="price-group-col">价格组</th>')) throw new Error('price group column missing');
if (!html.includes('cheap') || !html.includes('premium')) throw new Error('configured price groups missing');
if (!html.includes('aggregate-price-state">未设置')) throw new Error('unset price group state missing');
if (!html.includes('底层模型不存在')) throw new Error('missing model state missing');
if (html.includes('aggregate-member-price') || html.includes('manual_price') || html.includes('输入 ') || html.includes('输出 ')) throw new Error('removed price UI still rendered');
if (members[0].manual_price !== 9) throw new Error('legacy manual price was mutated');

const firstRow = context.config.renderAggregateMemberRow(members[0], 0, members.length);
const coolingRow = context.config.renderAggregateMemberRow(members[1], 1, members.length);
const disabledRow = context.config.renderAggregateMemberRow(members[2], 2, members.length);
const lastRow = context.config.renderAggregateMemberRow(members[3], members.length - 1, members.length);
if (!firstRow.includes('data-action="disable"') || !/data-action="up"[^>]*disabled/.test(firstRow)) throw new Error('normal first row actions missing');
if (!coolingRow.includes('data-action="recover"') || !coolingRow.includes('重试恢复')) throw new Error('cooling recovery action missing');
if (!disabledRow.includes('data-action="enable"')) throw new Error('disabled enable action missing');
if (!/data-action="down"[^>]*disabled/.test(lastRow)) throw new Error('last row down action must be disabled');
console.log('V063_MEMBER_PRICE_GROUP_RENDER_OK');
'''
    assert run_node(script) == "V063_MEMBER_PRICE_GROUP_RENDER_OK"
