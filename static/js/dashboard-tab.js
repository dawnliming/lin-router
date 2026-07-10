const DashboardTab = {
  refresh() {
    const panel = document.getElementById('panel-dashboard');
    if (!panel) return;
    panel.innerHTML = this.render();
    this.attachEvents(panel);
  },

  render() {
    const state = Store.state || {};
    const groups = state.groups || [];
    const models = state.models || [];
    const aggregates = state.aggregate_models || [];
    const members = state.aggregate_members || [];
    const logs = state.logs || [];
    const recent = logs.filter(log => !this.isConfigSkip(log)).slice(0, 30);
    const success = recent.filter(l => String(l.status || '').startsWith('2')).length;
    const successRate = recent.length ? `${Math.round((success / recent.length) * 100)}%` : '-';
    const fallbackCount = recent.filter(l => ['fallback', 'retry_ok'].includes(String(l.event || ''))).length;
    const busyCount = recent.filter(l => {
      const text = `${l.event || ''};${l.detail || ''}`;
      return text.includes('candidate_busy') || text.includes('large_task_in_progress') || text.includes('waf_lock_timeout');
    }).length;
    const upstreamTimeoutCount = recent.filter(l => `${l.event || ''};${l.detail || ''}`.includes('upstream_timeout') || `${l.detail || ''}`.includes('read_timeout') || `${l.detail || ''}`.includes('stream_idle_timeout')).length;
    const wafBlockedCount = recent.filter(l => `${l.event || ''};${l.detail || ''}`.includes('waf_blocked')).length;
    const availableGroups = groups.filter(g => g.usable !== false).length;
    const enabledAggregates = aggregates.filter(a => a.enabled !== false).length;
    const baseUrl = `${window.location.origin}/v1`;
    const enabledAggregateList = aggregates.filter(a => a.enabled !== false);
    const hasConfig = groups.length > 0 || aggregates.length > 0;
    const aggregateSummary = this.aggregateSummaryCards(aggregates, recent);
    const liveRequests = state.live_requests || [];

    return `
      <div class="dashboard-page">
        <div class="dashboard-hero">
          <div>
            <div class="dashboard-eyebrow">Lin Router 正在运行</div>
            <h2>首页 / Dashboard</h2>
            <p>快速查看状态、复制接入信息，并跳转到常用配置入口。</p>
          </div>
          <div class="dashboard-hero-actions">
            <button type="button" class="btn-primary" data-dashboard-action="new-group">+ 连接组</button>
            <button type="button" class="btn-secondary" data-dashboard-action="new-aggregate">+ 聚合模型</button>
          </div>
        </div>
        <div class="dashboard-grid">
          ${this.metricCard('服务状态', state.log_write_error ? '日志异常' : '运行中', state.log_write_error || '本地代理服务已启动')}
          ${this.metricCard('可用连接组', `${availableGroups} / ${groups.length}`, `${models.length} 个模型配置`)}
          ${this.metricCard('聚合模型', `${enabledAggregates} / ${aggregates.length}`, `${members.length} 个聚合成员`)}
          ${this.metricCard('最近成功率', successRate, `最近 ${recent.length} 条请求`)}
          ${this.metricCard('Fallback', `${fallbackCount} 次`, '最近请求自动切换次数')}
          ${this.metricCard('候选忙', `${busyCount} 次`, '大上下文或锁等待切换')}
          ${this.metricCard('上游超时 / WAF', `${upstreamTimeoutCount} / ${wafBlockedCount}`, '最近请求健康信号')}
        </div>
        ${this.renderLiveRequests(liveRequests)}
        ${aggregateSummary}
        <div class="dashboard-two-col">
          <section class="dashboard-card dashboard-access-section">
            <h3>客户端接入</h3>
            ${this.renderAggregateAccessCards(enabledAggregateList, baseUrl)}
          </section>
          <section class="dashboard-card">
            <h3>${hasConfig ? '快捷操作' : '新手步骤'}</h3>
            <div class="quick-steps">
              <button type="button" data-dashboard-action="new-group">1. 创建连接组</button>
              <button type="button" data-dashboard-action="config">2. 导入或添加模型</button>
              <button type="button" data-dashboard-action="new-aggregate">3. 创建聚合模型</button>
              <button type="button" data-dashboard-action="test">4. 发起测试请求</button>
              <button type="button" data-dashboard-action="logs">5. 查看请求日志</button>
            </div>
          </section>
        </div>
      </div>
    `;
  },

  metricCard(label, value, hint) {
    return `<section class="dashboard-metric"><span>${Utils.escapeHtml(label)}</span><strong>${Utils.escapeHtml(value)}</strong><small>${Utils.escapeHtml(hint || '')}</small></section>`;
  },

  renderLiveRequests(items) {
    const rows = (items || []).slice(0, 8).map(item => {
      const slow = item.slow ? ' slow' : '';
      const elapsed = this.formatElapsed(item.elapsed_ms || 0);
      const hint = item.possible_reason || (item.slow ? '请求耗时较长，请关注当前阶段' : '处理中');
      const stageLabel = item.stage_label || this.liveStageLabel(item.stage);
      return `
        <div class="live-request-row${slow}">
          <div><strong>${Utils.escapeHtml(item.request_id_short || String(item.request_id || '').slice(0, 8))}</strong><span>${Utils.escapeHtml(item.requested_model || item.model || '-')}</span></div>
          <div>${Utils.escapeHtml(item.group || item.candidate || '-')}</div>
          <div><span class="pill ${item.slow ? 'warning' : 'info'}">${Utils.escapeHtml(stageLabel)}</span></div>
          <div>${Utils.escapeHtml(elapsed)}</div>
          <small>${Utils.escapeHtml(hint)}</small>
        </div>`;
    }).join('');
    return `
      <section class="dashboard-card live-requests-card">
        <div class="section-title-row">
          <h3>实时请求观测</h3>
          <span class="pill ${items.length ? 'warning' : 'success'}">${items.length ? `${items.length} 个进行中` : '空闲'}</span>
        </div>
        ${rows || '<div class="empty-hint">当前没有正在处理的请求。</div>'}
      </section>
    `;
  },

  formatElapsed(ms) {
    const seconds = Math.max(0, Math.round(Number(ms || 0) / 1000));
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  },

  liveStageLabel(stage) {
    const map = {
      selecting_candidate: '选择候选',
      preparing_upstream: '准备上游请求',
      waiting_waf_lock: '等待 WAF 锁',
      connecting_upstream: '连接上游',
      waiting_first_byte: '等待首包',
      streaming: '接收流式响应',
      receiving_response: '接收响应',
      candidate_busy: '候选忙/等待锁超时'
    };
    return map[stage] || stage || '处理中';
  },

  renderAggregateAccessCards(aggregates, baseUrl) {
    if (!aggregates.length) {
      return `
        <div class="dashboard-empty-access">
          <p>暂无启用的聚合模型。</p>
          <button type="button" class="btn-primary btn-sm" data-dashboard-action="new-aggregate">去创建聚合模型</button>
        </div>
      `;
    }
    return `<div class="dashboard-access-grid">${aggregates.map(aggregate => `
      <div class="dashboard-access-card">
        <div class="dashboard-access-title">${Utils.escapeHtml(aggregate.display_name || aggregate.name)}</div>
        <div class="copy-row"><span>Base URL</span><code>${Utils.escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(baseUrl)}">复制</button></div>
        <div class="copy-row"><span>API Key</span><code>${Utils.escapeHtml(aggregate.route_key || '未生成')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregate.route_key || '')}" ${aggregate.route_key ? '' : 'disabled'}>复制</button></div>
        <div class="copy-row"><span>Model</span><code>${Utils.escapeHtml(aggregate.name || '-')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregate.name || '')}" ${aggregate.name ? '' : 'disabled'}>复制</button></div>
      </div>
    `).join('')}</div>`;
  },

  aggregateSummaryCards(aggregates, logs) {
    if (!aggregates.length) return '';
    const cards = aggregates.slice(0, 4).map(aggregate => {
      const related = logs.filter(log => log.aggregate_id === aggregate.id || log.aggregate_model === aggregate.name || log.model === aggregate.name);
      const real = related.filter(log => !this.isConfigSkip(log));
      const success = real.filter(log => String(log.status || '').startsWith('2') && ['ok', 'stream_done', 'retry_ok', 'stream_ok'].includes(String(log.event || ''))).length;
      const busy = related.filter(log => `${log.event || ''};${log.detail || ''}`.includes('candidate_busy') || `${log.detail || ''}`.includes('large_task_in_progress')).length;
      const cached = real.reduce((sum, log) => sum + Number(log.cached_tokens || 0), 0);
      const prompt = real.reduce((sum, log) => sum + Number(log.prompt_tokens || 0), 0);
      const rate = real.length ? `${Math.round((success / real.length) * 100)}%` : '暂无数据';
      const cacheRate = prompt ? `${Math.round((cached / prompt) * 100)}%` : '暂无数据';
      return `
        <section class="dashboard-card dashboard-aggregate-card" data-dashboard-action="aggregate" data-aggregate-id="${aggregate.id}">
          <h3>${Utils.escapeHtml(aggregate.display_name || aggregate.name)}</h3>
          <div class="dashboard-mini-grid">
            <span>成功率 <strong>${rate}</strong></span>
            <span>候选忙 <strong>${busy}</strong></span>
            <span>Cache <strong>${cacheRate}</strong></span>
          </div>
        </section>
      `;
    }).join('');
    return `<div class="dashboard-aggregate-summary"><h3>聚合收益摘要</h3><div class="dashboard-aggregate-grid">${cards}</div></div>`;
  },

  isConfigSkip(log) {
    if (log.event !== 'skip') return false;
    const detail = String(log.detail || '');
    return ['member_disabled', 'member_cooling', 'underlying_model_disabled', 'underlying_model_cooling'].some(reason => detail.includes(`skip_reason=${reason}`));
  },

  attachEvents(panel) {
    panel.querySelectorAll('[data-copy-value]').forEach(btn => {
      btn.addEventListener('click', () => {
        const value = (btn.dataset.copyValue || '').trim();
        if (!value) return Toast.warning('暂无可复制内容');
        Utils.copy(value).then(ok => ok ? Toast.success('已复制') : Toast.error('复制失败'));
      });
    });
    panel.querySelectorAll('[data-dashboard-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const action = btn.dataset.dashboardAction;
        if (action === 'new-group') return App.createGroup();
        if (action === 'new-aggregate') return App.createAggregate();
        if (action === 'config') return Tabs.switch('config');
        if (action === 'test') return Tabs.switch('test');
        if (action === 'logs') return Tabs.switch('logs');
        if (action === 'aggregate') { Store.select('aggregate', btn.dataset.aggregateId); return Tabs.switch('config'); }
      });
    });
  }
};
