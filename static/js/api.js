const API = {
  base: '',

  async req(path, opts = {}) {
    const url = `${this.base}${path}`;
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...opts
    });
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(text || `HTTP ${res.status}`);
    }
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('application/json')) return res.json();
    return res.text();
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
  testProxy(data) { return this.req('/api/test', { method: 'POST', body: JSON.stringify(data) }); }
};
