const ConfigTabRuntimeView = {
  dispose(controller) {
    controller._stopCooldownTimer();
    clearTimeout(controller._autoSaveTimer);
    controller._autoSaveTimer = null;
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
        const actions = document.querySelector(`[data-member-actions="${member.id}"]`);
        const actionButtons = actions?.querySelector('.aggregate-member-action-buttons');
        const recover = actionButtons?.querySelector('[data-action="recover"]');
        const healthState = member.derived_status || member.health_state || 'normal';
        const canRecover = member.enabled !== false
          && member.smart_breaker_effective_enabled !== false
          && member.derived_status !== 'breaker_policy_disabled'
          && ['cooling', 'breaker_open'].includes(healthState);
        if (!canRecover && recover) {
          recover.remove();
        } else if (canRecover && !recover && actionButtons) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'btn-recover btn-sm';
          button.dataset.action = 'recover';
          button.dataset.memberId = member.id;
          button.textContent = '重试恢复';
          button.addEventListener('click', () => controller.onRecoverAggregateMember(member.id));
          actionButtons.insertBefore(button, actionButtons.querySelector('[data-action="up"]'));
        }
      });
    }
  },

  async refreshRuntimeNow(controller) {
    if (
      Store.selected?.type === 'aggregate'
      && Store.selected.id
      && typeof controller.clearAggregateMemberSelection === 'function'
    ) {
      // 手动刷新后丢弃旧勾选，避免基于过期成员状态继续批量操作。
      controller.clearAggregateMemberSelection(Store.selected.id);
    }
    try {
      if (typeof App !== 'undefined' && typeof App.refreshRuntimeState === 'function') {
        // 与后台轮询共用 config scope 的单飞与 revision，手动刷新只提升反馈，不改数据范围。
        await App.refreshRuntimeState('config', { background: false, silent: false });
        // changed=false 时 Store 不会 emit；仍更新当前倒计时与已挂载状态标签。
        controller.onRuntimeStateUpdate();
      } else {
        // 独立模块测试及旧页面加载顺序的兼容路径：config scope 永远不写入日志。
        const data = await API.getRuntimeState({ scope: 'config' });
        this.applyRuntimePatch(data);
        controller.onRuntimeStateUpdate();
      }
      Toast.success('运行状态已刷新');
    } catch (err) {
      Toast.error('刷新状态失败：' + err.message);
    }
  },

  applyRuntimePatch(data) {
    const nested = data?.state && typeof data.state === 'object' && !Array.isArray(data.state) ? data.state : {};
    const payload = { ...nested, ...(data || {}) };
    const patch = {};
    if (Array.isArray(payload.models)) {
      const runtimeByModel = new Map(payload.models.map(item => [item.model_id, item]));
      patch.models = (Store.state.models || []).map(model => runtimeByModel.has(model.id) ? { ...model, ...runtimeByModel.get(model.id) } : model);
    }
    if (Array.isArray(payload.aggregate_members)) {
      const runtimeByMember = new Map(payload.aggregate_members.map(item => [item.member_id, item]));
      patch.aggregate_members = (Store.state.aggregate_members || []).map(member => runtimeByMember.has(member.id) ? { ...member, ...runtimeByMember.get(member.id) } : member);
    }
    if (Array.isArray(payload.live_requests)) patch.live_requests = payload.live_requests;
    if (Object.prototype.hasOwnProperty.call(payload, 'log_write_error')) patch.log_write_error = payload.log_write_error || '';
    if (Object.keys(patch).length) Store.update(patch);
  },

  _startCooldownTimer(controller) {
    controller._stopCooldownTimer();
    controller._cooldownExpiryRefreshes = controller._cooldownExpiryRefreshes || new Set();
    controller.updateCooldownDisplay();
    controller._cooldownTimer = setInterval(() => controller.updateCooldownDisplay(), 1000);
  },

  _stopCooldownTimer(controller) {
    if (controller._cooldownTimer) {
      clearInterval(controller._cooldownTimer);
      controller._cooldownTimer = null;
    }
  },

  healthDeadline(controller, item) {
    if (typeof controller?.healthDeadline === 'function') {
      return Number(controller.healthDeadline(item) || 0);
    }
    // 运行态模块可独立加载；兼容旧 controller stub 与旧页面加载顺序。
    return Number(item?.health_state === 'breaker_open' ? item?.breaker_until : item?.cooldown_until || 0);
  },

  updateCooldownDisplay(controller) {
    const display = document.getElementById('model-cooldown-display');
    if (display) {
      const modelId = document.getElementById('model-id')?.value;
      const m = modelId ? Store.getModel(modelId) : null;
      const healthState = m?.disabled_by_user
        ? 'manual_disabled'
        : (m?.derived_status || m?.health_state || 'normal');
      const deadline = this.healthDeadline(controller, m);
      const untilMs = deadline * 1000;
      display.dataset.healthState = healthState;
      display.dataset.healthDeadline = String(deadline);
      if (deadline) {
        const remain = Math.max(0, Math.ceil((untilMs - Date.now()) / 1000));
        const mm = Math.floor(remain / 60).toString().padStart(2, '0');
        const ss = (remain % 60).toString().padStart(2, '0');
        display.textContent = `${Utils.formatDate(deadline)}（${remain ? `还剩 ${mm}:${ss}` : '已到期'}）`;
      } else {
        display.textContent = '-';
      }
      const stateEl = document.getElementById('model-health-state');
      if (stateEl) stateEl.textContent = controller.modelHealthLabel(healthState);
      const failuresEl = document.getElementById('model-consecutive-failures');
      if (failuresEl) failuresEl.textContent = `${Number(m?.consecutive_failures || 0)} 次`;
      const reasonEl = document.getElementById('model-health-reason');
      if (reasonEl) reasonEl.textContent = m?.derived_reason || m?.breaker_reason || m?.cooldown_reason || m?.last_error || '-';
      const row = display.closest('.form-row');
      const recover = row?.querySelector('#model-recover');
      const canRecover = Boolean(
        m
        && !m.disabled_by_user
        && m?.smart_breaker_effective_enabled !== false
        && m?.derived_status !== 'breaker_policy_disabled'
        && ['cooling', 'breaker_open'].includes(healthState)
      );
      if (canRecover && !recover) {
        const button = document.createElement('button');
        button.type = 'button';
        button.id = 'model-recover';
        button.className = 'btn-recover btn-sm';
        button.textContent = '重试恢复';
        button.addEventListener('click', () => controller.onRecoverModel());
        row?.appendChild(button);
      } else if (!canRecover && recover) {
        recover.remove();
      }
    }
    document.querySelectorAll('[data-aggregate-member-status]').forEach(el => {
      const member = Store.state.aggregate_members?.find(item => item.id === el.dataset.aggregateMemberStatus);
      if (!member) return;
      const status = controller.aggregateMemberStatus(member, Store.getModel(member.model_id));
      el.className = `pill ${status.class}`;
      el.textContent = status.text;
      el.title = status.title;
    });
    this.refreshExpiredCooldowns(controller);
  },

  refreshExpiredCooldowns(controller) {
    if (Tabs.current !== 'config' || document.hidden) return;
    const selected = Store.selected || {};
    const candidates = [];
    if (selected.type === 'model' && selected.id) {
      const model = Store.getModel(selected.id);
      if (model) candidates.push(['model', model.id, this.healthDeadline(controller, model)]);
    }
    if (selected.type === 'aggregate' && selected.id) {
      Store.getAggregateMembers(selected.id).forEach(member => {
        candidates.push(['member', member.id, this.healthDeadline(controller, member)]);
        const model = Store.getModel(member.model_id);
        if (model) candidates.push(['model', model.id, this.healthDeadline(controller, model)]);
      });
    }

    const now = Date.now();
    const seen = controller._cooldownExpiryRefreshes || (controller._cooldownExpiryRefreshes = new Set());
    const justExpired = candidates.some(([kind, id, cooldownUntil]) => {
      const until = Number(cooldownUntil || 0);
      if (!until || until * 1000 > now) return false;
      const key = `${kind}:${id}:${until}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    if (!justExpired) return;

    // 倒计时由本地时钟展示；跨过到期点才补一次服务端状态，绝不把每秒 tick 变成网络轮询。
    if (typeof App !== 'undefined' && typeof App.refreshRuntimeState === 'function') {
      App.refreshRuntimeState('config', { background: true, silent: true });
      return;
    }
    API.getRuntimeState({ scope: 'config' }, { silent: true })
      .then(data => {
        this.applyRuntimePatch(data);
        controller.onRuntimeStateUpdate();
      })
      .catch(err => console.warn('冷却到期状态刷新失败', err));
  }
};
