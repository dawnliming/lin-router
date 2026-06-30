const API = {
  base: '',
  _loading: 0,

  setLoading(delta) {
    this._loading = Math.max(0, this._loading + delta);
    const el = document.getElementById('global-loading');
    if (el) el.classList.toggle('hidden', this._loading === 0);
  },

  async req(path, opts = {}) {
    const url = `${this.base}${path}`;
    this.setLoading(1);
    try {
      // FormData 上传时不能手动设置 Content-Type，浏览器需要自动生成 boundary
      const headers = {};
      if (!(opts.body instanceof FormData) && (!opts.headers || !opts.headers['Content-Type'])) {
        headers['Content-Type'] = 'application/json';
      }
      const res = await fetch(url, {
        headers,
        ...opts
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        let message = text || `HTTP ${res.status}`;
        // 后端可能返回 JSON 错误对象，优先提取其中的具体信息
        try {
          const json = JSON.parse(text);
          if (json && typeof json === 'object') {
            if (json.message) {
              message = String(json.message);
            } else if (json.error) {
              if (typeof json.error === 'string') {
                message = json.error;
              } else if (json.error && typeof json.error === 'object' && json.error.message) {
                message = String(json.error.message);
              }
            }
          }
        } catch (_) {
          // 不是 JSON，保持原始文本
        }
        throw new Error(message);
      }
      const contentType = res.headers.get('content-type') || '';
      if (contentType.includes('application/json')) return res.json();
      return res.text();
    } finally {
      this.setLoading(-1);
    }
  },

  getState() { return this.req('/api/state'); },
  getLogs(params = {}) {
    const qs = new URLSearchParams(params).toString();
    return this.req(`/api/logs${qs ? '?' + qs : ''}`);
  },
  clearLogs() { return this.req('/api/logs/clear', { method: 'POST' }); },
  exportLogs() { return this.req('/api/logs/export'); },
  saveGroup(id, data) { return this.req(`/api/groups/${id}`, { method: 'PUT', body: JSON.stringify(data) }); },
  createGroup(data) { return this.req('/api/groups', { method: 'POST', body: JSON.stringify(data) }); },
  deleteGroup(id) { return this.req(`/api/groups/${id}`, { method: 'DELETE' }); },
  saveModel(id, data) { return this.req(`/api/models/${id}`, { method: 'PUT', body: JSON.stringify(data) }); },
  createModel(data) { return this.req('/api/models', { method: 'POST', body: JSON.stringify(data) }); },
  deleteModel(id) { return this.req(`/api/models/${id}`, { method: 'DELETE' }); },
  moveModel(id, data) { return this.req(`/api/models/${id}/move`, { method: 'POST', body: JSON.stringify(data) }); },
  cloneGroup(id) { return this.req(`/api/groups/${id}/clone`, { method: 'POST' }); },
  cloneModel(id) { return this.req(`/api/models/${id}/clone`, { method: 'POST' }); },
  setModelUsable(id, usable) { return this.req(`/api/models/${id}/usable`, { method: 'POST', body: JSON.stringify({ usable }) }); },
  setGroupUsable(id, usable) { return this.req(`/api/groups/${id}/usable`, { method: 'POST', body: JSON.stringify({ usable }) }); },
  setAllUsable(usable) { return this.req('/api/models/usable/all', { method: 'POST', body: JSON.stringify({ usable }) }); },
  resetCooldown(id) { return this.req(`/api/models/${id}/reset`, { method: 'POST' }); },
  resetGroupCooldown(id) { return this.req(`/api/groups/${id}/reset`, { method: 'POST' }); },
  fetchUpstreamModels(id) { return this.req(`/api/groups/${id}/fetch-models`, { method: 'POST' }); },
  getSettings() { return this.req('/api/settings'); },
  saveSettings(data) { return this.req('/api/settings', { method: 'PUT', body: JSON.stringify(data) }); },
  exportConfig() { return this.req('/api/config/export'); },
  importConfig(file) {
    const form = new FormData();
    form.append('file', file);
    return this.req('/api/config/import', { method: 'POST', body: form });
  },
  importBackup(file) {
    const form = new FormData();
    form.append('file', file);
    return this.req('/api/backup/import', { method: 'POST', body: form });
  },
  testProxy(data) { return this.req('/api/test', { method: 'POST', body: JSON.stringify(data) }); }
};
