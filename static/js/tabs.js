const Tabs = {
  current: 'config',
  tabs: [
    { id: 'config', label: '配置', icon: '📝' },
    { id: 'test', label: '代理测试', icon: '🧪' },
    { id: 'logs', label: '最近请求', icon: '📜' },
    { id: 'stats', label: '统计', icon: '📊' }
  ],

  init() {
    const bar = document.getElementById('tabbar');
    bar.innerHTML = this.tabs.map(t => `
      <button class="tab-btn ${t.id === this.current ? 'active' : ''}" data-tab="${t.id}">
        <span>${t.icon}</span><span>${t.label}</span>
      </button>
    `).join('');

    bar.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      this.switch(btn.dataset.tab);
    });

    this.renderPanels();
  },

  switch(tabId) {
    if (this.current === tabId) return;
    this.current = tabId;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.tab === tabId));

    if (tabId === 'logs') LogsTab.refresh();
    if (tabId === 'stats') StatsTab.refresh();
    if (tabId === 'test') TestTab.onShow();
    if (tabId === 'config') ConfigTab.onShow();
  },

  renderPanels() {
    const content = document.getElementById('tab-content');
    content.innerHTML = this.tabs.map(t => `
      <div class="tab-panel ${t.id === this.current ? 'active' : ''}" data-tab="${t.id}">
        <div class="tab-panel-inner" id="panel-${t.id}"></div>
      </div>
    `).join('');
  }
};
