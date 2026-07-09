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
    const recent = logs.slice(0, 30);
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
    const firstAggregate = aggregates.find(a => a.enabled !== false) || aggregates[0];
    const aggregateKey = firstAggregate?.route_key || '';
    const hasConfig = groups.length > 0 || aggregates.length > 0;

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
        <div class="dashboard-two-col">
          <section class="dashboard-card">
            <h3>客户端接入</h3>
            <div class="copy-row"><span>Base URL</span><code>${Utils.escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(baseUrl)}">复制</button></div>
            <div class="copy-row"><span>聚合 Key</span><code>${Utils.escapeHtml(aggregateKey || '暂无聚合模型')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregateKey)}" ${aggregateKey ? '' : 'disabled'}>复制</button></div>
            <div class="copy-row"><span>模型名</span><code>${Utils.escapeHtml(firstAggregate?.name || '选择你的模型名')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(firstAggregate?.name || '')}" ${firstAggregate?.name ? '' : 'disabled'}>复制</button></div>
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
      });
    });
  }
};
