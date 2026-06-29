const StatsTab = {
  refresh() {
    const panel = document.getElementById('panel-stats');
    if (!panel) return;
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-stats');
    const stats = this.compute();
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
      </div>
      <div class="stat-trend">
        <h3>24 小时请求趋势</h3>
        <div class="trend-chart">${this.renderTrend(stats.hourly)}</div>
      </div>
    `;
  },

  compute() {
    const logs = Store.state.logs || [];
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

    const hourly = {};
    for (let i = 0; i < 24; i++) hourly[i] = 0;
    logs.filter(l => this.itemTime(l) >= dayAgo).forEach(l => {
      const h = new Date(this.itemTime(l)).getHours();
      hourly[h]++;
    });

    return { total, successRate, avgDuration, totalTokens: totalTokens || '-', hourly };
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
