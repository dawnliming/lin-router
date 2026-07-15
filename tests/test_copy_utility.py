"""复制工具在权限受限浏览器中的回退契约。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_copy(*, clipboard: str, exec_result: bool) -> dict:
    script = r"""
const fs = require('fs');
const vm = require('vm');
const clipboardMode = process.argv[1];
const execResult = process.argv[2] === 'true';
const appended = [];
const context = {
  Promise,
  setTimeout,
  clearTimeout,
  document: {
    body: {
      appendChild(node) { appended.push(node); },
      removeChild() {},
    },
    createElement() { return { select() {} }; },
    execCommand() { return execResult; },
  },
};
if (clipboardMode === 'reject') {
  context.navigator = { clipboard: { writeText: async () => { throw new Error('denied'); } } };
}
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/utils.js', 'utf8') + '\nthis.Utils = Utils;', context);
context.Utils.copy('route-key').then(result => {
  console.log(JSON.stringify({ result, appended: appended.length }));
});
"""
    completed = subprocess.run(
        ["node", "-e", script, clipboard, str(exec_result).lower()],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    return json.loads(completed.stdout)


def test_copy_falls_back_when_clipboard_permission_is_rejected():
    result = run_copy(clipboard="reject", exec_result=True)

    assert result == {"result": True, "appended": 1}


def test_copy_reports_failure_when_compatibility_copy_fails():
    result = run_copy(clipboard="missing", exec_result=False)

    assert result == {"result": False, "appended": 1}


def test_copy_source_has_a_bounded_clipboard_wait():
    source = (ROOT / "static/js/utils.js").read_text(encoding="utf-8")

    assert "Promise.race" in source
    assert "1200" in source
