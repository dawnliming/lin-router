const LogsTab = {
  filters: { start: '', end: '', group: '', status: '' },
  currentOnly: false,
  autoRefresh: true,
  refreshTimer: null,
  REFRESH_INTERVAL: 5000,

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
    return parsed.skip_reason === 'member_disabled';
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
    const map = { ok:'成功', stream_ok:'首包成功', retry_ok:'重试成功', cooldown:'冷却切换', fallback:'自动切换', skip:'跳过', network:'网络错误', error:'错误', system:'系统', stream_timeout:'流式超时', waf_lock_timeout:'候选忙', stream_done:'流式完成', stream_idle_timeout:'流式空闲超时', client_disconnected:'客户端断开' };
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
    const wasAtBottom = keepScroll && wrap ? (wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 30) : false;
    // 记住当前展开的详情行，避免自动刷新时把它关上
    const openIdx = Array.from(document.querySelectorAll('[data-log-detail-row]')).findIndex(r => !r.classList.contains('hidden'));
    const filtered = this.filterLogs();
    if (filtered.length === 0) {
      tbody.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');
    tbody.innerHTML = filtered.map((item, idx) => this.rowHtml(item, idx)).join('');
    if (openIdx >= 0 && openIdx < filtered.length) {
      const row = document.querySelector(`[data-log-detail-row="${openIdx}"]`);
      row?.classList.remove('hidden');
    }
    tbody.querySelectorAll('[data-log-detail]').forEach(btn => {
      btn.addEventListener('click', () => this.toggleDetail(Number(btn.dataset.logDetail)));
    });
    // 点击详情单元格也可展开/收起详情行，解决列表截断后无法查看完整内容的问题
    tbody.querySelectorAll('[data-log-detail-preview]').forEach(cell => {
      cell.addEventListener('click', () => this.toggleDetail(Number(cell.dataset.logDetailPreview)));
    });

    if (wasAtBottom && wrap) {
      wrap.scrollTop = wrap.scrollHeight;
    }
  },

  rowHtml(item, idx) {
    return `
      <tr>
        <td class="tiny">${Utils.escapeHtml(item.time)}</td>
        <td class="tiny">${Utils.escapeHtml(this.groupName(item))}</td>
        <td>${Utils.escapeHtml(item.model || '-')}</td>
        <td><span class="pill ${this.statusClass(item.status, item)}">${Utils.escapeHtml(item.status)}</span></td>
        <td class="tiny">${Utils.escapeHtml(this.eventLabel(item.event, item))}</td>
        <td class="tiny">${Number(item.attempt || 0) || 1}</td>
        <td class="tiny">${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</td>
        <td class="tiny">${Utils.escapeHtml(this.tokenSummary(item))}</td>
        <td class="tiny result-text log-detail-preview" title="${Utils.escapeHtml(item.detail)}" data-log-detail-preview="${idx}">${this.formatDetailPreview(item.detail)}</td>
        <td><button type="button" data-log-detail="${idx}">查看</button></td>
      </tr>
      <tr class="log-detail-row hidden" data-log-detail-row="${idx}">
        <td colspan="10">${this.detailHtml(item)}</td>
      </tr>
    `;
  },

  formatDetailPreview(detail) {
    if (!detail) return '-';
    // 把分号分隔的 detail 做简单可读化处理
    const text = String(detail).replace(/;/g, '; ');
    const escaped = Utils.escapeHtml(text);
    return escaped.length > 200 ? escaped.slice(0, 200) + '…' : escaped;
  },

  formatJsonBlock(value) {
    if (!value) return '-';
    const str = String(value);
    // 尝试把分号键值对或 JSON 字符串格式化显示
    let formatted = Utils.escapeHtml(str);
    // 如果有 model=... 这类键值对，高亮关键 key
    formatted = formatted.replace(/(requested|group_name|model|upstream|channel|mode|error)=/g, '<strong>$1=</strong>');
    return formatted;
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
      const isStable = parsed.skip_reason === 'member_disabled';
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
    const message = parsed.message || '上游中转站拦截了请求';
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

  detailHtml(item) {
    const parsed = this.parseDetail(item.detail);
    const rawDetail = item.detail ? String(item.detail) : '';
    const rawBlock = rawDetail ? `
      <div class="log-detail-block log-detail-raw-block">
        <h4>详情原文</h4>
        <div class="log-detail-raw">${this.formatJsonBlock(rawDetail)}</div>
      </div>
    ` : '';
    const wafHint = this.renderWafHint(parsed);
    const isAggregate = parsed.resolved_as && parsed.resolved_as.startsWith('aggregate');
    const routeSteps = isAggregate ? this.aggregateRouteSteps(parsed, item) : [
      parsed.requested ? Utils.escapeHtml(parsed.requested) : (item.model || 'lin-router-auto'),
      parsed.group_name ? Utils.escapeHtml(parsed.group_name) : Utils.escapeHtml(this.groupName(item)),
      parsed.model ? Utils.escapeHtml(parsed.model) : Utils.escapeHtml(item.model),
      parsed.upstream ? Utils.escapeHtml(parsed.upstream) : '-',
      parsed.channel ? Utils.escapeHtml(parsed.channel) : '-',
    ];
    const aggregateChain = isAggregate ? this.renderAggregateChain(parsed) : '';
    const candidateFilterDetails = this.renderCandidateFilterDetails(item);
    return `
      ${rawBlock}
      ${wafHint}
      <div class="log-detail-grid">
        <div class="log-detail-block">
          <h4>基础信息</h4>
          <dl>
            <dt>时间</dt><dd>${Utils.escapeHtml(item.time)}</dd>
            <dt>耗时</dt><dd>${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</dd>
            <dt>状态</dt><dd><span class="pill ${this.statusClass(item.status, item)}">${Utils.escapeHtml(item.status)}</span> ${Utils.escapeHtml(this.eventLabel(item.event, item))}</dd>
            <dt>请求 ID / 次</dt><dd>${Utils.escapeHtml(item.request_id || '-')} / ${Number(item.attempt || 0) || 1}</dd>
          </dl>
        </div>
        <div class="log-detail-block">
          <h4>${isAggregate ? '聚合调度路径' : '路由路径'}</h4>
          <div class="log-route-path">${routeSteps.map((s, i) => i === 0 ? `<span>${s}</span>` : `<span class="arrow">→</span><span>${s}</span>`).join('')}</div>
          ${isAggregate ? `<dl style="margin-top:10px;"><dt>selection_reason</dt><dd>${Utils.escapeHtml(parsed.selection_reason || '-')}</dd><dt>fallback_index</dt><dd>${Utils.escapeHtml(parsed.fallback_index || '0')}</dd></dl>` : `
          <dl style="margin-top:10px;">
            <dt>模式</dt><dd>${Utils.escapeHtml(parsed.provider || item.provider_type || '-')}</dd>
            <dt>Mode</dt><dd>${Utils.escapeHtml(parsed.mode || '-')}</dd>
          </dl>`}
        </div>
        <div class="log-detail-block">
          <h4>诊断信息</h4>
          <dl>
            <dt>Usage 来源</dt><dd>${Utils.escapeHtml(parsed.usage_source || item.usage_source || '-')}</dd>
            <dt>Header 策略</dt><dd>${Utils.escapeHtml(parsed.header_policy || '-')}</dd>
            <dt>Accept</dt><dd>${Utils.escapeHtml(parsed.accept || '-')}</dd>
            <dt>Content-Type</dt><dd>${Utils.escapeHtml(parsed.content_type || '-')}</dd>
            <dt>UA 类型</dt><dd>${Utils.escapeHtml(parsed.user_agent_family || '-')}</dd>
            <dt>WAF 兼容</dt><dd>${Utils.escapeHtml(parsed.waf_compatible || '-')}</dd>
            <dt>WAF 锁</dt><dd>${Utils.escapeHtml(parsed.waf_lock_enabled || '-')}</dd>
            <dt>等待锁</dt><dd>${Utils.escapeHtml(parsed.lock_wait_ms ? parsed.lock_wait_ms + ' ms' : '-')}</dd>
            <dt>Fallback 原因</dt><dd>${Utils.escapeHtml(parsed.fallback_reason || parsed.selection_reason || '-')}</dd>
            <dt>Failure Scope</dt><dd>${Utils.escapeHtml(item.failure_scope || parsed.failure_scope || '-')}</dd>
            <dt>HTTP 客户端</dt><dd>${Utils.escapeHtml(parsed.http_client || '-')}</dd>
            <dt>HTTP 版本</dt><dd>${Utils.escapeHtml(parsed.upstream_http_version || '-')}</dd>
            <dt>Tools 排序</dt><dd>${Utils.escapeHtml(parsed.tools_normalized || '-')}</dd>
            <dt>Payload 预警</dt><dd>${this.renderPayloadWarnings(parsed)}</dd>
          </dl>
        </div>
      </div>
      ${aggregateChain}
      ${candidateFilterDetails}
      <details style="margin-top:10px;">
        <summary style="font-size:12px; color:var(--text-tertiary); cursor:pointer;">技术细节</summary>
        <div class="log-detail-grid" style="margin-top:8px;">
          <div class="log-detail-block">
            <dl>
              <dt>上游地址</dt><dd>${Utils.escapeHtml(parsed.upstream || '-')}</dd>
              <dt>Body 模式</dt><dd>${Utils.escapeHtml(parsed.body || '-')}</dd>
              <dt>Fingerprint</dt><dd>${Utils.escapeHtml(parsed.fingerprint || '-')}</dd>
            </dl>
          </div>
          <div class="log-detail-block">
            <dl>
              <dt>Tokens</dt><dd>${Utils.escapeHtml(this.tokenSummary(item))}</dd>
              <dt>详情原文</dt><dd class="log-detail-raw">${this.formatJsonBlock(item.detail)}</dd>
            </dl>
          </div>
        </div>
      </details>
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
