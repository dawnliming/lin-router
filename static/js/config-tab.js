const ConfigTab = {
  onShow() {
    const panel = document.getElementById('panel-config');
    if (!panel) return;
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-config');
    const sel = Store.selected;
    if (!sel.id) {
      panel.innerHTML = this.renderEmptyState();
      this.attachEmptyEvents(panel);
      return;
    }
    const item = sel.type === 'group' ? Store.getGroup(sel.id) : Store.getModel(sel.id);
    const title = item ? Utils.escapeHtml(item.name) : (sel.type === 'group' ? '新建连接组' : '新建模型');

    panel.innerHTML = `
      <div class="config-header">
        <h2>${title}</h2>
        <div class="save-status" id="save-status"></div>
      </div>
      <div class="config-layout">
        <div class="config-main">
          ${sel.type === 'group' || sel.type === null ? this.renderGroupSection() : this.renderModelSection()}
        </div>
        <div class="config-side">
          ${sel.type === 'group' || sel.type === null ? this.renderGroupSide() : ''}
        </div>
      </div>
    `;
    this.attachEvents(panel);
    this.syncUIFromState();
  },

  renderEmptyState() {
    return `
      <div class="empty-state">
        <div class="empty-icon">🚀</div>
        <h2>欢迎使用 Lin Router</h2>
        <p class="empty-subtitle">点击右上角 + 新建你的第一个连接组，或导入已有配置</p>
        <div class="empty-actions">
          <button type="button" class="btn-primary" id="empty-new-group">新建连接组</button>
          <button type="button" class="btn-secondary" id="empty-import">导入配置</button>
        </div>
        <p class="empty-hint">全局Key: <code>lin-router</code>，本地地址点击顶部复制即可使用</p>
      </div>
    `;
  },

  attachEmptyEvents(panel) {
    panel.querySelector('#empty-new-group')?.addEventListener('click', () => App.createGroup());
    panel.querySelector('#empty-import')?.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = 'application/json,.json';
      input.addEventListener('change', e => this.onConfigImport(e));
      input.click();
    });
  },

  renderGroupSection() {
    const sel = Store.selected;
    const g = sel.type === 'group' ? Store.getGroup(sel.id) : null;
    const provider = g?.provider_type || 'ark';
    const showAdvanced = provider === 'relay';
    return `
      <form class="config-form" id="group-form" data-type="group">
        <input type="hidden" id="group-id" value="${g?.id || ''}">
        <section class="form-card">
          <h3>基础配置</h3>
          <div class="form-row">
            <label>组名</label>
            <input id="group-name" value="${Utils.escapeHtml(g?.name || '')}" placeholder="默认组">
          </div>
          <div class="form-row">
            <label>模式</label>
            <select id="group-provider">
              <option value="ark" ${provider === 'ark' ? 'selected' : ''}>火山方舟</option>
              <option value="relay" ${provider === 'relay' ? 'selected' : ''}>中转站</option>
              <option value="proxy" ${provider === 'proxy' ? 'selected' : ''}>通用 OpenAI 代理</option>
            </select>
          </div>
          <div class="form-row">
            <label>Base URL</label>
            <input id="group-base" value="${Utils.escapeHtml(g?.base_url || '')}" placeholder="https://example.com/v1">
          </div>
          <div class="form-row" id="group-key-row">
            <label id="group-key-label">Ark API Key</label>
            <div class="input-with-btn">
              <input id="group-key" type="password" value="${Utils.escapeHtml(this.groupKeyValue(g) || '')}" placeholder="sk-xxxx">
              <button type="button" id="group-key-toggle">显示</button>
            </div>
          </div>
        </section>
        <section class="form-card ${showAdvanced ? '' : 'hidden'}" id="group-advanced-card">
          <h3>高级配置</h3>
          <div class="form-row" id="group-cooldown-row">
            <label>自动冷却分钟</label>
            <input id="group-cooldown" type="number" min="0" step="1" value="${g?.auto_model_cooldown_minutes ?? 5}">
          </div>
          <div class="form-row" id="group-waf-row">
            <label class="checkbox">
              <input id="group-waf" type="checkbox" ${g?.waf_compatible ? 'checked' : ''}>
              <span>仅中转站 WAF 兼容</span>
            </label>
          </div>
        </section>
        <section class="form-card">
          <h3>其他</h3>
          <div class="form-row">
            <label>本地路由 Key</label>
            <div class="input-with-btn">
              <input id="group-route-key" value="${Utils.escapeHtml(g?.route_key || '')}" readonly>
              <button type="button" id="group-copy-route-key" title="复制路由 Key">📋</button>
            </div>
          </div>
          <div class="form-row">
            <label>模式说明</label>
            <div class="form-hint" id="group-mode-hint"></div>
          </div>
          <div class="form-actions form-actions-split">
            <div class="form-actions-left">
              <button type="submit" class="btn-primary">保存更改</button>
              ${g ? `<button type="button" id="group-clone" class="btn-secondary">复制组</button>` : ''}
            </div>
            <div class="form-actions-right">
              ${g ? `<button type="button" class="btn-danger" id="group-delete">删除组</button>` : ''}
            </div>
          </div>
        </section>
      </form>
    `;
  },

  renderModelSection() {
    const sel = Store.selected;
    const m = sel.type === 'model' ? Store.getModel(sel.id) : null;
    const groupId = m?.group_id || Store.state.groups?.[0]?.id || '';
    const group = Store.getGroup(groupId);
    const isArk = group?.provider_type === 'ark';
    const isRelay = group?.provider_type === 'relay';
    const isProxy = group?.provider_type === 'proxy';
    const needUpstream = isRelay || isProxy;
    return `
      <form class="config-form" id="model-form" data-type="model">
        <input type="hidden" id="model-id" value="${m?.id || ''}">
        <section class="form-card">
          <h3>基础配置</h3>
          <div class="form-row">
            <label>显示名称</label>
            <input id="model-name" value="${Utils.escapeHtml(m?.name || '')}" placeholder="DeepSeek">
          </div>
          ${!isArk ? `
          <div class="form-row" id="model-key-row">
            <label>${isRelay ? '中转站 API Key' : '上游 API Key'}</label>
            <input id="model-key" type="password" value="${Utils.escapeHtml(m?.api_key || '')}" placeholder="sk-xxxx">
          </div>
          ` : ''}
          <div class="form-row ${needUpstream ? 'hidden' : ''}" id="model-ep-row">
            <label>上游模型 / EP</label>
            <input id="model-ep" value="${Utils.escapeHtml(m?.ep_id || '')}" placeholder="ep-xxxx / deepseek-chat">
          </div>
          <div class="form-row ${needUpstream ? '' : 'hidden'}" id="model-upstream-row">
            <label>上游模型</label>
            <div class="input-with-btn">
              <select id="model-upstream"></select>
              <button type="button" id="model-fetch">获取</button>
            </div>
          </div>
          <div class="form-row">
            <label>连接组</label>
            <select id="model-group">${this.renderGroupOptions(groupId, group?.provider_type)}</select>
          </div>
        </section>
        <section class="form-card">
          <h3>调度配置</h3>
          ${isRelay ? `
          <div class="form-row" id="model-price-row">
            <label>价格组 / 通道</label>
            <input id="model-price" value="${Utils.escapeHtml(m?.price_group || '')}" placeholder="cheap / standard / premium">
          </div>
          ` : ''}
          <div class="form-row" id="model-price-input-row">
            <label>输入单价（元 / 千 Token）</label>
            <input id="model-price-input" type="number" step="0.0001" min="0" value="${m?.price_input ? m.price_input : ''}" placeholder="可选，用于统计花费">
          </div>
          <div class="form-row" id="model-price-output-row">
            <label>输出单价（元 / 千 Token）</label>
            <input id="model-price-output" type="number" step="0.0001" min="0" value="${m?.price_output ? m.price_output : ''}" placeholder="可选，用于统计花费">
          </div>
          <div class="form-row">
            <label class="checkbox">
              <input id="model-usable" type="checkbox" ${m?.usable !== false ? 'checked' : ''}>
              <span>可用</span>
            </label>
          </div>
        </section>
        <section class="form-card">
          <h3>状态信息</h3>
          <div class="form-row read-only">
            <label>最后成功</label>
            <span>${m?.last_success_at || '-'}</span>
          </div>
          <div class="form-row read-only">
            <label>最后检查</label>
            <span>${m?.last_checked_at || '-'}</span>
          </div>
          <div class="form-row read-only">
            <label>冷却截止</label>
            <span id="model-cooldown-display">-</span>
          </div>
          <div class="form-row read-only">
            <label>最近错误</label>
            <span class="error-text">${Utils.escapeHtml(m?.last_error || '-')}</span>
          </div>
          <div class="form-actions form-actions-split">
            <div class="form-actions-left">
              <button type="submit" class="btn-primary">保存模型</button>
              ${m ? `<button type="button" id="model-clone" class="btn-secondary">复制</button>` : ''}
            </div>
            <div class="form-actions-right">
              ${m ? `<button type="button" id="model-delete" class="btn-danger">删除</button>` : ''}
            </div>
          </div>
        </section>
      </form>
    `;
  },

  renderGroupSide() {
    return `
      ${this.renderBatchImport()}
      ${this.renderConfigTools()}
    `;
  },

  renderBatchImport() {
    return `
      <section class="form-card">
        <h3>批量添加模型</h3>
        <div class="form-row">
          <button type="button" id="group-add-model" class="btn-primary" style="width:100%">+ 添加模型</button>
        </div>
        <div class="form-row">
          <label>连接组</label>
          <select id="batch-group">${this.renderGroupOptions()}</select>
        </div>
        <div class="form-row">
          <label>模型列表</label>
          <textarea id="batch-models" rows="4" placeholder='粘贴模型JSON数组，格式示例：[{&quot;name&quot;:&quot;模型名&quot;,&quot;ep_id&quot;:&quot;端点ID&quot;}]'></textarea>
        </div>
        <details class="batch-example">
          <summary>查看格式示例</summary>
          <pre>[\n  {&quot;name&quot;: &quot;DeepSeek-V3&quot;, &quot;ep_id&quot;: &quot;deepseek-chat&quot;},\n  {&quot;name&quot;: &quot;GPT-4o&quot;, &quot;ep_id&quot;: &quot;gpt-4o&quot;}\n]</pre>
        </details>
        <div class="form-row">
          <label>中转站 API Key</label>
          <input id="batch-key" type="password" placeholder="批量导入时可填">
        </div>
        <div class="form-row">
          <label>价格组 / 通道</label>
          <input id="batch-price" placeholder="cheap / standard / premium">
        </div>
        <div class="form-actions" style="justify-content:flex-end">
          <button type="button" id="batch-import" class="btn-primary">批量导入</button>
        </div>
      </section>
    `;
  },

  renderConfigTools() {
    return `
      <section class="form-card">
        <h3>配置导入 / 导出</h3>
        <div class="form-actions" style="flex-direction:column">
          <button type="button" id="config-export" class="btn-secondary" style="width:100%">导出连接组配置</button>
          <button type="button" id="config-import" class="btn-secondary" style="width:100%">导入连接组配置</button>
          <input id="config-import-file" type="file" accept="application/json,.json" style="display:none">
        </div>
        <div class="form-hint">导出包含当前所有连接组和模型配置，不会导出本地代理设置、日志等数据。</div>
      </section>
    `;
  },

  renderGroupOptions(selectedId, providerType) {
    return Store.state.groups?.filter(g => !providerType || g.provider_type === providerType).map(g =>
      `<option value="${g.id}" ${g.id === selectedId ? 'selected' : ''}>${Utils.escapeHtml(g.name)}</option>`
    ).join('') || '';
  },

  groupKeyValue(g) {
    if (!g) return '';
    if (g.provider_type === 'proxy') return g.api_key || '';
    return g.ark_api_key || '';
  },

  syncUIFromState() {
    const sel = Store.selected;
    if (sel.type === 'group' || sel.type === null) this.syncGroupModeUI();
    else this.syncModelModeUI();
  },

  syncGroupModeUI() {
    const mode = document.getElementById('group-provider')?.value || 'ark';
    const needsKey = mode === 'ark' || mode === 'proxy';
    const keyRow = document.getElementById('group-key-row');
    const advancedCard = document.getElementById('group-advanced-card');
    const cooldownRow = document.getElementById('group-cooldown-row');
    const wafRow = document.getElementById('group-waf-row');
    const hint = document.getElementById('group-mode-hint');
    const label = document.getElementById('group-key-label');

    if (keyRow) keyRow.classList.toggle('hidden', !needsKey);
    if (advancedCard) advancedCard.classList.toggle('hidden', mode !== 'relay');
    if (cooldownRow) cooldownRow.classList.toggle('hidden', mode !== 'relay');
    if (wafRow) wafRow.classList.toggle('hidden', mode !== 'relay');
    if (label) label.textContent = mode === 'ark' ? 'Ark API Key' : '上游 API Key';

    if (hint) {
      if (mode === 'ark') hint.textContent = '火山方舟：组内保存 Ark Key，模型里填写 EP ID。';
      else if (mode === 'relay') hint.textContent = '中转站：组内只保存 Base URL；每个模型通道单独保存 API Key 和上游模型。';
      else hint.textContent = '通用代理：组内保存 Base URL 和上游 API Key；未配置的具体模型保持原样透传。';
    }
  },

  syncModelModeUI() {
    const groupId = document.getElementById('model-group')?.value;
    const group = Store.getGroup(groupId);
    const relay = group?.provider_type === 'relay';
    const proxy = group?.provider_type === 'proxy';
    const ark = group?.provider_type === 'ark';

    const epRow = document.getElementById('model-ep-row');
    const upstreamRow = document.getElementById('model-upstream-row');

    if (epRow) epRow.classList.toggle('hidden', relay || proxy);
    if (upstreamRow) upstreamRow.classList.toggle('hidden', !(relay || proxy));

    if (relay || proxy) this.renderUpstreamOptions(groupId);

    // 更新冷却显示
    const m = Store.getModel(document.getElementById('model-id')?.value);
    const display = document.getElementById('model-cooldown-display');
    if (display && m) {
      if (m.cooldown_until && m.cooldown_until * 1000 > Date.now()) {
        display.textContent = Utils.formatDate(m.cooldown_until);
      } else {
        display.textContent = '-';
      }
    }
  },

  renderUpstreamOptions(groupId) {
    const select = document.getElementById('model-upstream');
    if (!select) return;
    // 回显当前模型已选中的上游模型
    const m = Store.getModel(document.getElementById('model-id')?.value);
    select.dataset.current = m?.upstream_model || m?.ep_id || '';
    const group = Store.getGroup(groupId);
    const upstreams = group?.upstream_models || [];
    const current = select.dataset.current || '';
    select.innerHTML = [
      '<option value="">请选择上游模型</option>',
      ...upstreams.map(m => {
        const value = m.ep_id || m.root || m.name;
        return `<option value="${Utils.escapeHtml(value)}" ${value === current ? 'selected' : ''}>${Utils.escapeHtml(m.name || value)}</option>`;
      })
    ].join('');
  },

  attachEvents(panel) {
    // 组表单
    const groupForm = panel.querySelector('#group-form');
    if (groupForm) {
      groupForm.addEventListener('submit', e => this.onGroupSubmit(e));
      panel.querySelector('#group-provider')?.addEventListener('change', () => { this.syncGroupModeUI(); this.autoSaveGroup(); });
      panel.querySelector('#group-key-toggle')?.addEventListener('click', e => {
        const input = document.getElementById('group-key');
        input.type = input.type === 'password' ? 'text' : 'password';
        e.target.textContent = input.type === 'password' ? '显示' : '隐藏';
      });
      panel.querySelector('#group-copy-route-key')?.addEventListener('click', () => {
        const key = document.getElementById('group-route-key')?.value || '';
        Utils.copy(key).then(ok => ok ? Toast.success('路由 Key 已复制') : Toast.error('复制失败'));
      });
      panel.querySelector('#group-delete')?.addEventListener('click', () => this.onGroupDelete());
      panel.querySelector('#group-clone')?.addEventListener('click', () => this.onGroupClone());
      panel.querySelector('#group-add-model')?.addEventListener('click', () => this.onAddModelToGroup());
      this.bindAutoSave(groupForm, () => this.autoSaveGroup());
    }

    // 模型表单
    const modelForm = panel.querySelector('#model-form');
    if (modelForm) {
      modelForm.addEventListener('submit', e => this.onModelSubmit(e));
      panel.querySelector('#model-group')?.addEventListener('change', () => { this.syncModelModeUI(); this.autoSaveModel(); });
      panel.querySelector('#model-upstream')?.addEventListener('change', () => this.autoSaveModel());
      panel.querySelector('#model-delete')?.addEventListener('click', () => this.onModelDelete());
      panel.querySelector('#model-clone')?.addEventListener('click', () => this.onModelClone());
      panel.querySelector('#model-fetch')?.addEventListener('click', () => this.onFetchUpstream());
      this.bindAutoSave(modelForm, () => this.autoSaveModel());
    }

    // 批量导入
    panel.querySelector('#batch-import')?.addEventListener('click', () => this.onBatchImport());

    // 配置导入/导出
    panel.querySelector('#config-export')?.addEventListener('click', () => App.exportConfig());
    panel.querySelector('#config-import')?.addEventListener('click', () => panel.querySelector('#config-import-file')?.click());
    panel.querySelector('#config-import-file')?.addEventListener('change', e => this.onConfigImport(e));
  },

  bindAutoSave(form, callback) {
    if (!form) return;
    form.querySelectorAll('input, select, textarea').forEach(el => {
      const event = el.tagName === 'SELECT' || el.type === 'checkbox' ? 'change' : 'blur';
      el.addEventListener(event, () => callback());
    });
  },

  setSaveStatus(status, message) {
    const el = document.getElementById('save-status');
    if (!el) return;
    el.className = 'save-status';
    if (status === 'saving') {
      el.textContent = '保存中…';
      el.classList.add('saving');
    } else if (status === 'saved') {
      el.textContent = '已保存';
      el.classList.add('saved');
      setTimeout(() => {
        if (el.textContent === '已保存') el.textContent = '';
      }, 2000);
    } else if (status === 'error') {
      el.textContent = message || '保存失败';
      el.classList.add('error');
    } else {
      el.textContent = '';
    }
  },

  autoSaveGroup() {
    const id = document.getElementById('group-id')?.value;
    if (!id) return; // 新建不自动保存
    clearTimeout(this._autoSaveTimer);
    this.setSaveStatus('saving');
    this._autoSaveTimer = setTimeout(() => {
      const form = document.getElementById('group-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }, 500);
  },

  autoSaveModel() {
    const id = document.getElementById('model-id')?.value;
    if (!id) return; // 新建不自动保存
    clearTimeout(this._autoSaveTimer);
    this.setSaveStatus('saving');
    this._autoSaveTimer = setTimeout(() => {
      const form = document.getElementById('model-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }, 500);
  },

  async onGroupSubmit(e) {
    e.preventDefault();
    const mode = document.getElementById('group-provider').value;
    const key = document.getElementById('group-key').value.trim();
    const id = document.getElementById('group-id').value;
    const payload = {
      name: document.getElementById('group-name').value.trim(),
      provider_type: mode,
      base_url: document.getElementById('group-base').value.trim() || undefined,
      ark_api_key: mode === 'ark' ? key : '',
      api_key: mode === 'proxy' ? key : '',
      auto_model_cooldown_minutes: mode === 'relay' ? Number(document.getElementById('group-cooldown').value || 0) : undefined,
      waf_compatible: mode === 'relay' ? document.getElementById('group-waf').checked : false,
    };
    try {
      this.setSaveStatus('saving');
      if (id) await API.saveGroup(id, payload);
      else await API.createGroup(payload);
      await Store.load();
      this.setSaveStatus('saved');
    } catch (err) {
      this.setSaveStatus('error', '保存失败：' + err.message);
      Toast.error('保存失败：' + err.message);
    }
  },

  async onGroupDelete() {
    const id = document.getElementById('group-id').value;
    const group = Store.getGroup(id);
    const ok = await Modal.confirm({
      title: '删除连接组',
      message: `确定删除连接组「${Utils.escapeHtml(group?.name || id)}」吗？组下所有模型也会被删除，此操作不可恢复。`,
      confirmText: '确定删除',
      confirmClass: 'btn-danger'
    });
    if (!ok) return;
    try {
      await API.deleteGroup(id);
      await Store.load();
      Toast.success('连接组已删除');
    } catch (err) {
      Toast.error('删除失败：' + err.message);
    }
  },

  async onAddModelToGroup() {
    const groupId = document.getElementById('group-id')?.value;
    if (!groupId) {
      Toast.warning('请先保存连接组');
      return;
    }
    try {
      const data = await API.createModel({ name: '新模型', ep_id: 'new-model', group_id: groupId, usable: true });
      await Store.load();
      Store.select('model', data.model.id);
      Tabs.switch('config');
      Toast.success('已新建模型，请直接编辑');
    } catch (err) {
      Toast.error('创建失败：' + err.message);
    }
  },

  async onGroupClone() {
    const id = document.getElementById('group-id').value;
    try {
      await API.cloneGroup(id);
      await Store.load();
      Toast.success('连接组已复制');
    } catch (err) {
      Toast.error('复制失败：' + err.message);
    }
  },

  async onModelSubmit(e) {
    e.preventDefault();
    const groupId = document.getElementById('model-group').value;
    const group = Store.getGroup(groupId);
    const useUpstream = ['relay', 'proxy'].includes(group?.provider_type);
    const upstream = useUpstream ? document.getElementById('model-upstream').value.trim() : document.getElementById('model-ep').value.trim();
    const id = document.getElementById('model-id').value;
    const payload = {
      name: document.getElementById('model-name').value.trim(),
      ep_id: upstream || document.getElementById('model-ep')?.value?.trim(),
      group_id: groupId,
      api_key: document.getElementById('model-key')?.value?.trim() || '',
      price_group: document.getElementById('model-price')?.value?.trim() || '',
      price_input: Number(document.getElementById('model-price-input').value || 0),
      price_output: Number(document.getElementById('model-price-output').value || 0),
      upstream_model: upstream,
      usable: document.getElementById('model-usable').checked,
    };
    try {
      this.setSaveStatus('saving');
      if (id) await API.saveModel(id, payload);
      else await API.createModel(payload);
      await Store.load();
      this.setSaveStatus('saved');
    } catch (err) {
      this.setSaveStatus('error', '保存失败：' + err.message);
      Toast.error('保存失败：' + err.message);
    }
  },

  async onModelDelete() {
    const id = document.getElementById('model-id').value;
    const model = Store.getModel(id);
    const ok = await Modal.confirm({
      title: '删除模型',
      message: `确定删除模型「${Utils.escapeHtml(model?.name || id)}」吗？此操作不可恢复。`,
      confirmText: '确定删除',
      confirmClass: 'btn-danger'
    });
    if (!ok) return;
    try {
      await API.deleteModel(id);
      await Store.load();
      Toast.success('模型已删除');
    } catch (err) {
      Toast.error('删除失败：' + err.message);
    }
  },

  async onModelClone() {
    const id = document.getElementById('model-id').value;
    const m = Store.getModel(id);
    if (!m) return;
    try {
      await API.createModel({ ...m, id: undefined, name: `${m.name} 副本` });
      await Store.load();
      Toast.success('模型已复制');
    } catch (err) {
      Toast.error('复制失败：' + err.message);
    }
  },

  async onFetchUpstream() {
    const groupId = document.getElementById('model-group').value;
    const group = Store.getGroup(groupId);
    if (!['relay', 'proxy'].includes(group?.provider_type)) {
      Toast.warning('只有中转站或通用代理模式才能获取模型');
      return;
    }
    const apiKey = document.getElementById('model-key')?.value?.trim() || '';
    const btn = document.getElementById('model-fetch');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '获取中...';
    try {
      await API.fetchUpstreamModels(groupId, apiKey);
      await Store.load();
      this.syncModelModeUI();
      Toast.success('上游模型已获取');
    } catch (err) {
      Toast.error('获取失败：' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  },

  async onBatchImport() {
    const raw = document.getElementById('batch-models').value.trim();
    if (!raw) {
      Toast.warning('请输入模型列表');
      return;
    }
    // 优先尝试 JSON 数组格式
    let text = raw;
    try {
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) throw new Error('must be array');
      text = arr.map(item => `${item.name || item.ep_id || ''},${item.ep_id || item.name || ''}`).join('\n');
    } catch (jsonErr) {
      // 不是 JSON 数组则按原有 CSV 格式处理，后续后端也做基础校验
      if (raw.startsWith('[') || raw.startsWith('{')) {
        Toast.error('格式错误，请检查 JSON 格式是否正确，参考示例');
        return;
      }
    }
    try {
      await API.req('/api/models/batch', {
        method: 'POST',
        body: JSON.stringify({
          group_id: document.getElementById('batch-group').value,
          text,
          api_key: document.getElementById('batch-key').value.trim(),
          price_group: document.getElementById('batch-price').value.trim(),
        })
      });
      document.getElementById('batch-models').value = '';
      await Store.load();
      Toast.success('批量导入完成');
    } catch (err) {
      Toast.error('导入失败：' + err.message);
    }
  },

  async onConfigImport(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      await API.importConfig(file);
      await Store.load();
      Toast.success('配置已导入');
    } catch (err) {
      Toast.error('导入失败：' + err.message);
    } finally {
      e.target.value = '';
    }
  }
};
