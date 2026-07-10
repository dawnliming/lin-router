const Tree = {
  el: null,
  search: '',
  expanded: new Set(),
  dragId: null,

  init() {
    this.el = document.getElementById('sidebar');
    this.el.innerHTML = `
      <div class="sidebar-header">
        <span class="sidebar-title">连接组</span>
        <button class="icon-btn" id="sidebar-collapse" title="折叠/展开">◀</button>
      </div>
      <div class="tree-container" id="tree-root"></div>
    `;
    document.getElementById('sidebar-collapse').addEventListener('click', () => App.toggleSidebar());
    App.applySidebarState?.();
    this.hideMenu = this.hideMenu.bind(this);
    document.addEventListener('click', this.hideMenu);
    this.loadExpanded();
    Store.subscribe(() => this.render());

    // 树空白处右键：全局批量操作
    const treeRoot = document.getElementById('tree-root');
    treeRoot?.addEventListener('contextmenu', e => {
      if (e.target.closest('[data-context]')) return;
      e.preventDefault();
      this.showGlobalMenu(e);
    });
  },

  loadExpanded() {
    try {
      const raw = localStorage.getItem('lin-router-expanded');
      if (raw) {
        const ids = JSON.parse(raw);
        if (Array.isArray(ids)) this.expanded = new Set(ids);
      }
    } catch (_) {}
  },

  saveExpanded() {
    try {
      localStorage.setItem('lin-router-expanded', JSON.stringify([...this.expanded]));
    } catch (_) {}
  },

  render() {
    const root = document.getElementById('tree-root');
    if (!root) return;
    root.innerHTML = this.buildTreeHtml();
    this.attachEvents(root);
  },

  buildTreeHtml() {
    const groups = Store.state.groups || [];
    const models = Store.state.models || [];
    const aggregates = Store.state.aggregate_models || [];
    const aggregateMembers = Store.state.aggregate_members || [];
    if (!groups.length && !aggregates.length) return '<div class="config-placeholder">暂无连接组，点击右上角 + 新建</div>';

    const groupsHtml = groups.map(g => this.buildGroupHtml(g, models)).join('');
    const aggregatesHtml = this.buildAggregatesSection(aggregates, aggregateMembers);
    return `<div class="tree-root">${groupsHtml}${aggregatesHtml}</div>`;
  },

  buildAggregatesSection(aggregates, members) {
    const filtered = this.search
      ? aggregates.filter(a => a.name.toLowerCase().includes(this.search) || (a.display_name || '').toLowerCase().includes(this.search))
      : aggregates;
    if (this.search && !filtered.length) return '';
    const itemsHtml = filtered.map(a => this.buildAggregateHtml(a, members)).join('');
    const emptyHtml = !aggregates.length ? `
      <div class="tree-aggregate-empty">
        <div class="tree-empty-title">暂无聚合模型</div>
        <div class="tree-empty-desc">聚合模型用于替代旧全局模型能力，实现多中转站 fallback 调度。</div>
        <button type="button" class="btn-primary btn-sm" id="tree-new-aggregate">新建聚合模型</button>
      </div>
    ` : '';
    return `
      <div class="tree-aggregate-section">
        <div class="tree-section-title">聚合模型</div>
        ${itemsHtml}
        ${emptyHtml}
      </div>
    `;
  },

  buildAggregateHtml(a, members) {
    const status = this.aggregateStatus(a, members);
    const active = Store.selected.type === 'aggregate' && Store.selected.id === a.id ? 'active' : '';
    const memberCount = members.filter(m => m.aggregate_id === a.id).length;
    return `
      <div class="tree-aggregate ${active}" data-type="aggregate" data-id="${a.id}" data-context="aggregate" title="${Utils.escapeHtml(a.description || '')}">
        <span class="tree-status ${status}"></span>
        <span class="tree-label">${this.highlight(Utils.escapeHtml(a.display_name || a.name))}</span>
        <span class="tree-meta">${memberCount}成员</span>
      </div>
    `;
  },

  aggregateStatus(a, members) {
    if (!a.enabled) return 'error';
    const myMembers = members.filter(m => m.aggregate_id === a.id && m.enabled);
    if (!myMembers.length) return 'error';
    const now = Math.floor(Date.now() / 1000);
    const available = myMembers.filter(m => !m.cooldown_until || m.cooldown_until <= now).length;
    if (available === 0) return 'error';
    if (available < myMembers.length) return 'warning';
    return 'ok';
  },

  buildGroupHtml(g, models) {
    const groupModels = models.filter(m => m.group_id === g.id);
    const expanded = this.expanded.has(g.id);
    const status = this.groupStatus(g, groupModels);
    const modeLabel = { ark: '方舟', relay: '中转', proxy: '代理' }[g.provider_type] || g.provider_type;
    const active = Store.selected.type === 'group' && Store.selected.id === g.id ? 'active' : '';
    const filtered = this.search && !this.matchesGroup(g, groupModels) ? 'hidden' : '';

    return `
      <div class="tree-node ${filtered}" data-type="group" data-id="${g.id}">
        <div class="tree-group ${active}" data-type="group" data-id="${g.id}" data-context="group">
          <span class="tree-toggle" data-action="toggle">${expanded ? '▼' : '▶'}</span>
          <span class="tree-status ${status}"></span>
          <span class="tree-label">${this.highlight(Utils.escapeHtml(g.name))}</span>
          <span class="tree-badge">${modeLabel}</span>
          <span class="tree-meta">${groupModels.length}模型</span>
        </div>
        <div class="tree-children ${expanded ? '' : 'hidden'}">
          ${groupModels.map(m => this.buildModelHtml(m)).join('')}
        </div>
      </div>
    `;
  },

  buildModelHtml(m) {
    const status = this.modelStatus(m);
    const active = Store.selected.type === 'model' && Store.selected.id === m.id ? 'active' : '';
    const meta = m.price_group ? `¥${m.price_group}` : (m.ep_id ? m.ep_id.slice(-6) : '');
    const coolingText = status === 'cooldown' ? this.cooldownText(m) : '';
    return `
      <div class="tree-model ${active}" data-type="model" data-id="${m.id}" data-context="model" draggable="true" title="${Utils.escapeHtml(m.last_error || '')}">
        <span class="tree-status ${status}"></span>
        <span class="tree-label">${this.highlight(Utils.escapeHtml(m.name))}</span>
        ${coolingText ? `<span class="tree-cooldown">${coolingText}</span>` : ''}
        <span class="tree-meta">${Utils.escapeHtml(meta)}</span>
      </div>
    `;
  },

  highlight(text) {
    if (!this.search) return text;
    const s = this.search.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`(${s})`, 'gi');
    return text.replace(re, '<mark>$1</mark>');
  },

  groupStatus(g, models) {
    if (!models.length) return 'error';
    const usable = models.filter(m => m.usable && !this.isCooling(m)).length;
    if (usable === 0) return 'error';
    if (usable < models.length) return 'warning';
    return 'ok';
  },

  modelStatus(m) {
    // 冷却状态优先于可用状态，避免冷却中的模型被误判为错误
    if (this.isCooling(m)) return 'cooldown';
    if (!m.usable) return 'error';
    return 'ok';
  },

  isCooling(m) {
    return m.cooldown_until && m.cooldown_until * 1000 > Date.now();
  },

  cooldownText(m) {
    const remain = Math.max(0, Math.ceil((m.cooldown_until * 1000 - Date.now()) / 1000));
    const mm = Math.floor(remain / 60).toString().padStart(2, '0');
    const ss = (remain % 60).toString().padStart(2, '0');
    return `${mm}:${ss}`;
  },

  matchesGroup(g, models) {
    if (!this.search) return true;
    const s = this.search.toLowerCase();
    if (g.name.toLowerCase().includes(s)) return true;
    return models.some(m => m.name.toLowerCase().includes(s));
  },

  attachEvents(root) {
    const newAggregateBtn = root.querySelector('#tree-new-aggregate');
    if (newAggregateBtn) {
      newAggregateBtn.addEventListener('click', () => App.createAggregate());
    }

    root.querySelectorAll('[data-type]').forEach(node => {
      node.addEventListener('click', e => {
        const action = e.target.dataset.action;
        const type = node.dataset.type;
        const id = node.dataset.id;
        if (action === 'toggle') {
          e.stopPropagation();
          if (this.expanded.has(id)) this.expanded.delete(id);
          else this.expanded.add(id);
          this.saveExpanded();
          this.render();
          return;
        }
        // 模型节点嵌套在组节点内，阻止冒泡避免同时触发组节点点击
        if (type === 'model') {
          e.stopPropagation();
        }
        Store.select(type, id);
        // 单击节点时直接切到配置 Tab，保证右侧内容与左侧选中一致
        Tabs.switch('config');
      });
      node.addEventListener('dblclick', e => {
        const action = e.target.dataset.action;
        if (action === 'toggle') return;
        const type = node.dataset.type;
        const id = node.dataset.id;
        if (type === 'group') {
          // 双击组名展开/折叠
          if (this.expanded.has(id)) this.expanded.delete(id);
          else this.expanded.add(id);
          this.saveExpanded();
          this.render();
          return;
        }
        Store.select(type, id);
        Tabs.switch('test');
      });
    });

    root.querySelectorAll('[data-context]').forEach(node => {
      node.addEventListener('contextmenu', e => {
        e.preventDefault();
        e.stopPropagation();
        this.showMenu(e, node.dataset.context, node.dataset.id);
      });
    });

    root.querySelectorAll('[draggable="true"]').forEach(node => {
      node.addEventListener('dragstart', e => {
        this.dragId = node.dataset.id;
        e.dataTransfer.effectAllowed = 'move';
      });
      node.addEventListener('dragend', () => { this.dragId = null; });
    });

    root.querySelectorAll('.tree-group, .tree-model').forEach(node => {
      node.addEventListener('dragover', e => {
        e.preventDefault();
        if (!this.dragId) return;
        e.dataTransfer.dropEffect = 'move';
        node.classList.add('drag-over');
      });
      node.addEventListener('dragleave', () => node.classList.remove('drag-over'));
      node.addEventListener('drop', e => this.onDrop(e, node));
    });
  },

  async onDrop(e, targetNode) {
    e.preventDefault();
    targetNode.classList.remove('drag-over');
    if (!this.dragId || this.dragId === targetNode.dataset.id) return;

    const dragModel = Store.getModel(this.dragId);
    if (!dragModel) return;

    const targetType = targetNode.dataset.type;
    let targetGroupId = targetType === 'group' ? targetNode.dataset.id : Store.getModel(targetNode.dataset.id)?.group_id;
    if (!targetGroupId) return;

    const sourceGroup = Store.getGroup(dragModel.group_id);
    const targetGroup = Store.getGroup(targetGroupId);
    if (sourceGroup && targetGroup && sourceGroup.provider_type !== targetGroup.provider_type) {
      Toast.warning('只能移动到相同模式的连接组');
      return;
    }

    if (dragModel.group_id !== targetGroupId) {
      // 跨组移动：先改 group_id，再移到目标组最下方
      try {
        await API.saveModel(dragModel.id, { ...dragModel, group_id: targetGroupId });
        await API.moveModel(dragModel.id, { direction: 'bottom' });
        await Store.load();
        Toast.success('模型已移动');
      } catch (err) {
        Toast.error('移动失败：' + err.message);
      }
      return;
    }

    // 同组内：简单上移/下移一次
    const models = Store.getModelsByGroup(targetGroupId);
    const fromIdx = models.findIndex(m => m.id === this.dragId);
    const toIdx = models.findIndex(m => m.id === targetNode.dataset.id);
    if (fromIdx < 0 || toIdx < 0) return;
    const direction = fromIdx > toIdx ? 'up' : 'down';
    try {
      await API.moveModel(this.dragId, { direction });
      await Store.load();
    } catch (err) {
      Toast.error('排序失败：' + err.message);
    }
  },

  showMenu(e, context, id) {
    const menu = document.getElementById('context-menu');
    menu.innerHTML = context === 'group' ? this.groupMenuHtml(id) : (context === 'aggregate' ? this.aggregateMenuHtml(id) : this.modelMenuHtml(id));
    menu.classList.remove('hidden');
    menu.style.left = `${Math.min(e.clientX, window.innerWidth - 180)}px`;
    menu.style.top = `${Math.min(e.clientY, window.innerHeight - 200)}px`;
    this.attachMenuEvents(menu);
  },

  showGlobalMenu(e) {
    const menu = document.getElementById('context-menu');
    menu.innerHTML = `
      <div class="context-item" data-action="new-group">新建连接组</div>
      <div class="context-item" data-action="new-aggregate">新建聚合模型</div>
      <div class="context-separator"></div>
      <div class="context-item" data-action="expand-all">全部展开</div>
      <div class="context-item" data-action="collapse-all">全部折叠</div>
      <div class="context-separator"></div>
      <div class="context-item" data-action="enable-all">全部启用所有模型</div>
    `;
    menu.classList.remove('hidden');
    menu.style.left = `${Math.min(e.clientX, window.innerWidth - 180)}px`;
    menu.style.top = `${Math.min(e.clientY, window.innerHeight - 200)}px`;
    this.attachMenuEvents(menu);
  },

  hideMenu() {
    document.getElementById('context-menu')?.classList.add('hidden');
  },

  groupMenuHtml(id) {
    const g = Store.getGroup(id);
    return `
      <div class="context-item" data-action="test" data-id="${id}">测试自动</div>
      <div class="context-item" data-action="copy-key" data-id="${id}">复制 Key</div>
      <div class="context-item" data-action="copy-client" data-id="${id}">复制 Hermes 配置</div>
      <div class="context-item" data-action="clone-group" data-id="${id}">复制组</div>
      <div class="context-item" data-action="rename-group" data-id="${id}">重命名</div>
      <div class="context-separator"></div>
      <div class="context-item" data-action="enable-group" data-id="${id}">全部启用本组模型</div>
      <div class="context-item" data-action="disable-group" data-id="${id}">全部禁用本组模型</div>
      <div class="context-item" data-action="expand-all" data-id="${id}">全部展开</div>
      <div class="context-item" data-action="collapse-all" data-id="${id}">全部折叠</div>
      <div class="context-separator"></div>
      <div class="context-item danger" data-action="delete-group" data-id="${id}">删除组</div>
    `;
  },

  aggregateMenuHtml(id) {
    const a = Store.getAggregate(id);
    return `
      <div class="context-item" data-action="edit-aggregate" data-id="${id}">编辑</div>
      <div class="context-separator"></div>
      <div class="context-item danger" data-action="delete-aggregate" data-id="${id}">删除</div>
    `;
  },

  modelMenuHtml(id) {
    const m = Store.getModel(id);
    const toggleLabel = m?.usable ? '停用' : '启用';
    const cooling = this.isCooling(m);
    const sourceGroup = Store.getGroup(m?.group_id);
    return `
      <div class="context-item" data-action="edit-model" data-id="${id}">编辑</div>
      <div class="context-item" data-action="clone-model" data-id="${id}">复制模型</div>
      <div class="context-item has-submenu">
        移动到其他组
        <div class="context-submenu">
          ${(Store.state.groups || []).map(g => {
            const sameMode = sourceGroup && g.provider_type === sourceGroup.provider_type;
            return `<div class="context-item ${sameMode ? '' : 'disabled'}" data-action="move-to-group" data-id="${id}" data-target="${g.id}" data-disabled="${!sameMode}">${Utils.escapeHtml(g.name)} <span class="mode-tag">${{ark:'方舟', relay:'中转', proxy:'代理'}[g.provider_type] || g.provider_type}</span></div>`;
          }).join('')}
        </div>
      </div>
      <div class="context-separator"></div>
      <div class="context-item" data-action="toggle-usable" data-id="${id}">${toggleLabel}</div>
      ${cooling ? `<div class="context-item" data-action="reset-cooldown" data-id="${id}">恢复冷却</div>` : ''}
      <div class="context-separator"></div>
      <div class="context-item danger" data-action="delete-model" data-id="${id}">删除模型</div>
    `;
  },

  attachMenuEvents(menu) {
    menu.querySelectorAll('.context-item').forEach(item => {
      item.addEventListener('click', e => {
        e.stopPropagation();
        if (item.dataset.disabled === 'true') return;
        const action = item.dataset.action;
        const id = item.dataset.id;
        const target = item.dataset.target;
        this.hideMenu();
        this.handleMenuAction(action, id, target);
      });
    });
  },

  async handleMenuAction(action, id, target) {
    switch (action) {
      case 'new-group':
        App.createGroup();
        break;
      case 'new-aggregate':
        App.createAggregate();
        break;
      case 'edit-aggregate':
        Store.select('aggregate', id);
        Tabs.switch('config');
        break;
      case 'delete-aggregate': {
        const a = Store.getAggregate(id);
        const ok = await Modal.confirm({
          title: '删除聚合模型',
          message: `确定删除聚合模型「${Utils.escapeHtml(a?.display_name || a?.name || id)}」吗？其下所有成员也会被删除，此操作不可恢复。`,
          confirmText: '确定删除',
          confirmClass: 'btn-danger'
        });
        if (!ok) return;
        try { await API.deleteAggregate(id); await Store.load(); Toast.success('聚合模型已删除'); }
        catch (err) { Toast.error(err.message); }
        break;
      }
      case 'test':
        Store.select('group', id);
        Tabs.switch('test');
        break;
      case 'copy-key': {
        const g = Store.getGroup(id);
        await Utils.copy(g?.route_key || '');
        Toast.success('Key 已复制');
        break;
      }
      case 'copy-client': {
        try {
          const cfg = await API.req(`/api/client-config/${id}`);
          const text = `Base URL: ${cfg.base_url}\nAPI Key: ${cfg.api_key}\nModel: ${cfg.model}`;
          await Utils.copy(text);
          Toast.success('Hermes 配置已复制');
        } catch (err) {
          Toast.error('复制失败：' + err.message);
        }
        break;
      }
      case 'clone-group':
        try { await API.cloneGroup(id); await Store.load(); Toast.success('组已复制'); }
        catch (err) { Toast.error(err.message); }
        break;
      case 'rename-group': {
        const g = Store.getGroup(id);
        const name = prompt('新组名：', g?.name);
        if (name) {
          try { await API.saveGroup(id, { ...g, name }); await Store.load(); Toast.success('已重命名'); }
          catch (err) { Toast.error(err.message); }
        }
        break;
      }
      case 'enable-group':
        try { await API.setGroupUsable(id, true); await Store.load(); Toast.success('本组模型已启用'); }
        catch (err) { Toast.error(err.message); }
        break;
      case 'disable-group':
        try { await API.setGroupUsable(id, false); await Store.load(); Toast.success('本组模型已禁用'); }
        catch (err) { Toast.error(err.message); }
        break;
      case 'enable-all':
        try { await API.setAllUsable(true); await Store.load(); Toast.success('所有模型已启用'); }
        catch (err) { Toast.error(err.message); }
        break;
      case 'expand-all':
        (Store.state.groups || []).forEach(g => this.expanded.add(g.id));
        this.saveExpanded();
        this.render();
        break;
      case 'collapse-all':
        this.expanded.clear();
        this.saveExpanded();
        this.render();
        break;
      case 'delete-group': {
        const g = Store.getGroup(id);
        const ok = await Modal.confirm({
          title: '删除连接组',
          message: `确定删除连接组「${Utils.escapeHtml(g?.name || id)}」吗？组下所有模型也会被删除，此操作不可恢复。`,
          confirmText: '确定删除',
          confirmClass: 'btn-danger'
        });
        if (!ok) return;
        try { await API.deleteGroup(id); await Store.load(); Toast.success('组已删除'); }
        catch (err) { Toast.error(err.message); }
        break;
      }
      case 'edit-model':
        Store.select('model', id);
        break;
      case 'clone-model': {
        const m = Store.getModel(id);
        try {
          await API.createModel({ ...m, id: undefined, name: `${m.name} 副本` });
          await Store.load();
          Toast.success('模型已复制');
        } catch (err) { Toast.error(err.message); }
        break;
      }
      case 'move-to-group': {
        const m = Store.getModel(id);
        const sourceGroup = Store.getGroup(m?.group_id);
        const targetGroup = Store.getGroup(target);
        if (sourceGroup && targetGroup && sourceGroup.provider_type !== targetGroup.provider_type) {
          Toast.warning('只能移动到相同模式的连接组');
          return;
        }
        try { await API.saveModel(id, { ...m, group_id: target }); await Store.load(); Toast.success('模型已移动'); }
        catch (err) { Toast.error(err.message); }
        break;
      }
      case 'toggle-usable':
      case 'reset-cooldown':
        try { await API.req(`/api/models/${id}/toggle`, { method: 'POST' }); await Store.load(); Toast.success('状态已切换'); }
        catch (err) { Toast.error(err.message); }
        break;
      case 'delete-model': {
        const m = Store.getModel(id);
        const ok = await Modal.confirm({
          title: '删除模型',
          message: `确定删除模型「${Utils.escapeHtml(m?.name || id)}」吗？此操作不可恢复。`,
          confirmText: '确定删除',
          confirmClass: 'btn-danger'
        });
        if (!ok) return;
        try { await API.deleteModel(id); await Store.load(); Toast.success('模型已删除'); }
        catch (err) { Toast.error(err.message); }
        break;
      }
    }
  },

  setSearch(s) {
    this.search = (s || '').trim().toLowerCase();
    this.render();
  },

  jumpToFirstMatch() {
    if (!this.search) return;
    const models = Store.state.models || [];
    const groups = Store.state.groups || [];
    // 优先匹配模型
    const firstModel = models.find(m => m.name.toLowerCase().includes(this.search));
    if (firstModel) {
      const g = Store.getGroup(firstModel.group_id);
      if (g) {
        this.expanded.add(g.id);
        this.saveExpanded();
      }
      Store.select('model', firstModel.id);
      this.render();
      this.scrollIntoView(`[data-type="model"][data-id="${firstModel.id}"]`);
      return;
    }
    // 再匹配组
    const firstGroup = groups.find(g => g.name.toLowerCase().includes(this.search));
    if (firstGroup) {
      this.expanded.add(firstGroup.id);
      this.saveExpanded();
      Store.select('group', firstGroup.id);
      this.render();
      this.scrollIntoView(`[data-type="group"][data-id="${firstGroup.id}"]`);
    }
  },

  scrollIntoView(selector) {
    const el = document.querySelector(selector);
    if (el) {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      el.classList.add('flash');
      setTimeout(() => el.classList.remove('flash'), 800);
    }
  }
};
