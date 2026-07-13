const ConfigTabActions = {
  async onRecoverModel(controller) {
    const id = document.getElementById('model-id')?.value;
    const model = id ? Store.getModel(id) : null;
    if (!id || !model) return;
    const ok = await Modal.confirm({
      title: '重试恢复模型',
      message: `将向当前模型发送最小探测请求；仅探测成功后才恢复参与调度，不影响其他冷却模型。确认继续？`,
      confirmText: '确认重试恢复',
      allowHtml: true,
    });
    if (!ok) return;
    try {
      const res = await API.recoverModel(id);
      await Store.load();
      if (Tabs.current === 'config' && Store.selected.type === 'model' && Store.selected.id === id) controller.render();
      Toast.success(res.message || '模型已重试恢复');
    } catch (err) {
      Toast.error('重试恢复失败：' + err.message);
    }
  },

  onGroupWorkflowAction(controller, action, modelId) {
    const groupId = document.getElementById('group-id')?.value;
    if (action === 'focus-required') {
      controller.validateGroupForm({ focus: true });
      return;
    }
    if (action === 'fetch-models') return controller.fetchModelsForGroup(groupId);
    if (action === 'add-model') return controller.onAddModelToGroup(groupId);
    if (action === 'edit-model') {
      Store.select('model', modelId);
      return Tabs.switch('config');
    }
    if (action === 'test-model') return controller.openQuickTest(modelId);
    if (action === 'copy-client') return controller.copyGroupClientConfig(groupId, modelId);
  },

  openQuickTest(controller, modelId) {
    if (!modelId) return Toast.warning('请先添加一个模型');
    Store.select('model', modelId);
    Tabs.switch('test');
  },

  copyGroupClientConfig(controller, groupId, modelId) {
    const group = Store.getGroup(groupId);
    const model = Store.getModel(modelId) || (group ? ConnectionStatus.group(group).representative : null);
    if (!group?.route_key || !model?.name) return Toast.warning('当前连接组还没有可复制的客户端配置');
    const text = `Base URL: ${window.location.origin}/v1\nAPI Key: ${group.route_key}\nModel: ${model.name}`;
    return Utils.copy(text).then(ok => ok ? Toast.success('客户端配置已复制') : Toast.error('复制失败'));
  },

  async onGroupSubmit(controller, e) {
    e.preventDefault();
    const explicitSubmit = Boolean(e.submitter);
    const validation = controller.validateGroupForm({ focus: explicitSubmit });
    if (!validation.ok) {
      if (explicitSubmit) Toast.warning(validation.message);
      else controller.setSaveStatus('error', validation.message);
      return;
    }
    const id = document.getElementById('group-id').value;
    const isNewGroupDraft = !id && controller.isNewGroupDraft();
    if (Store.selected.type !== 'group' || (id ? Store.selected.id !== id : !isNewGroupDraft)) {
      Toast.error('当前连接组表单状态已过期，请重新选择后再保存');
      controller.render();
      return;
    }
    const mode = document.getElementById('group-provider').value;
    const key = document.getElementById('group-key').value.trim();
    const payload = {
      name: document.getElementById('group-name').value.trim(),
      provider_type: mode,
      base_url: document.getElementById('group-base').value.trim(),
      ark_api_key: mode === 'ark' ? key : '',
      api_key: mode === 'proxy' ? key : '',
      auto_model_name: document.getElementById('group-auto-model-name').value.trim(),
      auto_model_cooldown_minutes: Number(document.getElementById('group-cooldown').value || 0),
      stream_idle_timeout: Math.max(0, Math.min(600, Number(document.getElementById('group-stream-timeout').value || 0))),
      reasoning_support: document.getElementById('group-reasoning-support')?.value || 'unknown',
      waf_client_mode: mode === 'relay' && document.getElementById('group-waf').checked
        ? (document.getElementById('group-waf-client-mode')?.value || 'always')
        : 'always',
      waf_compatible: mode === 'relay' ? document.getElementById('group-waf').checked : false,
      serial_protection: mode === 'relay'
        && document.querySelector('input[name="group-request-concurrency"]:checked')?.value === 'serial',
      waf_accept_policy: mode === 'relay' && document.getElementById('group-waf').checked
        ? (document.getElementById('group-waf-policy')?.value || 'default')
        : 'default',
    };
    try {
      controller.setSaveStatus('saving');
      const result = id ? await API.saveGroup(id, payload) : await API.createGroup(payload);
      if (!id) controller._newGroupDraft = null;
      await Store.load();
      if (!id && result?.group?.id) Store.select('group', result.group.id);
      controller.setSaveStatus('saved');
      const savedGroupId = id || result?.group?.id;
      const stillViewingSavedGroup = Tabs.current === 'config'
        && Store.selected.type === 'group'
        && Store.selected.id === savedGroupId;
      if (explicitSubmit && stillViewingSavedGroup) {
        controller.render();
        Toast.success('连接组已保存，请按状态提示完成下一步');
      }
    } catch (err) {
      controller.setSaveStatus('error', '保存失败：' + err.message);
      Toast.error('保存失败：' + err.message);
    }
  },

  async onGroupDelete(controller) {
    const id = document.getElementById('group-id').value;
    const group = Store.getGroup(id);
    let preview = null;
    try { preview = await API.previewDeleteGroup(id); } catch (_) {}
    const impact = preview?.ok ? `
      <div class="preview-impact">
        <p>将删除连接组「${Utils.escapeHtml(group?.name || id)}」以及 ${preview.affected_models || 0} 个模型。</p>
        ${(preview.affected_model_names || []).length ? `<p>受影响模型：${Utils.escapeHtml(preview.affected_model_names.join('、'))}</p>` : ''}
        ${(preview.affected_aggregate_members || []).length ? `<p>受影响聚合成员：${preview.affected_aggregate_members.map(item => `${Utils.escapeHtml(item.aggregate_name)} / ${Utils.escapeHtml(item.model)}`).join('；')}</p>` : ''}
        ${(preview.warnings || []).map(w => `<p class="danger-text">${Utils.escapeHtml(w)}</p>`).join('')}
        <p>此操作不可恢复，建议先导出备份。</p>
      </div>` : `确定删除连接组「${Utils.escapeHtml(group?.name || id)}」吗？组下所有模型也会被删除，此操作不可恢复。`;
    const ok = await Modal.confirm({
      title: '删除连接组影响预览',
      message: impact,
      allowHtml: !!preview?.ok,
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

  async onAddModelToGroup(controller, groupId = '') {
    groupId = groupId || document.getElementById('group-id')?.value;
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

  async onGroupClone(controller) {
    const id = document.getElementById('group-id').value;
    try {
      await API.cloneGroup(id);
      await Store.load();
      Toast.success('连接组已复制');
    } catch (err) {
      Toast.error('复制失败：' + err.message);
    }
  },

  async onModelSubmit(controller, e) {
    e.preventDefault();
    const explicitSubmit = Boolean(e.submitter);
    const validation = controller.validateModelForm({ focus: explicitSubmit });
    if (!validation.ok) {
      if (explicitSubmit) Toast.warning(validation.message);
      else controller.setSaveStatus('error', validation.message);
      return;
    }
    const id = document.getElementById('model-id').value;
    if (Store.selected.type !== 'model' || Store.selected.id !== id) {
      Toast.error('当前模型表单状态已过期，请重新选择后再保存');
      controller.render();
      return;
    }
    const groupId = document.getElementById('model-group').value;
    const group = Store.getGroup(groupId);
    const useUpstream = ['relay', 'proxy'].includes(group?.provider_type);
    const upstream = useUpstream ? document.getElementById('model-upstream').value.trim() : document.getElementById('model-ep').value.trim();
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
      controller.setSaveStatus('saving');
      if (id) await API.saveModel(id, payload);
      else await API.createModel(payload);
      await Store.load();
      controller.setSaveStatus('saved');
    } catch (err) {
      controller.setSaveStatus('error', '保存失败：' + err.message);
      Toast.error('保存失败：' + err.message);
    }
  },

  async onModelDelete(controller) {
    const id = document.getElementById('model-id').value;
    const model = Store.getModel(id);
    let preview = null;
    try { preview = await API.previewDeleteModel(id); } catch (_) {}
    const impact = preview?.ok ? `
      <div class="preview-impact">
        <p>将删除模型「${Utils.escapeHtml(model?.name || id)}」。</p>
        ${(preview.affected_aggregate_members || []).length ? `<p>依赖它的聚合成员：${preview.affected_aggregate_members.map(item => `${Utils.escapeHtml(item.aggregate_name)} / ${Utils.escapeHtml(item.member_id)}`).join('；')}</p>` : '<p>没有聚合成员依赖该模型。</p>'}
        ${(preview.warnings || []).map(w => `<p class="danger-text">${Utils.escapeHtml(w)}</p>`).join('')}
        <p>此操作不可恢复，建议先导出备份。</p>
      </div>` : `确定删除模型「${Utils.escapeHtml(model?.name || id)}」吗？此操作不可恢复。`;
    const ok = await Modal.confirm({
      title: '删除模型影响预览',
      message: impact,
      allowHtml: !!preview?.ok,
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

  async onModelClone(controller) {
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

  async onAggregateSubmit(controller, e) {
    e.preventDefault();
    const id = document.getElementById('aggregate-id').value;
    if (Store.selected.type !== 'aggregate' || Store.selected.id !== id) {
      Toast.error('当前聚合模型表单状态已过期，请重新选择后再保存');
      controller.render();
      return;
    }
    const payload = {
      name: document.getElementById('aggregate-name').value.trim(),
      display_name: document.getElementById('aggregate-display-name').value.trim(),
      description: document.getElementById('aggregate-description').value.trim(),
      client_model_aliases: document.getElementById('aggregate-client-model-aliases').value.split(/[\n,]+/).map(value => value.trim()).filter(Boolean),
      enabled: document.getElementById('aggregate-enabled').checked,
      cooldown_minutes: Math.max(0, Number(document.getElementById('aggregate-cooldown').value || 0)),
      strategy: document.getElementById('aggregate-strategy').value,
    };
    try {
      controller.setSaveStatus('saving');
      if (id) await API.saveAggregate(id, payload);
      else await API.createAggregate(payload);
      await Store.load();
      controller.setSaveStatus('saved');
    } catch (err) {
      controller.setSaveStatus('error', '保存失败：' + err.message);
      Toast.error('保存失败：' + err.message);
    }
  },

  async onAggregateDelete(controller) {
    const id = document.getElementById('aggregate-id').value;
    const aggregate = Store.getAggregate(id);
    const ok = await Modal.confirm({
      title: '删除聚合模型',
      message: `确定删除聚合模型「${Utils.escapeHtml(aggregate?.display_name || aggregate?.name || id)}」吗？其下所有成员也会被删除，此操作不可恢复。`,
      confirmText: '确定删除',
      confirmClass: 'btn-danger'
    });
    if (!ok) return;
    try {
      await API.deleteAggregate(id);
      await Store.load();
      Toast.success('聚合模型已删除');
    } catch (err) {
      Toast.error('删除失败：' + err.message);
    }
  },

  async onAddAggregateMember(controller) {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    if (!aggregateId) {
      Toast.warning('请先保存聚合模型');
      return;
    }
    const groups = (Store.state.groups || []).filter(g => g.provider_type === 'relay');
    if (!groups.length) {
      Toast.warning('请先创建 relay 中转站连接组');
      return;
    }
    const groupOptions = groups.map(g => `<option value="${g.id}">${Utils.escapeHtml(g.name)}</option>`).join('');
    const html = `
      <div class="form-row">
        <label>连接组</label>
        <select id="member-group">${groupOptions}</select>
      </div>
      <div class="form-row">
        <label>模型</label>
        <select id="member-model"><option value="">请选择模型</option></select>
      </div>
      <div class="form-row">
        <label>手动价格（可选）</label>
        <input id="member-price" type="number" step="0.001" placeholder="默认继承底层模型价格，可手动覆盖">
      </div>
      <div class="form-row">
        <label>预览</label>
        <div id="member-preview" class="form-hint">-</div>
      </div>
    `;
    const updateModelOptions = (overlay) => {
      const groupSelect = overlay.querySelector('#member-group');
      const modelSelect = overlay.querySelector('#member-model');
      const preview = overlay.querySelector('#member-preview');
      if (!groupSelect || !modelSelect) return;
      const priceInput = overlay.querySelector('#member-price');
      const applyDefaultPrice = () => {
        const model = Store.getModel(modelSelect.value);
        if (!priceInput || !model || priceInput.dataset.touched === 'true') return;
        const candidates = [model.price_input, model.price_output].map(v => Number(v || 0)).filter(v => v > 0);
        priceInput.value = candidates.length ? String(Math.min(...candidates)) : '';
      };
      const refresh = () => {
        const groupId = groupSelect.value;
        const group = Store.getGroup(groupId);
        if (!group || group.provider_type !== 'relay') {
          modelSelect.innerHTML = '<option value="">请选择 relay 连接组</option>';
          controller._updateMemberPreview('', '', preview);
          return;
        }
        const models = Store.getModelsByGroup(groupId).filter(m => m.usable !== false);
        const existing = Store.getAggregateMembers(aggregateId).map(m => m.model_id);
        modelSelect.innerHTML = '<option value="">请选择模型</option>' + models.map(m =>
          `<option value="${m.id}" ${existing.includes(m.id) ? 'disabled' : ''}>${Utils.escapeHtml(m.name)}${m.upstream_model && m.upstream_model !== m.name ? ` (${Utils.escapeHtml(m.upstream_model)})` : ''}</option>`
        ).join('');
        controller._updateMemberPreview(groupSelect.value, modelSelect.value, preview);
      };
      priceInput?.addEventListener('input', () => { priceInput.dataset.touched = 'true'; });
      groupSelect.addEventListener('change', () => {
        if (priceInput) { priceInput.dataset.touched = ''; priceInput.value = ''; }
        refresh();
      });
      modelSelect.addEventListener('change', () => {
        applyDefaultPrice();
        controller._updateMemberPreview(groupSelect.value, modelSelect.value, preview);
      });
      refresh();
    };
    const values = await Modal.form({
      title: '添加聚合成员',
      html,
      onRender: updateModelOptions,
      validate: (vals) => {
        if (!vals['member-group']) return '请选择连接组';
        if (!vals['member-model']) return '请选择模型';
        return null;
      }
    });
    if (!values) return;
    const priceValue = values['member-price']?.trim();
    const manualPrice = priceValue === '' ? null : Number(priceValue);
    if (priceValue !== '' && (isNaN(manualPrice) || manualPrice < 0)) {
      Toast.error('手动价格必须是大于等于 0 的数字');
      return;
    }
    try {
      await API.createAggregateMember(aggregateId, { group_id: values['member-group'], model_id: values['member-model'], manual_price: manualPrice });
      await controller.reloadAfterAggregateMemberChange();
      Toast.success('成员已添加');
    } catch (err) {
      Toast.error('添加失败：' + err.message);
    }
  },

  _updateMemberPreview(controller, groupId, modelId, previewEl) {
    if (!previewEl) return;
    const group = Store.getGroup(groupId);
    const model = Store.getModel(modelId);
    if (!group || !model) {
      previewEl.textContent = '-';
      return;
    }
    const upstream = model.upstream_model || model.ep_id || model.name;
    const priceHint = (Number(model.price_input || 0) || Number(model.price_output || 0))
      ? ` / 底层价格 输入 ${model.price_input || 0} 输出 ${model.price_output || 0}`
      : '';
    previewEl.innerHTML = Utils.escapeHtml(`${group.name} / ${model.name} → ${upstream}${model.price_group ? ' / 价格组 ' + model.price_group : ''}${priceHint}`);
  },

  onAggregateMemberAction(controller, action, memberId) {
    if (action === 'delete') return controller.onDeleteAggregateMember(memberId);
    if (action === 'recover') return controller.onRecoverAggregateMember(memberId);
    if (action === 'enable') return controller.onToggleAggregateMember(memberId, true);
    if (action === 'disable') return controller.onToggleAggregateMember(memberId, false);
    if (['up', 'down', 'top', 'bottom'].includes(action)) return controller.onMoveAggregateMember(memberId, action);
  },

  aggregateChainSummary(controller, chain) {
    const items = (chain || []).slice(0, 8).map((item, idx) => {
      const status = item.derived_status && item.derived_status !== 'healthy' ? `（${item.derived_reason || item.derived_status}）` : '';
      return `${idx + 1}. ${item.group_name || '-'} / ${item.model_name || '-'}${status}`;
    });
    const suffix = (chain || []).length > 8 ? `\n… 其余 ${(chain || []).length - 8} 个候选` : '';
    return items.join('\n') + suffix;
  },

  async confirmAggregateMemberPreview(controller, title, preview, confirmText) {
    if (!preview?.ok) return false;
    const before = Utils.escapeHtml(controller.aggregateChainSummary(preview.candidate_chain_before || []));
    const after = Utils.escapeHtml(controller.aggregateChainSummary(preview.candidate_chain_after || []));
    return Modal.confirm({
      title,
      message: `
        <p>聚合模型：${Utils.escapeHtml(preview.aggregate_name || preview.aggregate_id || '-')}</p>
        <div class="preview-grid">
          <div><strong>变更前候选链</strong><pre>${before || '-'}</pre></div>
          <div><strong>变更后候选链</strong><pre>${after || '-'}</pre></div>
        </div>
      `,
      confirmText,
      cancelText: '取消',
      allowHtml: true,
      wide: true,
    });
  },

  async reloadAfterAggregateMemberChange(controller) {
    await Store.load();
    if (Tabs.current === 'config' && Store.selected.type === 'aggregate') {
      controller.render();
    }
  },

  async onRecoverAggregateMember(controller, memberId) {
    const button = document.querySelector(`[data-action="recover"][data-member-id="${CSS.escape(memberId)}"]`);
    if (button) {
      button.disabled = true;
      button.textContent = '恢复中…';
    }
    try {
      const res = await API.recoverAggregateMember(memberId);
      await controller.reloadAfterAggregateMemberChange();
      Toast.success(res.message || '成员已重试恢复');
    } catch (err) {
      if (button) {
        button.disabled = false;
        button.textContent = '重试恢复';
      }
      Toast.error('恢复失败：' + err.message);
    }
  },

  async onMoveAggregateMember(controller, memberId, direction) {
    try {
      await API.saveAggregateMember(memberId, { direction });
      await controller.reloadAfterAggregateMemberChange();
    } catch (err) {
      Toast.error('排序失败：' + err.message);
    }
  },

  bindAggregateMemberDragAndDrop(controller, panel) {
    const body = panel.querySelector('.aggregate-members-table tbody');
    if (!body) return;
    let draggedRow = null;
    body.querySelectorAll('tr[data-member-id]').forEach(row => {
      row.addEventListener('dragstart', event => {
        draggedRow = row;
        row.classList.add('aggregate-member-dragging');
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', row.dataset.memberId || '');
      });
      row.addEventListener('dragend', () => {
        draggedRow = null;
        body.querySelectorAll('.aggregate-member-drop-target').forEach(item => item.classList.remove('aggregate-member-drop-target'));
        row.classList.remove('aggregate-member-dragging');
      });
      row.addEventListener('dragover', event => {
        if (!draggedRow || draggedRow === row) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        body.querySelectorAll('.aggregate-member-drop-target').forEach(item => item.classList.remove('aggregate-member-drop-target'));
        row.classList.add('aggregate-member-drop-target');
      });
      row.addEventListener('drop', async event => {
        event.preventDefault();
        if (!draggedRow || draggedRow === row) return;
        const rows = [...body.querySelectorAll('tr[data-member-id]')];
        const from = rows.indexOf(draggedRow);
        const to = rows.indexOf(row);
        if (from < 0 || to < 0) return;
        body.insertBefore(draggedRow, from < to ? row.nextSibling : row);
        const memberIds = [...body.querySelectorAll('tr[data-member-id]')].map(item => item.dataset.memberId).filter(Boolean);
        await controller.onReorderAggregateMembers(memberIds, body);
      });
    });
  },

  async onReorderAggregateMembers(controller, memberIds, body) {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    if (!aggregateId || !memberIds.length) return;
    const expectedRevision = Store.getAggregateMemberRevision(aggregateId);
    body.classList.add('aggregate-members-saving');
    try {
      await API.reorderAggregateMembers(aggregateId, memberIds, expectedRevision);
      await controller.reloadAfterAggregateMemberChange();
      Toast.success('成员顺序已保存');
    } catch (err) {
      const conflict = err.code === 'aggregate_member_revision_conflict';
      Toast.error(conflict ? '排序冲突：成员顺序已被其他操作更新，已刷新最新顺序。' : '排序未保存，已刷新最新顺序：' + err.message);
      await controller.reloadAfterAggregateMemberChange();
    } finally {
      body.classList.remove('aggregate-members-saving');
    }
  },

  async onCopyAggregateRouteKey(controller) {
    const input = document.getElementById('aggregate-route-key');
    if (!input || !input.value) return;
    try {
      await navigator.clipboard.writeText(input.value);
      Toast.success('聚合模型 Key 已复制');
    } catch (err) {
      input.select();
      document.execCommand('copy');
      Toast.success('聚合模型 Key 已复制');
    }
  },

  async onUpdateAggregateMemberPrice(controller, memberId, value) {
    const member = Store.state.aggregate_members?.find(m => m.id === memberId);
    if (!member) return;
    const trimmed = value.trim();
    const manualPrice = trimmed === '' ? null : Number(trimmed);
    if (trimmed !== '' && (isNaN(manualPrice) || manualPrice < 0)) {
      Toast.error('手动价格必须是大于等于 0 的数字');
      return;
    }
    // 避免 change + blur 重复保存，或重新渲染后旧值触发无意义请求
    const currentPrice = member.manual_price != null ? Number(member.manual_price) : null;
    if (manualPrice === currentPrice) return;
    try {
      await API.saveAggregateMember(memberId, { manual_price: manualPrice });
      await controller.reloadAfterAggregateMemberChange();
    } catch (err) {
      Toast.error('价格更新失败：' + err.message);
    }
  },

  async onToggleAggregateMember(controller, memberId, enabled) {
    const member = Store.state.aggregate_members?.find(m => m.id === memberId);
    if (!member) return;
    try {
      await API.saveAggregateMember(memberId, { enabled, clear_cooldown: enabled });
      await controller.reloadAfterAggregateMemberChange();
      Toast.success(enabled ? '聚合成员已启用并清理冷却' : '聚合成员已停用');
    } catch (err) {
      Toast.error('状态更新失败：' + err.message);
    }
  },

  async onDeleteAggregateMember(controller, memberId) {
    const ok = await Modal.confirm({
      title: '删除成员',
      message: '确定从聚合模型中移除该成员吗？',
      confirmText: '确定删除',
      confirmClass: 'btn-danger'
    });
    if (!ok) return;
    try {
      await API.deleteAggregateMember(memberId);
      await controller.reloadAfterAggregateMemberChange();
      Toast.success('成员已删除');
    } catch (err) {
      Toast.error('删除失败：' + err.message);
    }
  },

  async onFetchUpstream(controller) {
    const groupId = document.getElementById('model-group').value;
    const apiKey = document.getElementById('model-key')?.value?.trim() || '';
    const btn = document.getElementById('model-fetch');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '获取中...';
    try {
      await controller.fetchModelsForGroup(groupId, apiKey);
      // 获取到新列表后清空旧值，并直接渲染下拉，避免 datalist 在 Safari 中不刷新
      const upstreamInput = document.getElementById('model-upstream');
      if (upstreamInput) upstreamInput.value = '';
      controller.renderUpstreamOptions(groupId, true);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  },

  async fetchModelsForGroup(controller, groupId, suppliedKey = '') {
    const group = Store.getGroup(groupId);
    if (!['relay', 'proxy'].includes(group?.provider_type)) {
      Toast.warning('当前模式不支持自动获取模型，请直接手动添加模型。');
      return false;
    }
    let apiKey = suppliedKey || group?.api_key || '';
    if (group?.provider_type === 'relay' && !apiKey) {
      apiKey = window.prompt('请输入中转站 API Key 以获取模型列表。该 Key 只用于本次获取，不会保存到连接组。', '') || '';
    }
    if (!apiKey) {
      Toast.warning('需要 API Key 才能获取上游模型；你也可以手动添加模型。');
      return false;
    }
    try {
      const result = await API.fetchUpstreamModels(groupId, apiKey);
      await Store.load();
      const count = Number(result?.count || result?.models?.length || 0);
      Toast.success(count ? `已获取 ${count} 个上游模型` : '获取完成；未返回模型，可手动添加模型');
      return true;
    } catch (err) {
      Toast.error('获取模型失败：' + err.message + '。你仍可以手动添加模型。');
      return false;
    }
  },

  async onBatchImport(controller) {
    const raw = document.getElementById('batch-models').value.trim();
    if (!raw) {
      Toast.warning('请输入模型列表');
      return;
    }
    const sel = Store.selected;
    const groupId = sel.type === 'group' ? sel.id : '';
    if (!groupId) {
      Toast.warning('请先选择一个连接组');
      return;
    }
    const group = Store.getGroup(groupId);
    const format = document.getElementById('batch-format')?.value || 'lines';
    const defaults = {
      usable: document.getElementById('batch-usable')?.checked ?? true,
      price_input: Number(document.getElementById('batch-price-input')?.value || 0) || undefined,
      price_output: Number(document.getElementById('batch-price-output')?.value || 0) || undefined,
    };
    if (group?.provider_type === 'relay') {
      defaults.api_key = document.getElementById('batch-api-key')?.value?.trim() || undefined;
      defaults.price_group = document.getElementById('batch-price-group')?.value?.trim() || undefined;
    }
    // 先请求预览
    let preview;
    try {
      preview = await API.req('/api/models/batch', {
        method: 'POST',
        body: JSON.stringify({ group_id: groupId, text: raw, format, defaults, preview: true })
      });
    } catch (err) {
      Toast.error('预览失败：' + err.message);
      return;
    }
    if (!preview.ok) {
      Toast.error('预览失败：' + (preview.message || '未知错误'));
      return;
    }
    // 展示预览并等待确认
    const confirmed = await controller._showBatchPreview(preview, group);
    if (!confirmed) return;
    // 确认导入
    try {
      const result = await API.req('/api/models/batch', {
        method: 'POST',
        body: JSON.stringify({ group_id: groupId, text: raw, format, defaults, preview: false })
      });
      document.getElementById('batch-models').value = '';
      await Store.load();
      Toast.success(`批量导入完成：新增 ${result.added || 0} 个，跳过 ${result.skipped || 0} 个`);
    } catch (err) {
      Toast.error('导入失败：' + err.message);
    }
  },

  _showBatchPreview(controller, preview, group) {
    const { summary, items } = preview;
    const statusMap = {
      new: '<span class="batch-status batch-status-new">新增</span>',
      duplicate: '<span class="batch-status batch-status-dup">重复</span>',
      invalid: '<span class="batch-status batch-status-invalid">无效</span>',
    };
    const isRelay = group?.provider_type === 'relay';
    const rows = items.map(item => `
      <tr class="batch-row-${item.status}">
        <td>${Number(item.line || 0) || '-'}</td>
        <td class="wrap-cell" title="${Utils.escapeHtml(item.name)}">${Utils.escapeHtml(item.name)}</td>
        <td class="wrap-cell" title="${Utils.escapeHtml(item.ep_id)}">${Utils.escapeHtml(item.ep_id)}</td>
        ${isRelay ? `
          <td class="wrap-cell" title="${Utils.escapeHtml(item.upstream_model)}">${Utils.escapeHtml(item.upstream_model)}</td>
          <td>${item.has_api_key ? '已填' : '-'}</td>
          <td class="wrap-cell" title="${Utils.escapeHtml(item.price_group || '')}">${Utils.escapeHtml(item.price_group || '-')}</td>
        ` : ''}
        <td>${statusMap[item.status] || item.status}</td>
        <td class="wrap-cell" title="${Utils.escapeHtml(item.reason || '')}">${Utils.escapeHtml(item.reason || '-')}</td>
      </tr>
    `).join('');
    const header = isRelay
      ? '<tr><th>行号</th><th>名称</th><th>上游模型/EP</th><th>中转模型</th><th>API Key</th><th>价格组</th><th>状态</th><th>原因</th></tr>'
      : '<tr><th>行号</th><th>名称</th><th>上游模型/EP</th><th>状态</th><th>原因</th></tr>';
    const summaryText = `将导入 ${summary.total} 个模型：新增 ${summary.new}，跳过重复 ${summary.duplicate}，无效 ${summary.invalid}。`;
    const tableClass = isRelay ? 'batch-preview-table relay-preview-table' : 'batch-preview-table';
    const body = `
      <div class="batch-preview-summary">${summaryText}</div>
      <div class="batch-preview-table-wrap">
        <table class="${tableClass}">${header}${rows}</table>
      </div>
    `;
    return Modal.confirm({
      title: '导入预览',
      message: body,
      confirmText: summary.invalid > 0 ? '存在无效记录' : '确认导入',
      confirmClass: summary.invalid > 0 ? 'btn-secondary' : 'btn-primary',
      cancelText: '取消',
      allowHtml: true,
      disableConfirm: summary.invalid > 0 || summary.total === 0,
      wide: true,
    });
  },

  async onConfigImport(controller, e) {
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
