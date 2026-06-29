const StatsTab = {
  refresh() {
    const panel = document.getElementById('panel-stats');
    if (!panel) return;
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-stats');
    const stats = this.compute();
    const costHtml = stats.hasPrice ? `
      <div class="stat-card">
        <div class="stat-value">¥${stats.estimatedCost}</div>
        <div class="stat-label">估算花费</div>
      </div>
    ` : '';
    panel.innerHTML = `
      <h2>统计</h2>
      <div class="stats-grid">
        <div class="stat-card">
          <div class="stat-value">${stats.total}</div>
          <div class="stat-label">今日请求数</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.successRate}</div>
          <div class="stat-label">成功率</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.avgDuration}</div>
          <div class="stat-label">平均耗时</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.totalTokens}</div>
          <div class="stat-label">总 Token</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.cachedTokens}</div>
          <div class="stat-label">缓存节省 Token</div>
        </div>
        ${costHtml}
      </div>
      <div class="stat-trend">
        <h3>24 小时请求趋势</h3>
        <div class="trend-chart">${this.renderTrend(stats.hourly)}</div>
      </div>
    `;
  },

  compute() {
    const logs = Store.state.logs || [];
    const models = Store.state.models || [];
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const dayAgo = now.getTime() - 24 * 60 * 60 * 1000;

    const today = logs.filter(l => this.itemTime(l) >= todayStart);
    const total = today.length;
    const success = today.filter(l => String(l.status || '').startsWith('2')).length;
    const successRate = total ? `${Math.round((success / total) * 100)}%` : '-';

    const durations = today.map(l => Number(l.duration_ms || 0)).filter(Boolean);
    const avgDuration = durations.length ? `${Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)} ms` : '-';

    const totalTokens = today.reduce((sum, l) => sum + Number(l.total_tokens || 0), 0);
    const cachedTokens = today.reduce((sum, l) => sum + Number(l.cached_tokens || 0), 0);

    // 估算花费：按模型单价（元/千 Token）计算
    let estimatedCost = 0;
    let hasPrice = false;
    const priceMap = new Map(models.filter(m => m.price_input || m.price_output).map(m => [m.name, m]));
    today.forEach(l => {
      const m = priceMap.get(l.model);
      if (!m) return;
      hasPrice = true;
      const prompt = Number(l.prompt_tokens || 0);
      const completion = Number(l.completion_tokens || 0);
      estimatedCost += (prompt * (m.price_input || 0) + completion * (m.price_output || 0)) / 1000;
    });

    const hourly = {};
    for (let i = 0; i < 24; i++) hourly[i] = 0;
    logs.filter(l => this.itemTime(l) >= dayAgo).forEach(l => {
      const h = new Date(this.itemTime(l)).getHours();
      hourly[h]++;
    });

    return {
      total,
      successRate,
      avgDuration,
      totalTokens: totalTokens || '-',
      cachedTokens: cachedTokens || '-',
      estimatedCost: estimatedCost.toFixed(4),
      hasPrice,
      hourly,
    };
  },

  itemTime(item) {
    return item.time ? new Date(String(item.time).replace(' ', 'T')).getTime() : 0;
  },

  renderTrend(hourly) {
    const values = Object.values(hourly);
    const max = Math.max(...values, 1);
    const hours = Object.keys(hourly);
    if (!values.some(v => v > 0)) return '<div class="trend-empty">近 24 小时暂无数据</div>';
    return `
      <div class="trend-bars">
        ${hours.map(h => {
          const v = hourly[h];
          const pct = (v / max) * 100;
          return `<div class="trend-bar" style="height:${pct}%" title="${h}:00 - ${v} 请求"></div>`;
        }).join('')}
      </div>
      <div class="trend-labels">${hours.map(h => `<span>${h}</span>`).join('')}</div>
    `;
  }
};
