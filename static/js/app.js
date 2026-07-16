const App = {
  theme: 'system',
  _lastSelectionKey: '',
  _runtimeRefreshTimer: null,
  _runtimeRefreshActiveScope: '',
  _runtimeVisibilityHandler: null,
  _runtimeRefreshStates: {
    dashboard: { revision: '', activityCursor: '', inFlight: null, nextPollAt: 0, failures: 0 },
    config: { revision: '', activityCursor: '', inFlight: null, nextPollAt: 0, failures: 0 },
  },
  RUNTIME_IDLE_INTERVAL: 5000,
  RUNTIME_LIVE_INTERVAL: 1000,
  RUNTIME_BACKOFF_INTERVALS: [5000, 10000, 30000],
  RUNTIME_ACTIVITY_LIMIT: 30,

  async init() {
    this.renderTopbar();
    this.renderFabs();
    Tree.init();
    Tabs.init();
    this.bindShortcuts();

    await Store.load();
    // 应用服务器端保存的设置
    this.applySettings(Store.state.settings);
    // 首次进入显示可回访首页，同时预渲染配置页以保留原有引导能力
    DashboardTab.refresh();
    ConfigTab.onShow();

    this._lastSelectionKey = `${Store.selected.type || ''}:${Store.selected.id || ''}`;
    Store.subscribe((state, selected) => {
      document.getElementById('server-addr').textContent = `${window.location.origin}/v1`;
      this.updateStatusDot(state);
      if (Tabs.current === 'dashboard') DashboardTab.refresh();
      const selectionKey = `${selected.type || ''}:${selected.id || ''}`;
      const selectionChanged = selectionKey !== this._lastSelectionKey;
      this._lastSelectionKey = selectionKey;
      if (Tabs.current === 'config') {
        if (selectionChanged) ConfigTab.onShow();
        else ConfigTab.onRuntimeStateUpdate();
      }
      Tree.render();
    });

    this.startRuntimeRefresh();
    setInterval(() => Tree.render(), 1000);
  },

  applySettings(settings) {
    const s = settings || {};
    this.setTheme(s.theme || localStorage.getItem('lin-router-theme') || 'system', false);
    LogsTab.setAutoRefresh(s.auto_refresh_logs !== false);
  },


  startRuntimeRefresh() {
    if (this._runtimeRefreshTimer) clearInterval(this._runtimeRefreshTimer);
    this._bindRuntimeVisibility();
    this._runtimeRefreshActiveScope = '';
    // 计时器只负责检查当前 Tab；实际请求由 nextPollAt 控制，避免固定 1 秒全量拉取。
    this._runtimeRefreshTimer = setInterval(() => this._runtimeRefreshTick(), 1000);
    this._runtimeRefreshTick();
  },

  _bindRuntimeVisibility() {
    if (this._runtimeVisibilityHandler || typeof document === 'undefined') return;
    this._runtimeVisibilityHandler = () => this._onRuntimeVisibilityChange();
    document.addEventListener('visibilitychange', this._runtimeVisibilityHandler);
  },

  _onRuntimeVisibilityChange() {
    if (document.hidden) {
      if (this._runtimeRefreshTimer) clearInterval(this._runtimeRefreshTimer);
      this._runtimeRefreshTimer = null;
      return;
    }
    if (!this._runtimeRefreshTimer) {
      this._runtimeRefreshTimer = setInterval(() => this._runtimeRefreshTick(), 1000);
    }
    // 恢复可见后只立即补一次；后续仍由成功间隔或错误退避控制。
    this._runtimeRefreshActiveScope = '';
    this._runtimeRefreshTick(true);
  },

  _runtimeScopeForCurrentTab() {
    return ['dashboard', 'config'].includes(Tabs.current) ? Tabs.current : '';
  },

  _runtimeStateFor(scope) {
    return this._runtimeRefreshStates[scope] || null;
  },

  _runtimeRefreshTick(force = false) {
    if (document.hidden) return;
    const scope = this._runtimeScopeForCurrentTab();
    if (!scope) {
      this._runtimeRefreshActiveScope = '';
      return;
    }
    const state = this._runtimeStateFor(scope);
    const scopeChanged = this._runtimeRefreshActiveScope !== scope;
    if (scopeChanged) {
      this._runtimeRefreshActiveScope = scope;
      state.nextPollAt = 0;
    }
    if (state.inFlight || (!force && state.nextPollAt > Date.now())) return;
    this.refreshRuntimeState(scope, { background: true, silent: true });
  },

  _hasRuntimeField(payload, field) {
    return Object.prototype.hasOwnProperty.call(payload || {}, field);
  },

  _runtimePayload(data) {
    // 过渡期间允许服务端把运行态包在 state 内；顶层新协议优先，避免覆盖 scope/revision。
    const nested = data?.state && typeof data.state === 'object' && !Array.isArray(data.state) ? data.state : {};
    return { ...nested, ...(data || {}) };
  },

  _mergeRuntimeItems(current, runtimeItems, currentId, runtimeId) {
    const byId = new Map(runtimeItems.map(item => [item[runtimeId], item]));
    return (current || []).map(item => byId.has(item[currentId]) ? { ...item, ...byId.get(item[currentId]) } : item);
  },

  _runtimeActivityKey(item) {
    return [
      item?.request_id || '', item?.attempt || 0, item?.time || '', item?.aggregate_member_id || '',
      item?.selected_model || item?.model || '', item?.fallback_index || 0,
    ].join('|');
  },

  _mergeRuntimeActivity(previous, activity) {
    const incoming = Array.isArray(activity?.logs) ? activity.logs : [];
    if (activity?.mode !== 'delta') return incoming.slice(0, this.RUNTIME_ACTIVITY_LIMIT);
    const seen = new Set(incoming.map(item => this._runtimeActivityKey(item)));
    // delta 只包含变化项，保留之前的近期活动；同一请求的终态会覆盖旧快照。
    return [...incoming, ...(previous || []).filter(item => !seen.has(this._runtimeActivityKey(item)))]
      .slice(0, this.RUNTIME_ACTIVITY_LIMIT);
  },

  _runtimePollDelay(scope, payload) {
    const hasLiveRequests = Array.isArray(payload?.live_requests)
      ? payload.live_requests.length > 0
      : (Store.state.live_requests || []).length > 0;
    // next_poll_ms 仅接受本期已定义的两个档位，防止异常服务端值突破页面刷新上限。
    const expected = scope === 'dashboard' && hasLiveRequests ? this.RUNTIME_LIVE_INTERVAL : this.RUNTIME_IDLE_INTERVAL;
    const hint = Number(payload?.next_poll_ms);
    return hint === expected ? hint : expected;
  },

  _runtimeBackoffDelay(failures) {
    const index = Math.min(Math.max(failures - 1, 0), this.RUNTIME_BACKOFF_INTERVALS.length - 1);
    return this.RUNTIME_BACKOFF_INTERVALS[index];
  },

  applyRuntimeState(data, scope) {
    const payload = this._runtimePayload(data);
    const state = this._runtimeStateFor(scope);
    const patch = {};
    if (Array.isArray(payload.models)) {
      patch.models = this._mergeRuntimeItems(Store.state.models, payload.models, 'id', 'model_id');
    }
    if (Array.isArray(payload.aggregate_members)) {
      patch.aggregate_members = this._mergeRuntimeItems(Store.state.aggregate_members, payload.aggregate_members, 'id', 'member_id');
    }
    if (Array.isArray(payload.live_requests)) patch.live_requests = payload.live_requests;
    if (this._hasRuntimeField(payload, 'log_write_error')) patch.log_write_error = payload.log_write_error || '';

    if (scope === 'dashboard') {
      if (payload.activity && this._hasRuntimeField(payload.activity, 'logs')) {
        patch.logs = this._mergeRuntimeActivity(Store.state.logs, payload.activity);
      } else if (!payload.activity && Array.isArray(payload.logs)) {
        // 旧服务端尚未支持 activity 时，维持首页最近活动的兼容展示。
        patch.logs = payload.logs;
      }
      const activityCursor = this._hasRuntimeField(payload.activity, 'cursor')
        ? payload.activity.cursor
        : payload.activity_cursor;
      if (activityCursor !== undefined && activityCursor !== null) state.activityCursor = String(activityCursor);
    }

    const revision = payload.runtime_revision ?? payload.revision;
    if (revision !== undefined && revision !== null) state.revision = String(revision);
    if (Object.keys(patch).length) Store.update(patch);
    return payload;
  },

  async refreshRuntimeState(scope = this._runtimeScopeForCurrentTab(), options = {}) {
    if (!scope || !this._runtimeStateFor(scope)) return null;
    const state = this._runtimeStateFor(scope);
    if (state.inFlight) return state.inFlight;
    const params = { scope };
    if (state.revision) params.revision = state.revision;
    if (scope === 'dashboard' && state.activityCursor) params.activity_cursor = state.activityCursor;
    const request = API.getRuntimeState(params, { silent: options.silent !== false });
    state.inFlight = request;
    try {
      const data = await request;
      const payload = this.applyRuntimeState(data, scope);
      state.failures = 0;
      state.nextPollAt = Date.now() + this._runtimePollDelay(scope, payload);
      return data;
    } catch (err) {
      state.failures += 1;
      state.nextPollAt = Date.now() + this._runtimeBackoffDelay(state.failures);
      if (!options.background) throw err;
      console.warn('运行态刷新失败', err);
      return null;
    } finally {
      if (state.inFlight === request) state.inFlight = null;
    }
  },

  renderTopbar() {
    const topbar = document.getElementById('topbar');
    topbar.innerHTML = `
      <div class="topbar-left">
        <span class="service-status" id="service-status" title="本地代理服务运行中">
          <span class="status-dot" id="status-dot" aria-hidden="true"></span>
          <span id="status-text">服务运行中</span>
        </span>
        <button type="button" class="copy-chip" id="server-addr" title="复制兼容 OpenAI 的接口地址（自动带 /v1）" aria-label="复制兼容 OpenAI 的接口地址（自动带 /v1）">${window.location.origin}/v1</button>
      </div>
      <div class="topbar-center">
        <input type="text" class="global-search" id="global-search" placeholder="搜索连接组或模型..." aria-label="搜索连接组或模型">
      </div>
      <div class="topbar-right">
        <button class="btn-primary btn-sm" id="btn-new-group" title="新建连接组">+ 连接组</button>
        <button class="btn-secondary btn-sm" id="btn-new-aggregate" title="新建聚合模型">+ 聚合模型</button>
        <button class="utility-btn btn-sm" id="btn-export" title="导出连接组配置">导出</button>
        <button class="utility-btn btn-sm" id="btn-settings" title="打开设置">设置</button>
        <button class="utility-btn btn-sm" id="btn-theme" title="切换主题">主题</button>
      </div>
    `;

    topbar.querySelector('#server-addr').addEventListener('click', e => {
      const addr = `${window.location.origin}/v1`;
      Utils.copy(addr).then(ok => ok ? Toast.success('接口地址已复制（含 /v1）') : Toast.error('复制失败'));
    });
    const searchInput = topbar.querySelector('#global-search');
    searchInput.addEventListener('input', e => Tree.setSearch(e.target.value));
    searchInput.addEventListener('keydown', e => {
      if (e.key === 'Enter') {
        e.preventDefault();
        Tree.jumpToFirstMatch();
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        searchInput.value = '';
        Tree.setSearch('');
      }
    });
    topbar.querySelector('#btn-theme').addEventListener('click', () => this.cycleTheme());

    topbar.querySelector('#btn-new-group').addEventListener('click', () => this.createGroup());
    topbar.querySelector('#btn-new-aggregate').addEventListener('click', () => this.createAggregate());
    topbar.querySelector('#btn-export').addEventListener('click', () => this.exportConfig());
    topbar.querySelector('#btn-settings').addEventListener('click', () => this.openSettings());
  },

  loadTheme() {
    this.theme = Store.state.settings?.theme || localStorage.getItem('lin-router-theme') || 'system';
    document.documentElement.setAttribute('data-theme', this.theme);
  },

  setTheme(theme, save = true) {
    this.theme = theme || 'system';
    localStorage.setItem('lin-router-theme', this.theme);
    document.documentElement.setAttribute('data-theme', this.theme);
    const themeLabel = { light: '浅色', dark: '深色', system: '跟随系统' }[this.theme] || '跟随系统';
    const themeButton = document.getElementById('btn-theme');
    if (themeButton) {
      themeButton.title = `当前主题：${themeLabel}，点击切换`;
      themeButton.setAttribute('aria-label', `当前主题：${themeLabel}，点击切换`);
    }
    if (save) {
      API.saveSettings({ theme: this.theme }).catch(err => Toast.error('保存主题失败：' + err.message));
    }
  },

  cycleTheme() {
    const order = ['light', 'dark', 'system'];
    const idx = order.indexOf(this.theme);
    const next = order[(idx + 1) % order.length];
    this.setTheme(next);
    Toast.info(`主题已切换：${{light:'浅色', dark:'深色', system:'跟随系统'}[next]}`);
  },

  updateStatusDot(state) {
    const dot = document.getElementById('status-dot');
    if (!dot) return;
    const err = state?.log_write_error || '';
    const statusText = document.getElementById('status-text');
    const status = document.getElementById('service-status');
    if (err) {
      dot.className = 'status-dot error';
      if (statusText) statusText.textContent = '服务异常';
      if (status) status.title = err;
    } else {
      dot.className = 'status-dot';
      if (statusText) statusText.textContent = '服务运行中';
      if (status) status.title = '本地代理服务运行中';
    }
  },

  renderFabs() {
    const container = document.getElementById('fab-container');
    if (!container) return;
    container.innerHTML = `
      <button type="button" class="fab" id="fab-top" title="回到顶部" aria-label="回到顶部">↑</button>
    `;
    container.querySelector('#fab-top').addEventListener('click', () => {
      document.querySelector('.tab-panel.active')?.scrollTo({ top: 0, behavior: 'smooth' });
    });
  },

  bindShortcuts() {
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        Tree.hideMenu();
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        document.getElementById('global-search').focus();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        this.saveCurrentConfig();
      }
      if ((e.ctrlKey || e.metaKey) && /^[1-5]$/.test(e.key)) {
        e.preventDefault();
        const map = {1:'dashboard',2:'config',3:'logs',4:'test',5:'stats'};
        Tabs.switch(map[e.key]);
      }
    });
  },

  saveCurrentConfig() {
    const sel = Store.selected;
    if (Tabs.current === 'dashboard') return;
    if (sel.type === 'group') {
      const form = document.getElementById('group-form');
      if (form) form.dispatchEvent(new Event('submit'));
    } else if (sel.type === 'model') {
      const form = document.getElementById('model-form');
      if (form) form.dispatchEvent(new Event('submit'));
    } else if (sel.type === 'aggregate') {
      const form = document.getElementById('aggregate-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }
  },

  createGroup() {
    ConfigTab.startNewGroup();
  },

  async createAggregate() {
    try {
      const data = await API.createAggregate({ name: '新聚合模型', display_name: '新聚合模型', enabled: true, cooldown_minutes: 5, strategy: 'priority' });
      await Store.load();
      Store.select('aggregate', data.aggregate_model.id);
      Tabs.switch('config');
      Toast.success('已新建聚合模型，请直接编辑');
    } catch (err) {
      Toast.error('创建失败：' + err.message);
    }
  },

  async exportConfig() {
    try {
      const cfg = await API.exportConfig();
      const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'lin-router-config-export.json';
      a.click();
      Toast.success('配置已导出');
    } catch (err) {
      Toast.error('导出失败：' + err.message);
    }
  },

  importConfig() {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'application/json,.json';
    input.addEventListener('change', event => ConfigTab.onConfigImport(event));
    input.click();
  },

  openSettings() {
    SettingsPanel.open();
  }
};

document.addEventListener('DOMContentLoaded', () => App.init());
