const TestTab = {
  lastResponse: '',
  history: JSON.parse(localStorage.getItem('lin-router-test-history') || '[]'),

  saveHistory() {
    localStorage.setItem('lin-router-test-history', JSON.stringify(this.history.slice(0, 10)));
  },

  onShow() {
    const panel = document.getElementById('panel-test');
    if (!panel) return;
    this.render();
    this.syncSelection();
  },

  render() {
    const panel = document.getElementById('panel-test');
    panel.innerHTML = `
      <h2>代理测试</h2>
      <div class="test-layout">
        <div class="test-col">
          <section class="form-card">
            <h3>请求</h3>
            <div class="form-row">
              <label>连接组</label>
              <select id="test-group">${this.renderGroupOptions()}</select>
            </div>
            <div class="form-row">
              <label>模板</label>
              <select id="test-template">
                <option value="auto">自动调度</option>
                <option value="chat">普通聊天</option>
                <option value="model">指定模型</option>
                <option value="stream">流式请求</option>
                <option value="non-stream">非流式测试</option>
                <option value="tool-call">工具调用</option>
              </select>
            </div>
            <div class="form-row">
              <label>模型</label>
              <select id="test-model"></select>
            </div>
            <div class="form-row">
              <label>路径</label>
              <input id="test-path" value="/v1/chat/completions">
            </div>
            <div class="form-row" style="align-items:flex-start">
              <label>请求体</label>
              <textarea id="test-body" rows="10">{ "messages": [{"role":"user","content":"hello"}], "temperature": 0.2 }</textarea>
            </div>
            <div class="form-actions">
              <button type="button" id="test-send" class="btn-primary">发送测试</button>
            </div>
          </section>
        </div>
        <div class="test-col">
          <section class="form-card response-card">
            <h3>响应</h3>
            <div class="response-status" id="test-status">等待操作。</div>
            <pre class="response-body" id="test-response"></pre>
          </section>
        </div>
      </div>
      <section class="form-card test-history">
        <h3>最近请求（可一键重放）</h3>
        ${this.history.length ? `
          <div class="history-list">
            ${this.history.map((h, i) => `
              <div class="history-item" data-idx="${i}">
                <span class="history-time">${Utils.escapeHtml(h.time)}</span>
                <span class="history-model">${Utils.escapeHtml(h.model)}</span>
                <span class="history-path">${Utils.escapeHtml(h.path)}</span>
                <button type="button" class="btn-secondary btn-sm" data-action="replay" data-idx="${i}">重放</button>
              </div>
            `).join('')}
          </div>
        ` : '<div class="empty-tip">暂无历史记录</div>'}
      </section>
    `;
    this.attachEvents(panel);
    this.renderModelOptions();
    this.applyTemplate();
  },

  renderGroupOptions() {
    const selected = Store.selected.type === 'group' ? Store.selected.id : (Store.state.groups?.[0]?.id || '');
    return Store.state.groups?.map(g =>
      `<option value="${g.id}" ${g.id === selected ? 'selected' : ''}>${Utils.escapeHtml(g.name)}</option>`
    ).join('') || '';
  },

  syncSelection() {
    const templateSel = document.getElementById('test-template');
    if (Store.selected.type === 'group') {
      const sel = document.getElementById('test-group');
      if (sel) sel.value = Store.selected.id;
    } else if (Store.selected.type === 'model') {
      const model = Store.getModel(Store.selected.id);
      const sel = document.getElementById('test-group');
      if (sel && model) sel.value = model.group_id;
      // 选中具体模型时切换到指定模型模板，方便直接测试
      if (templateSel && model) templateSel.value = 'model';
    }
    this.renderModelOptions();
    this.applyTemplate();
    // 双击跳转等场景下自动聚焦请求体输入框
    document.getElementById('test-body')?.focus();
  },

  renderModelOptions() {
    const groupId = document.getElementById('test-group')?.value;
    const group = Store.getGroup(groupId);
    const models = group ? Store.getModelsByGroup(group.id) : [];
    const autoName = Store.state.auto_model_name || 'lin-router-auto';
    const selected = document.getElementById('test-model')?.value;

    const html = [
      `<option value="${autoName}">${autoName} - 自动调度</option>`,
      ...models.map(m => `<option value="${Utils.escapeHtml(m.name)}" ${m.name === selected ? 'selected' : ''}>${Utils.escapeHtml(this.modelLabel(m))}</option>`)
    ].join('');

    const select = document.getElementById('test-model');
    if (select) {
      select.innerHTML = html;
      if (Store.selected.type === 'model') {
        const model = Store.getModel(Store.selected.id);
        if (model && [...select.options].some(o => o.value === model.name)) select.value = model.name;
      }
    }
  },

  modelLabel(m) {
    const group = Store.getGroup(m.group_id);
    const upstream = m.upstream_model || m.ep_id;
    if (group?.provider_type === 'relay') return `${m.name} - ${upstream || '中转站'}`;
    if (group?.provider_type === 'proxy') return `${m.name} - ${upstream || '通用代理'}`;
    return `${m.name} - ${m.ep_id}`;
  },

  applyTemplate() {
    const template = document.getElementById('test-template')?.value || 'auto';
    const model = document.getElementById('test-model')?.value;
    const bodyEl = document.getElementById('test-body');
    if (!bodyEl) return;
    const base = { messages: [{ role: 'user', content: 'hello' }], temperature: 0.2 };
    if (template === 'auto' || template === 'chat') {
      bodyEl.value = JSON.stringify(base, null, 2);
    } else if (template === 'model') {
      bodyEl.value = JSON.stringify({ ...base, model }, null, 2);
    } else if (template === 'stream') {
      bodyEl.value = JSON.stringify({ ...base, model, stream: true }, null, 2);
    } else if (template === 'non-stream') {
      bodyEl.value = JSON.stringify({ ...base, model, stream: false }, null, 2);
    } else if (template === 'tool-call') {
      bodyEl.value = JSON.stringify({
        ...base,
        model,
        tools: [{
          type: 'function',
          function: { name: 'get_weather', description: '获取指定城市天气', parameters: { type: 'object', properties: { city: { type: 'string' } }, required: ['city'] } }
        }],
        messages: [{ role: 'user', content: '北京今天天气怎么样' }]
      }, null, 2);
    }
  },

  async send() {
    const btn = document.getElementById('test-send');
    const statusEl = document.getElementById('test-status');
    const respEl = document.getElementById('test-response');
    const groupId = document.getElementById('test-group').value;
    const group = Store.getGroup(groupId);
    const path = document.getElementById('test-path').value;
    const bodyText = document.getElementById('test-body').value;
    const selectedModel = document.getElementById('test-model').value;

    btn.disabled = true;
    btn.textContent = '发送中...';
    try {
      const payload = JSON.parse(bodyText);
      const autoName = Store.state.auto_model_name || 'lin-router-auto';
      if (selectedModel && selectedModel !== autoName) payload.model = selectedModel;

      const startedAt = performance.now();
      const headers = { 'Content-Type': 'application/json', 'Authorization': `Bearer ${group?.route_key || ''}` };
      if (group?.provider_type === 'relay' && group?.waf_compatible) {
        headers['X-LinRouter-Test'] = 'relay-waf';
      }
      const resp = await fetch(path, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      });
      const text = await resp.text();
      const elapsed = Math.round(performance.now() - startedAt);
      statusEl.textContent = `HTTP ${resp.status} - ${elapsed} ms`;
      respEl.textContent = this.formatResponse(text);
      this.lastResponse = text;

      // 保存到历史记录（最近 10 条）
      this.history.unshift({
        time: new Date().toLocaleString(),
        group_id: groupId,
        group_name: group?.name || '',
        model: selectedModel || autoName,
        path,
        body: bodyText,
        template: document.getElementById('test-template')?.value || 'auto',
      });
      this.history = this.history.slice(0, 10);
      this.saveHistory();
      this.renderHistory();

      await Store.load();
    } catch (err) {
      statusEl.textContent = '请求失败';
      respEl.textContent = String(err);
    } finally {
      btn.disabled = false;
      btn.textContent = '发送测试';
    }
  },

  renderHistory() {
    const container = document.querySelector('.test-history');
    if (!container) return;
    container.innerHTML = `
      <h3>最近请求（可一键重放）</h3>
      ${this.history.length ? `
        <div class="history-list">
          ${this.history.map((h, i) => `
            <div class="history-item" data-idx="${i}">
              <span class="history-time">${Utils.escapeHtml(h.time)}</span>
              <span class="history-model">${Utils.escapeHtml(h.model)}</span>
              <span class="history-path">${Utils.escapeHtml(h.path)}</span>
              <button type="button" class="btn-secondary btn-sm" data-action="replay" data-idx="${i}">重放</button>
            </div>
          `).join('')}
        </div>
      ` : '<div class="empty-tip">暂无历史记录</div>'}
    `;
    container.querySelectorAll('[data-action="replay"]').forEach(btn => {
      btn.addEventListener('click', e => this.replay(Number(e.target.dataset.idx)));
    });
  },

  replay(idx) {
    const h = this.history[idx];
    if (!h) return;
    document.getElementById('test-group').value = h.group_id;
    this.renderModelOptions();
    if (h.model) {
      const modelSel = document.getElementById('test-model');
      if ([...modelSel.options].some(o => o.value === h.model)) modelSel.value = h.model;
    }
    document.getElementById('test-path').value = h.path;
    document.getElementById('test-body').value = h.body;
    document.getElementById('test-template').value = h.template || 'auto';
    Toast.info('已重放历史请求');
  },

  formatResponse(text) {
    try { return JSON.stringify(JSON.parse(text), null, 2); }
    catch { return text; }
  },

  attachEvents(panel) {
    panel.querySelector('#test-group')?.addEventListener('change', () => { this.renderModelOptions(); this.applyTemplate(); });
    panel.querySelector('#test-template')?.addEventListener('change', () => this.applyTemplate());
    panel.querySelector('#test-model')?.addEventListener('change', () => { if (['model', 'stream', 'non-stream'].includes(document.getElementById('test-template').value)) this.applyTemplate(); });
    panel.querySelector('#test-send')?.addEventListener('click', () => this.send());
    panel.querySelectorAll('[data-action="replay"]').forEach(btn => {
      btn.addEventListener('click', e => this.replay(Number(e.target.dataset.idx)));
    });
  }
};
