const StatsTab = {
  // 时间范围：today / 7d / 30d / all
  range: 'today',
  // 总请求数卡片模式：today / total
  totalMode: 'today',
  // 趋势图维度：requests / success / duration / cost
  trendMetric: 'requests',
  // 趋势图时间粒度：24h / 7d
  trendGranularity: '24h',
  // 全量日志缓存，避免统计页受 /api/state 的 30 条最近日志限制
  allLogs: [],

  async refresh() {
    const panel = document.getElementById('panel-stats');
    if (!panel) return;
    try {
      this.allLogs = await API.getAllLogs();
    } catch (err) {
      Toast.error('加载统计数据失败：' + err.message);
      this.allLogs = Store.state.logs || [];
    }
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-stats');
    const stats = this.compute(this.range);
    const trendStats = this.computeTrend(this.trendGranularity);

    panel.innerHTML = `
      <div class="stats-header">
        <h2>统计</h2>
        <div class="stats-filters">
          <select id="stats-range" class="stats-select" title="统计时间范围">
            <option value="today" ${this.range === 'today' ? 'selected' : ''}>今日</option>
            <option value="7d" ${this.range === '7d' ? 'selected' : ''}>近7天</option>
            <option value="30d" ${this.range === '30d' ? 'selected' : ''}>近30天</option>
            <option value="all" ${this.range === 'all' ? 'selected' : ''}>全部</option>
          </select>
        </div>
      </div>

      <div class="stats-grid">
        <div class="stat-card stat-card-clickable" id="stat-total-card" title="点击切换今日/累计">
          <div class="stat-value">${this.totalMode === 'today' ? stats.total : stats.totalAll}</div>
          <div class="stat-label">${this.totalMode === 'today' ? '今日请求数' : '累计请求数'}</div>
          <div class="stat-switch-hint">点击切换</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.successRate}</div>
          <div class="stat-label">成功率</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.avgDuration}</div>
          <div class="stat-label">平均响应时间</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.retryCount}</div>
          <div class="stat-label">重试总次数</div>
        </div>
        <div class="stat-card">
          <div class="stat-value stat-sub-values">
            <span>${stats.inputTokens}</span>
            <span class="stat-sub-sep">/</span>
            <span>${stats.outputTokens}</span>
          </div>
          <div class="stat-label">输入 / 输出 Token</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">¥${stats.estimatedCost}</div>
          <div class="stat-label">预估花费</div>
        </div>
      </div>

      <div class="stat-trend">
        <div class="stat-trend-header">
          <h3>趋势</h3>
          <div class="stat-trend-controls">
            <div class="radio-group compact">
              <label class="radio"><input type="radio" name="trend-granularity" value="24h" ${this.trendGranularity === '24h' ? 'checked' : ''}><span>近24小时</span></label>
              <label class="radio"><input type="radio" name="trend-granularity" value="7d" ${this.trendGranularity === '7d' ? 'checked' : ''}><span>近7天</span></label>
            </div>
            <select id="stats-trend-metric" class="stats-select">
              <option value="requests" ${this.trendMetric === 'requests' ? 'selected' : ''}>请求数</option>
              <option value="success" ${this.trendMetric === 'success' ? 'selected' : ''}>成功率</option>
              <option value="duration" ${this.trendMetric === 'duration' ? 'selected' : ''}>响应时间</option>
              <option value="cost" ${this.trendMetric === 'cost' ? 'selected' : ''}>Token花费</option>
            </select>
          </div>
        </div>
        <div class="trend-chart">${this.renderTrend(trendStats)}</div>
      </div>

      <div class="stat-groups">
        <h3>按连接组统计</h3>
        <div class="stats-table-wrap">
          <table class="stats-table">
            <thead>
              <tr>
                <th>连接组</th>
                <th>请求数</th>
                <th>成功率</th>
                <th>平均响应</th>
                <th>Token</th>
                <th>花费</th>
              </tr>
            </thead>
            <tbody>
              ${this.renderGroupRows(stats.groupStats)}
            </tbody>
          </table>
        </div>
      </div>
    `;

    this.bindEvents();
  },

  bindEvents() {
    const panel = document.getElementById('panel-stats');
    if (!panel) return;

    panel.querySelector('#stats-range')?.addEventListener('change', e => {
      this.range = e.target.value;
      this.render();
    });

    panel.querySelector('#stat-total-card')?.addEventListener('click', () => {
      this.totalMode = this.totalMode === 'today' ? 'total' : 'today';
      this.render();
    });

    panel.querySelectorAll('input[name="trend-granularity"]').forEach(radio => {
      radio.addEventListener('change', e => {
        if (e.target.checked) {
          this.trendGranularity = e.target.value;
          this.render();
        }
      });
    });

    panel.querySelector('#stats-trend-metric')?.addEventListener('change', e => {
      this.trendMetric = e.target.value;
      this.render();
    });

    panel.querySelectorAll('.stats-group-name').forEach(el => {
      el.addEventListener('click', () => {
        const groupId = el.dataset.groupId;
        if (groupId) this.jumpToGroup(groupId);
      });
    });
  },

  jumpToGroup(groupId) {
    Store.select('group', groupId);
    Tabs.switch('config');
  },

  compute(range) {
    const logs = this.allLogs || Store.state.logs || [];
    const models = Store.state.models || [];
    const now = new Date();
    const start = this.rangeStart(now, range);

    const filtered = logs.filter(l => this.itemTime(l) >= start);
    const total = filtered.length;
    const totalAll = logs.length;
    const success = filtered.filter(l => String(l.status || '').startsWith('2')).length;
    const successRate = total ? `${Math.round((success / total) * 100)}%` : '-';

    const durations = filtered.map(l => Number(l.duration_ms || 0)).filter(Boolean);
    const avgDuration = durations.length ? `${Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)} ms` : '-';

    const retryCount = filtered.reduce((sum, l) => sum + Math.max(0, (Number(l.attempt) || 1) - 1), 0);

    const inputTokens = filtered.reduce((sum, l) => sum + Number(l.prompt_tokens || 0), 0);
    const outputTokens = filtered.reduce((sum, l) => sum + Number(l.completion_tokens || 0), 0);

    // 估算花费：按模型单价（元/千 Token）计算
    let estimatedCost = 0;
    const priceMap = new Map(models.filter(m => m.price_input || m.price_output).map(m => [m.name, m]));
    filtered.forEach(l => {
      const m = priceMap.get(l.model);
      if (!m) return;
      const prompt = Number(l.prompt_tokens || 0);
      const completion = Number(l.completion_tokens || 0);
      estimatedCost += (prompt * (m.price_input || 0) + completion * (m.price_output || 0)) / 1000;
    });

    const groupStats = this.computeGroupStats(filtered, models);

    return {
      total,
      totalAll,
      successRate,
      avgDuration,
      retryCount,
      inputTokens: this.formatNumber(inputTokens),
      outputTokens: this.formatNumber(outputTokens),
      estimatedCost: estimatedCost.toFixed(4),
      groupStats,
    };
  },

  computeTrend(granularity) {
    const logs = this.allLogs || Store.state.logs || [];
    const models = Store.state.models || [];
    const now = new Date();
    const priceMap = new Map(models.filter(m => m.price_input || m.price_output).map(m => [m.name, m]));

    if (granularity === '7d') {
      // 最近7天按天聚合
      const buckets = {};
      for (let i = 6; i >= 0; i--) {
        const d = new Date(now.getTime() - i * 24 * 60 * 60 * 1000);
        const key = `${d.getMonth() + 1}/${d.getDate()}`;
        buckets[key] = { label: key, total: 0, success: 0, durations: [], cost: 0 };
      }
      const start = now.getTime() - 7 * 24 * 60 * 60 * 1000;
      logs.filter(l => this.itemTime(l) >= start).forEach(l => {
        const d = new Date(this.itemTime(l));
        const key = `${d.getMonth() + 1}/${d.getDate()}`;
        if (!buckets[key]) return;
        buckets[key].total += 1;
        if (String(l.status || '').startsWith('2')) buckets[key].success += 1;
        const ms = Number(l.duration_ms || 0);
        if (ms) buckets[key].durations.push(ms);
        const m = priceMap.get(l.model);
        if (m) {
          buckets[key].cost += (Number(l.prompt_tokens || 0) * (m.price_input || 0) + Number(l.completion_tokens || 0) * (m.price_output || 0)) / 1000;
        }
      });
      return Object.values(buckets);
    }

    // 默认近24小时按小时聚合
    const buckets = {};
    for (let i = 23; i >= 0; i--) {
      const d = new Date(now.getTime() - i * 60 * 60 * 1000);
      const key = `${d.getHours()}:00`;
      buckets[key] = { label: key, total: 0, success: 0, durations: [], cost: 0 };
    }
    const start = now.getTime() - 24 * 60 * 60 * 1000;
    logs.filter(l => this.itemTime(l) >= start).forEach(l => {
      const d = new Date(this.itemTime(l));
      const key = `${d.getHours()}:00`;
      if (!buckets[key]) return;
      buckets[key].total += 1;
      if (String(l.status || '').startsWith('2')) buckets[key].success += 1;
      const ms = Number(l.duration_ms || 0);
      if (ms) buckets[key].durations.push(ms);
      const m = priceMap.get(l.model);
      if (m) {
        buckets[key].cost += (Number(l.prompt_tokens || 0) * (m.price_input || 0) + Number(l.completion_tokens || 0) * (m.price_output || 0)) / 1000;
      }
    });
    return Object.values(buckets);
  },

  computeGroupStats(logs, models) {
    const priceMap = new Map(models.filter(m => m.price_input || m.price_output).map(m => [m.name, m]));
    const groupMap = new Map((Store.state.groups || []).map(g => [g.id, g]));
    const stats = {};

    logs.forEach(l => {
      const groupId = l.group_id || '';
      const groupName = l.group_name || groupMap.get(groupId)?.name || '未知组';
      if (!stats[groupId]) {
        stats[groupId] = {
          id: groupId,
          name: groupName,
          total: 0,
          success: 0,
          durations: [],
          tokens: 0,
          cost: 0,
        };
      }
      const s = stats[groupId];
      s.total += 1;
      if (String(l.status || '').startsWith('2')) s.success += 1;
      const ms = Number(l.duration_ms || 0);
      if (ms) s.durations.push(ms);
      s.tokens += Number(l.total_tokens || 0);
      const m = priceMap.get(l.model);
      if (m) {
        s.cost += (Number(l.prompt_tokens || 0) * (m.price_input || 0) + Number(l.completion_tokens || 0) * (m.price_output || 0)) / 1000;
      }
    });

    return Object.values(stats).sort((a, b) => b.total - a.total);
  },

  rangeStart(now, range) {
    if (range === 'today') {
      return new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    }
    if (range === '7d') {
      return now.getTime() - 7 * 24 * 60 * 60 * 1000;
    }
    if (range === '30d') {
      return now.getTime() - 30 * 24 * 60 * 60 * 1000;
    }
    return 0;
  },

  itemTime(item) {
    return item.time ? new Date(String(item.time).replace(' ', 'T')).getTime() : 0;
  },

  formatNumber(n) {
    if (n === 0) return '0';
    if (n >= 10000) return `${(n / 10000).toFixed(1)}w`;
    return String(n);
  },

  renderTrend(buckets) {
    if (!buckets.length || buckets.every(b => b.total === 0)) {
      return '<div class="trend-empty">暂无数据</div>';
    }

    let values;
    let suffix = '';
    let format = v => v;

    if (this.trendMetric === 'success') {
      values = buckets.map(b => (b.total ? Math.round((b.success / b.total) * 100) : 0));
      suffix = '%';
    } else if (this.trendMetric === 'duration') {
      values = buckets.map(b => (b.durations.length ? Math.round(b.durations.reduce((a, c) => a + c, 0) / b.durations.length) : 0));
      suffix = 'ms';
    } else if (this.trendMetric === 'cost') {
      values = buckets.map(b => b.cost);
      suffix = '元';
      format = v => v.toFixed(2);
    } else {
      values = buckets.map(b => b.total);
      suffix = '次';
    }

    const max = Math.max(...values, 1);
    const labels = buckets.map(b => b.label);

    return `
      <div class="trend-bars">
        ${values.map((v, idx) => {
          const pct = (v / max) * 100;
          const tooltip = `${labels[idx]}：${format(v)}${suffix}`;
          return `<div class="trend-bar" style="height:${pct}%" title="${Utils.escapeHtml(tooltip)}"></div>`;
        }).join('')}
      </div>
      <div class="trend-labels">${labels.map(l => `<span>${l}</span>`).join('')}</div>
    `;
  },

  renderGroupRows(groupStats) {
    if (!groupStats.length) {
      return `<tr><td colspan="6" class="stats-empty-cell">暂无数据</td></tr>`;
    }
    return groupStats.map(g => {
      const successRate = g.total ? `${Math.round((g.success / g.total) * 100)}%` : '-';
      const avgDuration = g.durations.length ? `${Math.round(g.durations.reduce((a, b) => a + b, 0) / g.durations.length)} ms` : '-';
      return `
        <tr>
          <td><span class="stats-group-name" data-group-id="${g.id || ''}">${Utils.escapeHtml(g.name)}</span></td>
          <td>${g.total}</td>
          <td>${successRate}</td>
          <td>${avgDuration}</td>
          <td>${this.formatNumber(g.tokens)}</td>
          <td>¥${g.cost.toFixed(4)}</td>
        </tr>
      `;
    }).join('');
  }
};
