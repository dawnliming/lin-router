const LogsTab = {
  filters: { start: '', end: '', group: '', status: '' },
  currentOnly: false,

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
              <th>请求#次</th>
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
    panel.querySelector('#logs-clear')?.addEventListener('click', () => this.clear());
    panel.querySelector('#logs-export')?.addEventListener('click', () => { location.href = '/api/logs/export'; });
    panel.querySelector('#logs-reset')?.addEventListener('click', () => this.resetFilters());
  },

  readFilters() {
    this.filters.start = document.getElementById('log-start')?.value || '';
    this.filters.end = document.getElementById('log-end')?.value || '';
    this.filters.group = document.getElementById('log-group')?.value || '';
    this.filters.status = document.getElementById('log-status')?.value || '';
    this.renderRows();
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

  eventLabel(event) {
    const map = { ok:'成功', stream_ok:'流式成功', retry_ok:'重试成功', cooldown:'冷却切换', fallback:'自动切换', skip:'跳过', network:'网络错误', error:'错误', system:'系统' };
    return map[event] || event || '-';
  },

  statusClass(status) {
    const text = String(status || '');
    if (text === '200' || text.startsWith('2')) return 'success';
    if (text === 'network' || text.includes('failed') || text.startsWith('5')) return 'error';
    return 'warning';
  },

  tokenSummary(item) {
    const prompt = Number(item.prompt_tokens || 0);
    const completion = Number(item.completion_tokens || 0);
    const total = Number(item.total_tokens || 0);
    const cached = Number(item.cached_tokens || 0);
    if (!total && !prompt && !completion && !cached) return '-';
    const hit = prompt ? Math.round((cached / prompt) * 100) : 0;
    return `入 ${prompt} / 出 ${completion} / 总 ${total} / 缓 ${cached} (${hit}%)`;
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

  renderRows() {
    const tbody = document.getElementById('log-tbody');
    const empty = document.getElementById('logs-empty');
    const filtered = (Store.state.logs || []).filter(item => this.matches(item));
    if (!filtered.length) {
      tbody.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }
    empty.classList.add('hidden');
    tbody.innerHTML = filtered.map((item, idx) => this.rowHtml(item, idx)).join('');
    tbody.querySelectorAll('[data-log-detail]').forEach(btn => {
      btn.addEventListener('click', () => this.toggleDetail(Number(btn.dataset.logDetail)));
    });
  },

  rowHtml(item, idx) {
    return `
      <tr>
        <td class="tiny">${Utils.escapeHtml(item.time)}</td>
        <td class="tiny">${Utils.escapeHtml(this.groupName(item))}</td>
        <td>${Utils.escapeHtml(item.model || '-')}</td>
        <td><span class="pill ${this.statusClass(item.status)}">${Utils.escapeHtml(item.status)}</span></td>
        <td class="tiny">${Utils.escapeHtml(this.eventLabel(item.event))}</td>
        <td class="tiny">${item.request_id ? `${Utils.escapeHtml(item.request_id)}#${Number(item.attempt || 0)}` : '-'}</td>
        <td class="tiny">${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</td>
        <td class="tiny">${Utils.escapeHtml(this.tokenSummary(item))}</td>
        <td class="tiny result-text" title="${Utils.escapeHtml(item.detail)}">${Utils.escapeHtml(item.detail)}</td>
        <td><button type="button" data-log-detail="${idx}">查看</button></td>
      </tr>
      <tr class="log-detail-row hidden" data-log-detail-row="${idx}">
        <td colspan="10">${this.detailHtml(item)}</td>
      </tr>
    `;
  },

  detailHtml(item) {
    const parsed = this.parseDetail(item.detail);
    const routeSteps = [
      parsed.requested ? Utils.escapeHtml(parsed.requested) : (item.model || 'lin-router-auto'),
      parsed.group_name ? Utils.escapeHtml(parsed.group_name) : Utils.escapeHtml(this.groupName(item)),
      parsed.model ? Utils.escapeHtml(parsed.model) : Utils.escapeHtml(item.model),
      parsed.upstream ? Utils.escapeHtml(parsed.upstream) : '-',
      parsed.channel ? Utils.escapeHtml(parsed.channel) : '-',
    ];
    return `
      <div class="log-detail-grid">
        <div class="log-detail-block">
          <h4>基础信息</h4>
          <dl>
            <dt>时间</dt><dd>${Utils.escapeHtml(item.time)}</dd>
            <dt>耗时</dt><dd>${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</dd>
            <dt>状态</dt><dd><span class="pill ${this.statusClass(item.status)}">${Utils.escapeHtml(item.status)}</span> ${Utils.escapeHtml(this.eventLabel(item.event))}</dd>
            <dt>请求 ID / 次</dt><dd>${Utils.escapeHtml(item.request_id || '-')} #${Number(item.attempt || 0)}</dd>
          </dl>
        </div>
        <div class="log-detail-block">
          <h4>路由路径</h4>
          <div class="log-route-path">${routeSteps.map((s, i) => i === 0 ? `<span>${s}</span>` : `<span class="arrow">→</span><span>${s}</span>`).join('')}</div>
          <dl style="margin-top:10px;">
            <dt>模式</dt><dd>${Utils.escapeHtml(parsed.provider || item.provider_type || '-')}</dd>
            <dt>Mode</dt><dd>${Utils.escapeHtml(parsed.mode || '-')}</dd>
          </dl>
        </div>
      </div>
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
              <dt>详情原文</dt><dd style="white-space:pre-wrap; overflow-wrap:anywhere;">${Utils.escapeHtml(item.detail)}</dd>
            </dl>
          </div>
        </div>
      </details>
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
    if (!confirm('确定清空所有请求日志吗？本地日志文件也会被一起删除。此操作不可恢复。')) return;
    try {
      await API.clearLogs();
      await Store.load();
      Toast.success('日志已清空');
    } catch (err) {
      Toast.error('清空失败：' + err.message);
    }
  }
};
