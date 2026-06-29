const App = {
  theme: 'system',
  sidebarCollapsed: false,

  async init() {
    this.loadTheme();
    this.renderTopbar();
    Tree.init();
    Tabs.init();
    this.bindShortcuts();
    this.bindResize();

    await Store.load();
    Store.subscribe(() => {
      document.getElementById('server-addr').textContent = window.location.origin;
      ConfigTab.onShow();
      Tree.render();
    });

    setInterval(() => Tree.render(), 1000);
  },

  renderTopbar() {
    const topbar = document.getElementById('topbar');
    topbar.innerHTML = `
      <div class="topbar-left">
        <span class="status-dot" id="status-dot" title="运行中"></span>
        <span class="copy-chip" id="server-addr" title="点击复制本地地址">http://127.0.0.1:8234</span>
        <span class="copy-chip" id="global-key" title="点击复制全局 Key">lin-router</span>
      </div>
      <div class="topbar-center">
        <input type="text" class="global-search" id="global-search" placeholder="搜索连接组或模型...">
      </div>
      <div class="topbar-right">
        <button class="icon-btn" id="btn-new-group" title="新建连接组">+</button>
        <button class="icon-btn" id="btn-export" title="导出配置">💾</button>
        <button class="icon-btn" id="btn-settings" title="设置">⚙</button>
        <button class="icon-btn" id="btn-theme" title="切换主题">🌓</button>
      </div>
    `;

    topbar.querySelector('#server-addr').addEventListener('click', e => {
      Utils.copy(e.target.textContent).then(ok => ok ? Toast.success('地址已复制') : Toast.error('复制失败'));
    });
    topbar.querySelector('#global-key').addEventListener('click', () => {
      Utils.copy('lin-router').then(ok => ok ? Toast.success('全局 Key 已复制') : Toast.error('复制失败'));
    });
    topbar.querySelector('#global-search').addEventListener('input', e => Tree.setSearch(e.target.value));
    topbar.querySelector('#btn-theme').addEventListener('click', () => this.cycleTheme());

    topbar.querySelector('#btn-new-group').addEventListener('click', () => this.createGroup());
    topbar.querySelector('#btn-export').addEventListener('click', () => this.exportConfig());
    topbar.querySelector('#btn-settings').addEventListener('click', () => this.openSettings());
  },

  loadTheme() {
    this.theme = localStorage.getItem('lin-router-theme') || 'system';
    document.documentElement.setAttribute('data-theme', this.theme);
  },

  cycleTheme() {
    const order = ['light', 'dark', 'system'];
    const idx = order.indexOf(this.theme);
    this.theme = order[(idx + 1) % order.length];
    localStorage.setItem('lin-router-theme', this.theme);
    document.documentElement.setAttribute('data-theme', this.theme);
    Toast.info(`主题已切换：${{light:'浅色', dark:'深色', system:'跟随系统'}[this.theme]}`);
  },

  toggleSidebar() {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    document.querySelector('.app').classList.toggle('sidebar-collapsed', this.sidebarCollapsed);
    const btn = document.getElementById('sidebar-collapse');
    if (btn) btn.textContent = this.sidebarCollapsed ? '▶' : '◀';
  },

  bindShortcuts() {
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        document.getElementById('global-search').focus();
      }
      if ((e.ctrlKey || e.metaKey) && /^[1-4]$/.test(e.key)) {
        e.preventDefault();
        const map = {1:'config',2:'test',3:'logs',4:'stats'};
        Tabs.switch(map[e.key]);
      }
    });
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
    const name = prompt('新建连接组名称：', '新连接组');
    if (!name) return;
    try {
      await API.createGroup({ name, provider_type: 'ark', base_url: '', api_key: '', ark_api_key: '' });
      await Store.load();
      Toast.success('连接组已创建');
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
    Tabs.switch('config');
    Toast.info('设置面板将在迭代2中接入');
  }
};

document.addEventListener('DOMContentLoaded', () => App.init());
