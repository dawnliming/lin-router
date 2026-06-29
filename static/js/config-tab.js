const ConfigTab = {
  onShow() {
    const panel = document.getElementById('panel-config');
    if (!panel) return;
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-config');
    const sel = Store.selected;
    const item = sel.type === 'group' ? Store.getGroup(sel.id) : Store.getModel(sel.id);
    const title = item ? Utils.escapeHtml(item.name) : (sel.type === 'group' ? '新建连接组' : '新建模型');

    panel.innerHTML = `
      <h2>${title}</h2>
      <div class="config-layout">
        <div class="config-main">
          ${sel.type === 'group' || sel.type === null ? this.renderGroupSection() : this.renderModelSection()}
        </div>
        <div class="config-side">
          ${this.renderSettings()}
          ${this.renderBatchImport()}
          ${this.renderConfigTools()}
        </div>
      </div>
    `;
    this.attachEvents(panel);
    this.syncUIFromState();
  },

  renderGroupSection() {
    const sel = Store.selected;
    const g = sel.type === 'group' ? Store.getGroup(sel.id) : null;
    const provider = g?.provider_type || 'ark';
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
        <section class="form-card">
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
            <input id="group-route-key" value="${Utils.escapeHtml(g?.route_key || '')}" readonly>
          </div>
          <div class="form-row">
            <label>模式说明</label>
            <div class="form-hint" id="group-mode-hint"></div>
          </div>
          <div class="form-actions">
            <button type="submit" class="btn-primary">保存连接组</button>
            ${g ? `<button type="button" class="btn-danger" id="group-delete">删除组</button>` : ''}
            ${g ? `<button type="button" id="group-clone">复制组</button>` : ''}
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
    return `
      <form class="config-form" id="model-form" data-type="model">
        <input type="hidden" id="model-id" value="${m?.id || ''}">
        <section class="form-card">
          <h3>基础配置</h3>
          <div class="form-row">
            <label>显示名称</label>
            <input id="model-name" value="${Utils.escapeHtml(m?.name || '')}" placeholder="DeepSeek">
          </div>
          <div class="form-row">
            <label>连接组</label>
            <select id="model-group">${this.renderGroupOptions(groupId)}</select>
          </div>
          <div class="form-row" id="model-ep-row">
            <label>上游模型 / EP</label>
            <input id="model-ep" value="${Utils.escapeHtml(m?.ep_id || '')}" placeholder="ep-xxxx / deepseek-chat">
          </div>
          <div class="form-row hidden" id="model-upstream-row">
            <label>上游模型</label>
            <select id="model-upstream"></select>
          </div>
          <div class="form-row" id="model-key-row">
            <label>中转站 API Key</label>
            <input id="model-key" type="password" value="${Utils.escapeHtml(m?.api_key || '')}" placeholder="sk-xxxx">
          </div>
        </section>
        <section class="form-card">
          <h3>调度配置</h3>
          <div class="form-row" id="model-price-row">
            <label>价格组 / 通道</label>
            <input id="model-price" value="${Utils.escapeHtml(m?.price_group || '')}" placeholder="cheap / standard / premium">
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
          <div class="form-actions">
            <button type="submit" class="btn-primary">保存模型</button>
            ${m ? `<button type="button" id="model-delete" class="btn-danger">删除</button>` : ''}
            ${m ? `<button type="button" id="model-clone">复制</button>` : ''}
            ${m ? `<button type="button" id="model-fetch">自动获取模型</button>` : ''}
          </div>
        </section>
      </form>
    `;
  },

  renderSettings() {
    const s = Store.state.settings || {};
    return `
      <section class="form-card">
        <h3>设置</h3>
        <label class="checkbox-row">
          <span>开机自启（Windows 当前用户）</span>
          <input id="setting-auto-start" type="checkbox" ${s.auto_start ? 'checked' : ''}>
        </label>
        <label class="checkbox-row">
          <span>启动后最小化到托盘</span>
          <input id="setting-start-minimized" type="checkbox" ${s.start_minimized ? 'checked' : ''}>
        </label>
        <div class="form-hint">开机自启会写入注册表启动项；被杀软拦截属于正常情况。</div>
      </section>
    `;
  },

  renderBatchImport() {
    return `
      <section class="form-card">
        <h3>批量导入</h3>
        <div class="form-row">
          <label>连接组</label>
          <select id="batch-group">${this.renderGroupOptions()}</select>
        </div>
        <div class="form-row">
          <label>模型列表</label>
          <textarea id="batch-models" rows="4" placeholder="显示名称,上游模型&#10;另一个模型,upstream-model-2"></textarea>
        </div>
        <div class="form-row">
          <label>中转站 API Key</label>
          <input id="batch-key" type="password" placeholder="批量导入时可填">
        </div>
        <div class="form-row">
          <label>价格组 / 通道</label>
          <input id="batch-price" placeholder="cheap / standard / premium">
        </div>
        <button type="button" id="batch-import" class="btn-primary" style="width:100%">批量导入模型</button>
      </section>
    `;
  },

  renderConfigTools() {
    return `
      <section class="form-card">
        <h3>配置导入 / 导出</h3>
        <div class="form-actions" style="flex-direction:column">
          <button type="button" id="config-export" class="btn-secondary" style="width:100%">导出配置</button>
          <button type="button" id="config-import" class="btn-secondary" style="width:100%">导入配置</button>
          <input id="config-import-file" type="file" accept="application/json,.json" style="display:none">
        </div>
        <div class="form-hint">导出当前连接组与模型为 JSON；导入时按 ID 合并，同名覆盖、其余保留。</div>
      </section>
    `;
  },

  renderGroupOptions(selectedId) {
    return Store.state.groups?.map(g =>
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
    const cooldownRow = document.getElementById('group-cooldown-row');
    const wafRow = document.getElementById('group-waf-row');
    const hint = document.getElementById('group-mode-hint');
    const label = document.getElementById('group-key-label');

    if (keyRow) keyRow.classList.toggle('hidden', !needsKey);
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

    const epRow = document.getElementById('model-ep-row');
    const upstreamRow = document.getElementById('model-upstream-row');
    const keyRow = document.getElementById('model-key-row');
    const priceRow = document.getElementById('model-price-row');

    if (epRow) epRow.classList.toggle('hidden', relay || proxy);
    if (upstreamRow) upstreamRow.classList.toggle('hidden', !(relay || proxy));
    if (keyRow) keyRow.classList.toggle('hidden', !relay);
    if (priceRow) priceRow.classList.toggle('hidden', !relay);

    if (relay || proxy) this.renderUpstreamOptions(groupId);
  },

  renderUpstreamOptions(groupId) {
    const select = document.getElementById('model-upstream');
    if (!select) return;
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
      panel.querySelector('#group-provider')?.addEventListener('change', () => this.syncGroupModeUI());
      panel.querySelector('#group-key-toggle')?.addEventListener('click', e => {
        const input = document.getElementById('group-key');
        input.type = input.type === 'password' ? 'text' : 'password';
        e.target.textContent = input.type === 'password' ? '显示' : '隐藏';
      });
      panel.querySelector('#group-delete')?.addEventListener('click', () => this.onGroupDelete());
      panel.querySelector('#group-clone')?.addEventListener('click', () => this.onGroupClone());
    }

    // 模型表单
    const modelForm = panel.querySelector('#model-form');
    if (modelForm) {
      modelForm.addEventListener('submit', e => this.onModelSubmit(e));
      panel.querySelector('#model-group')?.addEventListener('change', () => this.syncModelModeUI());
      panel.querySelector('#model-delete')?.addEventListener('click', () => this.onModelDelete());
      panel.querySelector('#model-clone')?.addEventListener('click', () => this.onModelClone());
      panel.querySelector('#model-fetch')?.addEventListener('click', () => this.onFetchUpstream());
    }

    // 设置
    panel.querySelector('#setting-auto-start')?.addEventListener('change', e => this.updateSetting('auto_start', e.target.checked));
    panel.querySelector('#setting-start-minimized')?.addEventListener('change', e => this.updateSetting('start_minimized', e.target.checked));

    // 批量导入
    panel.querySelector('#batch-import')?.addEventListener('click', () => this.onBatchImport());

    // 配置导入/导出
    panel.querySelector('#config-export')?.addEventListener('click', () => App.exportConfig());
    panel.querySelector('#config-import')?.addEventListener('click', () => panel.querySelector('#config-import-file')?.click());
    panel.querySelector('#config-import-file')?.addEventListener('change', e => this.onConfigImport(e));
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
      if (id) await API.saveGroup(id, payload);
      else await API.createGroup(payload);
      await Store.load();
      Toast.success('连接组已保存');
    } catch (err) {
      Toast.error('保存失败：' + err.message);
    }
  },

  async onGroupDelete() {
    const id = document.getElementById('group-id').value;
    const group = Store.getGroup(id);
    if (!confirm(`删除连接组「${group?.name || id}」？`)) return;
    try {
      await API.deleteGroup(id);
      await Store.load();
      Toast.success('连接组已删除');
    } catch (err) {
      Toast.error('删除失败：' + err.message);
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
      ep_id: upstream,
      group_id: groupId,
      api_key: document.getElementById('model-key').value.trim(),
      price_group: document.getElementById('model-price').value.trim(),
      upstream_model: upstream,
      usable: document.getElementById('model-usable').checked,
    };
    try {
      if (id) await API.saveModel(id, payload);
      else await API.createModel(payload);
      await Store.load();
      Toast.success('模型已保存');
    } catch (err) {
      Toast.error('保存失败：' + err.message);
    }
  },

  async onModelDelete() {
    const id = document.getElementById('model-id').value;
    const model = Store.getModel(id);
    if (!confirm(`删除模型「${model?.name || id}」？`)) return;
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
    try {
      await API.cloneModel(id);
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
      Toast.warning('只有中转站或通用代理模式才能自动获取模型');
      return;
    }
    const btn = document.getElementById('model-fetch');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '获取中...';
    try {
      await API.fetchUpstreamModels(groupId);
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

  async onBatchImport() {
    try {
      await API.req('/api/models/batch', {
        method: 'POST',
        body: JSON.stringify({
          group_id: document.getElementById('batch-group').value,
          text: document.getElementById('batch-models').value,
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
