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
    // silent 模式不触发全局 loading，用于后台轮询等无感更新场景
    const silent = opts.silent === true;
    if (!silent) this.setLoading(1);
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
        let errorCode = '';
        let errorRevision;
        try {
          const json = JSON.parse(text);
          if (json && typeof json === 'object') {
            const error = json.error && typeof json.error === 'object' ? json.error : json;
            errorCode = String(error.code || '');
            errorRevision = error.revision;
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
        const error = new Error(message);
        error.status = res.status;
        error.code = errorCode;
        if (errorRevision !== undefined) error.revision = errorRevision;
        throw error;
      }
      const contentType = res.headers.get('content-type') || '';
      if (contentType.includes('application/json')) return res.json();
      return res.text();
    } finally {
      if (!silent) this.setLoading(-1);
    }
  },

  getState() { return this.req('/api/state'); },
  getRuntimeState(opts = {}) { return this.req('/api/runtime-state', opts); },
  getLiveRequests(opts = {}) { return this.req('/api/live-requests', opts); },
  cancelLiveRequest(requestId) { return this.req(`/api/live-requests/${encodeURIComponent(requestId)}/cancel`, { method: 'POST' }); },
  diagnoseRequest(requestId, opts = {}) { return this.req(`/api/diagnose/${encodeURIComponent(requestId)}`, opts); },
  getLogs(params = {}, opts = {}) {
    const qs = new URLSearchParams(params).toString();
    return this.req(`/api/logs${qs ? '?' + qs : ''}`, opts);
  },
  getAllLogs() { return this.req('/api/logs/all'); },
  clearLogs() { return this.req('/api/logs/clear', { method: 'POST' }); },
  exportLogs() { return this.req('/api/logs/export'); },
  saveGroup(id, data) { return this.req(`/api/groups/${id}`, { method: 'PUT', body: JSON.stringify(data) }); },
  createGroup(data) { return this.req('/api/groups', { method: 'POST', body: JSON.stringify(data) }); },
  deleteGroup(id) { return this.req(`/api/groups/${id}`, { method: 'DELETE' }); },
  previewDeleteGroup(id) { return this.req(`/api/groups/${id}/delete-preview`, { method: 'POST' }); },
  saveModel(id, data) { return this.req(`/api/models/${id}`, { method: 'PUT', body: JSON.stringify(data) }); },
  createModel(data) { return this.req('/api/models', { method: 'POST', body: JSON.stringify(data) }); },
  deleteModel(id) { return this.req(`/api/models/${id}`, { method: 'DELETE' }); },
  previewDeleteModel(id) { return this.req(`/api/models/${id}/delete-preview`, { method: 'POST' }); },
  moveModel(id, data) { return this.req(`/api/models/${id}/move`, { method: 'POST', body: JSON.stringify(data) }); },
  cloneGroup(id) { return this.req(`/api/groups/${id}/clone`, { method: 'POST' }); },
  cloneModel(id) { return this.req(`/api/models/${id}/clone`, { method: 'POST' }); },
  setModelUsable(id, usable) { return this.req(`/api/models/${id}/usable`, { method: 'POST', body: JSON.stringify({ usable }) }); },
  setGroupUsable(id, usable) { return this.req(`/api/groups/${id}/usable`, { method: 'POST', body: JSON.stringify({ usable }) }); },
  setAllUsable(usable) { return this.req('/api/models/usable/all', { method: 'POST', body: JSON.stringify({ usable }) }); },
  resetCooldown(id) { return this.req(`/api/models/${id}/reset`, { method: 'POST' }); },
  recoverModel(id) { return this.req(`/api/models/${id}/recover`, { method: 'POST' }); },
  resetGroupCooldown(id) { return this.req(`/api/groups/${id}/reset`, { method: 'POST' }); },
  fetchUpstreamModels(groupId, apiKey) {
    return this.req('/api/models/fetch-upstream', {
      method: 'POST',
      body: JSON.stringify({ group_id: groupId, api_key: apiKey })
    });
  },
  getSettings() { return this.req('/api/settings'); },
  saveSettings(data) { return this.req('/api/settings', { method: 'PUT', body: JSON.stringify(data) }); },
  exportConfig() { return this.req('/api/config/export'); },
  getAggregates() { return this.req('/api/aggregates'); },
  speedTestGroup(id) { return this.req(`/api/groups/${encodeURIComponent(id)}/speed-test`, { method: 'POST' }); },
  speedTestAggregate(id) { return this.req(`/api/aggregates/${encodeURIComponent(id)}/speed-test`, { method: 'POST' }); },
  getAggregateStats(id, limit = 100) { return this.req(`/api/aggregates/${id}/stats?limit=${encodeURIComponent(limit)}`); },
  createAggregate(data) { return this.req('/api/aggregates', { method: 'POST', body: JSON.stringify(data) }); },
  saveAggregate(id, data) { return this.req(`/api/aggregates/${id}`, { method: 'PUT', body: JSON.stringify(data) }); },
  deleteAggregate(id) { return this.req(`/api/aggregates/${id}`, { method: 'DELETE' }); },
  createAggregateMember(aggregateId, data) { return this.req(`/api/aggregates/${aggregateId}/members`, { method: 'POST', body: JSON.stringify(data) }); },
  reorderAggregateMembers(aggregateId, memberIds, expectedRevision) { return this.req(`/api/aggregates/${aggregateId}/members/reorder`, { method: 'POST', body: JSON.stringify({ member_ids: memberIds, expected_revision: expectedRevision }) }); },
  saveAggregateMember(id, data) { return this.req(`/api/aggregate-members/${id}`, { method: 'PUT', body: JSON.stringify(data) }); },
  clearAggregateMemberCooldown(id) { return this.req(`/api/aggregate-members/${id}/clear-cooldown`, { method: 'POST' }); },
  recoverAggregateMember(id) { return this.req(`/api/aggregate-members/${id}/recover`, { method: 'POST' }); },
  previewAggregateMemberClearCooldown(id) { return this.req(`/api/aggregate-members/${id}/clear-cooldown-preview`, { method: 'POST' }); },
  previewAggregateMemberSort(id, direction) { return this.req(`/api/aggregate-members/${id}/sort-preview`, { method: 'POST', body: JSON.stringify({ direction }) }); },
  deleteAggregateMember(id) { return this.req(`/api/aggregate-members/${id}`, { method: 'DELETE' }); },
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
  testProxy(data) { return this.req('/api/test', { method: 'POST', body: JSON.stringify(data) }); },
  getDebugCapture() { return this.req('/api/debug/capture'); },
  replayDebug(data) { return this.req('/api/debug/replay', { method: 'POST', body: JSON.stringify(data) }); }
};
