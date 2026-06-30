const SettingsPanel = {
  open() {
    let panel = document.getElementById('settings-panel');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'settings-panel';
      panel.className = 'settings-panel hidden';
      document.body.appendChild(panel);
    }
    panel.innerHTML = this.render();
    panel.classList.remove('hidden');
    this.attachEvents(panel);
  },

  close() {
    document.getElementById('settings-panel')?.classList.add('hidden');
  },

  render() {
    const s = Store.state.settings || {};
    return `
      <div class="settings-backdrop"></div>
      <div class="settings-drawer">
        <div class="settings-header">
          <h2>设置</h2>
          <button type="button" class="settings-close" id="settings-close">×</button>
        </div>
        <div class="settings-body">
          <section class="settings-section">
            <h3>启动</h3>
            <label class="settings-row">
              <span>开机自启（Windows 当前用户）</span>
              <input id="setting-auto-start" type="checkbox" ${s.auto_start ? 'checked' : ''}>
            </label>
            <label class="settings-row">
              <span>启动后最小化到托盘</span>
              <input id="setting-start-minimized" type="checkbox" ${s.start_minimized ? 'checked' : ''}>
            </label>
            <div class="settings-hint">开机自启会写入注册表启动项；被杀软拦截属于正常情况。</div>
          </section>

          <section class="settings-section">
            <h3>外观</h3>
            <div class="settings-row">
              <span>主题</span>
              <div class="radio-group">
                <label class="radio"><input type="radio" name="setting-theme" value="light" ${s.theme === 'light' ? 'checked' : ''}><span>浅色</span></label>
                <label class="radio"><input type="radio" name="setting-theme" value="dark" ${s.theme === 'dark' ? 'checked' : ''}><span>深色</span></label>
                <label class="radio"><input type="radio" name="setting-theme" value="system" ${s.theme === 'system' ? 'checked' : ''}><span>跟随系统</span></label>
              </div>
            </div>
          </section>

          <section class="settings-section">
            <h3>日志</h3>
            <label class="settings-row">
              <span>日志页自动刷新</span>
              <input id="setting-auto-refresh-logs" type="checkbox" ${s.auto_refresh_logs !== false ? 'checked' : ''}>
            </label>
          </section>

          <section class="settings-section">
            <h3>备份与恢复</h3>
            <div class="settings-actions">
              <button type="button" id="settings-backup" class="btn-secondary">导出全部数据</button>
              <button type="button" id="settings-restore" class="btn-secondary">导入恢复全部数据</button>
              <input id="settings-restore-file" type="file" accept="application/json,.json" style="display:none">
            </div>
            <div class="settings-hint">全部数据包含所有连接组、模型配置以及设置，不包含日志。</div>
          </section>

          <section class="settings-section">
            <h3>关于</h3>
            <div class="settings-about">
              <div class="settings-about-row"><span>版本</span><span>v0.4.1</span></div>
              <div class="settings-about-row"><span>项目地址</span><a href="https://github.com/yourname/lin-router" target="_blank" rel="noopener">GitHub</a></div>
            </div>
          </section>
        </div>
      </div>
    `;
  },

  attachEvents(panel) {
    panel.querySelector('.settings-backdrop')?.addEventListener('click', () => this.close());
    panel.querySelector('#settings-close')?.addEventListener('click', () => this.close());

    panel.querySelector('#setting-auto-start')?.addEventListener('change', e => this.updateSetting('auto_start', e.target.checked));
    panel.querySelector('#setting-start-minimized')?.addEventListener('change', e => this.updateSetting('start_minimized', e.target.checked));
    panel.querySelector('#setting-auto-refresh-logs')?.addEventListener('change', e => this.updateSetting('auto_refresh_logs', e.target.checked));

    panel.querySelectorAll('input[name="setting-theme"]').forEach(radio => {
      radio.addEventListener('change', e => {
        if (e.target.checked) {
          this.updateSetting('theme', e.target.value);
          App.setTheme(e.target.value);
        }
      });
    });

    panel.querySelector('#settings-backup')?.addEventListener('click', () => this.backupAll());
    panel.querySelector('#settings-restore')?.addEventListener('click', () => panel.querySelector('#settings-restore-file')?.click());
    panel.querySelector('#settings-restore-file')?.addEventListener('change', e => this.restoreAll(e));
  },

  async updateSetting(key, value) {
    try {
      await API.saveSettings({ [key]: value });
      await Store.load();
      Toast.success('设置已保存');
    } catch (err) {
      Toast.error('保存设置失败：' + err.message);
      await Store.load();
    }
  },

  async backupAll() {
    try {
      const data = await API.req('/api/backup/export');
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `lin-router-backup-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      Toast.success('全部数据已导出');
    } catch (err) {
      Toast.error('导出失败：' + err.message);
    }
  },

  async restoreAll(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    const ok = await Modal.confirm({
      title: '恢复全部数据',
      message: '导入将覆盖当前所有连接组、模型和设置，此操作不可恢复。是否继续？',
      confirmText: '确定恢复',
      confirmClass: 'btn-danger'
    });
    if (!ok) {
      e.target.value = '';
      return;
    }
    try {
      await API.importBackup(file);
      await Store.load();
      Toast.success('全部数据已恢复');
      // 主题立即生效
      App.setTheme(Store.state.settings?.theme || 'system');
      // 日志自动刷新立即生效
      LogsTab.setAutoRefresh(Store.state.settings?.auto_refresh_logs !== false);
    } catch (err) {
      Toast.error('恢复失败：' + err.message);
    } finally {
      e.target.value = '';
    }
  }
};
