const LogsTab = {
  filters: { start: '', end: '', group: '', status: '' },
  currentOnly: false,
  autoRefresh: true,
  refreshTimer: null,
  REFRESH_INTERVAL: 5000,
  page: 0,
  pageSize: 50,
  total: 0,
  _lastRenderSignature: '',
  _openDetailKey: '',
  _detailEventsBound: false,
  _currentOnlySelectionKey: '',
  _allCurrentOnlyLogs: null,
  _refreshInFlight: null,
  _refreshFailures: 0,
  _nextAutoRefreshAt: 0,
  _visibilityHandler: null,
  REFRESH_BACKOFF_INTERVALS: [5000, 10000, 30000],

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
      <div class="logs-pagination" id="logs-pagination">
        <label>每页
          <select id="logs-page-size">
            ${[50, 100, 200].map(size => `<option value="${size}" ${this.pageSize === size ? 'selected' : ''}>${size}</option>`).join('')}
          </select>
          条
        </label>
        <span id="logs-page-summary">加载中…</span>
        <button type="button" id="logs-prev" class="btn-secondary" disabled>上一页</button>
        <button type="button" id="logs-next" class="btn-secondary" disabled>下一页</button>
      </div>
    `;
    this.attachEvents(panel);
    this.bindVisibility();
    this._lastRenderSignature = '';
    this._detailEventsBound = false;
    // 隐藏页不启动自动日志请求，恢复可见后由 visibilitychange 补一次。
    if (!document.hidden) this.manualRefresh();
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
    panel.querySelector('#logs-current-only')?.addEventListener('change', e => this.setCurrentOnly(e.target.checked));
    panel.querySelector('#logs-auto-refresh')?.addEventListener('change', e => { this.setAutoRefresh(e.target.checked); });
    panel.querySelector('#logs-refresh')?.addEventListener('click', () => this.manualRefresh());
    panel.querySelector('#logs-clear')?.addEventListener('click', () => this.clear());
    panel.querySelector('#logs-export')?.addEventListener('click', () => { location.href = '/api/logs/export'; });
    panel.querySelector('#logs-reset')?.addEventListener('click', () => this.resetFilters());
    panel.querySelector('#logs-page-size')?.addEventListener('change', event => {
      this.pageSize = Number(event.target.value) || 50;
      this.page = 0;
      this.manualRefresh();
    });
    panel.querySelector('#logs-prev')?.addEventListener('click', () => this.changePage(-1));
    panel.querySelector('#logs-next')?.addEventListener('click', () => this.changePage(1));
  },

  setAutoRefresh(enabled) {
    this.autoRefresh = enabled;
    if (enabled) this.startAutoRefresh();
    else this.stopAutoRefresh();
  },

  startAutoRefresh() {
    this.stopAutoRefresh();
    if (!this.autoRefresh) return;
    this.bindVisibility();
    this.refreshTimer = setInterval(() => this.autoRefreshTick(), this.REFRESH_INTERVAL);
  },

  stopAutoRefresh() {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  },

  bindVisibility() {
    if (this._visibilityHandler || typeof document === 'undefined') return;
    this._visibilityHandler = () => {
      if (document.hidden || !this.autoRefresh || Tabs.current !== 'logs') return;
      // 页面恢复时只补一次，正在进行的刷新由 single-flight 复用，不会重复发请求。
      this.autoRefreshTick(true);
    };
    document.addEventListener('visibilitychange', this._visibilityHandler);
  },

  refreshBackoffDelay() {
    const index = Math.min(Math.max(this._refreshFailures - 1, 0), this.REFRESH_BACKOFF_INTERVALS.length - 1);
    return this.REFRESH_BACKOFF_INTERVALS[index];
  },

  currentOnlySelectionKey() {
    if (!this.currentOnly) return '';
    const selected = Store.selected || {};
    return `${selected.type || ''}:${selected.id || ''}`;
  },

  syncCurrentOnlySelection() {
    const key = this.currentOnlySelectionKey();
    if (key === this._currentOnlySelectionKey) return false;
    this._currentOnlySelectionKey = key;
    this.page = 0;
    this._openDetailKey = '';
    return true;
  },

  setCurrentOnly(enabled) {
    this.currentOnly = !!enabled;
    this._currentOnlySelectionKey = this.currentOnly ? this.currentOnlySelectionKey() : '';
    this.page = 0;
    this._openDetailKey = '';
    this.manualRefresh();
  },

  shouldUseLocalCurrentOnlyPagination() {
    const selected = Store.selected || {};
    // 后端已有连接组筛选；模型/聚合筛选需要保留完整历史后再分页，避免总数与当前页不一致。
    return this.currentOnly && ['model', 'aggregate'].includes(selected.type) && !!selected.id;
  },

  hasCurrentOnlyGroupConflict() {
    const selected = Store.selected || {};
    return this.currentOnly && selected.type === 'group' && !!selected.id
      && !!this.filters.group && this.filters.group !== selected.id;
  },

  syncPageToTotal() {
    const lastPage = Math.max(0, Math.ceil(this.total / this.pageSize) - 1);
    if (this.page <= lastPage) return false;
    this.page = lastPage;
    this._openDetailKey = '';
    return true;
  },

  async autoRefreshTick(force = false) {
    if (!this.autoRefresh || document.hidden) return;
    // 只在当前是 logs tab 时刷新
    if (Tabs.current !== 'logs') return;
    if (this._refreshInFlight || (!force && this._nextAutoRefreshAt > Date.now())) return;
    // 自动刷新使用静默模式，不触发全局 loading 遮罩
    const ok = await this.manualRefresh(true);
    if (ok) {
      this._refreshFailures = 0;
      this._nextAutoRefreshAt = 0;
    } else {
      this._refreshFailures += 1;
      this._nextAutoRefreshAt = Date.now() + this.refreshBackoffDelay();
    }
  },

  async manualRefresh(silent = false) {
    if (silent && document.hidden) return false;
    if (this._refreshInFlight) return this._refreshInFlight;
    const request = this._manualRefresh(silent);
    this._refreshInFlight = request;
    request.then(
      () => { if (this._refreshInFlight === request) this._refreshInFlight = null; },
      () => { if (this._refreshInFlight === request) this._refreshInFlight = null; },
    );
    return request;
  },

  async _manualRefresh(silent = false) {
    this.syncCurrentOnlySelection();
    if (silent && this.page !== 0) return true;
    if (this.hasCurrentOnlyGroupConflict()) {
      this._allCurrentOnlyLogs = null;
      this.total = 0;
      Store.update({ logs: [] });
      this.renderRows(true);
      this.renderPagination();
      return true;
    }
    try {
      if (this.shouldUseLocalCurrentOnlyPagination()) {
        const all = await API.getLogs({
          offset: 0,
          // 日志保留上限为 5000；先取得服务端已筛选的完整窗口，再按模型/聚合筛选与分页。
          limit: 5000,
          group: this.filters.group,
          status: this.filters.status,
          start: this.filters.start,
          end: this.filters.end,
        }, { silent });
        const source = all?.logs || [];
        this._allCurrentOnlyLogs = source;
        const filtered = this.filterSourceLogs(source);
        this.total = filtered.length;
        this.syncPageToTotal();
        const offset = this.page * this.pageSize;
        Store.update({ logs: filtered.slice(offset, offset + this.pageSize) });
      } else {
        const selected = Store.selected || {};
        const params = {
          offset: this.page * this.pageSize,
          limit: this.pageSize,
          group: this.currentOnly && selected.type === 'group' ? selected.id : this.filters.group,
          status: this.filters.status,
          start: this.filters.start,
          end: this.filters.end,
        };
        const data = await API.getLogs(params, { silent });
        this._allCurrentOnlyLogs = null;
        this.total = Number(data.total || 0);
        if (this.syncPageToTotal()) return this._manualRefresh(silent);
        Store.update({ logs: data.logs || [] });
      }
      this.renderRows(true);
      this.renderPagination();
      return true;
    } catch (err) {
      if (!silent) Toast.error('刷新失败：' + err.message);
      console.error('日志刷新失败', err);
      return false;
    }
  },

  changePage(delta) {
    const next = this.page + delta;
    if (next < 0 || next * this.pageSize >= this.total) return;
    this.page = next;
    this._openDetailKey = '';
    this.manualRefresh();
  },

  renderPagination() {
    const start = this.total ? this.page * this.pageSize + 1 : 0;
    const end = Math.min(this.total, (this.page + 1) * this.pageSize);
    const summary = document.getElementById('logs-page-summary');
    if (summary) summary.textContent = `第 ${start}–${end} 条 / 共 ${this.total} 条`;
    const prev = document.getElementById('logs-prev');
    const next = document.getElementById('logs-next');
    if (prev) prev.disabled = this.page === 0;
    if (next) next.disabled = end >= this.total;
  },

  readFilters() {
    this.filters.start = document.getElementById('log-start')?.value || '';
    this.filters.end = document.getElementById('log-end')?.value || '';
    this.filters.group = document.getElementById('log-group')?.value || '';
    this.filters.status = document.getElementById('log-status')?.value || '';
    this.page = 0;
    this._openDetailKey = '';
    this.manualRefresh();
  },

  filterLogs() {
    const logs = Store.state.logs || [];
    return this.filterSourceLogs(logs);
  },

  filterSourceLogs(logs) {
    return (logs || []).filter(item => this.matches(item) && !this.isStableConfigSkip(item) && item.usage_source !== 'manual_probe');
  },

  isStableConfigSkip(item) {
    if (!item || item.event !== 'skip') return false;
    const parsed = this.parseDetail(item.detail);
    return ['member_disabled', 'member_cooling', 'underlying_model_disabled', 'underlying_model_cooling'].includes(parsed.skip_reason);
  },

  requestRelatedLogs(item) {
    if (!item?.request_id) return [];
    const source = this._allCurrentOnlyLogs || Store.state.logs || [];
    return source.filter(log => log.request_id === item.request_id && log !== item);
  },

  resetFilters() {
    this.filters = { start: '', end: '', group: '', status: '' };
    this.page = 0;
    this._openDetailKey = '';
    this.render();
  },

  matches(item) {
    const start = this.dateValue(this.filters.start);
    const end = this.dateValue(this.filters.end);
    const t = this.itemTime(item);
    if (start && t < start) return false;
    if (end && t > end) return false;
    const selected = Store.selected || {};
    if (this.filters.group && item.group_id !== this.filters.group) return false;
    if (this.currentOnly) {
      const sel = selected;
      if (sel.type === 'group' && sel.id && item.group_id !== sel.id) return false;
      if (sel.type === 'model' && sel.id) {
        const model = Store.getModel(sel.id);
        const names = new Set([model?.name, model?.upstream_model, model?.ep_id].filter(Boolean));
        const logNames = [item.model, item.requested_model, item.selected_model, item.selected_upstream_model];
        if (!logNames.some(name => names.has(name))) return false;
      }
      if (sel.type === 'aggregate' && sel.id) {
        const aggregate = Store.getAggregate(sel.id);
        if (item.aggregate_id !== sel.id && item.aggregate_model !== aggregate?.name && item.model !== aggregate?.name) return false;
      }
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
    if (event === 'stream_ok' && item) {
      const finalResult = this.parseDetail(item.detail).final_result;
      if (finalResult === 'stream_done') return '首完整帧成功 / 流式完成';
      if (finalResult === 'stream_failed') return '首完整帧成功 / 流式失败';
      if (finalResult === 'stream_incomplete') return '首完整帧成功 / 流式不完整';
      if (finalResult === 'stream_idle_timeout') return '首完整帧成功 / 流式空闲超时';
      if (finalResult === 'client_disconnected') return '首完整帧成功 / 客户端断开';
    }
    const map = { ok:'成功', stream_ok:'首完整帧成功', retry_ok:'重试成功', cooldown:'冷却切换', fallback:'自动切换', skip:'跳过', network:'网络错误', protocol:'协议错误', stream_protocol_error:'上游流式协议错误', error:'错误', system:'系统', stream_timeout:'流式超时', serial_protection_timeout:'串行保护候选忙', waf_lock_timeout:'候选忙（旧版）', stream_done:'流式完成', stream_idle_timeout:'流式空闲超时', client_disconnected:'客户端断开', manual_probe:'人工探测' };
    return map[event] || event || '-';
  },

  isStreamRecord(item, parsed = this.parseDetail(item?.detail)) {
    const event = String(item?.event || '');
    const finalResult = String(parsed.final_result || parsed.lifecycle || '');
    return event.startsWith('stream')
      || ['request_cancelled', 'stream_disconnected_before_completion'].includes(event)
      || finalResult.startsWith('stream_')
      || parsed.stream_started_at_ms !== undefined
      || parsed.first_complete_frame_ms !== undefined;
  },

  streamTerminalLabel(item, parsed) {
    const finalResult = String(parsed.final_result || parsed.lifecycle || '').toLowerCase();
    const event = String(item?.event || '').toLowerCase();
    const completionSignal = String(parsed.completion_signal || '').toLowerCase();
    const map = {
      stream_done: '流式完成',
      done: '流式完成',
      stream_failed: '流式失败',
      stream_incomplete: '流式不完整',
      stream_idle_timeout: '流式超时',
      client_disconnected: '客户端断开',
      manual_cancelled: '客户端已取消',
      interrupted: '服务重启后中断',
      stream_interrupted_after_restart: '服务重启后中断',
    };
    if (map[finalResult]) return map[finalResult];
    if (['response.completed', '[done]', 'eof'].includes(completionSignal)) return '流式完成';
    if (completionSignal === 'response.failed') return '流式失败';
    if (completionSignal === 'response.incomplete') return '流式不完整';
    if (event === 'stream_timeout') return '流式超时';
    if (['request_cancelled', 'stream_disconnected_before_completion', 'client_disconnected'].includes(event)) return '客户端断开';
    if (event === 'stream_done' || event === 'stream_finalized') return '流式完成';
    return '流式进行中';
  },

  finalLifecycle(parsed) {
    return String(parsed.final_result || parsed.lifecycle || '').toLowerCase();
  },

  isHttpSuccess(item) {
    return /^2\d\d$/.test(String(item?.status || ''));
  },

  isExplicitFailureTerminal(item, parsed) {
    const lifecycle = this.finalLifecycle(parsed);
    if (['stream_failed', 'stream_incomplete', 'stream_idle_timeout', 'client_disconnected', 'manual_cancelled', 'cancelled', 'interrupted', 'stream_interrupted_after_restart'].includes(lifecycle)) return true;
    if (['response.failed', 'response.incomplete'].includes(String(parsed.completion_signal || '').toLowerCase())) return true;
    const event = String(item?.event || '').toLowerCase();
    return ['stream_timeout', 'stream_protocol_error', 'stream_disconnected_before_completion', 'request_cancelled'].includes(event);
  },

  isSuccessfulRecord(item, parsed) {
    // 聚合 fallback 的旧错误可能留在 detail；最终 2xx 和终态才代表本条记录的结论。
    return this.isHttpSuccess(item) && !this.isExplicitFailureTerminal(item, parsed);
  },

  structuredDiagnosticText(item, parsed) {
    const fields = [
      item?.status,
      item?.event,
      item?.failure_scope,
      parsed.failure_scope,
      parsed.reason,
      parsed.log_reason,
      parsed.error,
      parsed.error_code,
      parsed.error_type,
      parsed.auth_error,
      parsed.code,
      parsed.type,
      parsed.category,
      parsed.failure_reason,
      parsed.fallback_reason,
      parsed.waf_blocked,
      this.finalLifecycle(parsed),
    ];
    return fields.filter(value => value !== undefined && value !== null).join(' ').toLowerCase();
  },

  isCurrentAuthFailure(item, parsed) {
    const status = String(item?.status || '').trim().toLowerCase();
    if (status === 'auth_error' || /(^|\D)(401|403)(?:\D|$)/.test(status)) return true;
    if (String(parsed.auth_error || '').toLowerCase() === 'true') return true;
    const authValues = [
      parsed.error,
      parsed.error_code,
      parsed.error_type,
      parsed.auth_error,
      parsed.code,
      parsed.type,
      parsed.reason,
      parsed.log_reason,
      parsed.category,
      parsed.failure_reason,
    ].map(value => String(value || '').toLowerCase());
    return authValues.some(value => /(^|[_:-])(auth(?:entication|orization)?(?:_error)?|invalid[_-]?api[_-]?key|unauthorized|forbidden)(?:$|[_:-])/.test(value));
  },

  eventSummary(item) {
    const parsed = this.parseDetail(item?.detail);
    if (!this.isStreamRecord(item, parsed)) return this.eventLabel(item?.event, item);
    const terminal = this.streamTerminalLabel(item, parsed);
    const hasFirstFrame = ['stream_ok', 'stream_done', 'stream_finalized'].includes(String(item?.event || ''))
      || Number(parsed.chunks_received || 0) > 0
      || parsed.first_complete_frame_ms !== undefined;
    return hasFirstFrame ? `首包完成\n${terminal}` : terminal;
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
      protocol: '协议错误',
      timeout: '请求超时',
      busy: '候选忙',
      streaming: '流式中',
      stream_failed: '流式失败',
      stream_incomplete: '流式不完整',
      client_disconnected: '客户端断开',
      interrupted: '服务重启后中断',
    };
    return map[value] || value || '-';
  },

  statusClass(status, item = null) {
    if (item && this.isStableConfigSkip(item)) return 'info';
    const text = String(status || '');
    if (text === '200' || text.startsWith('2')) return 'success';
    if (text === 'network' || text === 'protocol' || text.includes('failed') || text.includes('protocol') || text.startsWith('5')) return 'error';
    return 'warning';
  },

  tokenSummary(item) {
    const metrics = this.tokenMetrics(item);
    if (!metrics) return '-';
    return [
      `输入：${metrics.input}`,
      `输出：${metrics.output}`,
      `命中：${metrics.cached}（${metrics.hit}%）`,
      `总计：${metrics.total}`,
    ].join('\n');
  },

  tokenMetrics(item) {
    const input = Number(item.prompt_tokens || 0);
    const output = Number(item.completion_tokens || 0);
    const total = Number(item.total_tokens || 0);
    const cached = Number(item.cached_tokens || 0);
    if (!total && !input && !output && !cached) return null;
    return {
      input,
      output,
      total,
      cached,
      hit: input ? Math.round((cached / input) * 100) : 0,
    };
  },

  tokenSummaryHtml(item) {
    const metrics = this.tokenMetrics(item);
    if (!metrics) return '-';
    return [
      `输入：${Utils.escapeHtml(metrics.input)}`,
      `输出：${Utils.escapeHtml(metrics.output)}`,
      `<span class="log-token-hit">命中：${Utils.escapeHtml(metrics.cached)}<span class="log-token-hit-rate">（${Utils.escapeHtml(metrics.hit)}%）</span></span>`,
      `总计：${Utils.escapeHtml(metrics.total)}`,
    ].join('<br>');
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

  formatDurationSeconds(value) {
    const milliseconds = Number(value);
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return '-';
    return `${(milliseconds / 1000).toFixed(2)} 秒`;
  },

  streamTiming(item) {
    const totalMilliseconds = Number(item?.duration_ms);
    if (!Number.isFinite(totalMilliseconds) || totalMilliseconds < 0) return null;
    const parsed = this.parseDetail(item?.detail);
    const numericTiming = key => {
      const value = Number(parsed[key]);
      return Number.isFinite(value) && value >= 0 ? value : null;
    };
    const firstContentDeltaMilliseconds = numericTiming('first_content_delta_ms');
    const firstCompleteFrameMilliseconds = numericTiming('first_complete_frame_ms') ?? numericTiming('first_byte_ms');
    const primaryMilliseconds = firstContentDeltaMilliseconds ?? firstCompleteFrameMilliseconds;
    const primaryLabel = primaryMilliseconds !== null ? '首包' : '';
    return {
      totalMilliseconds,
      firstContentDeltaMilliseconds,
      firstCompleteFrameMilliseconds,
      firstRawLineMilliseconds: numericTiming('first_raw_line_ms'),
      firstDownstreamFlushMilliseconds: numericTiming('first_downstream_flush_ms'),
      primaryMilliseconds,
      primaryLabel,
      streamMilliseconds: primaryMilliseconds === null ? null : Math.max(0, totalMilliseconds - primaryMilliseconds),
    };
  },

  durationSummary(item) {
    const timing = this.streamTiming(item);
    if (!timing) return '-';
    const parsed = this.parseDetail(item?.detail);
    if (!this.isStreamRecord(item, parsed)) return `总：${this.formatDurationSeconds(timing.totalMilliseconds)}`;
    const firstLabel = timing.primaryLabel || '首包';
    const first = timing.primaryMilliseconds === null ? '-' : this.formatDurationSeconds(timing.primaryMilliseconds);
    const remaining = timing.streamMilliseconds === null ? '-' : this.formatDurationSeconds(timing.streamMilliseconds);
    return [
      `${firstLabel}：${first}`,
      `后续：${remaining}`,
      `总：${this.formatDurationSeconds(timing.totalMilliseconds)}`,
    ].join('\n');
  },

  renderRows(keepScroll = false) {
    if (this.syncCurrentOnlySelection()) {
      this.manualRefresh(true);
      return;
    }
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
    const openKey = this._openDetailKey;
    if (filtered.length === 0) {
      tbody.innerHTML = '';
      empty.classList.remove('hidden');
      this._openDetailKey = '';
      return;
    }
    empty.classList.add('hidden');

    const seen = new Set();
    filtered.forEach((item, idx) => {
      const key = this.rowKey(item);
      seen.add(key);
      const temp = document.createElement('tbody');
      temp.innerHTML = this.rowHtml(item, idx, key).trim();
      const nextMain = temp.children[0];
      const nextDetail = temp.children[1];
      const currentMain = tbody.querySelector(`[data-log-main-row="${CSS.escape(key)}"]`);
      const currentDetail = tbody.querySelector(`[data-log-detail-row="${CSS.escape(key)}"]`);
      if (openKey === key) nextDetail.classList.remove('hidden');
      if (currentMain && currentDetail) {
        if (currentMain.outerHTML !== nextMain.outerHTML) currentMain.replaceWith(nextMain);
        if (currentDetail.outerHTML !== nextDetail.outerHTML) currentDetail.replaceWith(nextDetail);
        const main = tbody.querySelector(`[data-log-main-row="${CSS.escape(key)}"]`);
        const detail = tbody.querySelector(`[data-log-detail-row="${CSS.escape(key)}"]`);
        tbody.append(main, detail);
      } else {
        tbody.append(nextMain, nextDetail);
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

    if (openKey && !seen.has(openKey)) this._openDetailKey = '';
    if (!this._detailEventsBound) {
      tbody.addEventListener('click', event => {
        const target = event.target.closest('[data-log-detail-key], [data-log-detail-preview-key]');
        if (!target) return;
        this.toggleDetailByKey(target.dataset.logDetailKey || target.dataset.logDetailPreviewKey);
      });
      this._detailEventsBound = true;
    }

    if (wasAtBottom && wrap) wrap.scrollTop = wrap.scrollHeight;
    else if (wrap) wrap.scrollTop = scrollTop;
  },

  rowKey(item) {
    const parsed = this.parseDetail(item?.detail);
    // 流记录会在终态写回 status/event；键只使用创建时不变的身份字段，避免刷新时丢失已展开详情。
    const streamIdentity = this.isStreamRecord(item, parsed) ? 'stream' : (item.event || 'event');
    return [
      item.request_id || 'no-request-id',
      item.attempt || 0,
      item.time || '',
      item.aggregate_member_id || item.selected_model || item.model || '',
      item.fallback_index || 0,
      streamIdentity,
    ].join('|');
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
        <td class="tiny log-multiline">${Utils.escapeHtml(this.eventSummary(item))}</td>
        <td class="tiny">${Number(item.attempt || 0) || 1}</td>
        <td class="tiny log-multiline">${Utils.escapeHtml(this.durationSummary(item))}</td>
        <td class="tiny log-multiline log-token-summary">${this.tokenSummaryHtml(item)}</td>
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
    this._openDetailKey = willOpen ? key : '';
  },

  formatDetailPreview(item) {
    const parsed = this.parseDetail(item?.detail);
    const event = String(item?.event || '');
    if (event === 'manual_probe') {
      return String(item?.status || '') === 'probe_ok'
        ? '最小探测成功，候选已恢复参与调度'
        : '最小探测未通过，候选保持冷却';
    }
    if (event === 'stream_ok') {
      if (parsed.final_result === 'stream_done') return '流式响应已完成';
      if (parsed.final_result === 'stream_failed') return '上游返回流式失败终态';
      if (parsed.final_result === 'stream_incomplete') return '上游返回流式不完整终态';
      if (parsed.final_result === 'stream_idle_timeout') return '上游流式响应空闲超时';
      if (parsed.final_result === 'client_disconnected') return '客户端已断开连接';
      return '首完整帧成功，流式响应仍在进行';
    }
    if (event === 'stream_interrupted') return '服务重启时未收到流终态，已标记为中断';
    if (event === 'stream_done' || event === 'stream_finalized') return '流式响应已完成';
    if (event === 'client_disconnected') return '客户端已断开连接';
    if (event === 'serial_protection_timeout') return '该连接组已开启串行保护，候选忙后已切换';
    if (event === 'waf_lock_timeout') return '候选忙，等待锁超时后已切换（旧版）';
    return this.userFacingErrorReason(item || {}, parsed);
  },

  formatJsonBlock(value) {
    if (!value) return '-';
    const str = this.redactDiagnosticEvidence(value);
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
    const text = this.redactDiagnosticEvidence(detail).replace(/;/g, '; ');
    const clipped = text.length > maxLen ? text.slice(0, maxLen) + '…' : text;
    return Utils.escapeHtml(clipped);
  },

  redactDiagnosticEvidence(value) {
    let text = Utils.redactSensitive(String(value || ''));
    // 历史日志可能早于后端脱敏规则；前端展开证据时不展示完整上游地址或 Header。
    text = text.replace(/((?:out_)?headers\s*=\s*)\([^)]*\)/gi, '$1[REDACTED_HEADERS]');
    text = text.replace(/((?:upstream_endpoint|target_url|url)\s*=\s*)https?:\/\/[^;\s]+/gi, '$1[REDACTED_URL]');
    return text;
  },

  safeEndpoint(value) {
    const text = String(value || '').trim();
    if (!text || text === '-') return '-';
    try {
      const url = new URL(text);
      return url.pathname || '/';
    } catch (_) {
      return text.replace(/^https?:\/\/[^/\s]+/i, '').split(/[?#]/, 1)[0] || '-';
    }
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
    if (String(item.event || '') === 'serial_protection_timeout') return '串行保护候选忙，调度切换';
    if (String(item.event || '') === 'waf_lock_timeout') return '候选忙/等待锁超时，调度切换（旧版）';
    if (String(item.failure_scope || parsed.failure_scope || '') === 'upstream') return '上游超时/错误，健康失败';
    return this.skipReasonLabel(reason);
  },

  renderCandidateFilterDetails(item) {
    const related = this.requestRelatedLogs(item)
      .map(log => ({ log, parsed: this.parseDetail(log.detail) }))
      .filter(entry => {
        const event = String(entry.log.event || '');
        return event === 'skip' || event === 'serial_protection_timeout' || event === 'waf_lock_timeout' || event === 'stream_timeout' || event === 'cooldown' || event === 'fallback' || event === 'network';
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

  renderWafHint(parsed, diagnosis = null) {
    if (parsed.waf_blocked !== 'true' || diagnosis?.title === '请求成功' || diagnosis?.title === '请求级错误 / 上游拒绝') return '';
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
    if (this.isSuccessfulRecord(item, parsed)) {
      return { className: 'success', title: '请求成功', scope: '-', cooldown: '否', suggestion: '无需处理。' };
    }

    const lifecycle = this.finalLifecycle(parsed);
    const text = this.structuredDiagnosticText(item, parsed);
    if (lifecycle === 'manual_cancelled' || String(item.failure_scope || parsed.failure_scope || '') === 'client_cancelled' || String(item.event || '') === 'request_cancelled') {
      return { className: 'info', title: '客户端已取消请求', scope: 'request', cooldown: '否', suggestion: '请求由客户端主动终止，未判定为上游健康失败。' };
    }
    if (lifecycle === 'stream_failed') {
      return { className: 'danger', title: '上游流式响应失败', scope: 'upstream', cooldown: '否', suggestion: '上游已明确返回失败终态；已保留已收到的流内容，不会混入其他候选。' };
    }
    if (lifecycle === 'stream_incomplete') {
      return { className: 'warning', title: '上游流式响应不完整', scope: 'upstream', cooldown: '否', suggestion: '上游已明确返回不完整终态；已保留已收到的流内容，不会混入其他候选。' };
    }
    if (lifecycle === 'interrupted' || lifecycle === 'stream_interrupted_after_restart') {
      return { className: 'info', title: '服务重启后流已中断', scope: 'process_restart', cooldown: '否', suggestion: '重启前未收到流终态，记录已安全恢复为中断状态；不会被当作实时请求或成功响应。' };
    }
    if (lifecycle === 'stream_idle_timeout' || String(item.event || '') === 'stream_timeout') {
      return { className: 'danger', title: '上游流式响应空闲超时', scope: 'upstream', cooldown: item.cooldown_applied ? '是' : '可能', suggestion: '建议稍后重试，或对冷却中的单个模型/成员点击“重试恢复”。' };
    }
    if (text.includes('serial_protection_wait_timeout') || text.includes('waf_lock_wait_timeout') || text.includes('candidate_busy') || text.includes('large_task_in_progress')) {
      return { className: 'warning', title: '候选忙 / 串行保护等待超时', scope: 'local_lock', cooldown: '否', suggestion: '该连接组已开启串行保护，系统会临时切换到下一个候选；通常无需清冷却。' };
    }
    if (this.isCurrentAuthFailure(item, parsed)) {
      return { className: 'danger', title: '鉴权失败', scope: 'candidate', cooldown: '否', suggestion: '检查连接组或模型的 API Key / Route Key 是否正确。' };
    }
    if (text.includes('waf_blocked') || text.includes('request_level') || text.includes('upstream_request_rejected')) {
      return { className: 'warning', title: '请求级错误 / 上游拒绝', scope: 'request', cooldown: '否', suggestion: '请检查请求参数、内容策略或 WAF 兼容设置；这不会被诊断为模型健康失败。' };
    }
    if (text.includes('rate_limit') || text.includes('429')) {
      return { className: 'warning', title: '上游限流', scope: 'upstream', cooldown: item.cooldown_applied ? '是' : '否', suggestion: '稍后重试，或临时切换到其他候选。' };
    }
    if ((text.includes('timeout') || text.includes('read_timeout')) && !text.includes('waf_lock') && !text.includes('serial_protection')) {
      return { className: 'danger', title: '上游请求超时', scope: 'upstream', cooldown: item.cooldown_applied ? '是' : '可能', suggestion: '若频繁出现，检查中转站状态；确认恢复后可单点重试恢复。' };
    }
    if (text.includes('server_error') || text.includes('network') || String(item.status || '').startsWith('5')) {
      return { className: 'danger', title: '上游健康失败', scope: item.failure_scope || parsed.failure_scope || 'upstream', cooldown: item.cooldown_applied ? '是' : '否', suggestion: '真实上游故障会进入冷却；确认恢复后可单点重试。' };
    }
    return { className: 'info', title: '需要关注', scope: item.failure_scope || parsed.failure_scope || 'request', cooldown: item.cooldown_applied ? '是' : '否', suggestion: '请结合候选过滤详情和脱敏摘要继续排查。' };
  },

  wafDecisionLabel(value) {
    const map = {
      waf_compatible: '已套用 WAF 兼容 Header',
      codex_direct: 'Codex 直连 Header（智能兼容）',
      disabled: '未启用 WAF 兼容',
    };
    return map[String(value || '')] || '-';
  },

  boolLabel(value) {
    if (String(value) === 'true') return '是';
    if (String(value) === 'false') return '否';
    return '-';
  },

  reasoningPreservedLabel(value) {
    if (String(value) === 'true') return '是';
    if (String(value) === 'false') return '否';
    if (String(value) === 'n/a') return '不适用（未携带字段）';
    return '-';
  },

  reasoningValueStatusLabel(value) {
    const map = {
      absent: '未携带字段',
      recognized: '已识别',
      unrecognized: '未识别，日志已脱敏',
    };
    return map[String(value || '')] || '-';
  },

  renderDiagnosisCard(item, parsed, diagnosis = this.diagnosisFor(item, parsed)) {
    const d = diagnosis;
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
    if (this.isSuccessfulRecord(item, parsed)) return '请求成功';
    const text = `${item.status || ''} ${item.event || ''} ${item.failure_scope || ''} ${item.detail || ''}`.toLowerCase();
    if (text.includes('serial_protection_wait_timeout') || text.includes('waf_lock_wait_timeout') || text.includes('candidate_busy') || text.includes('large_task_in_progress')) return '该连接组已开启串行保护，候选忙后已尝试切换';
    if (text.includes('stream_idle_timeout')) return '上游流式响应长时间无数据，已判定为空闲超时';
    if (text.includes('read_timeout') || (text.includes('timeout') && !text.includes('waf_lock') && !text.includes('serial_protection'))) return '上游响应超时';
    if (this.isCurrentAuthFailure(item, parsed)) return '上游鉴权失败，请检查 API Key 或权限';
    if (text.includes('waf_blocked')) return '上游中转站的 WAF 拦截了请求';
    if (text.includes('rate_limit') || text.includes('429')) return '上游触发限流，请稍后重试';
    if (text.includes('request_level') || text.includes('upstream_request_rejected')) return '请求参数或内容策略被上游拒绝';
    if (text.includes('network')) return '连接上游网络失败';
    if (text.includes('server_error') || String(item.status || '').startsWith('5')) return '上游服务暂时异常';
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

  streamObservationDetails(parsed, timing) {
    const hasStreamEvidence = [
      'candidate_selected_ms', 'upstream_request_started_ms', 'upstream_headers_ms',
      'first_raw_line_ms', 'first_complete_frame_ms', 'first_content_delta_ms',
      'first_downstream_flush_ms', 'stream_frame_count', 'stream_wire_mode',
    ].some(key => parsed[key] !== undefined);
    if (!hasStreamEvidence) return '';
    const ms = value => {
      const number = Number(value);
      return Number.isFinite(number) && number >= 0 ? this.formatDurationSeconds(number) : '-';
    };
    const mode = {
      sse: '标准 SSE',
      json_compat: 'JSON 兼容响应',
      buffered_or_non_delimited: '缓冲或非分隔响应',
    }[parsed.stream_wire_mode] || '-';
    return `
      <div class="log-detail-block">
        <h4>流式时序</h4>
        <dl>
          <dt>候选完成</dt><dd>${ms(parsed.candidate_selected_ms)}</dd>
          <dt>开始上游请求</dt><dd>${ms(parsed.upstream_request_started_ms)}</dd>
          <dt>上游响应头</dt><dd>${ms(parsed.upstream_headers_ms)}</dd>
          <dt>首原始行</dt><dd>${ms(parsed.first_raw_line_ms)}</dd>
          <dt>首完整帧</dt><dd>${ms(timing?.firstCompleteFrameMilliseconds)}</dd>
          <dt>首文本 delta</dt><dd>${ms(timing?.firstContentDeltaMilliseconds)}</dd>
          <dt>首次下游 flush</dt><dd>${ms(timing?.firstDownstreamFlushMilliseconds)}</dd>
          <dt>帧数 / 首帧字节</dt><dd>${Utils.escapeHtml(parsed.stream_frame_count || '-')} / ${Utils.escapeHtml(parsed.initial_frame_bytes || '-')}</dd>
          <dt>上游传输</dt><dd>${Utils.escapeHtml(parsed.upstream_transport || parsed.http_client || '-')} / ${Utils.escapeHtml(parsed.upstream_http_version || '-')}</dd>
          <dt>响应模式</dt><dd>${Utils.escapeHtml(mode)}</dd>
          <dt>媒体 / 编码</dt><dd>${Utils.escapeHtml(parsed.upstream_content_type || '-')} / ${Utils.escapeHtml(parsed.upstream_content_encoding || '-')}</dd>
        </dl>
      </div>
    `;
  },

  detailHtml(item) {
    const parsed = this.parseDetail(item.detail);
    const debugMode = Store.state.settings?.debug_mode === true;
    const diagnosis = this.diagnosisFor(item, parsed);
    const wafHint = this.renderWafHint(parsed, diagnosis);
    const isAggregate = parsed.resolved_as && parsed.resolved_as.startsWith('aggregate');
    const routeSteps = isAggregate ? this.aggregateRouteSteps(parsed, item) : [
      parsed.requested ? Utils.escapeHtml(parsed.requested) : Utils.escapeHtml(item.requested_model || item.model || 'lin-router-auto'),
      parsed.group_name ? Utils.escapeHtml(parsed.group_name) : Utils.escapeHtml(this.groupName(item)),
      parsed.model ? Utils.escapeHtml(parsed.model) : Utils.escapeHtml(item.selected_model || item.model || '-'),
      Utils.escapeHtml(parsed.selected_upstream_model || '-'),
    ];
    const aggregateChain = isAggregate ? this.renderAggregateChain(parsed) : '';
    const candidateFilterDetails = this.renderCandidateFilterDetails(item);
    const diagnosisCard = this.renderDiagnosisCard(item, parsed, diagnosis);
    const requestIdDisplay = this.shortId(item.request_id || '-');
    const memberIdDisplay = this.shortId(this.firstValue(item.aggregate_member_id, parsed.aggregate_member_id));
    const fallbackReason = this.userFacingFallbackReason(item, parsed);
    const timing = this.streamTiming(item);
    const streamObservations = this.streamObservationDetails(parsed, timing);
    const streamLifecycle = {
      stream_done: '流式响应已完成',
      stream_failed: '上游返回流式失败终态',
      stream_incomplete: '上游返回流式不完整终态',
      stream_idle_timeout: '上游流式响应空闲超时',
      client_disconnected: '客户端已断开连接',
      streaming: '流式响应进行中',
    }[parsed.final_result || parsed.lifecycle || ''] || '-';
    const fallbackEvidence = this.fallbackChainValue(parsed, item);
    const debugFallback = !aggregateChain && fallbackEvidence !== '-'
      ? `<dt>Fallback Chain</dt><dd class="log-detail-raw">${this.formatJsonBlock(fallbackEvidence)}</dd>`
      : '';
    const debugTransport = !streamObservations ? `
          <dt>上游 HTTP 版本</dt><dd>${Utils.escapeHtml(parsed.upstream_http_version || '-')}</dd>
          <dt>Header Policy</dt><dd>${Utils.escapeHtml(parsed.header_policy || '-')}</dd>
          <dt>HTTP 客户端</dt><dd>${Utils.escapeHtml(parsed.http_client || '-')}</dd>` : '';
    const deepDiagnostics = debugMode ? `
      <div class="log-detail-block log-detail-raw-block">
        <h4>调试模式：深度诊断（已脱敏）</h4>
        <dl>
          <dt>完整 Request ID</dt><dd>${Utils.escapeHtml(item.request_id || '-')}</dd>
          <dt>完整 Member ID</dt><dd>${Utils.escapeHtml(this.firstValue(item.aggregate_member_id, parsed.aggregate_member_id))}</dd>
          <dt>Body Fingerprint</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.fingerprint, parsed.body_fingerprint))}</dd>
          <dt>Tools 排序</dt><dd>${Utils.escapeHtml(parsed.tools_normalized || '-')}</dd>
          ${debugFallback}
          ${debugTransport}
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
            <dt>首文本耗时</dt><dd>${timing?.firstContentDeltaMilliseconds !== null && timing?.firstContentDeltaMilliseconds !== undefined ? this.formatDurationSeconds(timing.firstContentDeltaMilliseconds) : '-'}</dd>
            <dt>后续流耗时</dt><dd>${timing?.streamMilliseconds !== null && timing?.streamMilliseconds !== undefined ? this.formatDurationSeconds(timing.streamMilliseconds) : '-'}</dd>
            <dt>总耗时</dt><dd>${timing ? this.formatDurationSeconds(timing.totalMilliseconds) : '-'}</dd>
            <dt>流生命周期</dt><dd>${Utils.escapeHtml(streamLifecycle)}</dd>
            <dt>完成信号</dt><dd>${Utils.escapeHtml(parsed.completion_signal || '-')}</dd>
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
            <dt>上游模型</dt><dd>${Utils.escapeHtml(this.firstValue(parsed.selected_upstream_model, parsed.model))}</dd>
            <dt>上游端点</dt><dd>${Utils.escapeHtml(this.safeEndpoint(parsed.upstream_endpoint))}</dd>
          </dl>
        </div>
        ${streamObservations}
        <div class="log-detail-block">
          <h4>请求证据</h4>
          <dl>
            <dt>Fallback 原因</dt><dd>${Utils.escapeHtml(fallbackReason)}</dd>
            <dt>WAF 兼容</dt><dd>${Utils.escapeHtml(parsed.waf_compatible || '-')}</dd>
            <dt>WAF 策略</dt><dd>${Utils.escapeHtml(parsed.waf_client_mode || '-')}</dd>
            <dt>WAF 实际套用</dt><dd>${Utils.escapeHtml(this.boolLabel(parsed.waf_applied))}</dd>
            <dt>WAF 决策</dt><dd>${Utils.escapeHtml(this.wafDecisionLabel(parsed.waf_decision))}</dd>
            <dt>请求客户端</dt><dd>${Utils.escapeHtml(parsed.client_family || '-')}</dd>
            <dt>请求并发</dt><dd>${Utils.escapeHtml(parsed.request_concurrency === 'serial_protection' ? '串行保护' : parsed.request_concurrency === 'parallel' ? '允许并发' : '-')}</dd>
            <dt>串行保护</dt><dd>${Utils.escapeHtml(this.boolLabel(parsed.serial_protection_enabled))}</dd>
            <dt>等待锁</dt><dd>${Utils.escapeHtml(parsed.lock_wait_ms ? parsed.lock_wait_ms + ' ms' : '-')}</dd>
            <dt>请求 API</dt><dd>${Utils.escapeHtml(parsed.request_api || '-')}</dd>
            <dt>请求推理强度</dt><dd>${Utils.escapeHtml(parsed.requested_reasoning_effort || 'unset')}</dd>
            <dt>推理字段来源</dt><dd>${Utils.escapeHtml(parsed.reasoning_field_source || 'none')}</dd>
            <dt>推理强度状态</dt><dd>${Utils.escapeHtml(this.reasoningValueStatusLabel(parsed.reasoning_value_status))}</dd>
            <dt>推理字段已保留</dt><dd>${Utils.escapeHtml(this.reasoningPreservedLabel(parsed.reasoning_preserved))}</dd>
            <dt>请求体模式</dt><dd>${Utils.escapeHtml(parsed.body_mode || parsed.body || '-')}</dd>
            <dt>Payload 预警</dt><dd>${this.renderPayloadWarnings(parsed)}</dd>
          </dl>
        </div>
      </div>
      ${aggregateChain}
      ${candidateFilterDetails}
      ${debugMode ? '' : this.renderTechnicalDetails(item.detail)}

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
