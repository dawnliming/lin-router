"""请求日志扫描与诊断的前端行为契约。"""

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_log_tab_diagnosis_and_multiline_summaries() -> None:
    logs_path = ROOT / "static" / "js" / "logs-tab.js"
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const context = {
  Utils: {
    escapeHtml: value => String(value ?? ''),
    redactSensitive: value => String(value ?? '')
      .replace(/(api[_-]?key\s*[:=]\s*)[^\s;,&}]+/gi, '$1[REDACTED]'),
  },
  Store: {
    state: { settings: {}, models: [], aggregate_models: [] },
    selected: {},
    getModel: () => null,
    getAggregate: () => null,
    getGroup: () => null,
  },
  console,
};
vm.runInNewContext(`${source}\nthis.LogsTab = LogsTab;`, context);
const logs = context.LogsTab;
const assert = (condition, message) => { if (!condition) throw new Error(message); };

const historicalAuth = {
  status: '200', event: 'stream_ok', request_id: 'request-1', attempt: 1,
  duration_ms: 2500, prompt_tokens: 100, completion_tokens: 20, cached_tokens: 50, total_tokens: 120,
  detail: 'final_result=stream_done; old_status=401; old_error=auth_error; previous=403; first_content_delta_ms=500',
};
assert(logs.diagnosisFor(historicalAuth, logs.parseDetail(historicalAuth.detail)).title === '请求成功', '2xx final record must win over historical auth text');
assert(logs.userFacingErrorReason(historicalAuth, logs.parseDetail(historicalAuth.detail)) === '请求成功', 'success preview must not repeat historical auth text');
assert(logs.eventSummary(historicalAuth) === '首包完成\n流式完成', 'stream event summary must have separate phases');
assert(logs.durationSummary(historicalAuth) === '首包：0.50 秒\n后续：2.00 秒\n总：2.50 秒', 'stream timing summary must have separate lines');
const completeFrameOnly = { ...historicalAuth, detail: 'first_complete_frame_ms=800' };
assert(logs.durationSummary(completeFrameOnly) === '首包：0.80 秒\n后续：1.70 秒\n总：2.50 秒', 'complete SSE frame timing must use the unified first-packet label');
assert(logs.tokenSummary(historicalAuth) === '输入：100\n输出：20\n命中：50（50%）\n总计：120', 'token summary must have separate lines');
const tokenSummaryHtml = logs.tokenSummaryHtml(historicalAuth);
assert(tokenSummaryHtml.includes('<span class="log-token-hit">命中：50<span class="log-token-hit-rate">（50%）</span></span>'), 'token hit rate must be kept on one line');
assert(!tokenSummaryHtml.includes('命中率'), 'token hit summary must only keep the percentage in parentheses');

const current401 = { status: '401', event: 'error', detail: 'failure_scope=candidate; request_level=true' };
assert(logs.diagnosisFor(current401, logs.parseDetail(current401.detail)).title === '鉴权失败', 'current 401 must remain auth failure');
const structuredAuth = { status: 'error', event: 'error', detail: 'failure_scope=candidate; error_code=auth_error' };
assert(logs.diagnosisFor(structuredAuth, logs.parseDetail(structuredAuth.detail)).title === '鉴权失败', 'structured auth code must remain auth failure');

const initialStream = { ...historicalAuth, status: 'streaming', detail: 'stream_started_at_ms=1; final_result=streaming' };
assert(logs.rowKey(initialStream) === logs.rowKey(historicalAuth), 'stream lifecycle update must keep row identity');
assert(logs.formatDetailPreview(initialStream) === '首完整帧成功，流式记录已中断（未记录终态）', 'persisted streaming history must not be previewed as active');
const activeStream = { ...initialStream, request_id: 'request-live' };
context.Store.state.live_requests = [{ request_id: 'request-live' }];
assert(logs.formatDetailPreview(activeStream) === '首完整帧成功，流式响应仍在进行', 'only a live request may be previewed as active');
context.Store.state.live_requests = [];
const recoveredStream = { ...historicalAuth, status: 'interrupted', event: 'stream_interrupted', detail: 'stream_started_at_ms=1; stream_finalized=true; lifecycle=stream_interrupted_after_restart; final_result=interrupted; recovery=recovered_after_restart' };
assert(logs.streamTerminalLabel(recoveredStream, logs.parseDetail(recoveredStream.detail)) === '服务重启后中断', 'recovered stream must not be shown as in progress');
assert(logs.eventSummary(recoveredStream).includes('服务重启后中断'), 'recovered stream summary must be terminal');
assert(logs.diagnosisFor(recoveredStream, logs.parseDetail(recoveredStream.detail)).title === '服务重启后流已中断', 'recovered stream must have restart diagnosis');
assert(logs.safeEndpoint('https://relay.example/v1/chat/completions?token=secret') === '/v1/chat/completions', 'endpoint display must hide upstream origin and query');
assert(!logs.redactDiagnosticEvidence('out_headers=(Authorization=Bearer secret); upstream_endpoint=https://relay.example/v1?token=secret').includes('secret'), 'detail evidence must redact headers and full URL');
'''
    result = subprocess.run(
        ["node", "-e", script, str(logs_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_log_tab_keeps_current_only_pagination_and_multiline_styles() -> None:
    logs_js = (ROOT / "static" / "js" / "logs-tab.js").read_text(encoding="utf-8")
    logs_css = (ROOT / "static" / "css" / "logs-tab.css").read_text(encoding="utf-8")

    assert "setCurrentOnly(enabled)" in logs_js
    assert "shouldUseLocalCurrentOnlyPagination()" in logs_js
    assert "this.total = filtered.length" in logs_js
    assert "this._openDetailKey = willOpen ? key : '';" in logs_js
    assert "item.attempt || 0" in logs_js
    assert ".logs-table td.log-multiline" in logs_css
    assert "white-space: pre-line" in logs_css
    status_column = re.search(r"\.logs-table th:nth-child\(4\)\s*\{([^}]*)\}", logs_css)
    assert status_column and "width: 100px" in status_column.group(1)
    multiline_summary = re.search(r"\.logs-table td\.log-multiline\s*\{([^}]*)\}", logs_css)
    assert multiline_summary and "vertical-align: middle" in multiline_summary.group(1)
    assert ".logs-table td:nth-child(4) .pill" in logs_css
    assert ".log-token-hit" in logs_css
    assert "white-space: nowrap" in logs_css
