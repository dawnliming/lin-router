const LogsTab = {
  filters: { start: '', end: '', group: '', status: '' },
  currentOnly: false,
  autoRefresh: true,
  refreshTimer: null,
  REFRESH_INTERVAL: 5000,
  _lastRenderSignature: '',

  refresh() {
    const panel = document.getElementById('panel-logs');
    if (!panel) return;
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-logs');
    panel.innerHTML = `
      <div class="logs-header">
        <h2>最近请求</h2>
        <div class="logs-actions">
          <label class="checkbox">
            <input type="checkbox" id="logs-auto-refresh" ${this.autoRefresh ? 'checked' : ''}>
            <span>自动刷新</span>
          </label>
          <button type="button" id="logs-refresh" class="btn-secondary" title="立即刷新">🔄 刷新</button>
          <label class="checkbox">
            <input type="checkbox" id="logs-current-only" ${this.currentOnly ? 'checked' : ''}>
            <span>仅显示当前选中组/模型</span>
          </label>
          <button type="button" id="logs-clear" class="btn-secondary">清空日志</button>
          <button type="button" id="logs-export" class="btn-secondary">导出 CSV</button>
        </div>
      </div>
      <div class="logs-filters">
        <div class="filter-field">
          <label>开始时间</label>
          <input type="datetime-local" id="log-start" value="${this.filters.start}">
        </div>
        <div class="filter-field">
          <label>结束时间</label>
          <input type="datetime-local" id="log-end" value="${this.filters.end}">
        </div>
        <div class="filter-field">
          <label>连接组</label>
          <select id="log-group">${this.renderGroupOptions()}</select>
        </div>
        <div class="filter-field">
          <label>状态</label>
          <select id="log-status">
            <option value="">全部</option>
            <option value="2xx" ${this.filters.status === '2xx' ? 'selected' : ''}>2xx 成功</option>
            <option value="cooldown" ${this.filters.status === 'cooldown' ? 'selected' : ''}>冷却/切换/重试</option>
            <option value="error" ${this.filters.status === 'error' ? 'selected' : ''}>错误</option>
          </select>
        </div>
      </div>
      <div class="logs-table-wrap">
        <table class="logs-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>组</th>
              <th>模型</th>
              <th>状态</th>
              <th>事件</th>
              <th>
                <span class="help-tip" title="同请求重试次数，首次请求为 1">请求#次 ?</span>
              </th>
              <th>耗时</th>
              <th>Token</th>
              <th>详情</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="log-tbody"></tbody>
        </table>
      </div>
      <div id="logs-empty" class="logs-empty hidden">
        <div>暂无符合条件的日志</div>
        <button type="button" id="logs-reset" class="btn-secondary">重置筛选</button>
      </div>
    `;
    this.attachEvents(panel);
    this._lastRenderSignature = '';
    this.renderRows();
    this.startAutoRefresh();
  },

  renderGroupOptions() {
    const groups = Store.state.groups || [];
    return '<option value="">全部</option>' + groups.map(g =>
      `<option value="${g.id}" ${g.id === this.filters.group ? 'selected' : ''}>${Utils.escapeHtml(g.name)}</option>`
    ).join('');
  },

  attachEvents(panel) {
    ['log-start', 'log-end', 'log-group', 'log-status'].forEach(id => {
      panel.querySelector(`#${id}`)?.addEventListener('change', () => this.readFilters());
    });
    panel.querySelector('#logs-current-only')?.addEventListener('change', e => { this.currentOnly = e.target.checked; this.renderRows(); });
    panel.querySelector('#logs-auto-refresh')?.addEventListener('change', e => { this.setAutoRefresh(e.target.checked); });
    panel.querySelector('#logs-refresh')?.addEventListener('click', () => this.manualRefresh());
    panel.querySelector('#logs-clear')?.addEventListener('click', () => this.clear());
    panel.querySelector('#logs-export')?.addEventListener('click', () => { location.href = '/api/logs/export'; });
    panel.querySelector('#logs-reset')?.addEventListener('click', () => this.resetFilters());
  },

  setAutoRefresh(enabled) {
    this.autoRefresh = enabled;
    if (enabled) this.startAutoRefresh();
    else this.stopAutoRefresh();
  },

  startAutoRefresh() {
    this.stopAutoRefresh();
    if (!this.autoRefresh) return;
    this.refreshTimer = setInterval(() => this.autoRefreshTick(), this.REFRESH_INTERVAL);
  },

  stopAutoRefresh() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  },

  async autoRefreshTick() {
    if (!this.autoRefresh) return;
    // 只在当前是 logs tab 时刷新
    if (Tabs.current !== 'logs') return;
    // 自动刷新使用静默模式，不触发全局 loading 遮罩
    await this.manualRefresh(true);
  },

  async manualRefresh(silent = false) {
    try {
      const data = await API.req('/api/state', { silent });
      // 同步最新的模型状态，确保配置页能正确显示冷却截止时间等信息
      Store.update({ logs: data.logs, models: data.models, groups: data.groups });
      this.renderRows(true);
    } catch (err) {
      // 自动刷新失败不弹 Toast，避免打扰
      if (!silent) Toast.error('刷新失败：' + err.message);
      console.error('日志刷新失败', err);
    }
  },

  readFilters() {
    this.filters.start = document.getElementById('log-start')?.value || '';
    this.filters.end = document.getElementById('log-end')?.value || '';
    this.filters.group = document.getElementById('log-group')?.value || '';
    this.filters.status = document.getElementById('log-status')?.value || '';
    this.renderRows();
  },

  filterLogs() {
    const logs = Store.state.logs || [];
    return logs.filter(item => this.matches(item) && !this.isStableConfigSkip(item));
  },

  isStableConfigSkip(item) {
    if (!item || item.event !== 'skip') return false;
    const parsed = this.parseDetail(item.detail);
    return ['member_disabled', 'member_cooling', 'underlying_model_disabled', 'underlying_model_cooling'].includes(parsed.skip_reason);
  },

  requestRelatedLogs(item) {
    if (!item?.request_id) return [];
    return (Store.state.logs || []).filter(log => log.request_id === item.request_id && log !== item);
  },

  resetFilters() {
    this.filters = { start: '', end: '', group: '', status: '' };
    this.render();
  },

  matches(item) {
    const start = this.dateValue(this.filters.start);
    const end = this.dateValue(this.filters.end);
    const t = this.itemTime(item);
    if (start && t < start) return false;
    if (end && t > end) return false;
    if (this.filters.group && item.group_id !== this.filters.group) return false;
    if (this.currentOnly) {
      const sel = Store.selected;
      if (sel.type === 'group' && item.group_id !== sel.id) return false;
      if (sel.type === 'model' && item.model !== Store.getModel(sel.id)?.name) return false;
    }
    const status = String(item.status || '');
    const event = String(item.event || '');
    if (this.filters.status === '2xx' && !status.startsWith('2')) return false;
    if (this.filters.status === 'cooldown' && !['cooldown', 'fallback', 'retry_ok'].includes(event)) return false;
    if (this.filters.status === 'error' && (status.startsWith('2') || ['cooldown', 'fallback', 'retry_ok'].includes(event))) return false;
    return true;
  },

  dateValue(v) { return v ? new Date(v).getTime() : 0; },
  itemTime(item) { return item.time ? new Date(String(item.time).replace(' ', 'T')).getTime() : 0; },

  groupName(item) {
    return item.group_name || Store.getGroup(item.group_id)?.name || '-';
  },

  eventLabel(event, item = null) {
    if (event === 'skip' && item) {
      const parsed = this.parseDetail(item.detail);
      if (parsed.skip_reason === 'member_disabled') return '已停用成员未参与';
    }
    const map = { ok:'成功', stream_ok:'首包成功', retry_ok:'重试成功', cooldown:'冷却切换', fallback:'自动切换', skip:'跳过', network:'网络错误', error:'错误', system:'系统', stream_timeout:'流式超时', waf_lock_timeout:'候选忙', stream_done:'流式完成', stream_idle_timeout:'流式空闲超时', client_disconnected:'客户端断开', manual_probe:'人工探测' };
    return map[event] || event || '-';
  },

  renderPayloadWarnings(parsed) {
    const warnings = [];
    const labels = {
      payload_large: 'body 较大',
      payload_very_large: 'body 很大',
      tools_large: 'tools 很大',
      tool_results_large: 'tool_results 很大',
      messages_many: 'messages 过多'
    };
    Object.entries(labels).forEach(([key, label]) => {
      if (parsed[key] === 'true') warnings.push(label);
    });
    return warnings.length ? `<span class="pill warning">${Utils.escapeHtml(warnings.join(' / '))}</span>` : '-';
  },

  statusLabel(status) {
    const value = String(status || '');
    const map = {
      probe_ok: '探测成功',
      probe_failed: '探测失败',
      network: '网络错误',
      timeout: '请求超时',
      busy: '候选忙',
    };
    return map[value] || value || '-';
  },

  statusClass(status, item = null) {
    if (item && this.isStableConfigSkip(item)) return 'info';
    const text = String(status || '');
    if (text === '200' || text.startsWith('2')) return 'success';
    if (text === 'network' || text.includes('failed') || text.startsWith('5')) return 'error';
    return 'warning';
  },

  tokenSummary(item) {
    const input = Number(item.prompt_tokens || 0);
    const output = Number(item.completion_tokens || 0);
    const total = Number(item.total_tokens || 0);
    const cached = Number(item.cached_tokens || 0);
    if (!total && !input && !output && !cached) return '-';
    const hit = input ? Math.round((cached / input) * 100) : 0;
    return `输入 ${input} / 输出 ${output} / 命中 ${cached} (${hit}%) / total ${total}`;
  },

  parseDetail(detail) {
    const result = {};
    if (!detail) return result;
    const regex = /(?:^|;\s*)([^=;]+)=([^;]*)/g;
    let match;
    while ((match = regex.exec(detail)) !== null) {
      result[match[1].trim()] = match[2].trim();
    }
    return result;
  },

  renderRows(keepScroll = false) {
    const tbody = document.getElementById('log-tbody');
    const empty = document.getElementById('logs-empty');
    const wrap = document.querySelector('.logs-table-wrap');
    if (!tbody) return;
    const filtered = this.filterLogs();
    const signature = filtered.map(item => this.rowSignature(item)).join('\n');
    if (signature === this._lastRenderSignature && tbody.children.length > 0) return;
    this._lastRenderSignature = signature;
    const wasAtBottom = keepScroll && wrap ? (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 30) : false;
    const scrollTop = wrap ? wrap.scrollTop : 0;
    const openKeys = new Set(Array.from(tbody.querySelectorAll('[data-log-detail-row]'))
      .filter(row => !row.classList.contains('hidden'))
      .map(row => row.dataset.logDetailRow));

    if (filtered.length === 0) {
      tbody.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');

    const seen = new Set();
    let cursor = tbody.firstChild;
    filtered.forEach((item, idx) => {
      const key = this.rowKey(item);
      seen.add(key);
      const temp = document.createElement('tbody');
      temp.innerHTML = this.rowHtml(item, idx, key).trim();
      const nextMain = temp.children[0];
      const nextDetail = temp.children[1];
      const currentMain = tbody.querySelector(`[data-log-main-row="${CSS.escape(key)}"]`);
      const currentDetail = tbody.querySelector(`[data-log-detail-row="${CSS.escape(key)}"]`);
      if (openKeys.has(key)) nextDetail.classList.remove('hidden');
      if (currentMain && currentDetail) {
        if (currentMain.outerHTML !== nextMain.outerHTML) currentMain.replaceWith(nextMain);
        if (currentDetail.outerHTML !== nextDetail.outerHTML) currentDetail.replaceWith(nextDetail);
        const main = tbody.querySelector(`[data-log-main-row="${CSS.escape(key)}"]`);
        const detail = tbody.querySelector(`[data-log-detail-row="${CSS.escape(key)}"]`);
        if (main !== cursor) tbody.insertBefore(main, cursor);
        cursor = main.nextSibling;
        if (detail !== cursor) tbody.insertBefore(detail, cursor);
        cursor = detail.nextSibling;
      } else {
        tbody.insertBefore(nextMain, cursor);
        tbody.insertBefore(nextDetail, cursor);
        cursor = nextDetail.nextSibling;
      }
    });

    Array.from(tbody.querySelectorAll('[data-log-main-row]')).forEach(row => {
      const key = row.dataset.logMainRow;
      if (!seen.has(key)) row.remove();
    });
    Array.from(tbody.querySelectorAll('[data-log-detail-row]')).forEach(row => {
      const key = row.dataset.logDetailRow;
      if (!seen.has(key)) row.remove();
    });

    tbody.querySelectorAll('[data-log-detail-key]').forEach(btn => {
      btn.addEventListener('click', () => this.toggleDetailByKey(btn.dataset.logDetailKey));
    });
    tbody.querySelectorAll('[data-log-detail-preview-key]').forEach(cell => {
      cell.addEventListener('click', () => this.toggleDetailByKey(cell.dataset.logDetailPreviewKey));
    });

    if (wasAtBottom && wrap) wrap.scrollTop = wrap.scrollHeight;
    else if (wrap) wrap.scrollTop = scrollTop;
  },

  rowKey(item) {
    return [item.request_id || item.time || '', item.event || '', item.fallback_index || 0, item.aggregate_member_id || '', item.status || ''].join('|');
  },

  rowSignature(item) {
    return [this.rowKey(item), item.time, item.status, item.event, item.duration_ms, item.detail, item.prompt_tokens, item.completion_tokens, item.cached_tokens, item.total_tokens, Store.state.settings?.debug_mode === true ? 'debug' : 'normal'].join('|');
  },

  rowHtml(item, idx, key = this.rowKey(item)) {
    return `
      <tr data-log-main-row="${Utils.escapeHtml(key)}">
        <td class="tiny">${Utils.escapeHtml(item.time)}</td>
        <td class="tiny">${Utils.escapeHtml(this.groupName(item))}</td>
        <td>${Utils.escapeHtml(item.model || '-')}</td>
        <td><span class="pill ${this.statusClass(item.status, item)}">${Utils.escapeHtml(this.statusLabel(item.status))}</span></td>
        <td class="tiny">${Utils.escapeHtml(this.eventLabel(item.event, item))}</td>
        <td class="tiny">${Number(item.attempt || 0) || 1}</td>
        <td class="tiny">${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</td>
        <td class="tiny">${Utils.escapeHtml(this.tokenSummary(item))}</td>
        <td class="tiny result-text log-detail-preview" title="${Utils.escapeHtml(this.formatDetailPreview(item))}" data-log-detail-preview-key="${Utils.escapeHtml(key)}">${Utils.escapeHtml(this.formatDetailPreview(item))}</td>
        <td><button type="button" data-log-detail-key="${Utils.escapeHtml(key)}">查看</button></td>
      </tr>
      <tr class="log-detail-row hidden" data-log-detail-row="${Utils.escapeHtml(key)}">
        <td colspan="10">${this.detailHtml(item)}</td>
      </tr>
    `;
  },

  toggleDetailByKey(key) {
    const row = document.querySelector(`[data-log-detail-row="${CSS.escape(key)}"]`);
    if (!row) return;
    const willOpen = row.classList.contains('hidden');
    document.querySelectorAll('[data-log-detail-row]').forEach(r => r.classList.add('hidden'));
    row.classList.toggle('hidden', !willOpen);
  },

  formatDetailPreview(item) {
    const parsed = this.parseDetail(item?.detail);
    const event = String(item?.event || '');
    if (event === 'manual_probe') {
      return String(item?.status || '') === 'probe_ok'
        ? '最小探测成功，候选已恢复参与调度'
        : '最小探测未通过，候选保持冷却';
    }
    if (event === 'stream_ok') return '首包成功，流式响应仍在进行';
    if (event === 'stream_done' || event === 'stream_finalized') return '流式响应已完成';
    if (event === 'client_disconnected') return '客户端已断开连接';
    if (event === 'waf_lock_timeout') return '候选忙，等待锁超时后已切换';
    return this.userFacingErrorReason(item || {}, parsed);
  },

  formatJsonBlock(value) {
    if (!value) return '-';
    const str = Utils.redactSensitive(String(value));
    // 尝试把分号键值对或 JSON 字符串格式化显示
    let formatted = Utils.escapeHtml(str);
    // 如果有 model=... 这类键值对，高亮关键 key
    formatted = formatted.replace(/(requested|group_name|model|upstream|channel|mode|error)=/g, '<strong>$1=</strong>');
    return formatted;
  },

  shortId(value, len = 8) {
    const text = String(value || '-');
    if (text === '-') return text;
    return text.length > len ? text.slice(0, len) : text;
  },

  detailSummary(detail, maxLen = 500) {
    if (!detail) return '-';
    const text = Utils.redactSensitive(String(detail)).replace(/;/g, '; ');
    const clipped = text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
    return Utils.escapeHtml(clipped);
  },

  firstValue(...values) {
    for (const value of values) {
      if (value !== undefined && value !== null && value !== '') return value;
    }
    return '-';
  },

  fallbackChainValue(parsed, item) {
    return parsed.fallback_chain || item.fallback_chain || parsed.selection_trace || item.selection_trace || '-';
  },


  skipReasonLabel(reason) {
    const map = {
      member_disabled: '已停用成员，信息级',
      member_cooling: '成员冷却中，健康过滤',
      underlying_model_disabled: '底层模型停用，配置过滤',
      underlying_model_cooling: '底层模型冷却中，健康过滤',
      underlying_model_missing: '底层模型不存在，配置异常',
      underlying_group_missing: '底层连接组不存在，配置异常'
    };
    return map[reason] || reason || '-';
  },

  runtimeReasonLabel(parsed, item) {
    const reason = parsed.fallback_reason || parsed.skip_reason || parsed.reason || '';
    if (reason === 'large_task_in_progress') return '候选正在处理大上下文请求，调度切换';
    if (reason === 'candidate_busy') return '候选忙/等待锁超时，调度切换';
    if (reason === 'stream_idle_timeout') return '上游流式空闲超时，健康失败';
    if (String(item.event || '') === 'waf_lock_timeout') return '候选忙/等待锁超时，调度切换';
    if (String(item.failure_scope || parsed.failure_scope || '') === 'upstream') return '上游超时/错误，健康失败';
    return this.skipReasonLabel(reason);
  },

  renderCandidateFilterDetails(item) {
    const related = this.requestRelatedLogs(item)
      .map(log => ({ log, parsed: this.parseDetail(log.detail) }))
      .filter(entry => {
        const event = String(entry.log.event || '');
        return event === 'skip' || event === 'waf_lock_timeout' || event === 'stream_timeout' || event === 'cooldown' || event === 'fallback' || event === 'network';
      });
    if (!related.length) return '';
    const rows = related.map(({ log, parsed }) => {
      const isStable = ['member_disabled', 'member_cooling', 'underlying_model_disabled', 'underlying_model_cooling'].includes(parsed.skip_reason);
      const severity = isStable ? 'info' : (log.cooldown_applied || log.failure_scope === 'upstream' ? 'warning' : 'info');
      const reason = parsed.skip_reason ? this.skipReasonLabel(parsed.skip_reason) : this.runtimeReasonLabel(parsed, log);
      return `
        <tr>
          <td class="tiny"><span class="pill ${severity}">${Utils.escapeHtml(severity === 'info' ? '信息' : '调度')}</span></td>
          <td>${Utils.escapeHtml(log.model || parsed.selected_model || '-')}</td>
          <td>${Utils.escapeHtml(this.eventLabel(log.event, log))}</td>
          <td>${Utils.escapeHtml(reason)}</td>
          <td class="tiny">${Utils.escapeHtml(log.failure_scope || parsed.failure_scope || '-')}</td>
          <td class="tiny">${log.cooldown_applied ? '是' : '否'}</td>
        </tr>
      `;
    }).join('');
    return `
      <div class="log-detail-block" style="margin-top:10px;">
        <h4>候选过滤详情</h4>
        <div class="aggregate-members-table-wrap">
          <table class="aggregate-members-table">
            <thead><tr><th>级别</th><th>候选</th><th>事件</th><th>原因</th><th>Scope</th><th>冷却</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    `;
  },

  renderWafHint(parsed) {
    if (parsed.waf_blocked !== 'true') return '';
    const message = '上游中转站拦截了本次请求。';
    const suggestion = parsed.suggestion || '';
    const wafOn = parsed.waf_compatible === 'true';
    const typeClass = wafOn ? 'log-waf-hint-open' : 'log-waf-hint-closed';
    const icon = wafOn ? '⚠️' : '🛡️';
    const title = wafOn ? 'WAF 已开启但仍被拦截' : 'WAF 兼容未开启';
    return `
      <div class="log-waf-hint ${typeClass}">
        <div class="log-waf-hint-title">${icon} ${Utils.escapeHtml(title)}</div>
        <div class="log-waf-hint-message">${Utils.escapeHtml(message)}</div>
        ${suggestion ? `<div class="log-waf-hint-suggestion"><strong>建议：</strong>${Utils.escapeHtml(suggestion)}</div>` : ''}
      </div>
    `;
  },

  diagnosisFor(item, parsed) {
    const text = `${item.status || ''} ${item.event || ''} ${item.failure_scope || ''} ${item.detail || ''}`.toLowerCase();
    if (text.includes('waf_lock_wait_timeout') || text.includes('candidate_busy') || text.includes('large_task_in_progress')) {
      return { className: 'warning', title: '候选忙 / 等待锁超时', scope: 'local_lock', cooldown: '否', suggestion: '候选正在处理大上下文请求，系统会临时切换到下一个候选；通常无需清冷却。' };
    }
    if (text.includes('stream_idle_timeout')) {
      return { className: 'danger', title: '上游流式响应空闲超时', scope: 'upstream', cooldown: item.cooldown_applied ? '是' : '可能', suggestion: '建议稍后重试，或对冷却中的单个模型/成员点击“重试恢复”。' };
    }
    if ((text.includes('timeout') || text.includes('read_timeout')) && !text.includes('waf_lock')) {
      return { className: 'danger', title: '上游请求超时', scope: 'upstream', cooldown: item.cooldown_applied ? '是' : '可能', suggestion: '若频繁出现，检查中转站状态；确认恢复后可单点重试恢复。' };
    }
    if (text.includes('waf_blocked') || text.includes('request_level') || text.includes('upstream_request_rejected')) {
      return { className: 'warning', title: '请求级错误 / 上游拒绝', scope: 'request', cooldown: '否', suggestion: '请检查请求参数、内容策略或 WAF 兼容设置；这不会被诊断为模型健康失败。' };
    }
    if (text.includes('auth_error') || text.includes('401') || text.includes('403')) {
      return { className: 'danger', title: '鉴权失败', scope: 'candidate', cooldown: '否', suggestion: '检查连接组或模型的 API Key / Route Key 是否正确。' };
    }
    if (text.includes('rate_limit') || text.includes('429')) {
      return { className: 'warning', title: '上游限流', scope: 'upstream', cooldown: item.cooldown_applied ? '是' : '否', suggestion: '稍后重试，或临时切换到其他候选。' };
    }
    if (text.includes('server_error') || text.includes('network') || String(item.status || '').startsWith('5')) {
      return { className: 'danger', title: '上游健康失败', scope: item.failure_scope || parsed.failure_scope || 'upstream', cooldown: item.cooldown_applied ? '是' : '否', suggestion: '真实上游故障会进入冷却；确认恢复后可单点重试。' };
    }
    if (String(item.status || '').startsWith('2')) {
      return { className: 'success', title: '请求成功', scope: item.failure_scope || '-', cooldown: '否', suggestion: '无需处理。' };
    }
    return { className: 'info', title: '需要关注', scope: item.failure_scope || parsed.failure_scope || 'request', cooldown: item.cooldown_applied ? '是' : '否', suggestion: '请结合候选过滤详情和脱敏摘要继续排查。' };
  },

  renderDiagnosisCard(item, parsed) {
    const d = this.diagnosisFor(item, parsed);
    return `
      <div class="diagnosis-card ${d.className}">
        <div>
          <div class="diagnosis-eyebrow">智能诊断</div>
          <strong>${Utils.escapeHtml(d.title)}</strong>
          <p>${Utils.escapeHtml(d.suggestion)}</p>
        </div>
        <dl>
          <dt>影响范围</dt><dd>${Utils.escapeHtml(this.failureScopeLabel(d.scope))}</dd>
          <dt>触发冷却</dt><dd>${Utils.escapeHtml(d.cooldown)}</dd>
        </dl>
      </div>`;
  },

  userFacingErrorReason(item, parsed) {
    const text = `${item.status || ''} ${item.event || ''} ${item.failure_scope || ''} ${item.detail || ''}`.toLowerCase();
    if (text.includes('waf_lock_wait_timeout') || text.includes('candidate_busy') || text.includes('large_task_in_progress')) return '候选正在处理请求，等待 WAF 锁超时后已尝试切换';
    if (text.includes('stream_idle_timeout')) return '上游流式响应长时间无数据，已判定为空闲超时';
    if (text.includes('read_timeout') || (text.includes('timeout') && !text.includes('waf_lock'))) return '上游响应超时';
    if (text.includes('waf_blocked')) return '上游中转站的 WAF 拦截了请求';
    if (text.includes('auth_error') || text.includes('401') || text.includes('403')) return '上游鉴权失败，请检查 API Key 或权限';
    if (text.includes('rate_limit') || text.includes('429')) return '上游触发限流，请稍后重试';
    if (text.includes('request_level') || text.includes('upstream_request_rejected')) return '请求参数或内容策略被上游拒绝';
    if (text.includes('network')) return '连接上游网络失败';
    if (text.includes('server_error') || String(item.status || '').startsWith('5')) return '上游服务暂时异常';
    if (String(item.status || '').startsWith('2')) return '请求成功';
    if (parsed.skip_reason) return this.skipReasonLabel(parsed.skip_reason);
    return '请求未完成，请查看下方诊断与技术细节';
  },

  userFacingFallbackReason(item, parsed) {
    const reason = String(this.firstValue(parsed.fallback_reason, parsed.selection_reason, parsed.skip_reason, '') || '');
    const map = {
      priority_first: '按优先级选择首个候选',
      fallback_after_failure: '前一候选失败后自动切换',
      large_task_in_progress: '候选正在处理大上下文请求，已切换',
      candidate_busy: '候选忙 / 等待锁超时，已切换',
      member_disabled: '已停用成员未参与',
      member_cooling: '成员冷却中，未参与',
      underlying_model_disabled: '底层模型已停用，未参与',
      underlying_model_cooling: '底层模型冷却中，未参与'
    };
    return map[reason] || (reason ? '系统已根据当前调度状态处理候选切换' : '-');
  },

  failureScopeLabel(scope) {
    const map = {
      upstream: '上游服务',
      request: '本次请求',
      busy: '候选繁忙',
      local_lock: '本地候选锁',
      candidate: '单个候选',
      manual: '人工探测',
    };
    const value = String(scope || '');
    if (!value || value === '-') return '-';
    return map[value] || (/[\u4e00-\u9fff]/.test(value) ? value : '其他');
  },

  renderTechnicalDetails(detail) {
    if (!detail) return '';
    return `
      <details class="log-technical-details">
        <summary>技术细节（已脱敏）</summary>
        <div class="log-detail-raw">${this.detailSummary(detail, 500)}</div>
      </details>
    `;
  },
  detailHtml(item) {
    const parsed = this.parseDetail(item.detail);
    const debugMode = Store.state.settings?.debug_mode === true;
    const safeDetail = item.detail ? Utils.redactSensitive(String(item.detail)) : '';
    const wafHint = this.renderWafHint(parsed);
    const isAggregate = parsed.resolved_as && parsed.resolved_as.startsWith('aggregate');
    const routeSteps = isAggregate ? this.aggregateRouteSteps(parsed, item) : [
      parsed.requested ? Utils.escapeHtml(parsed.requested) : Utils.escapeHtml(item.requested_model || item.model || 'lin-router-auto'),
      parsed.group_name ? Utils.escapeHtml(parsed.group_name) : Utils.escapeHtml(this.groupName(item)),
      parsed.model ? Utils.escapeHtml(parsed.model) : Utils.escapeHtml(item.selected_model || item.model || '-'),
      parsed.upstream ? Utils.escapeHtml(parsed.upstream) : Utils.escapeHtml(parsed.selected_upstream_model || '-'),
    ];
    const aggregateChain = isAggregate ? this.renderAggregateChain(parsed) : '';
    const candidateFilterDetails = this.renderCandidateFilterDetails(item);
    const diagnosisCard = this.renderDiagnosisCard(item, parsed);
    const requestIdDisplay = debugMode ? (item.request_id || '-') : this.shortId(item.request_id || '-');
    const memberIdDisplay = debugMode ? this.firstValue(item.aggregate_member_id, parsed.aggregate_member_id) : this.shortId(this.firstValue(item.aggregate_member_id, parsed.aggregate_member_id));
    const errorReason = this.userFacingErrorReason(item, parsed);
    const fallbackReason = this.userFacingFallbackReason(item, parsed);
    const cooldownApplied = this.firstValue(item.cooldown_applied, parsed.cooldown_applied, parsed.cooldown_reason ? 'true' : 'false');
    const deepDiagnostics = debugMode ? `
      <div class="log-detail-block log-detail-raw-block">
        <h4>调试模式：深度诊断（已脱敏）</h4>
        <dl>
          <dt>完整 Request ID</dt><dd>${Utils.escapeHtml(item.request_id || '-')}</dd>
          <dt>完整 Member ID</dt><dd>${Utils.escapeHtml(this.firstValue(item.aggregate_member_id, parsed.aggregate_member_id))}</dd>
          <dt>Fallback Chain</dt><dd class="log-detail-raw">${this.formatJsonBlock(this.fallbackChainValue(parsed, item))}</dd>
          <dt>详情原文</dt><dd class="log-detail-raw">${this.formatJsonBlock(safeDetail || '-')}</dd>
          <dt>上游 HTTP 版本</dt><dd>${Utils.escapeHtml(parsed.upstream_http_version || '-')}</dd>
          <dt>Header Policy</dt><dd>${Utils.escapeHtml(parsed.header_policy || '-')}</dd>
          <dt>Body Fingerprint</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.fingerprint, parsed.body_fingerprint))}</dd>
          <dt>HTTP 客户端</dt><dd>${Utils.escapeHtml(parsed.http_client || '-')}</dd>
          <dt>Tools 排序</dt><dd>${Utils.escapeHtml(parsed.tools_normalized || '-')}</dd>
        </dl>
      </div>
    ` : '';
    return `
      ${wafHint}
      ${diagnosisCard}
      <div class="log-detail-grid">
        <div class="log-detail-block">
          <h4>基础信息</h4>
          <dl>
            <dt>时间</dt><dd>${Utils.escapeHtml(item.time)}</dd>
            <dt>耗时</dt><dd>${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</dd>
            <dt>状态</dt><dd><span class="pill ${this.statusClass(item.status, item)}">${Utils.escapeHtml(this.statusLabel(item.status))}</span> ${Utils.escapeHtml(this.eventLabel(item.event, item))}</dd>
            <dt>请求 ID / 次</dt><dd>${Utils.escapeHtml(requestIdDisplay)} / ${Number(item.attempt || 0) || 1}</dd>
            <dt>成员 ID</dt><dd>${Utils.escapeHtml(memberIdDisplay)}</dd>
          </dl>
        </div>
        <div class="log-detail-block">
          <h4>${isAggregate ? '聚合调度路径' : '路由路径'}</h4>
          <div class="log-route-path">${routeSteps.map((step, i) => i === 0 ? `<span>${step}</span>` : `<span class="arrow">→</span><span>${step}</span>`).join('')}</div>
          <dl style="margin-top:10px;">
            <dt>请求模型</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.requested, item.requested_model, item.model))}</dd>
            <dt>连接组</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.group_name, parsed.selected_group, this.groupName(item)))}</dd>
            <dt>实际模型</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.selected_model, parsed.model, item.selected_model, item.model))}</dd>
            <dt>上游地址</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.selected_upstream_model, parsed.upstream))}</dd>
          </dl>
        </div>
        <div class="log-detail-block">
          <h4>诊断信息</h4>
          <dl>
            <dt>错误原因</dt><dd>${Utils.escapeHtml(errorReason)}</dd>
            <dt>Fallback 原因</dt><dd>${Utils.escapeHtml(fallbackReason)}</dd>
            <dt>影响范围</dt><dd>${Utils.escapeHtml(this.failureScopeLabel(this.firstValue(item.failure_scope, parsed.failure_scope, '')))}</dd>
            <dt>触发冷却</dt><dd>${Utils.escapeHtml(String(cooldownApplied) === 'true' ? '是' : '否')}</dd>
            <dt>WAF 兼容</dt><dd>${Utils.escapeHtml(parsed.waf_compatible || '-')}</dd>
            <dt>WAF 锁</dt><dd>${Utils.escapeHtml(parsed.waf_lock_enabled || '-')}</dd>
            <dt>等待锁</dt><dd>${Utils.escapeHtml(parsed.lock_wait_ms ? parsed.lock_wait_ms + ' ms' : '-')}</dd>
            <dt>Payload 预警</dt><dd>${this.renderPayloadWarnings(parsed)}</dd>
          </dl>
        </div>
      </div>
      ${aggregateChain}
      ${candidateFilterDetails}
      ${this.renderTechnicalDetails(item.detail)}

      ${deepDiagnostics}
    `;
  },

  aggregateRouteSteps(parsed, item) {
    return [
      Utils.escapeHtml(parsed.requested || item.model || 'lin-router-auto'),
      `<span class="pill">${Utils.escapeHtml(parsed.resolved_as)}</span>`,
      Utils.escapeHtml(parsed.aggregate_model || parsed.aggregate || item.model || '-'),
      Utils.escapeHtml(parsed.selected_group || this.groupName(item)),
      Utils.escapeHtml(parsed.selected_model || item.model || '-'),
      Utils.escapeHtml(parsed.selected_upstream_model || parsed.upstream || '-'),
    ];
  },

  renderAggregateChain(parsed) {
    if (!parsed.fallback_chain) return '';
    let chain;
    try {
      chain = JSON.parse(parsed.fallback_chain);
    } catch (_) {
      return '';
    }
    if (!Array.isArray(chain) || !chain.length) return '';
    const rows = chain.map((step, idx) => `
      <tr>
        <td class="tiny">${idx + 1}</td>
        <td>${Utils.escapeHtml(step.member_id || '-')}</td>
        <td>${Utils.escapeHtml(step.group || '-')}</td>
        <td>${Utils.escapeHtml(step.model || '-')}</td>
        <td class="tiny">${Utils.escapeHtml(String(step.status || '-'))}</td>
        <td>${Utils.escapeHtml(step.reason || '-')}</td>
      </tr>
    `).join('');
    return `
      <div class="log-detail-block" style="margin-top:10px;">
        <h4>Fallback 链路</h4>
        <div class="aggregate-members-table-wrap">
          <table class="aggregate-members-table">
            <thead><tr><th>顺序</th><th>成员 ID</th><th>连接组</th><th>模型</th><th>状态</th><th>原因</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    `;
  },

  toggleDetail(idx) {
    const row = document.querySelector(`[data-log-detail-row="${idx}"]`);
    if (!row) return;
    const isHidden = row.classList.contains('hidden');
    document.querySelectorAll('[data-log-detail-row]').forEach(r => r.classList.add('hidden'));
    row.classList.toggle('hidden', !isHidden);
  },

  async clear() {
    const ok = await Modal.confirm({
      title: '清空日志',
      message: '确定清空所有日志吗？此操作不可恢复。',
      confirmText: '确定清空',
      confirmClass: 'btn-danger'
    });
    if (!ok) return;
    try {
      await API.clearLogs();
      await Store.load();
      Toast.success('日志已清空');
    } catch (err) {
      Toast.error('清空失败：' + err.message);
    }
  }
};
