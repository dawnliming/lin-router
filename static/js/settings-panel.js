const SettingsPanel = {
  _lastFocused: null,
  _keyHandler: null,

  open() {
    let panel = document.getElementById('settings-panel');
    const wasHidden = !panel || panel.classList.contains('hidden');
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'settings-panel';
      panel.className = 'settings-panel hidden';
      document.body.appendChild(panel);
    }
    if (wasHidden) this._lastFocused = document.activeElement;
    panel.innerHTML = this.render();
    panel.classList.remove('hidden');
    this.attachEvents(panel);
    this.bindFocusTrap(panel);
  },

  close() {
    const panel = document.getElementById('settings-panel');
    if (!panel || panel.classList.contains('hidden')) return;
    panel.classList.add('hidden');
    if (this._keyHandler) document.removeEventListener('keydown', this._keyHandler);
    this._keyHandler = null;
    if (this._lastFocused?.isConnected && typeof this._lastFocused.focus === 'function') {
      this._lastFocused.focus({ preventScroll: true });
    }
    this._lastFocused = null;
  },

  render() {
    const s = Store.state.settings || {};
    return `
      <div class="settings-backdrop"></div>
      <div class="settings-drawer" role="dialog" aria-modal="true" aria-labelledby="settings-title" tabindex="-1">
        <div class="settings-header">
          <h2 id="settings-title">设置</h2>
          <button type="button" class="settings-close" id="settings-close" aria-label="关闭设置">×</button>
        </div>
        <div class="settings-body">
          <section class="settings-section">
            <h3>启动</h3>
            <label class="settings-row">
              <span>开机自启</span>
              <input id="setting-auto-start" type="checkbox" ${s.auto_start ? 'checked' : ''}>
            </label>
            <label class="settings-row">
              <span>启动后最小化到托盘/状态栏</span>
              <input id="setting-start-minimized" type="checkbox" ${s.start_minimized ? 'checked' : ''}>
            </label>
            <div class="settings-hint">开机自启会写入系统启动项（Windows：注册表；macOS：LaunchAgent）；被系统安全软件拦截属于正常情况。</div>
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
            <label class="settings-row">
              <span>调试模式：日志详情显示原始诊断字段</span>
              <input id="setting-debug-mode" type="checkbox" ${s.debug_mode ? 'checked' : ''}>
            </label>
            <div class="settings-hint">调试模式只展示脱敏后的 request_id、member_id、failure_scope、fallback_chain 等摘要，不展示完整 Key、Authorization 或请求 body。</div>
          </section>

          <section class="settings-section">
            <h3>路由保护</h3>
            <label class="settings-row">
              <span>智能熔断</span>
              <input id="setting-smart-breaker-enabled" type="checkbox" ${s.smart_breaker_enabled !== false ? 'checked' : ''}>
            </label>
            <div class="settings-hint">连续上游故障前 3 次仅记录并切换候选，第 4 次起逐级冷却，最高 5 分钟。关闭会停止全部连接组和聚合模型的熔断策略并清理自动健康状态，不会启用手动停用对象。</div>
          </section>

          <section class="settings-section">
            <h3>实验功能</h3>
            <div class="settings-row">
              <span>上游 HTTP 客户端</span>
              <select id="setting-upstream-http-client">
                <option value="urllib" ${s.upstream_http_client === 'urllib' ? 'selected' : ''}>urllib（默认）</option>
                <option value="httpx" ${s.upstream_http_client === 'httpx' ? 'selected' : ''}>httpx</option>
              </select>
            </div>
            <label class="settings-row">
              <span>启用 HTTP/2（仅 httpx）</span>
              <input id="setting-upstream-http2" type="checkbox" ${s.upstream_http2 ? 'checked' : ''}>
            </label>
            <label class="settings-row">
              <span>启用 HTTP keep-alive（仅 httpx）</span>
              <input id="setting-upstream-keepalive" type="checkbox" ${s.upstream_keepalive ? 'checked' : ''}>
            </label>
            <label class="settings-row">
              <span>归一化 tools 顺序（实验）</span>
              <input id="setting-normalize-tools-order" type="checkbox" ${s.normalize_tools_order ? 'checked' : ''}>
            </label>
            <div class="settings-hint">默认保持 urllib；httpx / HTTP2 / keep-alive 仅用于对照实验，确认有效后再长期开启。tools 排序默认关闭，不修改现有请求。</div>
          </section>

          <section class="settings-section">
            <h3>诊断工具</h3>
            <label class="settings-row">
              <span>捕获最近请求快照</span>
              <input id="setting-debug-capture-enabled" type="checkbox" ${s.debug_capture_enabled ? 'checked' : ''}>
            </label>
            <label class="settings-row">
              <span>同时捕获完整请求体（仅本地）</span>
              <input id="setting-debug-capture-last-body" type="checkbox" ${s.debug_capture_last_body ? 'checked' : ''}>
            </label>
            <div class="settings-hint">快照保存到 .tmp/cache-debug/latest.json，不进入 git，不写入日志正文。完整 body 默认不捕获。</div>
            <div class="settings-row" style="margin-top:12px; flex-wrap:wrap; gap:10px;">
              <div style="display:flex; align-items:center; gap:6px;">
                <label style="font-size:12px; color:var(--text-secondary);">重放次数</label>
                <input id="debug-replay-count" type="number" min="1" max="50" value="10" style="width:60px;">
              </div>
              <div style="display:flex; align-items:center; gap:6px;">
                <label style="font-size:12px; color:var(--text-secondary);">客户端</label>
                <select id="debug-replay-client">
                  <option value="">当前设置</option>
                  <option value="urllib">urllib</option>
                  <option value="httpx">httpx</option>
                </select>
              </div>
              <label class="checkbox" style="font-size:12px;">
                <input id="debug-replay-waf-off" type="checkbox">
                <span>WAF off 对照（一次性）</span>
              </label>
            </div>
            <div class="settings-actions" style="margin-top:10px;">
              <button type="button" id="debug-replay-btn" class="btn-secondary">开始重放</button>
            </div>
            <div id="debug-replay-results" style="margin-top:10px;"></div>
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
              <div class="settings-about-row"><span>版本</span><span>v0.6.3</span></div>
              <div class="settings-about-row"><span>项目地址</span><a href="https://github.com/dawnliming/lin-router" target="_blank" rel="noopener">GitHub</a></div>
            </div>
          </section>
        </div>
      </div>
    `;
  },

  attachEvents(panel) {
    panel.querySelector('.settings-backdrop')?.addEventListener('click', () => this.close());
    panel.querySelector('#settings-close')?.addEventListener('click', () => this.close());

    panel.querySelector('#setting-auto-start')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'auto_start'));
    panel.querySelector('#setting-start-minimized')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'start_minimized'));
    panel.querySelector('#setting-auto-refresh-logs')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'auto_refresh_logs'));
    panel.querySelector('#setting-debug-mode')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'debug_mode'));
    panel.querySelector('#setting-smart-breaker-enabled')?.addEventListener('change', e => this.updateSmartBreakerSetting(e));

    panel.querySelectorAll('input[name="setting-theme"]').forEach(radio => {
      radio.addEventListener('change', e => {
        if (e.target.checked) {
          this.updateSetting('theme', e.target.value);
        }
      });
    });

    panel.querySelector('#settings-backup')?.addEventListener('click', () => this.backupAll());
    panel.querySelector('#settings-restore')?.addEventListener('click', () => panel.querySelector('#settings-restore-file')?.click());
    panel.querySelector('#settings-restore-file')?.addEventListener('change', e => this.restoreAll(e));

    // 实验功能
    panel.querySelector('#setting-upstream-http-client')?.addEventListener('change', e => {
      this.updateSetting('upstream_http_client', e.target.value);
      this.syncExperimentalUI(panel);
    });
    panel.querySelector('#setting-upstream-http2')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'upstream_http2'));
    panel.querySelector('#setting-upstream-keepalive')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'upstream_keepalive'));
    panel.querySelector('#setting-normalize-tools-order')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'normalize_tools_order'));

    // 诊断工具
    panel.querySelector('#setting-debug-capture-enabled')?.addEventListener('change', e => {
      this.updateCheckboxSetting(e, 'debug_capture_enabled');
      this.syncExperimentalUI(panel);
    });
    panel.querySelector('#setting-debug-capture-last-body')?.addEventListener('change', e => this.updateCheckboxSetting(e, 'debug_capture_last_body'));

    this.syncExperimentalUI(panel);
  },

  bindFocusTrap(panel) {
    if (this._keyHandler) document.removeEventListener('keydown', this._keyHandler);
    const drawer = panel.querySelector('.settings-drawer');
    this._keyHandler = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        this.close();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = [...drawer.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])')];
      if (!focusable.length) {
        event.preventDefault();
        drawer.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', this._keyHandler);
    setTimeout(() => drawer.querySelector('#settings-close')?.focus(), 0);
  },

  syncExperimentalUI(panel) {
    const client = panel.querySelector('#setting-upstream-http-client')?.value || 'urllib';
    const http2 = panel.querySelector('#setting-upstream-http2');
    const keepalive = panel.querySelector('#setting-upstream-keepalive');
    if (http2) {
      http2.disabled = client !== 'httpx';
      http2.parentElement.style.opacity = client === 'httpx' ? '1' : '0.5';
    }
    if (keepalive) {
      keepalive.disabled = client !== 'httpx';
      keepalive.parentElement.style.opacity = client === 'httpx' ? '1' : '0.5';
    }
  },

  async runReplay() {
    const panel = document.getElementById('settings-panel');
    const resultsEl = panel?.querySelector('#debug-replay-results');
    if (!resultsEl) return;
    const count = Math.max(1, Math.min(50, Number(document.getElementById('debug-replay-count')?.value || 10)));
    const client = document.getElementById('debug-replay-client')?.value || '';
    const wafOff = document.getElementById('debug-replay-waf-off')?.checked || false;
    resultsEl.innerHTML = '<div class="settings-hint">重放中…</div>';
    try {
      const data = await API.replayDebug({ count, client: client || undefined, waf_off_variant: wafOff });
      resultsEl.innerHTML = this.renderReplayResults(data.results || []);
    } catch (err) {
      resultsEl.innerHTML = `<div class="settings-hint" style="color:var(--danger);">重放失败：${Utils.escapeHtml(err.message)}</div>`;
    }
  },

  renderReplayResults(results) {
    if (!results.length) return '<div class="settings-hint">无重放结果</div>';
    if (results[0]?.error) {
      return `<div class="settings-hint" style="color:var(--danger);">${Utils.escapeHtml(results[0].error)}</div>`;
    }
    const rows = results.map(r => {
      const warn = r.waf_off_unusable ? ' <span style="color:var(--danger);">(WAF off 不可用)</span>' : '';
      return `<div style="font-size:12px; margin-bottom:4px;">
        #${r.index}: status=${r.status}, client=${r.http_client}, version=${r.http_version},
        hit_rate=${(r.hit_rate * 100).toFixed(1)}%, tokens=${r.total_tokens}, cached=${r.cached_tokens}, duration=${r.duration_ms}ms${warn}
      </div>`;
    }).join('');
    const rates = results.filter(r => typeof r.hit_rate === 'number').map(r => r.hit_rate);
    const avg = rates.length ? (rates.reduce((a, b) => a + b, 0) / rates.length * 100).toFixed(1) : '-';
    return `<div class="settings-hint">平均命中率：${avg}%</div>${rows}`;
  },

  updateCheckboxSetting(event, key) {
    event.stopPropagation();
    const input = event.currentTarget || event.target;
    if (!input || input.disabled) return;
    this.updateSetting(key, !!input.checked);
  },

  async updateSmartBreakerSetting(event) {
    event.stopPropagation();
    const input = event.currentTarget || event.target;
    if (!input || input.disabled) return;
    if (input.checked) {
      this.updateSetting('smart_breaker_enabled', true);
      return;
    }
    const confirmed = await Modal.confirm({
      title: '关闭智能熔断',
      message: '关闭后会停止全部连接组和聚合模型的熔断策略，并立即清理系统健康状态和冷却记录，但不会改变手动停用对象。是否继续？',
      confirmText: '确定关闭',
      confirmClass: 'btn-danger',
    });
    if (!confirmed) {
      input.checked = true;
      return;
    }
    this.updateSetting('smart_breaker_enabled', false);
  },

  applySettingSideEffects(key, value) {
    if (key === 'auto_refresh_logs') {
      LogsTab.setAutoRefresh(value);
    }
    if (key === 'debug_mode' && Tabs.current === 'logs') {
      LogsTab._lastRenderSignature = '';
      LogsTab.renderRows(true);
    }
    if (key === 'theme') {
      App.setTheme(value || 'system');
    }
  },

  refreshOpenPanelControls() {
    const panel = document.getElementById('settings-panel');
    if (!panel || panel.classList.contains('hidden')) return;
    const s = Store.state.settings || {};
    const controls = {
      'setting-auto-start': !!s.auto_start,
      'setting-start-minimized': !!s.start_minimized,
      'setting-auto-refresh-logs': s.auto_refresh_logs !== false,
      'setting-debug-mode': !!s.debug_mode,
      'setting-smart-breaker-enabled': s.smart_breaker_enabled !== false,
      'setting-upstream-http2': !!s.upstream_http2,
      'setting-upstream-keepalive': !!s.upstream_keepalive,
      'setting-normalize-tools-order': !!s.normalize_tools_order,
      'setting-debug-capture-enabled': !!s.debug_capture_enabled,
      'setting-debug-capture-last-body': !!s.debug_capture_last_body,
    };
    Object.entries(controls).forEach(([id, checked]) => {
      const el = panel.querySelector(`#${id}`);
      if (el) el.checked = checked;
    });
    const client = panel.querySelector('#setting-upstream-http-client');
    if (client && s.upstream_http_client) client.value = s.upstream_http_client;
    const theme = panel.querySelector(`input[name="setting-theme"][value="${s.theme || 'system'}"]`);
    if (theme) theme.checked = true;
    this.syncExperimentalUI(panel);
  },

  async updateSetting(key, value) {
    const previousSettings = { ...(Store.state.settings || {}) };
    Store.update({ settings: { ...previousSettings, [key]: value } });
    this.applySettingSideEffects(key, value);
    try {
      const updated = await API.saveSettings({ [key]: value });
      Store.update({ settings: { ...(Store.state.settings || {}), ...(updated || {}) } });
      this.refreshOpenPanelControls();
      Toast.success('设置已保存');
    } catch (err) {
      Store.update({ settings: previousSettings });
      this.refreshOpenPanelControls();
      this.applySettingSideEffects(key, previousSettings[key]);
      Toast.error('保存设置失败：' + err.message);
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
