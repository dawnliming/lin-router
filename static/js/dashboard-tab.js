const DashboardTab = {
  _openAccessGroups: new Set(),
  _openFlowSummaries: new Set(),
  // 接入选择仅保留在当前页面内存中，绝不写入配置、日志或浏览器存储。
  _onboardingSelection: { groupId: '', modelId: '', client: 'codex' },
  _onboardingPreparedKey: '',
  _onboardingOpen: true,
  _accessGroupsInitialized: false,
  _accessGroupCount: 0,
  _accessGroupIdsSignature: '',
  _accessFilter: '',
  _structureSignature: '',
  _runtimeSignature: '',

  refresh() {
    const panel = document.getElementById('panel-dashboard');
    if (!panel) return;

    const structureSignature = this.structureSignature();
    if (!panel.dataset.dashboardRendered || structureSignature !== this._structureSignature) {
      this.renderFull(panel, structureSignature);
      return;
    }

    this.patchRuntime(panel);
  },

  renderFull(panel, structureSignature = this.structureSignature()) {
    const viewState = this.captureViewState(panel);
    const state = Store.state || {};
    const flow = ConnectionStatus.derive(state);
    panel.innerHTML = this.render();
    panel.dataset.dashboardRendered = 'true';
    panel.dataset.dashboardFlowCode = flow.code;
    panel.dataset.dashboardFlowSignature = this.flowSignature(flow);
    panel.dataset.dashboardAccessSignature = this.accessSignature(flow);
    panel.dataset.dashboardOnboardingSignature = this.onboardingSignature(flow, state);
    this._structureSignature = structureSignature;
    this._runtimeSignature = this.runtimeSignature();
    this.attachEvents(panel);
    this.restoreViewState(panel, viewState);
  },

  // 运行态轮询会频繁更新 logs/live_requests；这些字段不参与结构签名，避免整页闪烁。
  structureSignature(state = Store.state || {}) {
    const groups = (state.groups || []).map(group => [
      group.id, group.name, group.provider_type, group.base_url, group.route_key, group.enabled,
    ]);
    const models = (state.models || []).map(model => [
      model.id, model.group_id, model.name, model.upstream_model, model.ep_id,
    ]);
    const aggregates = (state.aggregate_models || []).map(aggregate => [
      aggregate.id, aggregate.name, aggregate.display_name, aggregate.route_key, aggregate.enabled,
    ]);
    const members = (state.aggregate_members || []).map(member => [member.id, member.aggregate_id]);
    return JSON.stringify({ groups, models, aggregates, members, logWriteError: state.log_write_error || '' });
  },

  runtimeSignature(state = Store.state || {}) {
    const logs = (state.logs || []).map(log => [
      log.request_id, log.time, log.status, log.event, log.detail, log.duration_ms,
      log.prompt_tokens, log.completion_tokens, log.cached_tokens, log.total_tokens,
    ]);
    const modelHealth = (state.models || []).map(model => [
      model.id, model.usable, model.cooldown_until, model.last_success_at,
      model.derived_status, model.risk_isolated, model.risk_until,
      model.risk_level, model.risk_affected_models,
    ]);
    const memberHealth = (state.aggregate_members || []).map(member => [
      member.id, member.enabled, member.cooldown_until,
    ]);
    return JSON.stringify({ logs, liveRequests: state.live_requests || [], modelHealth, memberHealth });
  },

  flowSignature(flow) {
    const primary = flow.primary || {};
    return JSON.stringify({
      code: flow.code,
      primary: [primary.group?.id || '', primary.code || '', primary.representative?.id || '', primary.label || '', primary.reason || ''],
      readyGroups: (flow.readyGroups || []).map(item => [item.group?.id || '', item.representative?.id || '', item.verifiedModel?.id || '', item.code || '', item.modelCount || 0]),
    });
  },

  accessSignature(flow) {
    return JSON.stringify((flow.readyGroups || []).map(item => [
      item.group?.id || '', item.representative?.id || '', item.verifiedModel?.id || '', item.code || '', item.modelCount || 0,
    ]));
  },

  onboardingSignature(flow, state = Store.state || {}) {
    return JSON.stringify({
      primaryGroupId: flow.primary?.group?.id || '',
      relayGroups: this.onboardingRelayGroups(flow, state).map(item => [
        item.group?.id || '', item.group?.name || '', item.group?.route_key || '',
        this.onboardingModels(item, state).map(model => [
          model.id || '', model.name || '', model.last_success_at || '', model.usable !== false, model.cooldown_until || 0,
        ]),
      ]),
    });
  },

  patchRuntime(panel) {
    const runtimeSignature = this.runtimeSignature();
    if (runtimeSignature === this._runtimeSignature) return;

    const viewState = this.captureViewState(panel);
    const state = Store.state || {};
    const flow = ConnectionStatus.derive(state);
    const recent = this.recentLogs(state.logs || []);

    if (panel.dataset.dashboardFlowCode !== flow.code) {
      this.renderFull(panel);
      return;
    }

    const flowSignature = this.flowSignature(flow);
    if (panel.dataset.dashboardFlowSignature !== flowSignature) {
      this.replaceSlot(panel, '[data-dashboard-hero]', this.renderHero(flow));
      if (!['S3', 'S4'].includes(flow.code)) {
        this.replaceSlot(panel, '[data-dashboard-flow-card]', this.renderFlowCard(flow));
      }
      panel.dataset.dashboardFlowSignature = flowSignature;
    }

    const accessSignature = this.accessSignature(flow);
    if (panel.dataset.dashboardAccessSignature !== accessSignature) {
      this.replaceSlot(panel, '[data-dashboard-direct-access-section]', this.renderDirectAccessSurface(flow.readyGroups, `${window.location.origin}/v1`));
      panel.dataset.dashboardAccessSignature = accessSignature;
    }

    const onboardingSignature = this.onboardingSignature(flow, state);
    if (panel.dataset.dashboardOnboardingSignature !== onboardingSignature) {
      this.replaceSlot(panel, '[data-dashboard-onboarding-section]', this.renderSelfServiceOnboarding(flow, `${window.location.origin}/v1`));
      panel.dataset.dashboardOnboardingSignature = onboardingSignature;
    }

    this.replaceSlot(panel, '[data-dashboard-live-requests]', this.renderLiveRequests(state.live_requests || []));
    this.replaceSlot(panel, '[data-dashboard-risk-alert]', this.renderRiskIsolationAlert(state));
    if (['S3', 'S4'].includes(flow.code)) {
      this.replaceSlot(panel, '[data-dashboard-metrics]', this.renderMetrics(state, flow, recent));
      this.replaceSlot(panel, '[data-dashboard-aggregate-summary]', this.aggregateSummaryCards(state.aggregate_models || [], recent));
    }

    this._runtimeSignature = runtimeSignature;
    this.restoreViewState(panel, viewState);
  },

  replaceSlot(panel, selector, html) {
    const current = panel.querySelector(selector);
    if (!current || current.outerHTML === html) return;
    current.outerHTML = html;
  },

  captureViewState(panel) {
    const scrollHost = panel.closest('.tab-panel');
    const active = document.activeElement;
    return {
      scrollTop: scrollHost ? scrollHost.scrollTop : 0,
      focus: active && panel.contains(active) ? this.focusDescriptor(active) : null,
    };
  },

  focusDescriptor(element) {
    const descriptor = {
      action: element.dataset?.dashboardAction || '',
      requestId: element.dataset?.requestId || '',
      groupId: element.dataset?.groupId || '',
      modelId: element.dataset?.modelId || '',
      aggregateId: element.dataset?.aggregateId || '',
      copyValue: element.dataset?.copyValue || '',
      copyClientGroup: element.dataset?.copyClientGroup || '',
      onboardingGroup: element.dataset?.onboardingGroup || '',
      onboardingModel: element.dataset?.onboardingModel || '',
      onboardingClient: element.dataset?.onboardingClient || '',
      onboardingCopy: element.dataset?.onboardingCopy || '',
      accessGroup: '',
      isAccessFilter: element.matches?.('[data-dashboard-access-filter]') || false,
      selectionStart: typeof element.selectionStart === 'number' ? element.selectionStart : null,
      selectionEnd: typeof element.selectionEnd === 'number' ? element.selectionEnd : null,
    };
    const accessGroup = element.closest?.('[data-dashboard-access-group]');
    descriptor.accessGroup = accessGroup?.dataset.accessGroup || '';
    return descriptor;
  },

  restoreViewState(panel, viewState) {
    if (!viewState) return;
    const scrollHost = panel.closest('.tab-panel');
    if (scrollHost) scrollHost.scrollTop = viewState.scrollTop;

    const focus = viewState.focus;
    if (!focus) return;
    let target = null;
    if (focus.isAccessFilter) {
      target = panel.querySelector('[data-dashboard-access-filter]');
    } else if (focus.action) {
      target = Array.from(panel.querySelectorAll('[data-dashboard-action]')).find(item =>
        item.dataset.dashboardAction === focus.action
        && item.dataset.requestId === focus.requestId
        && item.dataset.groupId === focus.groupId
        && item.dataset.modelId === focus.modelId
        && item.dataset.aggregateId === focus.aggregateId,
      );
    } else if (focus.onboardingGroup) {
      target = panel.querySelector('[data-onboarding-group]');
    } else if (focus.onboardingModel) {
      target = panel.querySelector('[data-onboarding-model]');
    } else if (focus.onboardingClient) {
      target = Array.from(panel.querySelectorAll('[data-onboarding-client]')).find(item => item.dataset.onboardingClient === focus.onboardingClient);
    } else if (focus.onboardingCopy) {
      target = Array.from(panel.querySelectorAll('[data-onboarding-copy]')).find(item => item.dataset.onboardingCopy === focus.onboardingCopy);
    } else if (focus.copyValue) {
      target = Array.from(panel.querySelectorAll('[data-copy-value]')).find(item => item.dataset.copyValue === focus.copyValue);
    } else if (focus.copyClientGroup) {
      target = Array.from(panel.querySelectorAll('[data-copy-client-group]')).find(item => item.dataset.copyClientGroup === focus.copyClientGroup);
    } else if (focus.accessGroup) {
      const group = Array.from(panel.querySelectorAll('[data-dashboard-access-group]')).find(item => item.dataset.accessGroup === focus.accessGroup);
      target = group?.querySelector('summary') || null;
    }
    if (!target) return;
    try {
      target.focus({ preventScroll: true });
      if (focus.selectionStart !== null && typeof target.setSelectionRange === 'function') {
        target.setSelectionRange(focus.selectionStart, focus.selectionEnd ?? focus.selectionStart);
      }
    } catch (_) {
      target.focus();
    }
  },

  recentLogs(logs) {
    return (logs || []).filter(log => !this.isConfigSkip(log)).slice(0, 30);
  },

  riskIsolationSummary(state = Store.state || {}) {
    const isolatedModels = (state.models || []).filter(model => model.risk_isolated === true);
    if (!isolatedModels.length) return null;
    const firstModel = isolatedModels[0];
    return {
      modelCount: isolatedModels.length,
      affectedCount: Math.max(
        isolatedModels.length,
        ...isolatedModels.map(model => Number(model.risk_affected_models || 0)),
      ),
      until: Math.max(...isolatedModels.map(model => Number(model.risk_until || 0))),
      modelId: firstModel.id,
    };
  },

  renderRiskIsolationAlert(state = Store.state || {}) {
    const summary = this.riskIsolationSummary(state);
    if (!summary) return '<section class="dashboard-risk-alert hidden" data-dashboard-risk-alert></section>';
    const until = summary.until > 0 ? Utils.formatDate(summary.until) : '未知到期时间';
    return `
      <section class="dashboard-risk-alert" data-dashboard-risk-alert aria-live="polite">
        <div>
          <div class="dashboard-risk-eyebrow">上游风控保护</div>
          <h3>检测到上游风控拦截</h3>
          <p>影响：当前有 ${summary.modelCount} 个本地模型处于隔离；按凭证范围共影响 ${summary.affectedCount} 个模型。系统已隔离至 ${Utils.escapeHtml(until)}，流量会转向其他候选。</p>
          <p>建议：不要连续重试；检查中转后台账号状态、渠道权限、频率限制和风控通知。</p>
        </div>
        <div class="dashboard-flow-actions">
          <button type="button" class="btn-secondary" data-dashboard-action="open-risk-model" data-model-id="${Utils.escapeHtml(summary.modelId)}">查看诊断与恢复</button>
        </div>
      </section>
    `;
  },

  render() {
    const state = Store.state || {};
    const groups = state.groups || [];
    const aggregates = state.aggregate_models || [];
    const flow = ConnectionStatus.derive(state);
    const operational = ['S3', 'S4'].includes(flow.code);
    const recent = this.recentLogs(state.logs || []);
    const baseUrl = `${window.location.origin}/v1`;

    return `
      <div class="dashboard-page">
        ${this.renderHero(flow)}
        ${this.renderRiskIsolationAlert(state)}
        ${this.renderFlowCard(flow)}
        ${this.renderLiveRequests(state.live_requests || [])}
        ${operational ? `
          ${this.renderMetrics(state, flow, recent)}
          ${this.aggregateSummaryCards(aggregates, recent)}
          ${this.renderSelfServiceOnboarding(flow, baseUrl)}
          <div class="dashboard-two-col">
            ${this.renderDirectAccessSurface(flow.readyGroups, baseUrl)}
            <section class="dashboard-access-section">
              ${this.renderAggregateAccessSection(aggregates, baseUrl)}
            </section>
          </div>
        ` : (flow.code === 'S0' ? '' : this.renderNextSteps(flow))}
      </div>
    `;
  },

  renderHero(flow) {
    const status = {
      S0: ['开始配置', 'info'],
      S1: ['待添加模型', 'warning'],
      S2: ['待验证', 'warning'],
      S3: ['可以接入', 'success'],
      S4: ['可扩展路由', 'success'],
      E1: ['需要处理', 'warning'],
    }[flow.code] || ['检查状态', 'info'];
    const operational = ['S3', 'S4'].includes(flow.code);
    if (operational) {
      const content = this.flowContent(flow);
      const readyModelCount = this.readyModelCount(flow);
      return `
        <header class="dashboard-hero dashboard-hero-operational dashboard-flow-card status-${Utils.escapeHtml(flow.code)}" data-dashboard-hero data-dashboard-flow-card>
          <div class="dashboard-hero-main">
            <div class="dashboard-eyebrow">Lin Router 正在运行</div>
            <h2>${Utils.escapeHtml(content.title)}</h2>
            <p>${content.facts}</p>
            <div class="dashboard-running-summary" aria-label="运行摘要">
              <span><strong>${flow.readyGroups.length} / ${(Store.state?.groups || []).length}</strong><small>可用连接组</small></span>
              <span><strong>${readyModelCount} / ${(Store.state?.models || []).length}</strong><small>已验证模型</small></span>
              <span><strong>${Utils.escapeHtml(flow.primary?.representative?.name || '-')}</strong><small>当前主模型</small></span>
            </div>
          </div>
          <div class="dashboard-hero-actions">
            <span class="status-tag ${status[1]}">${status[0]}</span>
            <div class="dashboard-flow-actions">${content.primary}${content.secondary || ''}</div>
          </div>
        </header>
      `;
    }
    return `
      <header class="dashboard-hero" data-dashboard-hero>
        <div>
          <div class="dashboard-eyebrow">Lin Router 正在运行</div>
          <h2>${flow.code === 'S0' ? '从添加连接组开始' : '连接与接入状态'}</h2>
          <p>${flow.code === 'S0' ? 'Lin Router 已启动，但还没有可处理请求的连接组。' : '按当前配置和真实请求记录给出下一步。'}</p>
        </div>
        <span class="status-tag ${status[1]}">${status[0]}</span>
      </header>
    `;
  },

  readyModelCount(flow) {
    const models = Store.state?.models || [];
    const readyGroupIds = new Set((flow.readyGroups || []).map(item => item.group?.id));
    return models.filter(model => readyGroupIds.has(model.group_id)
      && model.usable !== false
      && !ConnectionStatus.isCooling(model)
      && String(model.last_success_at || '').trim()).length;
  },

  flowContent(flow) {
    const item = flow.primary;
    const group = item?.group;
    const model = item?.representative;
    const hasRelayOnboarding = this.onboardingRelayGroups(flow).length > 0;
    const mode = { relay: '中转站', ark: '火山方舟', proxy: '通用 OpenAI 代理' }[group?.provider_type] || group?.provider_type || '';
    const groupId = Utils.escapeHtml(group?.id || '');
    const modelId = Utils.escapeHtml(model?.id || '');
    const content = {
      S0: {
        title: '还没有连接组',
        facts: '添加一个连接组后，你可以获取模型、测试请求，并将本机地址配置到 Codex、Hermes 或其他 OpenAI 兼容客户端。',
        primary: '<button type="button" class="btn-primary" data-dashboard-action="new-group">添加连接组</button>',
        secondary: '<button type="button" class="btn-secondary" data-dashboard-action="import">导入已有配置</button>',
      },
      S1: group?.provider_type === 'ark' ? {
        title: '连接组已添加，尚未有模型',
        facts: `${Utils.escapeHtml(group?.name || '当前连接组')}（${Utils.escapeHtml(mode)}）已保存，模型数为 0。`,
        primary: `<button type="button" class="btn-primary" data-dashboard-action="add-model" data-group-id="${groupId}">手动添加模型</button>`,
        secondary: '',
      } : {
        title: '连接组已添加，尚未有模型',
        facts: `${Utils.escapeHtml(group?.name || '当前连接组')}（${Utils.escapeHtml(mode)}）已保存，模型数为 0。`,
        primary: `<button type="button" class="btn-primary" data-dashboard-action="fetch-models" data-group-id="${groupId}">获取模型</button>`,
        secondary: `<button type="button" class="btn-secondary" data-dashboard-action="add-model" data-group-id="${groupId}">手动添加模型</button>`,
      },
      S2: {
        title: '模型已添加，建议先验证',
        facts: `${Utils.escapeHtml(model?.name || '当前模型')} 属于 ${Utils.escapeHtml(group?.name || '当前连接组')}，当前状态为待验证。`,
        primary: `<button type="button" class="btn-primary" data-dashboard-action="test-model" data-model-id="${modelId}">测试模型</button>`,
        secondary: `<button type="button" class="btn-secondary" data-dashboard-action="select-group" data-group-id="${groupId}">编辑连接组</button>`,
      },
      S3: {
        title: '已具备客户端接入条件',
        facts: `最近成功模型为 ${Utils.escapeHtml(model?.name || '当前模型')}，属于 ${Utils.escapeHtml(group?.name || '当前连接组')}。`,
        primary: hasRelayOnboarding
          ? '<button type="button" class="btn-primary" data-dashboard-action="open-onboarding">接入 Codex 或 Hermes</button>'
          : `<button type="button" class="btn-primary" data-dashboard-action="copy-client" data-group-id="${groupId}">复制客户端配置</button>`,
        secondary: `<button type="button" class="btn-secondary" data-dashboard-action="test-model" data-model-id="${modelId}">再次测试</button>`,
      },
      S4: {
        title: '多个连接组已可用，可选创建智能路由',
        facts: `已有 ${flow.readyGroups.length} 个可用连接组。智能路由可在候选之间自动切换，但不会影响现有直连接入。`,
        primary: hasRelayOnboarding
          ? '<button type="button" class="btn-primary" data-dashboard-action="open-onboarding">接入 Codex 或 Hermes</button>'
          : '<button type="button" class="btn-primary" data-dashboard-action="new-aggregate">创建智能路由</button>',
        secondary: hasRelayOnboarding
          ? '<button type="button" class="btn-secondary" data-dashboard-action="new-aggregate">创建智能路由</button>'
          : `<button type="button" class="btn-secondary" data-dashboard-action="copy-client" data-group-id="${groupId}">复制客户端配置</button>`,
      },
      E1: {
        title: '连接组需要处理',
        facts: `${Utils.escapeHtml(group?.name || '当前连接组')}：${Utils.escapeHtml(item?.reason || '当前没有可用模型')}。`,
        primary: `<button type="button" class="btn-primary" data-dashboard-action="select-group" data-group-id="${groupId}">查看并处理</button>`,
        secondary: '',
      },
    };
    return content[flow.code] || { title: '检查连接状态', facts: '', primary: '', secondary: '' };
  },

  renderFlowDetails(flow) {
    const item = flow.primary;
    return `
      ${flow.code === 'S0' ? `
        <div class="dashboard-third-party">
          <strong>没有可用的上游服务？</strong>
          <span>Lin Router 本身不提供模型额度。你可以使用已有的 OpenAI 兼容服务，也可以从第三方服务获取 Base URL 和 API Key。</span>
          <a href="https://www.codeok.cc/" target="_blank" rel="noopener noreferrer">推荐的第三方服务：CodeOK（第三方）</a>
        </div>` : ''}
      ${item && flow.code !== 'S0' ? `
        <div class="dashboard-flow-facts">
          <span><strong>状态：</strong>${Utils.escapeHtml(item.label)}</span>
          <span><strong>原因：</strong>${Utils.escapeHtml(item.reason)}</span>
          <span><strong>影响：</strong>${Utils.escapeHtml(item.impact)}</span>
          <span><strong>系统动作：</strong>${Utils.escapeHtml(item.systemAction)}</span>
        </div>` : ''}
    `;
  },

  renderFlowCard(flow) {
    if (['S3', 'S4'].includes(flow.code)) return '';
    const content = this.flowContent(flow);
    const item = flow.primary;
    return `
      <section class="dashboard-flow-card status-${Utils.escapeHtml(item?.code || flow.code)}" data-dashboard-flow-card>
        <div class="dashboard-flow-status">下一步</div>
        <h3>${content.title}</h3>
        <p>${content.facts}</p>
        ${this.renderFlowDetails(flow)}
        <div class="dashboard-flow-actions">${content.primary}${content.secondary}</div>
      </section>`;
  },

  renderNextSteps(flow) {
    const rows = flow.groups.map(item => `
      <div class="connection-summary-row">
        <div><strong>${Utils.escapeHtml(item.group.name)}</strong><span>${Utils.escapeHtml(item.label)} · ${item.modelCount} 个模型</span></div>
        <span>${Utils.escapeHtml(item.reason)}</span>
        <button type="button" class="btn-secondary btn-sm" data-dashboard-action="select-group" data-group-id="${Utils.escapeHtml(item.group.id)}">查看</button>
      </div>`).join('');
    return `<section class="dashboard-card connection-summary-card"><h3>连接组状态</h3>${rows || '<div class="empty-hint">暂无连接组。</div>'}</section>`;
  },

  metricCard(label, value, hint) {
    return `<section class="dashboard-metric"><span>${Utils.escapeHtml(label)}</span><strong>${Utils.escapeHtml(value)}</strong><small>${Utils.escapeHtml(hint || '')}</small></section>`;
  },

  renderMetrics(state, flow, recent) {
    const aggregates = state.aggregate_models || [];
    const members = state.aggregate_members || [];
    const success = recent.filter(log => String(log.status || '').startsWith('2')).length;
    const successRate = recent.length ? `${Math.round((success / recent.length) * 100)}%` : '-';
    const fallbackCount = recent.filter(log => ['fallback', 'retry_ok'].includes(String(log.event || ''))).length;
    const busyCount = recent.filter(log => {
      const text = `${log.event || ''};${log.detail || ''}`;
      return text.includes('candidate_busy') || text.includes('large_task_in_progress') || text.includes('serial_protection_timeout') || text.includes('waf_lock_timeout');
    }).length;
    const upstreamTimeoutCount = recent.filter(log => `${log.event || ''};${log.detail || ''}`.includes('upstream_timeout') || `${log.detail || ''}`.includes('read_timeout') || `${log.detail || ''}`.includes('stream_idle_timeout')).length;
    const wafBlockedCount = recent.filter(log => `${log.event || ''};${log.detail || ''}`.includes('waf_blocked')).length;
    const enabledAggregates = aggregates.filter(aggregate => aggregate.enabled !== false).length;
    return `
      <div class="dashboard-grid" data-dashboard-metrics>
        ${this.metricCard('服务状态', state.log_write_error ? '日志异常' : '运行中', state.log_write_error || '本地代理服务已启动')}
        ${this.metricCard('智能路由', `${enabledAggregates} / ${aggregates.length}`, `${members.length} 个聚合成员`)}
        ${this.metricCard('最近成功率', successRate, `最近 ${recent.length} 条请求`)}
        ${this.metricCard('Fallback', `${fallbackCount} 次`, '最近请求自动切换次数')}
        ${this.metricCard('候选忙', `${busyCount} 次`, '大上下文或串行保护等待')}
        ${this.metricCard('上游超时 / WAF', `${upstreamTimeoutCount} / ${wafBlockedCount}`, '最近请求健康信号')}
      </div>
    `;
  },

  renderLiveRequests(items) {
    const requests = items || [];
    const rows = requests.slice(0, 8).map(item => {
      const slow = item.slow ? ' slow' : '';
      const elapsed = this.formatElapsed(item.elapsed_ms || 0);
      const hint = item.possible_reason || (item.slow ? '请求耗时较长，请关注当前阶段' : '处理中');
      const stageLabel = item.stage_label || this.liveStageLabel(item.stage);
      return `
        <div class="live-request-row${slow}" data-dashboard-live-request="${Utils.escapeHtml(item.request_id || '')}">
          <div><strong>${Utils.escapeHtml(item.request_id_short || String(item.request_id || '').slice(0, 8))}</strong><span>${Utils.escapeHtml(item.requested_model || item.model || '-')}</span></div>
          <div>${Utils.escapeHtml(item.group || item.candidate || '-')}</div>
          <div><span class="pill ${item.slow ? 'warning' : 'info'}">${Utils.escapeHtml(stageLabel)}</span></div>
          <div>${Utils.escapeHtml(elapsed)}</div>
          <small title="${Utils.escapeHtml(hint)}">${Utils.escapeHtml(hint)}</small>
          <div class="live-request-action">${item.cancellable === false ? '<span class="pill warning">终止中…</span>' : `<button type="button" class="btn-secondary btn-sm" data-dashboard-action="cancel-request" data-request-id="${Utils.escapeHtml(item.request_id || '')}" data-request-short="${Utils.escapeHtml(item.request_id_short || String(item.request_id || '').slice(0, 8))}" data-request-model="${Utils.escapeHtml(item.requested_model || item.model || '-')}" data-request-group="${Utils.escapeHtml(item.group || item.candidate || '-')}" data-request-stage="${Utils.escapeHtml(stageLabel)}" data-request-elapsed="${Utils.escapeHtml(elapsed)}">终止请求</button>`}</div>
        </div>`;
    }).join('');
    return `
      <section class="dashboard-card live-requests-card" data-dashboard-live-requests aria-live="polite">
        <div class="section-title-row">
          <h3>实时请求观测</h3>
          <span class="pill ${requests.length ? 'warning' : 'success'}">${requests.length ? `${requests.length} 个进行中` : '空闲'}</span>
        </div>
        ${rows || '<div class="empty-hint">当前没有正在处理的请求。</div>'}
      </section>
    `;
  },

  // 专属引导只面向已验证且当前可用的 relay，方舟和通用代理继续使用原有通用接入卡。
  onboardingRelayGroups(flow, state = Store.state || {}) {
    return (flow.readyGroups || []).filter(item =>
      item.group?.provider_type === 'relay' && this.onboardingModels(item, state).length,
    );
  },

  onboardingModels(item, state = Store.state || {}) {
    return (state.models || []).filter(model =>
      model.group_id === item.group?.id
      && model.usable !== false
      && !ConnectionStatus.isCooling(model)
      && String(model.last_success_at || '').trim(),
    );
  },

  selectedOnboarding(flow, state = Store.state || {}) {
    const groups = this.onboardingRelayGroups(flow, state);
    if (!groups.length) return null;

    const defaultGroup = groups.find(item => item.group?.id === flow.primary?.group?.id) || groups[0];
    const item = groups.find(candidate => candidate.group?.id === this._onboardingSelection.groupId) || defaultGroup;
    const models = this.onboardingModels(item, state);
    const model = models.find(candidate => candidate.id === this._onboardingSelection.modelId)
      || models.find(candidate => candidate.id === item.verifiedModel?.id)
      || models[0];
    const client = this._onboardingSelection.client === 'hermes' ? 'hermes' : 'codex';

    this._onboardingSelection = {
      groupId: item.group.id,
      modelId: model?.id || '',
      client,
    };
    return { groups, item, group: item.group, models, model, client };
  },

  onboardingPreparedKey(selection) {
    if (!selection?.group?.id || !selection?.model?.id) return '';
    return `${selection.group.id}:${selection.model.id}:${selection.client}`;
  },

  onboardingLastSuccess(model) {
    return String(model?.last_success_at || '').trim() || '暂无成功记录';
  },

  renderOnboardingValue(label, value, hint, { copy = true, output = '' } = {}) {
    const text = String(value || '');
    return `
      <div class="dashboard-onboarding-value" ${output ? `data-onboarding-output="${Utils.escapeHtml(output)}"` : ''}>
        <div>
          <strong>${Utils.escapeHtml(label)}</strong>
          <span>${Utils.escapeHtml(hint)}</span>
        </div>
        <code>${Utils.escapeHtml(text || '未生成')}</code>
        ${copy ? `<button type="button" class="btn-secondary btn-sm" data-onboarding-copy="${Utils.escapeHtml(text)}" ${text ? '' : 'disabled'}>复制</button>` : ''}
      </div>`;
  },

  onboardingClientText(selection, baseUrl) {
    const { group, model, client } = selection;
    const clientName = client === 'hermes' ? 'Hermes' : 'Codex';
    return `客户端: ${clientName}\nBase URL: ${baseUrl}\nroute key: ${group?.route_key || ''}\nModel: ${model?.name || ''}`;
  },

  renderOnboardingClientGuide(selection, baseUrl) {
    const { client } = selection;
    const clientName = client === 'hermes' ? 'Hermes' : 'Codex';
    const accessText = this.onboardingClientText(selection, baseUrl);
    const prepared = this._onboardingPreparedKey === this.onboardingPreparedKey(selection);
    return `
      <div class="dashboard-onboarding-client-guide" data-dashboard-onboarding-client-guide>
        <div class="dashboard-onboarding-guide-heading">
          <div>
            <h4>${clientName} 接入信息</h4>
            <p>复制下方接入信息，在目标客户端中按其已有方式填写即可。</p>
          </div>
          <button type="button" class="btn-secondary btn-sm" data-onboarding-copy="${Utils.escapeHtml(accessText)}">一键复制接入信息</button>
        </div>
        <p class="dashboard-onboarding-use-hint">复制完成后，请在 ${clientName} 中按客户端已有方式填写并自行验证。</p>
        <p class="dashboard-onboarding-complete${prepared ? ' is-visible' : ''}" data-dashboard-onboarding-completion data-onboarding-complete>${prepared ? '接入信息已准备好，请在客户端中使用。' : ''}</p>
      </div>`;
  },

  renderSelfServiceOnboarding(flow, baseUrl) {
    const selection = this.selectedOnboarding(flow);
    if (!selection) return '<div data-dashboard-onboarding-section></div>';

    const { groups, group, models, model, client } = selection;
    return `
      <section class="dashboard-onboarding-section" data-dashboard-onboarding-section aria-labelledby="dashboard-onboarding-title">
        <details class="dashboard-onboarding-details" data-onboarding-collapse ${this._onboardingOpen ? 'open' : ''}>
          <summary>
            <span>
              <strong id="dashboard-onboarding-title">接入 Codex 或 Hermes</strong>
              <small>把已验证的本地 Lin Router 入口交给目标客户端使用。</small>
            </span>
            <span class="dashboard-onboarding-summary-state">${this._onboardingOpen ? '收起' : '展开'}</span>
          </summary>
          <div class="dashboard-onboarding-body">
            <div class="dashboard-onboarding-intro dashboard-onboarding-intro-compact">
              <div>
                <strong>用途</strong>
                <span>将当前已验证连接组的本地入口交给目标客户端使用。</span>
              </div>
              <div>
                <strong>安全提示</strong>
                <span>route key 是客户端到本机 Lin Router 的认证，不是上游 API Key。</span>
              </div>
            </div>
            <div class="dashboard-onboarding-controls">
              <label>
                <span>连接组</span>
                <select data-onboarding-group>
                  ${groups.map(candidate => `<option value="${Utils.escapeHtml(candidate.group.id)}" ${candidate.group.id === group.id ? 'selected' : ''}>${Utils.escapeHtml(candidate.group.name)}</option>`).join('')}
                </select>
              </label>
              <label>
                <span>已验证模型</span>
                <select data-onboarding-model>
                  ${models.map(candidate => `<option value="${Utils.escapeHtml(candidate.id)}" ${candidate.id === model?.id ? 'selected' : ''}>${Utils.escapeHtml(candidate.name)} · 最近成功 ${Utils.escapeHtml(this.onboardingLastSuccess(candidate))}</option>`).join('')}
                </select>
              </label>
              <div class="dashboard-onboarding-last-success"><span>最近验证成功</span><strong>${Utils.escapeHtml(this.onboardingLastSuccess(model))}</strong></div>
            </div>
            <div class="dashboard-onboarding-output" data-dashboard-onboarding data-onboarding>
              ${this.renderOnboardingValue('本地 Base URL', baseUrl, '客户端请求会先进入本机 Lin Router。', { output: 'base-url' })}
              ${this.renderOnboardingValue('本地 route key', group.route_key || '', '客户端到 Lin Router 的认证，不是上游 API Key。', { output: 'route-key' })}
              ${this.renderOnboardingValue('Model', model?.name || '', '决定本次使用的已验证上游模型。', { output: 'model' })}
              ${this.renderOnboardingValue('适用客户端', client === 'hermes' ? 'Hermes' : 'Codex', '选择客户端后显示对应的使用说明。', { copy: false, output: 'client' })}
              <div class="dashboard-onboarding-client-switch" role="group" aria-label="选择目标客户端">
                <button type="button" class="${client === 'codex' ? 'is-active' : ''}" data-onboarding-client="codex" aria-pressed="${client === 'codex'}">Codex</button>
                <button type="button" class="${client === 'hermes' ? 'is-active' : ''}" data-onboarding-client="hermes" aria-pressed="${client === 'hermes'}">Hermes</button>
              </div>
              ${this.renderOnboardingClientGuide(selection, baseUrl)}
            </div>
          </div>
        </details>
      </section>`;
  },

  formatElapsed(ms) {
    const seconds = Math.max(0, Math.round(Number(ms || 0) / 1000));
    if (seconds < 60) return `${seconds}s`;
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  },

  liveStageLabel(stage) {
    const map = {
      selecting_candidate: '选择候选',
      preparing_upstream: '准备上游请求',
      waiting_serial_protection: '等待串行保护',
      connecting_upstream: '连接上游',
      waiting_first_byte: '等待首完整帧',
      streaming: '接收流式响应',
      receiving_response: '接收响应',
      candidate_busy: '候选忙/串行保护等待超时',
    };
    return map[stage] || stage || '处理中';
  },

  ensureAccessGroups(groups) {
    const groupIds = new Set((groups || []).map(item => item.group?.id).filter(Boolean));
    const groupIdsSignature = [...groupIds].join('|');
    const identityChanged = this._accessGroupIdsSignature !== '' && this._accessGroupIdsSignature !== groupIdsSignature;
    this._openAccessGroups = new Set([...this._openAccessGroups].filter(id => groupIds.has(id)));
    if (!groupIds.size) {
      this._accessGroupsInitialized = false;
      this._accessGroupCount = 0;
      this._accessGroupIdsSignature = '';
      return;
    }
    if (!this._accessGroupsInitialized) {
      if (groups.length === 1) {
        const first = groups[0]?.group?.id;
        if (first) this._openAccessGroups.add(first);
      }
      this._accessGroupsInitialized = true;
    } else if (identityChanged || this._accessGroupCount !== groups.length) {
      // 连接组数量变化后回到可预期的默认展开规则，避免新增连接组后把大量 Key 一次性铺开。
      this._openAccessGroups.clear();
      if (groups.length === 1) {
        const first = groups[0]?.group?.id;
        if (first) this._openAccessGroups.add(first);
      }
    }
    this._accessGroupCount = groups.length;
    this._accessGroupIdsSignature = groupIdsSignature;
  },

  matchingAccessGroups(groups) {
    const query = this._accessFilter.trim().toLocaleLowerCase();
    if (!query) return groups || [];
    return (groups || []).filter(item => {
      const model = item.verifiedModel || item.representative;
      const searchable = [item.group?.name, model?.name, item.group?.provider_type].join(' ').toLocaleLowerCase();
      return searchable.includes(query);
    });
  },

  renderDirectAccessSection(groups, baseUrl) {
    this.ensureAccessGroups(groups);
    const matching = this.matchingAccessGroups(groups);
    return `
      <div class="dashboard-section-heading">
        <div><h3>客户端接入</h3><p>使用本地路由 Key，不是上游 API Key。</p></div>
        <label class="dashboard-access-filter-label">
          <span class="sr-only">筛选已验证连接组</span>
          <input type="search" class="dashboard-access-filter" data-dashboard-access-filter value="${Utils.escapeHtml(this._accessFilter)}" placeholder="筛选连接组或模型">
        </label>
      </div>
      <div class="dashboard-access-meta" data-dashboard-access-count>显示 ${matching.length} / ${groups.length} 个已验证连接组</div>
      ${this.renderDirectAccessCards(groups, baseUrl)}
    `;
  },

  renderDirectAccessSurface(groups, baseUrl) {
    return `<section class="dashboard-access-section" data-dashboard-direct-access-section>${this.renderDirectAccessSection(groups, baseUrl)}</section>`;
  },

  renderDirectAccessCards(groups, baseUrl) {
    this.ensureAccessGroups(groups);
    const matching = this.matchingAccessGroups(groups);
    if (!matching.length) {
      return '<div class="dashboard-empty-access" data-dashboard-direct-access>没有匹配的已验证连接组。</div>';
    }
    return `<div class="dashboard-access-grid" data-dashboard-direct-access>${matching.map(item => {
      const group = item.group;
      const model = item.verifiedModel || item.representative;
      const open = this._openAccessGroups.has(group.id);
      const provider = { relay: '中转站', ark: '火山方舟', proxy: '通用代理' }[group.provider_type] || group.provider_type || '未知渠道';
      const usableModels = (Store.state.models || []).filter(candidate =>
        candidate.group_id === group.id && candidate.usable !== false && !ConnectionStatus.isCooling(candidate),
      ).length;
      return `
        <details class="dashboard-access-card dashboard-access-group" data-dashboard-access-group="${Utils.escapeHtml(group.id)}" ${open ? 'open' : ''}>
          <summary>
            <span class="dashboard-access-title">${Utils.escapeHtml(group.name)}</span>
            <span class="dashboard-access-provider">${Utils.escapeHtml(provider)}</span>
            <span class="status-tag success">${Utils.escapeHtml(item.label || '可用')}</span>
            <span class="dashboard-access-model">可用模型 ${usableModels} / ${item.modelCount}</span>
          </summary>
          <div class="dashboard-access-content">
            <div class="dashboard-access-scope">适用范围：当前连接组。</div>
            <div class="copy-row"><span>Base URL</span><code>${Utils.escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(baseUrl)}">复制</button></div>
            <div class="copy-row"><span>route key</span><code>${Utils.escapeHtml(group.route_key || '未生成')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(group.route_key || '')}" ${group.route_key ? '' : 'disabled'}>复制</button></div>
            <div class="copy-row"><span>Model</span><code>${Utils.escapeHtml(model?.name || '-')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(model?.name || '')}" ${model?.name ? '' : 'disabled'}>复制</button></div>
            <button type="button" class="btn-secondary btn-sm" data-copy-client-group="${Utils.escapeHtml(group.id)}">复制完整通用配置</button>
          </div>
        </details>`;
    }).join('')}</div>`;
  },

  renderAggregateAccessSection(aggregates, baseUrl) {
    const enabled = aggregates.filter(aggregate => aggregate.enabled !== false);
    const disabled = aggregates.filter(aggregate => aggregate.enabled === false);
    return `
      <div class="dashboard-section-heading">
        <div><h3>智能路由</h3><p>聚合模型用于多候选自动切换，不影响连接组直连。</p></div>
      </div>
      ${this.renderAggregateAccessCards(enabled, baseUrl)}
      ${disabled.length ? this.renderDisabledAggregateList(disabled) : ''}
    `;
  },

  renderAggregateAccessCards(aggregates, baseUrl) {
    if (!aggregates.length) {
      return `
        <div class="dashboard-empty-access">
          <p>当前没有启用的智能路由。连接组直连已可用；智能路由仅用于多候选自动切换。</p>
          <button type="button" class="btn-secondary btn-sm" data-dashboard-action="new-aggregate">创建智能路由</button>
        </div>
      `;
    }
    return `<div class="dashboard-access-grid dashboard-aggregate-access-grid">${aggregates.map(aggregate => {
      return `
        <section class="dashboard-access-card dashboard-aggregate-access-card">
          <div class="dashboard-access-title">${Utils.escapeHtml(aggregate.display_name || aggregate.name)} <span class="status-tag info">智能路由</span></div>
          <div class="copy-row"><span>Base URL</span><code>${Utils.escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(baseUrl)}">复制</button></div>
          <div class="copy-row"><span>route key</span><code>${Utils.escapeHtml(aggregate.route_key || '未生成')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregate.route_key || '')}" ${aggregate.route_key ? '' : 'disabled'}>复制</button></div>
          <div class="copy-row"><span>Model</span><code>${Utils.escapeHtml(aggregate.name || '-')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregate.name || '')}" ${aggregate.name ? '' : 'disabled'}>复制</button></div>
        </section>`;
    }).join('')}</div>`;
  },

  renderDisabledAggregateList(aggregates) {
    return `
      <details class="dashboard-disabled-aggregates">
        <summary>已停用的聚合模型（${aggregates.length}）</summary>
        <div class="dashboard-disabled-aggregate-list">
          ${aggregates.map(aggregate => `<span>${Utils.escapeHtml(aggregate.display_name || aggregate.name)}</span>`).join('')}
        </div>
      </details>
    `;
  },

  aggregateSummaryCards(aggregates, logs) {
    if (!aggregates.length) return '<section class="dashboard-aggregate-summary hidden" data-dashboard-aggregate-summary></section>';
    const cards = aggregates.map(aggregate => {
      const related = logs.filter(log => log.aggregate_id === aggregate.id || log.aggregate_model === aggregate.name || log.model === aggregate.name);
      const real = related.filter(log => !this.isConfigSkip(log));
      const success = real.filter(log => String(log.status || '').startsWith('2') && ['ok', 'stream_done', 'retry_ok', 'stream_ok'].includes(String(log.event || ''))).length;
      const busy = related.filter(log => `${log.event || ''};${log.detail || ''}`.includes('candidate_busy') || `${log.detail || ''}`.includes('large_task_in_progress')).length;
      const cached = real.reduce((sum, log) => sum + Number(log.cached_tokens || 0), 0);
      const prompt = real.reduce((sum, log) => sum + Number(log.prompt_tokens || 0), 0);
      const rate = real.length ? `${Math.round((success / real.length) * 100)}%` : '暂无数据';
      const cacheRate = prompt ? `${Math.round((cached / prompt) * 100)}%` : '暂无数据';
      return `
        <button type="button" class="dashboard-card dashboard-aggregate-card ${aggregate.enabled === false ? 'is-disabled' : ''}" data-dashboard-action="aggregate" data-aggregate-id="${Utils.escapeHtml(aggregate.id)}">
          <span class="dashboard-aggregate-card-title">${Utils.escapeHtml(aggregate.display_name || aggregate.name)}${aggregate.enabled === false ? '（已停用）' : ''}</span>
          <span class="dashboard-mini-grid">
            <span>成功率 <strong>${rate}</strong></span>
            <span>候选忙 <strong>${busy}</strong></span>
            <span>Cache <strong>${cacheRate}</strong></span>
          </span>
        </button>
      `;
    }).join('');
    return `<section class="dashboard-aggregate-summary" data-dashboard-aggregate-summary><div class="dashboard-section-heading"><div><h3>聚合收益摘要</h3><p>点击聚合模型可进入配置管理。</p></div></div><div class="dashboard-aggregate-grid">${cards}</div></section>`;
  },

  isConfigSkip(log) {
    if (log.event !== 'skip') return false;
    const detail = String(log.detail || '');
    return ['member_disabled', 'member_cooling', 'underlying_model_disabled', 'underlying_model_cooling'].some(reason => detail.includes(`skip_reason=${reason}`));
  },

  clientConfigText(group, model, baseUrl = `${window.location.origin}/v1`) {
    return `Base URL: ${baseUrl}\nAPI Key: ${group?.route_key || ''}\nModel: ${model?.name || ''}`;
  },

  attachEvents(panel) {
    if (panel.dataset.dashboardEventsBound === 'true') return;
    panel.dataset.dashboardEventsBound = 'true';

    panel.addEventListener('click', event => {
      const target = event.target.closest('[data-copy-value], [data-copy-client-group], [data-onboarding-copy], [data-onboarding-client], [data-dashboard-action]');
      if (!target || !panel.contains(target)) return;
      if (target.dataset.onboardingCopy !== undefined) {
        const value = (target.dataset.onboardingCopy || '').trim();
        if (!value) {
          Toast.warning('暂无可复制内容');
          return;
        }
        Utils.copy(value).then(ok => {
          if (!ok) return Toast.error('复制失败');
          const flow = ConnectionStatus.derive(Store.state || {});
          const selection = this.selectedOnboarding(flow);
          this._onboardingPreparedKey = this.onboardingPreparedKey(selection);
          this.patchOnboarding(panel, flow);
          return Toast.success('接入信息已准备好，请在客户端中使用');
        });
        return;
      }
      if (target.dataset.onboardingClient !== undefined) {
        this._onboardingSelection.client = target.dataset.onboardingClient === 'hermes' ? 'hermes' : 'codex';
        this._onboardingPreparedKey = '';
        this.patchOnboarding(panel, undefined, `[data-onboarding-client="${this._onboardingSelection.client}"]`);
        return;
      }
      if (target.dataset.copyValue !== undefined) {
        const value = (target.dataset.copyValue || '').trim();
        if (!value) Toast.warning('暂无可复制内容');
        else Utils.copy(value).then(ok => ok ? Toast.success('已复制') : Toast.error('复制失败'));
        return;
      }
      if (target.dataset.copyClientGroup !== undefined) {
        const group = Store.getGroup(target.dataset.copyClientGroup);
        const status = group ? ConnectionStatus.group(group) : null;
        Utils.copy(this.clientConfigText(group, status?.verifiedModel || status?.representative))
          .then(ok => ok ? Toast.success('客户端配置已复制') : Toast.error('复制失败'));
        return;
      }
      void this.handleAction(target);
    });

    panel.addEventListener('input', event => {
      const input = event.target.closest?.('[data-dashboard-access-filter]');
      if (!input) return;
      this._accessFilter = input.value || '';
      this.patchDirectAccess(panel);
    });

    panel.addEventListener('change', event => {
      const input = event.target;
      if (input?.dataset?.onboardingGroup !== undefined) {
        this._onboardingSelection.groupId = input.value || '';
        this._onboardingSelection.modelId = '';
        this._onboardingPreparedKey = '';
        this.patchOnboarding(panel, undefined, '[data-onboarding-group]');
      }
      if (input?.dataset?.onboardingModel !== undefined) {
        this._onboardingSelection.modelId = input.value || '';
        this._onboardingPreparedKey = '';
        this.patchOnboarding(panel, undefined, '[data-onboarding-model]');
      }
    });

    panel.addEventListener('toggle', event => {
      const details = event.target;
      if (!(details instanceof HTMLDetailsElement)) return;
      if (details.dataset.dashboardAccessGroup) {
        if (details.open) this._openAccessGroups.add(details.dataset.dashboardAccessGroup);
        else this._openAccessGroups.delete(details.dataset.dashboardAccessGroup);
      }
      if (details.dataset.dashboardFlowSummary) {
        if (details.open) this._openFlowSummaries.add(details.dataset.dashboardFlowSummary);
        else this._openFlowSummaries.delete(details.dataset.dashboardFlowSummary);
      }
      if (details.dataset.onboardingCollapse !== undefined) {
        this._onboardingOpen = details.open;
        const summaryState = details.querySelector('.dashboard-onboarding-summary-state');
        if (summaryState) summaryState.textContent = details.open ? '收起' : '展开';
      }
    }, true);
  },

  patchDirectAccess(panel) {
    const flow = ConnectionStatus.derive(Store.state || {});
    const baseUrl = `${window.location.origin}/v1`;
    this.replaceSlot(panel, '[data-dashboard-direct-access]', this.renderDirectAccessCards(flow.readyGroups, baseUrl));
    const count = panel.querySelector('[data-dashboard-access-count]');
    if (count) {
      const matching = this.matchingAccessGroups(flow.readyGroups);
      count.textContent = `显示 ${matching.length} / ${flow.readyGroups.length} 个已验证连接组`;
    }
  },

  patchOnboarding(panel, flow = ConnectionStatus.derive(Store.state || {}), focusSelector = '') {
    const baseUrl = `${window.location.origin}/v1`;
    this.replaceSlot(panel, '[data-dashboard-onboarding-section]', this.renderSelfServiceOnboarding(flow, baseUrl));
    panel.dataset.dashboardOnboardingSignature = this.onboardingSignature(flow);
    if (!focusSelector) return;
    const focusTarget = panel.querySelector(focusSelector);
    try { focusTarget?.focus({ preventScroll: true }); } catch (_) { focusTarget?.focus(); }
  },

  async handleAction(btn) {
    const action = btn.dataset.dashboardAction;
    if (action === 'new-group') return App.createGroup();
    if (action === 'new-aggregate') return App.createAggregate();
    if (action === 'import') return App.importConfig();
    if (action === 'config') return Tabs.switch('config');
    if (action === 'test') return Tabs.switch('test');
    if (action === 'logs') return Tabs.switch('logs');
    if (action === 'open-risk-model') {
      const model = Store.getModel(btn.dataset.modelId);
      if (!model?.risk_isolated) return Toast.warning('该模型当前没有可处理的上游风控隔离');
      Store.select('model', model.id);
      return Tabs.switch('config');
    }
    if (action === 'select-group') { Store.select('group', btn.dataset.groupId); return Tabs.switch('config'); }
    if (action === 'add-model') return ConfigTab.onAddModelToGroup(btn.dataset.groupId);
    if (action === 'fetch-models') return ConfigTab.fetchModelsForGroup(btn.dataset.groupId);
    if (action === 'test-model') { Store.select('model', btn.dataset.modelId); return Tabs.switch('test'); }
    if (action === 'open-onboarding') {
      const target = document.querySelector('[data-dashboard-onboarding-section]');
      if (!target) return;
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const focusTarget = target.querySelector('[data-onboarding-client][aria-pressed="true"]') || target.querySelector('[data-onboarding-group]');
      focusTarget?.focus({ preventScroll: true });
      return;
    }
    if (action === 'copy-client') {
      const group = Store.getGroup(btn.dataset.groupId);
      const status = group ? ConnectionStatus.group(group) : null;
      return Utils.copy(this.clientConfigText(group, status?.verifiedModel || status?.representative))
        .then(ok => ok ? Toast.success('客户端配置已复制') : Toast.error('复制失败'));
    }
    if (action === 'cancel-request') {
      const ok = await Modal.confirm({
        title: '终止当前请求？',
        message: `请求：${Utils.escapeHtml(btn.dataset.requestShort || '-')} · ${Utils.escapeHtml(btn.dataset.requestModel || '-')}<br>当前：${Utils.escapeHtml(btn.dataset.requestGroup || '-')} · ${Utils.escapeHtml(btn.dataset.requestStage || '-')} · 已运行 ${Utils.escapeHtml(btn.dataset.requestElapsed || '-')}<br><br>将停止本工具对该请求的处理并尝试关闭上游连接。上游是否已经停止生成或继续计费，取决于上游服务，无法保证。`,
        confirmText: '终止请求',
        confirmClass: 'btn-danger',
        allowHtml: true,
      });
      if (!ok) return;
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.textContent = '终止中…';
      try {
        const result = await API.cancelLiveRequest(btn.dataset.requestId);
        Toast.success(result.message || '已发送终止指令，正在释放本地请求资源…');
        // 取消后仍走 dashboard scope，复用 revision/activity cursor，避免旧全量接口覆盖近期活动。
        await App.refreshRuntimeState('dashboard', { background: false, silent: true });
      } catch (err) {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btn.textContent = '终止请求';
        Toast.error(err.message || '终止指令未能确认，请刷新实时列表；如仍卡住可查看日志。');
      }
      return;
    }
    if (action === 'aggregate') { Store.select('aggregate', btn.dataset.aggregateId); return Tabs.switch('config'); }
  },
};
