"""路由策略配置页的四态和合并保存契约。"""
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


def test_policy_forms_render_all_four_states_and_canonical_payload_fields() -> None:
    config = (ROOT / "static/js/config-tab.js").read_text(encoding="utf-8")
    actions = (ROOT / "static/js/config-tab-actions.js").read_text(encoding="utf-8")

    for policy in ("smart_breaker", "fixed_cooldown", "sticky_route", "cooldown_off"):
        assert config.count(f'value="{policy}"') == 2
    assert 'id="group-cooldown"' in config
    assert 'id="aggregate-cooldown"' in config
    assert 'group-fixed-cooldown-minutes' not in config
    assert 'aggregate-fixed-cooldown-minutes' not in config
    assert "routing_policy:" in actions
    assert "fixed_cooldown_minutes:" not in actions
    assert "smart_breaker_enabled: document.getElementById('group-smart-breaker-enabled')" not in actions
    assert "smart_breaker_enabled: document.getElementById('aggregate-smart-breaker-enabled')" not in actions


def test_policy_auto_save_merges_edits_after_500ms_and_never_creates_new_object() -> None:
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/js/config-tab-form.js', 'utf8') + '\nthis.form = ConfigTabForm;';
const savedForm = { isConnected: true };
const context = {
  Store: { selected: { type: 'group', id: 'g1' } },
  document: {
    querySelector() { return savedForm; },
    getElementById() { return null; },
    querySelectorAll() { return []; },
  },
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(source, context);
let saves = 0;
const controller = {
  setSaveStatus() {},
  async onGroupSubmit(event) {
    if (!event.autoSave) throw new Error('automatic save marker missing');
    saves += 1;
  },
};
context.form.scheduleAutoSave(controller, savedForm);
context.form.scheduleAutoSave(controller, savedForm);
setTimeout(() => {
  if (saves !== 1) throw new Error(`expected one merged save, got ${saves}`);
  context.Store.selected = { type: 'group', id: '' };
  context.form.scheduleAutoSave(controller, savedForm);
  setTimeout(() => {
    if (saves !== 1) throw new Error('new draft must not be auto-created');
    console.log('ROUTING_POLICY_AUTO_SAVE_OK');
  }, 550);
}, 550);
'''
    assert _run_node(script) == "ROUTING_POLICY_AUTO_SAVE_OK"
