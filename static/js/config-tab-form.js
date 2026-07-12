const ConfigTabForm = {
  bindGlobalEvents(controller) {
    if (controller._upstreamOutsideClickHandler) return;
    controller._upstreamOutsideClickHandler = event => controller._onUpstreamOutsideClick(event);
    document.addEventListener('mousedown', controller._upstreamOutsideClickHandler);
  },

  dispose(controller) {
    if (!controller._upstreamOutsideClickHandler) return;
    document.removeEventListener('mousedown', controller._upstreamOutsideClickHandler);
    controller._upstreamOutsideClickHandler = null;
  },

  isNewGroupDraft(controller, selection = Store.selected) {
    return selection.type === 'group' && !selection.id && Boolean(controller._newGroupDraft);
  },

  isDefaultRelayBaseUrl(controller, value) {
    return String(value || '').trim() === controller.defaultRelayBaseUrl;
  },

  groupKeyValue(controller, g) {
    if (!g) return '';
    if (g.provider_type === 'proxy') return g.api_key || '';
    return g.ark_api_key || '';
  },

  syncUIFromState(controller) {
    const sel = Store.selected;
    if (sel.type === 'aggregate') controller.syncAggregateUI();
    else if (sel.type === 'group' || sel.type === null) controller.syncGroupModeUI();
    else controller.syncModelModeUI();
  },

  syncAggregateUI(controller) {
    // 聚合模型表单无需动态切换，定时刷新成员状态由 Store 订阅触发 re-render 完成
    controller.updateAggregateCooldownDisplay();
  },

  updateAggregateCooldownDisplay(controller) {
    // 可在成员表格中显示冷却倒计时，当前通过重新渲染实现
  },

  syncGroupModeUI(controller) {
    const mode = document.getElementById('group-provider')?.value || 'relay';
    const needsKey = mode === 'ark' || mode === 'proxy';
    const keyRow = document.getElementById('group-key-row');
    const advancedCard = document.getElementById('group-advanced-card');
    const cooldownRow = document.getElementById('group-cooldown-row');
    const streamTimeoutRow = document.getElementById('group-stream-timeout-row');
    const wafRow = document.getElementById('group-waf-row');
    const wafClientModeRow = document.getElementById('group-waf-client-mode-row');
    const wafPolicyRow = document.getElementById('group-waf-policy-row');
    const hint = document.getElementById('group-mode-hint');
    const label = document.getElementById('group-key-label');

    if (keyRow) keyRow.classList.toggle('hidden', !needsKey);
    if (advancedCard) advancedCard.classList.remove('hidden');
    if (cooldownRow) cooldownRow.classList.remove('hidden');
    if (streamTimeoutRow) streamTimeoutRow.classList.remove('hidden');
    if (wafRow) wafRow.classList.toggle('hidden', mode !== 'relay');
    const wafChecked = document.getElementById('group-waf')?.checked || false;
    if (wafClientModeRow) wafClientModeRow.classList.toggle('hidden', mode !== 'relay' || !wafChecked);
    if (wafPolicyRow) wafPolicyRow.classList.toggle('hidden', mode !== 'relay' || !wafChecked);
    if (label) label.textContent = mode === 'ark' ? 'Ark API Key' : '上游 API Key';
    controller.updateDefaultRelayBaseUrlHint();

    if (hint) {
      if (mode === 'ark') hint.textContent = '火山方舟：组内保存 Ark Key，模型里填写 EP ID。';
      else if (mode === 'relay') hint.textContent = '中转站：组内只保存 Base URL；每个模型通道单独保存 API Key 和上游模型。';
      else hint.textContent = '通用代理：组内保存 Base URL 和上游 API Key；未配置的具体模型保持原样透传。';
    }
  },

  updateDefaultRelayBaseUrlHint(controller) {
    const input = document.getElementById('group-base');
    const note = document.getElementById('group-base-default-note');
    if (!input || !note) return;
    const mode = document.getElementById('group-provider')?.value || 'relay';
    const remainsDefault = mode === 'relay'
      && input.dataset.codeokDefault === 'true'
      && controller.isDefaultRelayBaseUrl(input.value);
    if (!remainsDefault) input.dataset.codeokDefault = 'false';
    note.classList.toggle('hidden', !remainsDefault);
  },

  onGroupProviderChange(controller) {
    const baseInput = document.getElementById('group-base');
    if (baseInput?.dataset.codeokDefault === 'true') {
      baseInput.value = '';
      baseInput.dataset.codeokDefault = 'false';
    }
    controller.syncGroupModeUI();
    controller.refreshGroupWorkflowFromDraft();
    controller.autoSaveGroup();
  },

  groupStateFromForm(controller) {
    const id = document.getElementById('group-id')?.value || '';
    const stored = id ? Store.getGroup(id) : controller._newGroupDraft;
    if (!stored) return null;
    const mode = document.getElementById('group-provider')?.value || 'relay';
    const key = document.getElementById('group-key')?.value.trim() || '';
    return {
      ...stored,
      id: stored.id || '__new_group_draft__',
      name: document.getElementById('group-name')?.value.trim() || '',
      provider_type: mode,
      base_url: document.getElementById('group-base')?.value.trim() || '',
      ark_api_key: mode === 'ark' ? key : '',
      api_key: mode === 'proxy' ? key : '',
    };
  },

  syncNewGroupDraftFromForm(controller, group) {
    if (document.getElementById('group-id')?.value || !controller._newGroupDraft || !group) return;
    const { id, ...draft } = group;
    controller._newGroupDraft = { ...controller._newGroupDraft, ...draft };
  },

  refreshGroupWorkflowFromDraft(controller) {
    const group = controller.groupStateFromForm();
    const workflow = document.getElementById('group-workflow-card');
    if (!group || !workflow) return;
    const isDraft = !document.getElementById('group-id')?.value;
    if (isDraft) controller.syncNewGroupDraftFromForm(group);
    workflow.outerHTML = controller.renderGroupWorkflow(group, { isDraft });
    controller.bindGroupWorkflowActions(document.getElementById('panel-config'));
  },

  bindGroupWorkflowActions(controller, panel) {
    panel?.querySelectorAll('#group-workflow-card [data-group-action]').forEach(btn => {
      btn.addEventListener('click', () => controller.onGroupWorkflowAction(btn.dataset.groupAction, btn.dataset.modelId));
    });
  },

  onGroupDraftInput(controller) {
    controller.updateDefaultRelayBaseUrlHint();
    controller.refreshGroupWorkflowFromDraft();
  },

  bindAutoSave(controller, form, callback) {
    if (!form) return;
    form.querySelectorAll('input, select, textarea').forEach(el => {
      // 聚合成员字段有独立保存逻辑，避免 autoSaveAggregate 的 blur 事件与成员保存竞争
      const cls = el.className || '';
      if (cls.includes('aggregate-member-price')) return;
      const event = el.tagName === 'SELECT' || el.type === 'checkbox' ? 'change' : 'blur';
      el.addEventListener(event, () => callback());
    });
  },

  _captureFormValues(controller) {
    const values = {};
    const panel = document.getElementById('panel-config');
    if (!panel) return values;
    panel.querySelectorAll('input, select, textarea').forEach(el => {
      if (!el.id) return;
      if (el.type === 'checkbox' || el.type === 'radio') values[el.id] = el.checked;
      else values[el.id] = el.value;
    });
    return values;
  },

  // 恢复用户之前编辑的表单值,

  _restoreFormValues(controller, values) {
    if (!values) return;
    Object.entries(values).forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = Boolean(value);
      else el.value = value ?? '';
    });
  },

  // 启动冷却倒计时定时器，实时更新状态信息中的冷却截止时间,

  clearFieldErrors(controller, form) {
    form?.querySelectorAll('.field-error').forEach(el => el.remove());
    form?.querySelectorAll('[aria-invalid="true"]').forEach(el => el.removeAttribute('aria-invalid'));
  },

  setFieldError(controller, inputId, message) {
    const input = document.getElementById(inputId);
    if (!input) return;
    input.setAttribute('aria-invalid', 'true');
    const row = input.closest('.form-row');
    if (!row || row.querySelector('.field-error')) return;
    const error = document.createElement('div');
    error.className = 'field-error';
    error.textContent = message;
    const control = input.closest('.input-with-btn') || input.parentElement;
    control?.appendChild(error);
  },

  validateGroupForm(controller, { focus = false } = {}) {
    const form = document.getElementById('group-form');
    controller.clearFieldErrors(form);
    const mode = document.getElementById('group-provider')?.value || 'relay';
    const name = document.getElementById('group-name')?.value.trim() || '';
    const baseUrl = document.getElementById('group-base')?.value.trim() || '';
    const key = document.getElementById('group-key')?.value.trim() || '';
    const errors = [];
    if (!name) errors.push(['group-name', '请填写连接组名称。']);
    if (!baseUrl) errors.push(['group-base', '请填写 Base URL，例如 https://example.com/v1。']);
    else if (!ConnectionStatus.isValidBaseUrl(baseUrl)) errors.push(['group-base', 'Base URL 格式不正确，请使用 http:// 或 https:// 地址。']);
    if (['ark', 'proxy'].includes(mode) && !key) errors.push(['group-key', '请填写上游 API Key 后再保存。']);
    errors.forEach(([id, message]) => controller.setFieldError(id, message));
    if (focus && errors.length) document.getElementById(errors[0][0])?.focus();
    return { ok: !errors.length, message: errors[0]?.[1] || '' };
  },

  validateModelForm(controller, { focus = false } = {}) {
    const form = document.getElementById('model-form');
    controller.clearFieldErrors(form);
    const group = Store.getGroup(document.getElementById('model-group')?.value);
    const name = document.getElementById('model-name')?.value.trim() || '';
    const upstreamId = ['relay', 'proxy'].includes(group?.provider_type)
      ? 'model-upstream' : 'model-ep';
    const upstream = document.getElementById(upstreamId)?.value.trim() || '';
    const relayKey = document.getElementById('model-key')?.value.trim() || '';
    const errors = [];
    if (!name) errors.push(['model-name', '请填写模型名称。']);
    if (!upstream) errors.push([upstreamId, group?.provider_type === 'ark' ? '请填写上游模型或 EP ID。' : '请填写上游模型名称。']);
    if (group?.provider_type === 'relay' && !relayKey) errors.push(['model-key', '请填写中转站 API Key。']);
    errors.forEach(([id, message]) => controller.setFieldError(id, message));
    if (focus && errors.length) document.getElementById(errors[0][0])?.focus();
    return { ok: !errors.length, message: errors[0]?.[1] || '' };
  },

  autoSaveGroup(controller) {
    const id = document.getElementById('group-id')?.value;
    if (!id) {
      controller.syncNewGroupDraftFromForm(controller.groupStateFromForm());
      return;
    }
    clearTimeout(controller._autoSaveTimer);
    controller.setSaveStatus('saving');
    controller._autoSaveTimer = setTimeout(() => {
      const form = document.getElementById('group-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }, 500);
  },

  autoSaveModel(controller) {
    const id = document.getElementById('model-id')?.value;
    if (!id) return; // 新建不自动保存
    clearTimeout(controller._autoSaveTimer);
    controller.setSaveStatus('saving');
    controller._autoSaveTimer = setTimeout(() => {
      const form = document.getElementById('model-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }, 500);
  },

  autoSaveAggregate(controller) {
    const id = document.getElementById('aggregate-id')?.value;
    if (!id) return; // 新建不自动保存
    clearTimeout(controller._autoSaveTimer);
    controller.setSaveStatus('saving');
    controller._autoSaveTimer = setTimeout(() => {
      const form = document.getElementById('aggregate-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }, 500);
  }
};
