const App = {
  theme: 'system',
  sidebarCollapsed: false,
  _lastSelectionKey: '',
  _runtimeRefreshTimer: null,

  async init() {
    this.restoreSidebarState();
    this.renderTopbar();
    this.renderFabs();
    Tree.init();
    Tabs.init();
    this.bindShortcuts();
    this.bindResize();

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
    this._runtimeRefreshTimer = setInterval(() => this.refreshRuntimeState(), 5000);
  },

  async refreshRuntimeState() {
    if (document.hidden) return;
    if (!['dashboard', 'config', 'logs'].includes(Tabs.current)) return;
    try {
      const data = await API.getRuntimeState({ silent: true });
      const patch = {
        logs: data.logs || Store.state.logs || [],
        log_write_error: data.log_write_error || '',
        live_requests: data.live_requests || [],
      };
      if (data.models) {
        const runtimeById = new Map(data.models.map(item => [item.model_id, item]));
        patch.models = (Store.state.models || []).map(model => runtimeById.has(model.id) ? { ...model, ...runtimeById.get(model.id) } : model);
      }
      if (data.aggregate_members) {
        const runtimeById = new Map(data.aggregate_members.map(item => [item.member_id, item]));
        patch.aggregate_members = (Store.state.aggregate_members || []).map(member => runtimeById.has(member.id) ? { ...member, ...runtimeById.get(member.id) } : member);
      }
      Store.update(patch);
      if (Tabs.current === 'logs') LogsTab.renderRows(true);
    } catch (err) {
      console.warn('运行态刷新失败', err);
    }
  },

  renderTopbar() {
    const topbar = document.getElementById('topbar');
    topbar.innerHTML = `
      <div class="topbar-left">
        <span class="status-dot" id="status-dot" title="运行中"></span>
        <span class="copy-chip" id="server-addr" title="点击复制兼容 OpenAI 的接口地址（自动带 /v1）">${window.location.origin}/v1</span>
      </div>
      <div class="topbar-center">
        <input type="text" class="global-search" id="global-search" placeholder="搜索连接组或模型...">
      </div>
      <div class="topbar-right">
        <button class="btn-primary btn-sm" id="btn-new-group" title="新建连接组">+ 连接组</button>
        <button class="btn-primary btn-sm" id="btn-new-aggregate" title="新建聚合模型">+ 聚合模型</button>
        <button class="icon-btn" id="btn-export" title="导出连接组配置">💾</button>
        <button class="icon-btn" id="btn-settings" title="设置">⚙</button>
        <button class="icon-btn" id="btn-theme" title="切换主题">🌓</button>
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

  toggleSidebar() {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    this.applySidebarState();
    localStorage.setItem('lin-router-sidebar-collapsed', this.sidebarCollapsed ? '1' : '0');
  },

  restoreSidebarState() {
    this.sidebarCollapsed = localStorage.getItem('lin-router-sidebar-collapsed') === '1';
    this.applySidebarState();
  },

  applySidebarState() {
    document.querySelector('.app')?.classList.toggle('sidebar-collapsed', this.sidebarCollapsed);
    const btn = document.getElementById('sidebar-collapse');
    if (btn) btn.textContent = this.sidebarCollapsed ? '▶' : '◀';
  },

  updateStatusDot(state) {
    const dot = document.getElementById('status-dot');
    if (!dot) return;
    const err = state?.log_write_error || '';
    if (err) {
      dot.className = 'status-dot error';
      dot.title = err;
    } else {
      dot.className = 'status-dot';
      dot.title = '运行中';
    }
  },

  renderFabs() {
    const container = document.getElementById('fab-container');
    if (!container) return;
    container.innerHTML = `
      <button class="fab" id="fab-top" title="回到顶部">↑</button>
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
    }
  },

  bindResize() {
    const check = () => {
      const shouldCollapse = window.innerWidth < 1200;
      if (shouldCollapse !== this.sidebarCollapsed) {
        this.toggleSidebar();
      }
    };
    window.addEventListener('resize', Utils.debounce(check, 200));
    check();
  },

  async createGroup() {
    try {
      const data = await API.createGroup({ name: '新连接组', provider_type: 'ark', base_url: '', api_key: '', ark_api_key: '' });
      await Store.load();
      Store.select('group', data.group.id);
      Tabs.switch('config');
      Toast.success('已新建连接组，请直接编辑');
    } catch (err) {
      Toast.error('创建失败：' + err.message);
    }
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

  openSettings() {
    SettingsPanel.open();
  }
};

document.addEventListener('DOMContentLoaded', () => App.init());
