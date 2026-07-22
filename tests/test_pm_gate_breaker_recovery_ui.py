"""PM Gate：智能熔断管理台恢复入口的最小渲染契约。"""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent


def test_breaker_recovery_controls_follow_runtime_health_state() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab.js', 'utf8') + '\nthis.config = ConfigTab;';
const now = Math.floor(Date.now() / 1000);
const models = {
  'm-breaker': { id: 'm-breaker', name: 'breaker', group_id: 'g1', usable: false, health_state: 'breaker_open', attempt_window: ['qualified_failure','qualified_failure','qualified_failure'], consecutive_failures: 3, breaker_level: 1, breaker_until: now + 300, breaker_reason: 'redacted_sha256:1234,bytes:9' },
  'm-probe': { id: 'm-probe', name: 'probe', group_id: 'g1', usable: false, health_state: 'half_open_probe', attempt_window: ['qualified_failure','qualified_failure','qualified_failure'], consecutive_failures: 3, breaker_level: 1, breaker_until: now + 300 },
  'm-manual': { id: 'm-manual', name: 'manual', group_id: 'g1', usable: false, disabled_by_user: true, health_state: 'breaker_open', attempt_window: ['qualified_failure','qualified_failure','qualified_failure'], consecutive_failures: 3, breaker_level: 1, breaker_until: now + 300 },
  'm-underlying': { id: 'm-underlying', name: 'underlying', group_id: 'g1', usable: false, health_state: 'breaker_open', breaker_until: now + 300 },
};
const Store = {
  selected: { type: 'model', id: 'm-breaker' },
  state: { groups: [{ id: 'g1', provider_type: 'relay' }], aggregate_members: [] },
  getModel(id) { return models[id]; },
  getGroup() { return { id: 'g1', name: '一组', provider_type: 'relay' }; },
  getAggregateMembers() { return []; },
};
const Utils = { escapeHtml(value) { return value == null ? '' : String(value); } };
const context = { console, Store, Utils, Date, Map, Set, Number, String };
vm.runInNewContext(source, context);
context.config._itemWithDraft = (_selection, item) => item;

const breakerModel = context.config.renderModelSection(Store.selected);
if (!breakerModel.includes('健康状态') || !breakerModel.includes('已熔断')) throw new Error('breaker model state missing');
if (!breakerModel.includes('近 5 次合格失败') || !breakerModel.includes('3 / 5')) throw new Error('failure count missing');
if (!breakerModel.includes('熔断等级') || !breakerModel.includes('第 1 档')) throw new Error('breaker level missing');
if (!breakerModel.includes('熔断截止') || !breakerModel.includes('脱敏原因')) throw new Error('breaker metadata missing');
if (!breakerModel.includes('redacted_sha256:1234,bytes:9')) throw new Error('redacted reason missing');
if (!breakerModel.includes('id="model-recover"')) throw new Error('breaker model recovery missing');

Store.selected = { type: 'model', id: 'm-probe' };
if (context.config.renderModelSection(Store.selected).includes('id="model-recover"')) throw new Error('half-open model must not recover');
Store.selected = { type: 'model', id: 'm-manual' };
if (context.config.renderModelSection(Store.selected).includes('id="model-recover"')) throw new Error('manual-disabled model must not recover');

const ownBreaker = { id: 'am-breaker', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-breaker', enabled: true, derived_status: 'breaker_open', health_state: 'breaker_open' };
const probeMember = { id: 'am-probe', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-probe', enabled: true, derived_status: 'half_open_probe' };
const manualMember = { id: 'am-manual', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-manual', enabled: false, derived_status: 'manual_disabled' };
const underlyingBreaker = { id: 'am-underlying', aggregate_id: 'a1', group_id: 'g1', model_id: 'm-underlying', enabled: true, derived_status: 'underlying_model_breaker_open', derived_reason: '底层模型熔断' };
const ownBreakerRow = context.config.renderAggregateMemberRow(ownBreaker, 0, 4);
const probeRow = context.config.renderAggregateMemberRow(probeMember, 1, 4);
const manualRow = context.config.renderAggregateMemberRow(manualMember, 2, 4);
const underlyingRow = context.config.renderAggregateMemberRow(underlyingBreaker, 3, 4);
if (!ownBreakerRow.includes('data-action="recover"')) throw new Error('own breaker member recovery missing');
if (probeRow.includes('data-action="recover"')) throw new Error('half-open member must not recover');
if (manualRow.includes('data-action="recover"')) throw new Error('manual-disabled member must not recover');
if (underlyingRow.includes('data-action="recover"')) throw new Error('underlying breaker must not use member recovery');
if (!underlyingRow.includes('底层已熔断') || !underlyingRow.includes('真实模型配置中重试恢复')) throw new Error('underlying breaker guidance missing');
if (underlyingRow.includes('>正常<')) throw new Error('underlying breaker must not render healthy');
console.log('PM_GATE_BREAKER_RECOVERY_UI_OK');
'''
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PM_GATE_BREAKER_RECOVERY_UI_OK"
