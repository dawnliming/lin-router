const ConfigTabRuntimeView = {
  dispose(controller) {
    controller._stopCooldownTimer();
    clearTimeout(controller._autoSaveTimer);
    controller._autoSaveTimer = null;
  },

  isEditingConfigForm(controller) {
    const active = document.activeElement;
    return !!(active && active.closest && active.closest('#panel-config .config-form') && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName));
  },

  onRuntimeStateUpdate(controller) {
    const panel = document.getElementById('panel-config');
    if (!panel || Tabs.current !== 'config') return;
    controller.updateCooldownDisplay();
    controller.patchVisibleRuntimeStatus();
  },

  patchVisibleRuntimeStatus(controller) {
    const selected = Store.selected;
    if (selected.type === 'aggregate') {
      Store.getAggregateMembers(selected.id).forEach(member => {
        const cell = document.querySelector(`[data-member-status-cell="${member.id}"]`);
        if (!cell) return;
        const status = controller.aggregateMemberStatus(member, Store.getModel(member.model_id));
        const next = `<span data-aggregate-member-status="${member.id}" class="pill ${status.class}" title="${Utils.escapeHtml(status.title)}">${status.text}</span>`;
        if (cell.innerHTML !== next) cell.innerHTML = next;
      });
    }
  },

  async refreshRuntimeNow(controller) {
    try {
      const data = await API.getRuntimeState();
      const runtimeByModel = new Map((data.models || []).map(item => [item.model_id, item]));
      const runtimeByMember = new Map((data.aggregate_members || []).map(item => [item.member_id, item]));
      Store.update({
        logs: data.logs || Store.state.logs || [],
        models: (Store.state.models || []).map(model => runtimeByModel.has(model.id) ? { ...model, ...runtimeByModel.get(model.id) } : model),
        aggregate_members: (Store.state.aggregate_members || []).map(member => runtimeByMember.has(member.id) ? { ...member, ...runtimeByMember.get(member.id) } : member),
        log_write_error: data.log_write_error || '',
      });
      controller.onRuntimeStateUpdate();
      Toast.success('运行状态已刷新');
    } catch (err) {
      Toast.error('刷新状态失败：' + err.message);
    }
  },

  _startCooldownTimer(controller) {
    controller._stopCooldownTimer();
    controller.updateCooldownDisplay();
    controller._cooldownTimer = setInterval(() => controller.updateCooldownDisplay(), 1000);
  },

  _stopCooldownTimer(controller) {
    if (controller._cooldownTimer) {
      clearInterval(controller._cooldownTimer);
      controller._cooldownTimer = null;
    }
  },

  updateCooldownDisplay(controller) {
    const display = document.getElementById('model-cooldown-display');
    if (!display) return;
    const modelId = document.getElementById('model-id')?.value;
    const m = modelId ? Store.getModel(modelId) : null;
    if (m && m.cooldown_until && m.cooldown_until * 1000 > Date.now()) {
      const remain = Math.max(0, Math.ceil((m.cooldown_until * 1000 - Date.now()) / 1000));
      const mm = Math.floor(remain / 60).toString().padStart(2, '0');
      const ss = (remain % 60).toString().padStart(2, '0');
      display.textContent = `${Utils.formatDate(m.cooldown_until)}（还剩 ${mm}:${ss}）`;
    } else {
      display.textContent = '-';
    }
    document.querySelectorAll('[data-aggregate-member-status]').forEach(el => {
      const member = Store.state.aggregate_members?.find(item => item.id === el.dataset.aggregateMemberStatus);
      if (!member) return;
      const status = controller.aggregateMemberStatus(member, Store.getModel(member.model_id));
      el.className = `pill ${status.class}`;
      el.textContent = status.text;
      el.title = status.title;
    });
  }
};
