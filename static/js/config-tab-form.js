const ConfigTabForm = {
  draftKey(controller, selection = Store.selected) {
    const type = selection?.type || 'group';
    return `${type}:${selection?.id || 'new'}`;
  },

  captureDraft(controller, form = document.querySelector('#panel-config .config-form'), selection = Store.selected) {
    if (!form) return;
    const values = this._captureFormValues(controller, form);
    if (!Object.keys(values).length) return;
    controller._drafts ||= new Map();
    const key = this.draftKey(controller, selection);
    const baseline = controller._draftBaselines?.get(key);
    if (baseline && this.sameFormValues(values, baseline)) {
      controller._drafts.delete(key);
      controller._draftDirty?.delete(key);
      if (selection.type === Store.selected.type && selection.id === Store.selected.id) controller.setSaveStatus('');
      return;
    }
    controller._drafts.set(key, values);
    controller._draftDirty ||= new Set();
    controller._draftDirty.add(key);
    controller.setSaveStatus('draft');
  },

  draftValues(controller, selection = Store.selected) {
    return controller._drafts?.get(this.draftKey(controller, selection)) || null;
  },

  clearDraft(controller, selection = Store.selected) {
    const key = this.draftKey(controller, selection);
    controller._drafts?.delete(key);
    controller._draftDirty?.delete(key);
    controller._draftBaselines?.delete(key);
  },

  sameFormValues(left, right) {
    const leftKeys = Object.keys(left || {});
    const rightKeys = Object.keys(right || {});
    if (leftKeys.length !== rightKeys.length) return false;
    return leftKeys.every(key => left[key] === right[key]);
  },

  bindGlobalEvents(controller) {
    if (!controller._upstreamOutsideClickHandler) {
      controller._upstreamOutsideClickHandler = event => controller._onUpstreamOutsideClick(event);
      document.addEventListener('mousedown', controller._upstreamOutsideClickHandler);
    }
    if (!controller._draftBeforeUnloadHandler && typeof window !== 'undefined') {
      controller._draftBeforeUnloadHandler = event => {
        if (!controller._draftDirty?.size) return;
        event.preventDefault();
        event.returnValue = '';
      };
      window.addEventListener('beforeunload', controller._draftBeforeUnloadHandler);
    }
  },

  dispose(controller) {
    if (controller._upstreamOutsideClickHandler) {
      document.removeEventListener('mousedown', controller._upstreamOutsideClickHandler);
      controller._upstreamOutsideClickHandler = null;
    }
    // beforeunload 属于应用级草稿保护；离开配置 Tab 仍要保留，避免用户在首页刷新时静默丢稿。
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
    const concurrencyRow = document.getElementById('group-concurrency-row');
    const wafClientModeRow = document.getElementById('group-waf-client-mode-row');
    const wafPolicyRow = document.getElementById('group-waf-policy-row');
    const hint = document.getElementById('group-mode-hint');
    const label = document.getElementById('group-key-label');

    if (keyRow) keyRow.classList.toggle('hidden', !needsKey);
    if (advancedCard) advancedCard.classList.remove('hidden');
    if (cooldownRow) cooldownRow.classList.remove('hidden');
    if (streamTimeoutRow) streamTimeoutRow.classList.remove('hidden');
    if (wafRow) wafRow.classList.toggle('hidden', mode !== 'relay');
    if (concurrencyRow) concurrencyRow.classList.toggle('hidden', mode !== 'relay');
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
    const remainsDefault = controller.isSystemDefaultBaseUrl(input.value, mode);
    note.classList.toggle('hidden', !remainsDefault);
  },

  onGroupProviderChange(controller) {
    const baseInput = document.getElementById('group-base');
    const nextProvider = document.getElementById('group-provider')?.value || 'relay';
    if (baseInput) {
      const current = baseInput.value.trim();
      const previousProvider = baseInput.dataset.provider || nextProvider;
      const canReplace = !current || Boolean(baseInput.dataset.systemDefaultProvider)
        || controller.isSystemDefaultBaseUrl(current, previousProvider);
      if (canReplace) baseInput.value = controller.providerBaseUrl(nextProvider);
      baseInput.dataset.provider = nextProvider;
      baseInput.dataset.systemDefaultProvider = controller.systemDefaultProvider(baseInput.value);
    }
    controller.syncGroupModeUI();
    controller.refreshGroupWorkflowFromDraft();
    controller.captureDraft();
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
      smart_breaker_enabled: document.getElementById('group-smart-breaker-enabled')?.checked !== false,
      serial_protection: mode === 'relay'
        && document.querySelector('input[name="group-request-concurrency"]:checked')?.value === 'serial',
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
    const input = document.getElementById('group-base');
    const mode = document.getElementById('group-provider')?.value || 'relay';
    if (input && input.value.trim() !== controller.providerBaseUrl(mode)) {
      input.dataset.systemDefaultProvider = '';
    }
    controller.updateDefaultRelayBaseUrlHint();
    controller.refreshGroupWorkflowFromDraft();
  },

  bindAutoSave(controller, form) {
    if (!form) return;
    form.querySelectorAll('input, select, textarea').forEach(el => {
      // 失焦/变更只保存进程内草稿，正式保存统一由显式 submit 触发。
      if (el.id === 'aggregate-stats-limit' || el.dataset.transientControl === 'true') return;
      ['input', 'change', 'blur'].forEach(event => {
        el.addEventListener(event, () => this.captureDraft(controller, form));
      });
    });
  },

  _captureFormValues(controller, form = document.getElementById('panel-config')) {
    const values = {};
    if (!form) return values;
    form.querySelectorAll('input, select, textarea').forEach(el => {
      if (el.type === 'radio') {
        if (el.name && el.checked) values[`__radio:${el.name}`] = el.value;
        return;
      }
      if (el.dataset.transientControl === 'true') return;
      if (!el.id) return;
      if (el.type === 'checkbox') values[el.id] = el.checked;
      else values[el.id] = el.value;
    });
    return values;
  },

  // 恢复用户之前编辑的表单值,

  _restoreFormValues(controller, values) {
    if (!values) return;
    Object.entries(values).forEach(([id, value]) => {
      if (id.startsWith('__radio:')) {
        const name = id.slice('__radio:'.length).replace(/"/g, '\\"');
        document.querySelectorAll(`input[type="radio"][name="${name}"]`).forEach(el => {
          el.checked = el.value === value;
        });
        return;
      }
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
    row.appendChild(error);
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
    controller.captureDraft(document.getElementById('group-form'));
  },

  autoSaveModel(controller) {
    controller.captureDraft(document.getElementById('model-form'));
  },

  autoSaveAggregate(controller) {
    controller.captureDraft(document.getElementById('aggregate-form'));
  }
};
