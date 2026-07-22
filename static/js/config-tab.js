const ConfigTab = {
  defaultProviderBaseUrls: {
    ark: 'https://ark.cn-beijing.volces.com/api/v3',
    relay: 'https://www.codeok.cc/v1',
    proxy: '',
  },
  defaultRelayBaseUrl: 'https://www.codeok.cc/v1',
  _newGroupDraft: null,
  _drafts: new Map(),
  _draftDirty: new Set(),
  _draftBaselines: new Map(),
  _aggregateMemberUi: null,

  onShow() {
    const panel = document.getElementById('panel-config');
    if (!panel) return;
    ConfigTabForm.bindGlobalEvents(this);
    this.render();
  },

  startNewGroup() {
    this.clearDraft({ type: 'group', id: null });
    this._newGroupDraft = {
      name: '新连接组',
      provider_type: 'relay',
      base_url: this.defaultRelayBaseUrl,
      ark_api_key: '',
      api_key: '',
      auto_model_name: '',
      auto_model_cooldown_minutes: 5,
      stream_idle_timeout: 120,
      waf_compatible: false,
      serial_protection: false,
      waf_client_mode: 'always',
      waf_accept_policy: 'default',
    };
    Store.select('group', null);
    Tabs.switch('config');
    this.render();
  },

  providerBaseUrl(provider) {
    return this.defaultProviderBaseUrls[provider] ?? '';
  },

  providerBasePlaceholder(provider) {
    return this.providerBaseUrl(provider) || 'https://example.com/v1';
  },

  systemDefaultProvider(value) {
    const normalized = String(value || '').trim();
    if (!normalized) return '';
    return Object.entries(this.defaultProviderBaseUrls)
      .find(([, baseUrl]) => baseUrl && normalized === baseUrl)?.[0] || '';
  },

  isSystemDefaultBaseUrl(value, provider) {
    const normalized = String(value || '').trim();
    const defaultUrl = this.providerBaseUrl(provider);
    return Boolean(defaultUrl) && normalized === defaultUrl;
  },

  render() {
    const panel = document.getElementById('panel-config');
    const sel = Store.selected;
    // 清理旧的定时器，运行态刷新只 patch 状态，不触碰草稿控件。
    clearTimeout(this._autoSaveTimer);
    this._autoSaveTimer = null;
    this.setSaveStatus('');
    const formValues = this._draftValues(sel);
    this._stopCooldownTimer();
    const isNewGroupDraft = this.isNewGroupDraft(sel);
    if (this._newGroupDraft && !isNewGroupDraft) this._newGroupDraft = null;
    if (!sel.id && !isNewGroupDraft) {
      panel.innerHTML = this.renderEmptyState();
      this.attachEmptyEvents(panel);
      return;
    }
    const rawItem = isNewGroupDraft
      ? this._newGroupDraft
      : (sel.type === 'group' ? Store.getGroup(sel.id) : (sel.type === 'model' ? Store.getModel(sel.id) : Store.getAggregate(sel.id)));
    const item = this._itemWithDraft(sel, rawItem);
    const title = item ? Utils.escapeHtml(item.display_name || item.name) : (sel.type === 'group' ? '新建连接组' : (sel.type === 'model' ? '新建模型' : '新建聚合模型'));

    panel.innerHTML = `
      <div class="config-header">
        <h2>${title}</h2>
        <div class="config-header-actions">
          <button type="button" id="config-runtime-refresh" class="btn-secondary btn-sm" title="立即刷新运行状态">刷新状态</button>
          <div class="save-status" id="save-status"></div>
        </div>
      </div>
      <div class="config-layout">
        <div class="config-main">
          ${sel.type === 'aggregate' ? this.renderAggregateSection(sel) : (sel.type === 'group' || sel.type === null ? this.renderGroupSection(sel) : this.renderModelSection(sel))}
        </div>
        <div class="config-side">
          ${sel.type === 'group' || sel.type === null ? this.renderGroupSide() : ''}
        </div>
      </div>
    `;
    const form = panel.querySelector('.config-form');
    if (!formValues && form) this._rememberDraftBaseline(sel, form);
    this._restoreFormValues(formValues || {});
    this.attachEvents(panel);
    this.syncUIFromState();
    if (formValues) this.setSaveStatus('draft');
    this._startCooldownTimer();
  },

  renderEmptyState() {
    return `
      <div class="empty-state">
        <div class="empty-icon">🚀</div>
        <h2>欢迎使用 Lin Router</h2>
        <p class="empty-subtitle">还没有连接组。添加连接组后，可以获取模型、测试请求并复制客户端接入信息。</p>
        <div class="empty-actions">
          <button type="button" class="btn-primary" id="empty-new-group">新建连接组</button>
          <button type="button" class="btn-secondary" id="empty-import">导入配置</button>
        </div>
        <p class="empty-hint">Lin Router 本身不提供模型额度；客户端使用连接组的本地路由 Key，不是上游 API Key。</p>
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

  renderGroupSection(sel = Store.selected) {
    const storedGroup = sel.type === 'group' && sel.id ? Store.getGroup(sel.id) : null;
    const g = this._itemWithDraft(sel, storedGroup || (this.isNewGroupDraft(sel) ? this._newGroupDraft : null));
    const isDraft = !storedGroup && Boolean(g);
    const groupRisk = this.groupRiskSummary(g?.id);
    const provider = g?.provider_type || 'relay';
    const baseUrl = g?.base_url || '';
    const groupKeyConfigured = this.groupKeyConfigured(g, provider);
    const usesSystemDefault = this.isSystemDefaultBaseUrl(baseUrl, provider);
    const systemDefaultProvider = this.systemDefaultProvider(baseUrl);
    return `
      <form class="config-form" id="group-form" data-type="group" data-selected-type="group" data-selected-id="${g?.id || ''}">
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
            <input id="group-base" value="${Utils.escapeHtml(baseUrl)}" placeholder="${Utils.escapeHtml(this.providerBasePlaceholder(provider))}" data-provider="${Utils.escapeHtml(provider)}" data-system-default-provider="${Utils.escapeHtml(systemDefaultProvider)}">
            <div class="form-hint ${usesSystemDefault ? '' : 'hidden'}" id="group-base-default-note">系统默认地址，可修改</div>
          </div>
          <div class="form-row" id="group-key-row">
            <label id="group-key-label">Ark API Key</label>
            <div class="input-with-btn">
              <input id="group-key" type="password" value="${Utils.escapeHtml(this.groupKeyValue(g) || '')}" placeholder="${groupKeyConfigured ? '已配置，留空保持不变' : 'sk-xxxx'}">
              <button type="button" id="group-key-toggle">显示</button>
            </div>
            ${groupKeyConfigured ? '<div class="form-hint">已保存上游 API Key；留空保持不变，填写新值才会替换。</div>' : ''}
          </div>
        </section>
        ${this.renderGroupRiskAlert(groupRisk)}
        <details class="form-card advanced-config" id="group-advanced-card">
          <summary>高级配置</summary>
          <div class="advanced-config-body">
          <div class="form-row hidden" id="group-cooldown-row">
            <label>固定冷却分钟</label>
            <input id="group-cooldown" type="number" min="1" max="1440" step="1" value="${g?.auto_model_cooldown_minutes ?? 5}">
            <div class="form-hint">仅固定冷却策略生效，范围为 1 到 1440 分钟。</div>
          </div>
          <div class="form-row" id="group-routing-policy-row">
            <label>路由策略</label>
            <select id="group-routing-policy">
              <option value="smart_breaker" ${(g?.routing_policy || 'smart_breaker') === 'smart_breaker' ? 'selected' : ''}>智能熔断</option>
              <option value="fixed_cooldown" ${g?.routing_policy === 'fixed_cooldown' ? 'selected' : ''}>固定冷却</option>
              <option value="sticky_route" ${g?.routing_policy === 'sticky_route' ? 'selected' : ''}>粘性路由</option>
              <option value="cooldown_off" ${g?.routing_policy === 'cooldown_off' ? 'selected' : ''}>关闭自动冷却</option>
            </select>
            <div class="form-hint">策略只影响当前连接组；粘性路由按本地会话选取已符合健康条件的候选，不会绕过手动停用或冷却。</div>
          </div>
          <div class="form-row" id="group-stream-timeout-row">
            <label>流式空闲超时秒</label>
            <input id="group-stream-timeout" type="number" min="0" max="600" step="1" value="${g?.stream_idle_timeout ?? 45}">
          </div>
          <div class="form-row" id="group-waf-row">
            <label class="checkbox">
              <input id="group-waf" type="checkbox" ${g?.waf_compatible ? 'checked' : ''}>
              <span>仅中转站 WAF 兼容</span>
            </label>
          </div>
          <div class="form-row" id="group-concurrency-row">
            <label>请求并发</label>
            <div class="radio-group">
              <label class="radio"><input type="radio" name="group-request-concurrency" value="parallel" ${g?.serial_protection ? '' : 'checked'}><span>允许并发（推荐）</span></label>
              <label class="radio"><input type="radio" name="group-request-concurrency" value="serial" ${g?.serial_protection ? 'checked' : ''}><span>串行保护</span></label>
            </div>
            <div class="form-hint">允许同一模型同时处理多个请求。仅当渠道明确要求串行或并发会触发风控时，才开启串行保护。</div>
          </div>
          <div class="form-row hidden" id="group-waf-client-mode-row">
            <label>WAF 客户端策略</label>
            <select id="group-waf-client-mode">
              <option value="always" ${(g?.waf_client_mode || 'always') === 'always' ? 'selected' : ''}>始终使用 WAF 兼容</option>
              <option value="auto_bypass_codex" ${g?.waf_client_mode === 'auto_bypass_codex' ? 'selected' : ''}>智能兼容（Codex 直连 Header）</option>
            </select>
            <div class="form-hint">智能模式会识别 Codex UA 或 x-codex-* Header；仅跳过 Header 改写，不改请求体或请求并发策略。</div>
          </div>
          <div class="form-row hidden" id="group-waf-policy-row">
            <label>Accept 策略</label>
            <select id="group-waf-policy">
              <option value="default" ${(g?.waf_accept_policy || 'default') === 'default' ? 'selected' : ''}>默认（按请求类型）</option>
              <option value="text_event_stream" ${g?.waf_accept_policy === 'text_event_stream' ? 'selected' : ''}>固定 text/event-stream</option>
              <option value="passthrough" ${g?.waf_accept_policy === 'passthrough' ? 'selected' : ''}>passthrough（透传入站 Accept）</option>
            </select>
            <div class="form-hint">仅在 WAF 兼容开启时生效；流式请求默认使用 text/event-stream，passthrough 仅用于 debug 对照。</div>
          </div>
          <div class="form-row">
            <label>自动路由模型名</label>
            <input id="group-auto-model-name" value="${Utils.escapeHtml(g?.auto_model_name || '')}" placeholder="lin-router-auto">
            <div class="form-hint">留空则使用 lin-router-auto；只影响当前连接组的自动调度模型名。</div>
          </div>
          </div>
        </details>
        <section class="form-card">
          <h3>其他</h3>
          ${g ? `<div class="form-row">
            <label>本地路由 Key</label>
            <div class="input-with-btn">
              <input id="group-route-key" value="${Utils.escapeHtml(g?.route_key || '')}" readonly>
              <button type="button" id="group-copy-route-key" class="btn-secondary btn-sm" title="复制路由 Key">复制</button>
            </div>
          </div><div class="form-hint group-route-key-hint">客户端使用此 Key 访问本机 Lin Router，不是上游 API Key。</div>` : '<div class="form-hint">保存连接组后会生成本地路由 Key。</div>'}
          <div class="form-row">
            <label>模式说明</label>
            <div class="form-hint" id="group-mode-hint"></div>
          </div>
          <div class="form-actions form-actions-split">
            <div class="form-actions-left">
              <button type="submit" class="btn-primary">${isDraft ? '保存连接组' : '保存更改'}</button>
              ${g ? `<button type="button" id="group-clone" class="btn-secondary">复制组</button>` : ''}
            </div>
            <div class="form-actions-right">
              ${g ? `<button type="button" class="btn-danger" id="group-delete">删除组</button>` : ''}
            </div>
          </div>
        </section>
        ${g ? this.renderGroupWorkflow(g, { isDraft }) : ''}
        ${g && !isDraft ? this.renderSpeedTestCard('group', g.id, '连接组测速', '测速连接组') : ''}
      </form>
    `;
  },

  renderGroupWorkflow(group, { isDraft = false } = {}) {
    const status = isDraft ? ConnectionStatus.draftGroup(group) : ConnectionStatus.group(group);
    const supportsFetch = ['relay', 'proxy'].includes(group.provider_type);
    const actions = {
      needs_completion: '<button type="button" class="btn-primary" data-group-action="focus-required">补全字段</button>',
      draft_ready: '',
      needs_model_completion: `<button type="button" class="btn-primary" data-group-action="edit-model" data-model-id="${status.representative?.id || ''}">编辑模型</button>`,
      saved_no_model: `${supportsFetch ? '<button type="button" class="btn-primary" data-group-action="fetch-models">获取模型</button>' : ''}<button type="button" class="btn-secondary" data-group-action="add-model">手动添加模型</button>`,
      pending_verify: `<button type="button" class="btn-primary" data-group-action="test-model" data-model-id="${status.representative?.id || ''}">测试模型</button><button type="button" class="btn-secondary" data-group-action="add-model">添加模型</button>`,
      ready: `<button type="button" class="btn-primary" data-group-action="copy-client" data-model-id="${status.verifiedModel?.id || status.representative?.id || ''}">复制客户端配置</button><button type="button" class="btn-secondary" data-group-action="test-model" data-model-id="${status.verifiedModel?.id || status.representative?.id || ''}">再次测试</button>`,
      cooldown: `<button type="button" class="btn-primary" data-group-action="test-model" data-model-id="${status.representative?.id || ''}">重新测试</button>`,
      needs_attention: `<button type="button" class="btn-primary" data-group-action="test-model" data-model-id="${status.representative?.id || ''}">查看模型</button>`,
    };
    return `
      <section class="form-card group-workflow-card" id="group-workflow-card">
        <h3>连接状态</h3>
        <div class="group-workflow-line"><strong>状态：</strong><span class="connection-status-badge ${status.code}">${Utils.escapeHtml(status.label)}</span></div>
        <div class="group-workflow-line"><strong>原因：</strong><span>${Utils.escapeHtml(status.reason)}</span></div>
        <div class="group-workflow-line"><strong>影响：</strong><span>${Utils.escapeHtml(status.impact)}</span></div>
        <div class="group-workflow-line"><strong>系统动作：</strong><span>${Utils.escapeHtml(status.systemAction)}</span></div>
        <div class="form-actions group-workflow-actions">${actions[status.code] || ''}</div>
      </section>`;
  },

  modelHealthLabel(state) {
    const labels = {
      normal: '正常',
      observing: '观察中',
      cooling: '冷却中',
      breaker_open: '已熔断',
      half_open_probe: '恢复探测中',
      risk_isolated: '风险隔离',
      breaker_policy_disabled: '熔断保护已关闭',
      manual_disabled: '手动停用',
    };
    return labels[state] || '未知状态';
  },

  healthDeadline(item) {
    if (item?.risk_isolated && Number(item?.risk_until || 0) > 0) return Number(item.risk_until);
    const state = item?.health_state || 'normal';
    const until = state === 'breaker_open' ? item?.breaker_until : item?.cooldown_until;
    return Number(until || 0);
  },

  formatRiskUntil(until) {
    const timestamp = Number(until || 0);
    return timestamp > 0 ? Utils.formatDate(timestamp) : '未知到期时间';
  },

  groupRiskSummary(groupId) {
    if (!groupId) return null;
    const isolatedModels = (Store.state.models || []).filter(model => (
      model.group_id === groupId && model.risk_isolated === true
    ));
    if (!isolatedModels.length) return null;
    return {
      modelCount: isolatedModels.length,
      affectedCount: Math.max(
        isolatedModels.length,
        ...isolatedModels.map(model => Number(model.risk_affected_models || 0)),
      ),
      until: Math.max(...isolatedModels.map(model => Number(model.risk_until || 0))),
    };
  },

  renderGroupRiskAlert(summary) {
    if (!summary) return '';
    return `
      <section class="form-card group-workflow-card" data-group-risk-alert>
        <h3>上游风控保护</h3>
        <div class="group-workflow-line"><strong>状态：</strong><span class="connection-status-badge warning">检测到上游风控拦截</span></div>
        <div class="group-workflow-line"><strong>影响：</strong><span>该连接组内 ${summary.modelCount} 个模型处于隔离；同一上游凭证共影响 ${summary.affectedCount} 个模型。</span></div>
        <div class="group-workflow-line"><strong>系统动作：</strong><span>已隔离至 ${Utils.escapeHtml(this.formatRiskUntil(summary.until))}，流量会转向其他候选。</span></div>
        <div class="group-workflow-line"><strong>建议：</strong><span>不要连续重试；检查中转后台账号状态、渠道权限、频率限制和风控通知。</span></div>
        <div class="form-actions group-workflow-actions"><button type="button" class="btn-secondary" data-group-action="view-risk-diagnosis">查看诊断</button></div>
      </section>`;
  },

  renderModelSection(sel = Store.selected) {
    const m = this._itemWithDraft(sel, sel.type === 'model' ? Store.getModel(sel.id) : null);
    const modelHealthState = m?.disabled_by_user
      ? 'manual_disabled'
      : (m?.derived_status || m?.health_state || 'normal');
    const healthDeadline = this.healthDeadline(m);
    const canRecoverModel = Boolean(
      m
      && !m.disabled_by_user
      && m?.smart_breaker_effective_enabled !== false
      && m?.derived_status !== 'breaker_policy_disabled'
      && ['cooling', 'breaker_open'].includes(modelHealthState)
    );
    const attemptFailures = (m?.attempt_window || []).filter(result => result === 'qualified_failure').length;
    const breakerLevel = Number(m?.breaker_level || 0);
    const riskIsolated = Boolean(m?.risk_isolated || modelHealthState === 'risk_isolated');
    const riskUntil = this.formatRiskUntil(m?.risk_until);
    const groupId = m?.group_id || Store.state.groups?.[0]?.id || '';
    const group = Store.getGroup(groupId);
    const isArk = group?.provider_type === 'ark';
    const isRelay = group?.provider_type === 'relay';
    const isProxy = group?.provider_type === 'proxy';
    const needUpstream = isRelay || isProxy;
    const modelKeyConfigured = Boolean(m?.api_key_configured || m?.api_key);
    return `
      <form class="config-form" id="model-form" data-type="model" data-selected-type="model" data-selected-id="${m?.id || ''}">
        <input type="hidden" id="model-id" value="${m?.id || ''}">
        <div class="form-row model-group-meta">
          <label>连接组</label>
          <select id="model-group">${this.renderGroupOptions(groupId, group?.provider_type)}</select>
        </div>
        <section class="form-card">
          <h3>基础配置</h3>
          <div class="form-row">
            <label>模型名称</label>
            <input id="model-name" value="${Utils.escapeHtml(m?.name || '')}" placeholder="DeepSeek">
          </div>
          ${!isArk ? `
          <div class="form-row" id="model-key-row">
            <label>${isRelay ? '中转站 API Key' : '上游 API Key'}${isRelay ? '<span class="required-mark"> *</span>' : ''}</label>
            <input id="model-key" type="password" value="${Utils.escapeHtml(m?.api_key || '')}" placeholder="${modelKeyConfigured ? '已配置，留空保持不变' : 'sk-xxxx'}" ${isRelay && !modelKeyConfigured ? 'required' : ''}>
            ${modelKeyConfigured ? '<div class="form-hint">已保存上游 API Key；留空保持不变，填写新值才会替换。</div>' : ''}
          </div>
          ` : ''}
          <div class="form-row ${needUpstream ? 'hidden' : ''}" id="model-ep-row">
            <label>上游模型 / EP</label>
            <input id="model-ep" value="${Utils.escapeHtml(m?.ep_id || '')}" placeholder="ep-xxxx / deepseek-chat">
          </div>
          <div class="form-row ${needUpstream ? '' : 'hidden'}" id="model-upstream-row">
            <label>上游模型</label>
            <div class="input-with-btn">
              <div id="model-upstream-wrapper">
                <input id="model-upstream" value="${Utils.escapeHtml(m?.upstream_model || '')}" placeholder="输入或选择上游模型" autocomplete="off">
              </div>
              <button type="button" id="model-fetch">获取</button>
            </div>
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
            <label>健康状态</label>
            <span id="model-health-state">${this.modelHealthLabel(modelHealthState)}</span>
          </div>
          <div class="form-row read-only">
            <label>近 5 次合格失败</label>
            <span id="model-consecutive-failures">${attemptFailures} / 5</span>
          </div>
          <div class="form-row read-only">
            <label>熔断等级</label>
            <span>${breakerLevel ? `第 ${breakerLevel} 档` : '-'}</span>
          </div>
          <div class="form-row read-only">
            <label>${modelHealthState === 'risk_isolated' ? '风险隔离截止' : (modelHealthState === 'breaker_open' ? '熔断截止' : '冷却截止')}</label>
            <span id="model-cooldown-display" data-health-state="${modelHealthState}" data-health-deadline="${healthDeadline}">-</span>
            ${canRecoverModel ? '<button type="button" id="model-recover" class="btn-recover btn-sm">重试恢复</button>' : ''}
          </div>
          <div class="form-row read-only">
            <label>脱敏原因</label>
            <span id="model-health-reason" class="error-text">${Utils.escapeHtml(m?.derived_reason || m?.breaker_reason || m?.cooldown_reason || m?.last_error || '-')}</span>
          </div>
          <div class="form-row read-only">
            <label>最近错误</label>
            <span class="error-text">${Utils.escapeHtml(m?.last_error || '-')}</span>
          </div>
          ${riskIsolated ? `
          <div class="form-row read-only" data-model-risk-alert>
            <label>上游风控保护</label>
            <span class="error-text">检测到上游风控拦截，影响 ${Number(m?.risk_affected_models || 0)} 个同凭证模型；已隔离至 ${Utils.escapeHtml(riskUntil)}，流量会转向其他候选。</span>
            <div class="form-actions">
              <button type="button" id="model-risk-diagnosis" class="btn-secondary btn-sm">查看诊断</button>
              <button type="button" id="model-risk-recover" class="btn-danger btn-sm">我已检查账号，手动恢复</button>
            </div>
          </div>
          ` : ''}
          <div class="form-actions form-actions-split">
            <div class="form-actions-left">
              <button type="submit" class="btn-primary">保存模型</button>
              ${m ? '<button type="button" id="model-test" class="btn-secondary">测试模型</button>' : ''}
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

  renderAggregateSection(sel = Store.selected) {
    const a = this._itemWithDraft(sel, sel.type === 'aggregate' ? Store.getAggregate(sel.id) : null);
    return `
      <form class="config-form" id="aggregate-form" data-type="aggregate" data-selected-type="aggregate" data-selected-id="${a?.id || ''}">
        <input type="hidden" id="aggregate-id" value="${a?.id || ''}">
        <section class="form-card">
          <h3>基础配置</h3>
          <div class="form-row">
            <label>模型名<span class="required-mark"> *</span></label>
            <input id="aggregate-name" value="${Utils.escapeHtml(a?.name || '')}" placeholder="对外暴露的 model id，如 lin-router-gpt-5.5">
          </div>
          ${a ? `
          <div class="form-row">
            <label>聚合模型 Key</label>
            <div class="input-with-btn">
              <input id="aggregate-route-key" value="${Utils.escapeHtml(a.route_key || '')}" readonly>
              <button type="button" id="aggregate-copy-route-key" class="btn-secondary btn-sm">复制</button>
            </div>
            <div class="form-hint">客户端使用：Base URL + 该 Key + 聚合模型名。全局 Key 已停用。</div>
          </div>
          ` : ''}
          <div class="form-row">
            <label>显示名</label>
            <input id="aggregate-display-name" value="${Utils.escapeHtml(a?.display_name || '')}" placeholder="可选，用于界面展示">
          </div>
          <div class="form-row">
            <label>客户端公开模型别名</label>
            <textarea id="aggregate-client-model-aliases" rows="3" placeholder="每行一个，例如：gpt-5.5&#10;gpt-5.6-terra">${Utils.escapeHtml((a?.client_model_aliases || []).join('\n'))}</textarea>
            <div class="form-hint">用于 Codex 等客户端按已知模型名识别能力。命中任一别名仍进入当前聚合策略，不代表固定上游；仅填写已在目标客户端实测可携带目标协议字段的模型名。</div>
          </div>
          <div class="form-row">
            <label class="checkbox">
              <input id="aggregate-enabled" type="checkbox" ${a?.enabled !== false ? 'checked' : ''}>
              <span>启用此聚合路由</span>
            </label>
          </div>
          <div class="form-row" id="aggregate-routing-policy-row">
            <label>路由策略</label>
            <select id="aggregate-routing-policy">
              <option value="smart_breaker" ${(a?.routing_policy || 'smart_breaker') === 'smart_breaker' ? 'selected' : ''}>智能熔断</option>
              <option value="fixed_cooldown" ${a?.routing_policy === 'fixed_cooldown' ? 'selected' : ''}>固定冷却</option>
              <option value="sticky_route" ${a?.routing_policy === 'sticky_route' ? 'selected' : ''}>粘性路由</option>
              <option value="cooldown_off" ${a?.routing_policy === 'cooldown_off' ? 'selected' : ''}>关闭自动冷却</option>
            </select>
            <div class="form-hint">聚合策略只作用于当前成员链；粘性命中失效后会回退既有手动优先级顺序。</div>
          </div>
          <div class="form-row hidden" id="aggregate-cooldown-row">
            <label>固定冷却分钟</label>
            <input id="aggregate-cooldown" type="number" min="1" max="1440" step="1" value="${a?.cooldown_minutes ?? 5}">
            <div class="form-hint">仅固定冷却策略生效，范围为 1 到 1440 分钟。</div>
          </div>
          <div class="form-row">
            <label>调度策略</label>
            <select id="aggregate-strategy">
              <option value="priority" selected>手动优先级</option>
            </select>
            <div class="form-hint">按成员顺序依次尝试；价格信息仅用于模型配置与成本统计，不参与调度排序。</div>
          </div>
          <div class="form-actions form-actions-split">
            <div class="form-actions-left">
              <button type="submit" class="btn-primary">保存聚合模型</button>
            </div>
            <div class="form-actions-right">
              ${a ? `<button type="button" id="aggregate-delete" class="btn-danger">删除</button>` : ''}
            </div>
          </div>
        </section>
        ${a ? this.renderAggregateMembers(a) : ''}
        ${a ? this.renderSpeedTestCard('aggregate', a.id, '聚合测速', '测速聚合') : ''}
        ${a ? this.renderAggregateGainBoard(a) : ''}
      </form>
    `;
  },

  renderSpeedTestCard(type, id, title, buttonText) {
    return `
      <section class="form-card speed-test-card" data-speed-test-type="${type}" data-speed-test-id="${Utils.escapeHtml(id)}">
        <div class="speed-test-header">
          <div>
            <h3>${title}</h3>
            <div class="form-hint">健康检查测速不写入正式日志、统计或模型健康状态。</div>
          </div>
          <button type="button" class="btn-secondary" id="speed-test-${type}-button" data-speed-test-type="${type}" data-speed-test-id="${Utils.escapeHtml(id)}">${buttonText}</button>
        </div>
        <div id="speed-test-${type}-result" class="speed-test-result" aria-live="polite">
          <div class="form-hint">尚未测速。</div>
        </div>
      </section>
    `;
  },

  async runSpeedTest(type, id) {
    const button = document.getElementById(`speed-test-${type}-button`);
    const result = document.getElementById(`speed-test-${type}-result`);
    if (!id || !button || !result || button.disabled) return;
    button.disabled = true;
    button.textContent = '测速中…';
    result.innerHTML = '<div class="speed-test-running"><span class="speed-test-spinner"></span>正在执行健康检查测速，请稍候…</div>';
    try {
      const payload = type === 'group' ? await API.speedTestGroup(id) : await API.speedTestAggregate(id);
      result.innerHTML = this.renderSpeedTestResult(payload);
    } catch (err) {
      result.innerHTML = this.renderSpeedTestResult({
        ok: false,
        code: err.code || (err.status === 429 ? 'speed_test_rate_limited' : 'speed_test_error'),
        status: err.status,
        message: err.message || '测速请求失败',
      });
    } finally {
      button.disabled = false;
      button.textContent = type === 'group' ? '再次测速连接组' : '再次测速聚合';
    }
  },

  renderSpeedTestResult(payload) {
    const escape = value => Utils.escapeHtml(value == null ? '' : String(value));
    const reasonText = {
      speed_test_running: '该对象正在测速，请等待本次完成。',
      speed_test_rate_limited: '刚完成测速，请稍后再试。',
      missing_upstream_api_key: '缺少上游 API Key。',
      serial_protection_wait_timeout: '串行保护候选正忙，未判定为上游故障。',
      network: '连接或等待上游响应超时，请稍后重试。',
    };
    if (!payload || payload.code === 'speed_test_running') {
      return '<div class="speed-test-running">正在测速，请稍候…</div>';
    }
    if (payload.code && !payload.results) {
      const message = reasonText[payload.code] || payload.message || '测速请求失败，请稍后重试。';
      return `<div class="speed-test-error"><strong>测速失败</strong><span>${escape(message)}</span><small>${escape(payload.code)}${payload.status ? ` · HTTP ${escape(payload.status)}` : ''}</small></div>`;
    }
    const results = Array.isArray(payload.results) ? payload.results : [];
    const statusText = payload.ok ? 'ok' : 'failure';
    const summary = payload.aggregate
      ? `命中聚合：${escape(payload.aggregate)} · attempts ${escape(payload.attempts || 0)} · fallback ${payload.fallback ? '是' : '否'}`
      : `完成 ${escape(payload.completed || results.length)} · ok ${escape(payload.success || 0)} · failure ${escape(payload.failure || 0)}`;
    const resultRows = results.length ? results.map(item => {
      const ok = Boolean(item.ok);
      const message = reasonText[item.reason] || item.message || (ok ? '最小探测成功。' : '最小探测未通过，请检查上游服务状态。');
      return `<div class="speed-test-row ${ok ? 'is-ok' : 'is-failure'}">
        <span class="speed-test-state">${ok ? 'ok' : 'failure'}</span>
        <span class="speed-test-target">模型：${escape(item.model || '-')} · 连接组：${escape(item.group || '-')}</span>
        <span class="speed-test-time">${escape(item.total_ms == null ? '-' : item.total_ms)} ms</span>
        <span class="speed-test-message">${escape(message)}</span>
      </div>`;
    }).join('') : '<div class="form-hint">没有可测速的候选。</div>';
    return `<div class="speed-test-summary ${payload.ok ? 'is-ok' : 'is-failure'}">
      <div><strong>${statusText}</strong><span>${escape(payload.message || '')}</span></div>
      <div class="speed-test-meta">${summary} · total_ms ${escape(payload.total_ms == null ? '-' : payload.total_ms)}</div>
    </div>${resultRows}`;
  },

  renderAggregateGainBoard(a) {
    return `
      <section class="form-card aggregate-gain-card" data-aggregate-stats-id="${a.id}">
        <div class="aggregate-members-header">
          <div>
            <h3>调度收益看板</h3>
            <div class="form-hint">按 request_id 聚合真实请求，配置型 skip 不计入请求总数。</div>
          </div>
          <select id="aggregate-stats-limit" class="btn-sm">
            <option value="50">最近 50 条</option>
            <option value="100" selected>最近 100 条</option>
            <option value="500">最近 500 条</option>
          </select>
        </div>
        <div id="aggregate-stats-body" class="aggregate-stats-grid">
          <div class="form-hint">加载调度收益数据中…</div>
        </div>
      </section>
    `;
  },

  async refreshAggregateStats() {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    const body = document.getElementById('aggregate-stats-body');
    if (!aggregateId || !body) return;
    const limit = Number(document.getElementById('aggregate-stats-limit')?.value || 100);
    try {
      const stats = await API.getAggregateStats(aggregateId, limit);
      body.innerHTML = this.renderAggregateStats(stats);
    } catch (err) {
      body.innerHTML = `<div class="form-hint">收益数据加载失败：${Utils.escapeHtml(err.message)}</div>`;
    }
  },

  renderAggregateStats(stats) {
    if (!stats || !stats.ok || !stats.request_count) {
      return '<div class="form-hint">暂无数据：还没有可统计的真实聚合请求。</div>';
    }
    const pct = v => v == null ? '暂无数据' : `${(Number(v) * 100).toFixed(1)}%`;
    const ms = v => v == null ? '暂无数据' : `${Math.round(Number(v))} ms`;
    const num = v => v == null ? '暂无数据' : String(v);
    const cards = [
      ['请求总数', num(stats.request_count), '不含配置型 skip'],
      ['成功率', pct(stats.success_rate), `${stats.success_count || 0} 次成功`],
      ['fallback 成功', num(stats.fallback_success_count), '首选失败/忙后仍成功'],
      ['首选命中率', pct(stats.first_choice_success_rate), 'attempt=1 成功占比'],
      ['cooldown 跳过', num(stats.cooldown_skip_count), '避免等待不健康成员'],
      ['候选忙切换', num(stats.busy_switch_count), '大上下文并发占用'],
      ['cache 命中率', pct(stats.cache_hit_rate), `${stats.cached_tokens || 0} / ${stats.prompt_tokens || 0}`],
      ['平均首文本', ms(stats.avg_first_content_delta_ms), '首个真实文本 delta'],
      ['平均首完整帧', ms(stats.avg_first_complete_frame_ms), '完整 SSE frame 到达'],
    ];
    const risk = (stats.high_risk_members || []).length
      ? `<div class="aggregate-risk-list"><strong>高风险成员</strong>${stats.high_risk_members.map(item => `<div>${Utils.escapeHtml(item.model || item.member_id)}：timeout ${item.timeout_count || 0} / WAF ${item.waf_blocked_count || 0} / 失败 ${item.failure_count || 0}</div>`).join('')}</div>`
      : '<div class="form-hint">暂无高风险成员。</div>';
    return cards.map(([label, value, hint]) => `
      <div class="aggregate-stat-card"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>
    `).join('') + risk;
  },

  /**
   * 保存当前聚合成员表的瞬态筛选与选择状态；切换聚合时不能复用旧成员 ID。
   */
  getAggregateMemberUiState(aggregateId) {
    if (!this._aggregateMemberUi || this._aggregateMemberUi.aggregateId !== aggregateId) {
      this._aggregateMemberUi = {
        aggregateId,
        selectedIds: new Set(),
        filters: {
          groupId: '',
          status: 'all',
          query: '',
        },
        busy: false,
      };
    }
    return this._aggregateMemberUi;
  },

  aggregateMemberFilterStatus(member, model) {
    if (member.enabled === false) return 'manual_disabled';
    const now = Date.now();
    if (
      !model
      || model.usable === false
      || Boolean(member.last_error)
      || (member.cooldown_until && member.cooldown_until * 1000 > now)
      || (model.cooldown_until && model.cooldown_until * 1000 > now)
    ) {
      return 'unavailable';
    }
    return 'normal';
  },

  getFilteredAggregateMembers(aggregateId) {
    const state = this.getAggregateMemberUiState(aggregateId);
    const query = String(state.filters.query || '').trim().toLowerCase();
    return Store.getAggregateMembers(aggregateId).filter(member => {
      const model = Store.getModel(member.model_id);
      if (state.filters.groupId && member.group_id !== state.filters.groupId) return false;
      if (
        state.filters.status !== 'all'
        && this.aggregateMemberFilterStatus(member, model) !== state.filters.status
      ) {
        return false;
      }
      if (!query) return true;
      const searchText = [model?.name, model?.upstream_model, model?.ep_id]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return searchText.includes(query);
    });
  },

  getSelectedAggregateMemberIds(aggregateId) {
    const state = this.getAggregateMemberUiState(aggregateId);
    const currentIds = new Set(Store.getAggregateMembers(aggregateId).map(member => member.id));
    state.selectedIds.forEach(memberId => {
      if (!currentIds.has(memberId)) state.selectedIds.delete(memberId);
    });
    return [...state.selectedIds];
  },

  clearAggregateMemberSelection(aggregateId) {
    this.getAggregateMemberUiState(aggregateId).selectedIds.clear();
    this.updateAggregateMemberSelectionControls();
  },

  setAggregateMemberBulkBusy(aggregateId, busy) {
    this.getAggregateMemberUiState(aggregateId).busy = busy;
    this.updateAggregateMemberSelectionControls();
  },

  onAggregateMemberFiltersChanged(panel) {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    if (!aggregateId) return;
    const state = this.getAggregateMemberUiState(aggregateId);
    state.filters = {
      groupId: panel.querySelector('#aggregate-member-filter-group')?.value || '',
      status: panel.querySelector('#aggregate-member-filter-status')?.value || 'all',
      query: panel.querySelector('#aggregate-member-filter-query')?.value || '',
    };
    // 筛选范围变化后不能保留隐藏成员，避免用户误以为操作只作用于当前可见项。
    state.selectedIds.clear();
    this.applyAggregateMemberFilters(panel);
  },

  applyAggregateMemberFilters(panel) {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    if (!aggregateId) return;
    const visibleIds = new Set(this.getFilteredAggregateMembers(aggregateId).map(member => member.id));
    panel.querySelectorAll('tr[data-member-id]').forEach(row => {
      row.hidden = !visibleIds.has(row.dataset.memberId);
    });
    this.updateAggregateMemberSelectionControls(panel);
  },

  onAggregateMemberSelectionChanged(memberId, checked) {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    if (!aggregateId || !memberId) return;
    const state = this.getAggregateMemberUiState(aggregateId);
    if (checked) state.selectedIds.add(memberId);
    else state.selectedIds.delete(memberId);
    this.updateAggregateMemberSelectionControls();
  },

  onAggregateMemberSelectAllChanged(checked) {
    const aggregateId = document.getElementById('aggregate-id')?.value;
    if (!aggregateId) return;
    const state = this.getAggregateMemberUiState(aggregateId);
    this.getFilteredAggregateMembers(aggregateId).forEach(member => {
      if (checked) state.selectedIds.add(member.id);
      else state.selectedIds.delete(member.id);
    });
    this.updateAggregateMemberSelectionControls();
  },

  updateAggregateMemberSelectionControls(panel = null) {
    const root = panel || (typeof document === 'undefined' ? null : document.getElementById('panel-config'));
    const aggregateId = typeof document === 'undefined' ? '' : document.getElementById('aggregate-id')?.value;
    if (!root || !aggregateId) return;
    const state = this.getAggregateMemberUiState(aggregateId);
    const selectedIds = this.getSelectedAggregateMemberIds(aggregateId);
    const visibleMembers = this.getFilteredAggregateMembers(aggregateId);
    const visibleIds = new Set(visibleMembers.map(member => member.id));
    const visibleSelectedCount = selectedIds.filter(memberId => visibleIds.has(memberId)).length;
    const selectAll = root.querySelector('#aggregate-member-select-all');
    if (selectAll) {
      selectAll.checked = visibleMembers.length > 0 && visibleSelectedCount === visibleMembers.length;
      selectAll.indeterminate = visibleSelectedCount > 0 && visibleSelectedCount < visibleMembers.length;
      selectAll.disabled = state.busy || visibleMembers.length === 0;
    }
    root.querySelectorAll('.aggregate-member-select').forEach(input => {
      input.checked = state.selectedIds.has(input.dataset.memberId);
      input.disabled = state.busy;
    });
    const toolbar = root.querySelector('#aggregate-member-bulk-toolbar');
    if (toolbar) {
      toolbar.classList.toggle('hidden', selectedIds.length === 0);
      toolbar.classList.toggle('is-busy', state.busy);
      const count = toolbar.querySelector('[data-aggregate-selected-count]');
      if (count) count.textContent = `已选 ${selectedIds.length} 个`;
      toolbar.querySelectorAll('button[data-aggregate-bulk-action]').forEach(button => {
        button.disabled = state.busy || selectedIds.length === 0;
      });
    }
  },

  renderAggregateMembers(a) {
    const members = Store.getAggregateMembers(a.id);
    const state = this.getAggregateMemberUiState(a.id);
    const selectedIds = new Set(this.getSelectedAggregateMemberIds(a.id));
    const memberGroups = [...new Set(members.map(member => member.group_id))]
      .map(groupId => Store.getGroup(groupId))
      .filter(Boolean);
    const groupOptions = memberGroups.map(group => `
      <option value="${Utils.escapeHtml(group.id)}" ${state.filters.groupId === group.id ? 'selected' : ''}>${Utils.escapeHtml(group.name)}</option>
    `).join('');
    return `
      <section class="form-card aggregate-members-card">
        <div class="aggregate-members-header">
          <h3>聚合成员</h3>
          <div class="aggregate-members-header-actions">
            <button type="button" id="aggregate-add-members" class="btn-secondary btn-sm">添加成员</button>
          </div>
        </div>
        <div class="aggregate-status-note">成员状态不等于底层真实模型状态：手动停用只影响聚合成员；自动冷却表示上游健康失败；底层停用需要到真实模型配置中恢复。价格组仅展示模型配置，不参与调度排序。</div>
        ${members.length ? `
        <div class="aggregate-member-filters" aria-label="聚合成员筛选">
          <label>连接组
            <select id="aggregate-member-filter-group" data-transient-control="true">
              <option value="">全部连接组</option>
              ${groupOptions}
            </select>
          </label>
          <label>状态
            <select id="aggregate-member-filter-status" data-transient-control="true">
              <option value="all" ${state.filters.status === 'all' ? 'selected' : ''}>全部状态</option>
              <option value="normal" ${state.filters.status === 'normal' ? 'selected' : ''}>正常</option>
              <option value="manual_disabled" ${state.filters.status === 'manual_disabled' ? 'selected' : ''}>手动停用</option>
              <option value="unavailable" ${state.filters.status === 'unavailable' ? 'selected' : ''}>冷却或底层不可用</option>
            </select>
          </label>
          <label class="aggregate-member-filter-query">模型 / 上游模型
            <input id="aggregate-member-filter-query" data-transient-control="true" value="${Utils.escapeHtml(state.filters.query)}" placeholder="输入关键词筛选">
          </label>
        </div>
        <div id="aggregate-member-bulk-toolbar" class="aggregate-member-bulk-toolbar ${selectedIds.size ? '' : 'hidden'}" aria-live="polite">
          <strong data-aggregate-selected-count>已选 ${selectedIds.size} 个</strong>
          <button type="button" class="btn-secondary btn-sm" data-aggregate-bulk-action="enable">启用</button>
          <button type="button" class="btn-secondary btn-sm" data-aggregate-bulk-action="disable">停用</button>
          <button type="button" class="btn-danger btn-sm" data-aggregate-bulk-action="delete">删除</button>
          <button type="button" class="btn-secondary btn-sm" data-aggregate-bulk-action="clear">取消选择</button>
        </div>
        <div class="aggregate-members-table-wrap">
          <table class="aggregate-members-table">
            <thead>
              <tr>
                <th class="aggregate-member-selection-column">
                  <input id="aggregate-member-select-all" data-transient-control="true" type="checkbox" aria-label="全选当前筛选结果" title="全选当前筛选结果">
                  <span>顺序</span>
                </th>
                <th>连接组</th>
                <th>模型</th>
                <th>上游模型</th>
                <th class="price-group-col">价格组</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              ${members.map((m, idx) => this.renderAggregateMemberRow(m, idx, members.length)).join('')}
            </tbody>
          </table>
        </div>
        ` : '<div class="form-hint">暂无成员，点击右上角添加。</div>'}
      </section>
    `;
  },

  renderAggregateMemberRow(member, idx, total) {
    const group = Store.getGroup(member.group_id);
    const model = Store.getModel(member.model_id);
    const status = this.aggregateMemberStatus(member, model);
    const isCooling = member.cooldown_until && member.cooldown_until * 1000 > Date.now();
    const underlyingDisabled = !model || model.usable === false || (model.cooldown_until && model.cooldown_until * 1000 > Date.now());
    const warningBadge = underlyingDisabled
      ? '<span class="pill warning" title="底层真实模型不可用或处于冷却">底层不可用</span>'
      : '';
    const memberHealthState = member.derived_status || member.health_state || (isCooling ? 'cooling' : 'normal');
    const canRecoverMember = member.enabled !== false
      && member.smart_breaker_effective_enabled !== false
      && member.derived_status !== 'breaker_policy_disabled'
      && ['cooling', 'breaker_open'].includes(memberHealthState);
    const recoverBtn = canRecoverMember
      ? `<button type="button" class="btn-recover btn-sm" data-action="recover" data-member-id="${member.id}">重试恢复</button>`
      : '';
    const toggleBtn = member.enabled === false
      ? `<button type="button" class="btn-secondary btn-sm" data-action="enable" data-member-id="${member.id}">启用</button>`
      : `<button type="button" class="btn-secondary btn-sm" data-action="disable" data-member-id="${member.id}">停用</button>`;
    const selected = this.getAggregateMemberUiState(member.aggregate_id).selectedIds.has(member.id);
    const memberLabel = Utils.escapeHtml(model?.name || member.id);
    return `
      <tr data-member-id="${member.id}">
        <td class="tiny aggregate-member-selection-cell">
          <input type="checkbox" class="aggregate-member-select" data-transient-control="true" data-member-id="${member.id}" aria-label="选择成员 ${memberLabel}" ${selected ? 'checked' : ''}>
          <span class="aggregate-drag-handle" draggable="true" title="拖拽调整顺序" aria-label="拖拽调整顺序">⠿</span>
          <span>${idx + 1}</span>
        </td>
        <td class="truncate-cell" title="${Utils.escapeHtml(group?.name || '-')}">${Utils.escapeHtml(group?.name || '-')}${warningBadge}</td>
        <td class="truncate-cell" title="${Utils.escapeHtml(model?.name || '-')}">${Utils.escapeHtml(model?.name || '-')}</td>
        <td class="truncate-cell" title="${Utils.escapeHtml(model?.upstream_model || model?.ep_id || '-')}">${Utils.escapeHtml(model?.upstream_model || model?.ep_id || '-')}</td>
        <td class="price-group-col">${this.renderAggregatePriceGroup(model)}</td>
        <td class="tiny" data-member-status-cell="${member.id}"><span data-aggregate-member-status="${member.id}" class="pill ${status.class}" title="${Utils.escapeHtml(status.title)}">${status.text}</span></td>
        <td class="aggregate-member-actions" data-member-actions="${member.id}">
          <div class="aggregate-member-action-buttons">
            ${toggleBtn}
            ${recoverBtn}
            <button type="button" class="btn-icon" data-action="up" data-member-id="${member.id}" ${idx === 0 ? 'disabled' : ''} title="上移">↑</button>
            <button type="button" class="btn-icon" data-action="down" data-member-id="${member.id}" ${idx === total - 1 ? 'disabled' : ''} title="下移">↓</button>
            <button type="button" class="btn-icon btn-danger" data-action="delete" data-member-id="${member.id}" title="删除">×</button>
          </div>
        </td>
      </tr>
    `;
  },

  renderAggregatePriceGroup(model) {
    if (!model) return '<span class="aggregate-price-state is-missing">底层模型不存在</span>';
    const priceGroup = String(model.price_group || '').trim();
    if (!priceGroup) return '<span class="aggregate-price-state">未设置</span>';
    return `<span class="aggregate-price-group" title="${Utils.escapeHtml(priceGroup)}">${Utils.escapeHtml(priceGroup)}</span>`;
  },

  aggregateMemberStatus(member, model) {
    const derivedMap = {
      manual_disabled: { class: 'warning', text: '已停用', title: member.derived_reason || '该聚合成员已手动停用，不参与调度' },
      observing: { class: 'warning', text: '观察中', title: member.derived_reason || '聚合成员正在观察连续失败' },
      cooling: { class: 'cooldown', text: '冷却中', title: member.derived_reason || member.cooldown_reason || '聚合成员正在冷却' },
      breaker_open: { class: 'danger', text: '已熔断', title: member.derived_reason || '聚合成员已触发智能熔断' },
      half_open_probe: { class: 'warning', text: '恢复探测中', title: member.derived_reason || '聚合成员正在执行唯一恢复探测' },
      risk_isolated: { class: 'danger', text: '风险隔离', title: member.derived_reason || '检测到上游风控拦截，当前凭证已暂停自动请求' },
      breaker_policy_disabled: { class: 'warning', text: '熔断保护已关闭', title: member.derived_reason || '当前范围已关闭智能熔断保护' },
      underlying_model_disabled: { class: 'warning', text: '底层模型已停用', title: member.derived_reason || '请先启用底层真实模型' },
      underlying_model_observing: { class: 'warning', text: '底层观察中', title: member.derived_reason || '底层真实模型正在观察连续失败' },
      underlying_model_cooling: { class: 'cooldown', text: '底层模型冷却中', title: member.derived_reason || '底层真实模型正在冷却' },
      underlying_model_breaker_open: { class: 'danger', text: '底层已熔断', title: `${member.derived_reason || '底层真实模型已触发智能熔断'}；请到真实模型配置中重试恢复。` },
      underlying_model_half_open_probe: { class: 'warning', text: '底层恢复探测中', title: member.derived_reason || '底层真实模型正在执行唯一恢复探测' },
      config_error: { class: 'danger', text: '配置异常', title: member.derived_reason || '底层连接组或模型缺失' },
      warning: { class: 'warning', text: '最近错误', title: member.derived_reason || member.last_error || '最近发生错误' },
      healthy: { class: 'success', text: '正常', title: member.derived_reason || '该成员可参与聚合调度' },
    };
    if (member.enabled === false) return { class: 'warning', text: '已停用', title: '该聚合成员已手动停用，不参与调度' };
    if (member.derived_status === 'breaker_policy_disabled') return derivedMap.breaker_policy_disabled;
    if (member.cooldown_until && member.cooldown_until * 1000 > Date.now()) {
      const remainSec = Math.max(0, Math.ceil((member.cooldown_until * 1000 - Date.now()) / 1000));
      const mm = Math.floor(remainSec / 60).toString().padStart(2, '0');
      const ss = (remainSec % 60).toString().padStart(2, '0');
      return { class: 'cooldown', text: `冷却中（剩 ${mm}:${ss}）`, title: member.cooldown_reason || '该聚合成员因上游健康失败进入短期冷却' };
    }
    if (model?.cooldown_until && model.cooldown_until * 1000 > Date.now()) {
      const remainSec = Math.max(0, Math.ceil((model.cooldown_until * 1000 - Date.now()) / 1000));
      const mm = Math.floor(remainSec / 60).toString().padStart(2, '0');
      const ss = (remainSec % 60).toString().padStart(2, '0');
      return { class: 'cooldown', text: `底层冷却中（剩 ${mm}:${ss}）`, title: model.cooldown_reason || '底层真实模型正在冷却' };
    }
    if (member.derived_status && derivedMap[member.derived_status]) return derivedMap[member.derived_status];
    if (!model) return { class: 'danger', text: '底层模型不存在', title: '底层真实模型已删除或配置异常' };
    if (model.usable === false) return { class: 'warning', text: '底层模型已停用', title: '请先启用底层真实模型' };
    if (member.last_error) return { class: 'warning', text: '最近错误', title: member.last_error };
    return { class: 'success', text: '正常', title: '该成员可参与聚合调度' };
  },

  renderGroupSide() {
    const hasSavedGroup = Store.selected.type === 'group' && Boolean(Store.selected.id);
    return `
      ${hasSavedGroup ? this.renderBatchImport() : ''}
      ${this.renderConfigTools()}
    `;
  },

  renderBatchImport() {
    const sel = Store.selected;
    const group = sel.type === 'group' ? Store.getGroup(sel.id) : null;
    const provider = group?.provider_type || 'ark';
    const isRelay = provider === 'relay';
    return `
      <section class="form-card batch-import-card">
        <div class="batch-import-header">
          <h3>批量添加模型</h3>
          <button type="button" id="group-add-model" class="btn-secondary batch-add-one-btn" title="添加单个模型">+ 单个添加</button>
        </div>
        <div class="batch-import-body">
          <div class="batch-import-main">
            <div class="batch-models-field">
              <label for="batch-models">模型列表</label>
              <textarea id="batch-models" class="batch-models-textarea" placeholder="${Utils.escapeHtml(this._batchPlaceholder(provider))}"></textarea>
            </div>
            <details class="batch-example">
              <summary>查看格式示例</summary>
              <pre>${this._batchExample(provider)}</pre>
            </details>
          </div>
          <div class="batch-import-options">
            <div class="batch-option-grid">
              <div class="batch-option">
                <label for="batch-format" title="导入格式">导入格式</label>
                <select id="batch-format">
                  <option value="lines">每行一个模型名</option>
                  <option value="json">JSON 数组</option>
                  <option value="models_response">/v1/models 响应</option>
                </select>
              </div>
              <div class="batch-option batch-option-checkbox">
                <label class="checkbox" title="导入后默认可用">
                  <input id="batch-usable" type="checkbox" checked>
                  <span>导入后默认可用</span>
                </label>
              </div>
              ${isRelay ? `
              <div class="batch-option">
                <label for="batch-api-key" title="批量 API Key">批量 API Key</label>
                <input id="batch-api-key" type="password" placeholder="sk-xxxx">
              </div>
              <div class="batch-option">
                <label for="batch-price-group" title="批量价格分组">批量价格分组</label>
                <input id="batch-price-group" placeholder="cheap / standard">
              </div>
              ` : ''}
              <div class="batch-option">
                <label for="batch-price-input" title="输入单价（元 / 千 Token）">输入单价（元 / 千 Token）</label>
                <input id="batch-price-input" type="number" step="0.0001" min="0" placeholder="可选">
              </div>
              <div class="batch-option">
                <label for="batch-price-output" title="输出单价（元 / 千 Token）">输出单价（元 / 千 Token）</label>
                <input id="batch-price-output" type="number" step="0.0001" min="0" placeholder="可选">
              </div>
            </div>
            <div class="batch-import-actions">
              <button type="button" id="batch-import" class="btn-primary">预览导入</button>
            </div>
          </div>
        </div>
      </section>
    `;
  },

  _batchPlaceholder(provider) {
    if (provider === 'ark') return '每行一个 EP ID\\nep-xxxx\\nep-yyyy';
    if (provider === 'relay') return '每行一个上游模型名\\ngpt-5.5\\nclaude-4';
    return '每行一个模型名\\ngpt-4.1\\nclaude-opus-4';
  },

  _batchExample(provider) {
    if (provider === 'ark') {
      return '[\n  {&quot;name&quot;: &quot;豆包-pro&quot;, &quot;ep_id&quot;: &quot;ep-xxx&quot;, &quot;price_input&quot;: 0, &quot;price_output&quot;: 0, &quot;usable&quot;: true}\n]';
    }
    if (provider === 'relay') {
      return '[\n  {&quot;name&quot;: &quot;福利组&quot;, &quot;upstream_model&quot;: &quot;gpt-5.5&quot;, &quot;ep_id&quot;: &quot;gpt-5.5&quot;, &quot;api_key&quot;: &quot;sk-xxxx&quot;, &quot;price_group&quot;: &quot;0.065&quot;, &quot;usable&quot;: true}\n]';
    }
    return '[\n  {&quot;name&quot;: &quot;gpt-4.1&quot;, &quot;upstream_model&quot;: &quot;gpt-4.1&quot;, &quot;ep_id&quot;: &quot;gpt-4.1&quot;, &quot;usable&quot;: true}\n]';
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

    // 更新冷却显示（由定时器持续刷新）
    this.updateCooldownDisplay();
  },

  renderUpstreamOptions(groupId, preserveValue = false) {
    const input = document.getElementById('model-upstream');
    const wrapper = document.getElementById('model-upstream-wrapper');
    if (!input || !wrapper) return;
    // 回显当前模型已选中的上游模型（获取新列表时不覆盖，让用户看到完整候选）
    if (!preserveValue) {
      const m = Store.getModel(document.getElementById('model-id')?.value);
      input.value = m?.upstream_model || m?.ep_id || '';
    }
    input.placeholder = '输入或选择上游模型';
    const group = Store.getGroup(groupId);
    const upstreams = group?.upstream_models || [];
    this._upstreamOptions = upstreams.map(u => ({
      value: u.ep_id || u.root || u.name,
      label: u.name || u.ep_id || u.root || '',
    }));
    this._activeUpstreamIndex = -1;
    this._buildUpstreamDropdown(input, wrapper);
  },

  _buildUpstreamDropdown(input, wrapper) {
    const existing = wrapper.querySelector('.upstream-dropdown');
    if (existing) existing.remove();
    if (!this._upstreamOptions || !this._upstreamOptions.length) return;

    const list = document.createElement('div');
    list.className = 'upstream-dropdown';
    this._upstreamOptions.forEach((opt, idx) => {
      const item = document.createElement('div');
      item.className = 'upstream-option';
      item.textContent = opt.label && opt.label !== opt.value ? `${opt.label} (${opt.value})` : opt.value;
      item.dataset.value = opt.value;
      item.dataset.label = opt.label || '';
      item.dataset.index = idx;
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        this._selectUpstreamOption(opt.value);
      });
      list.appendChild(item);
    });
    wrapper.appendChild(list);
    list.style.display = 'none';
  },

  _showUpstreamDropdown() {
    const list = document.querySelector('#model-upstream-wrapper .upstream-dropdown');
    if (list) list.style.display = 'block';
  },

  _hideUpstreamDropdown() {
    const list = document.querySelector('#model-upstream-wrapper .upstream-dropdown');
    if (list) list.style.display = 'none';
    this._activeUpstreamIndex = -1;
    this._updateUpstreamActive();
  },

  _filterUpstreamDropdown(query) {
    const q = (query || '').toLowerCase().trim();
    const items = document.querySelectorAll('#model-upstream-wrapper .upstream-option');
    items.forEach(el => {
      const value = (el.dataset.value || '').toLowerCase();
      const label = (el.dataset.label || '').toLowerCase();
      el.style.display = (!q || value.includes(q) || label.includes(q)) ? 'block' : 'none';
    });
    this._activeUpstreamIndex = -1;
    this._updateUpstreamActive();
  },

  _updateUpstreamActive() {
    const items = [...document.querySelectorAll('#model-upstream-wrapper .upstream-option')]
      .filter(el => el.style.display !== 'none');
    items.forEach(el => el.classList.remove('active'));
    if (this._activeUpstreamIndex >= 0 && this._activeUpstreamIndex < items.length) {
      items[this._activeUpstreamIndex].classList.add('active');
      items[this._activeUpstreamIndex].scrollIntoView({ block: 'nearest' });
    }
  },

  _selectUpstreamOption(value) {
    const input = document.getElementById('model-upstream');
    if (!input) return;
    input.value = value;
    this._hideUpstreamDropdown();
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.focus();
  },

  _onUpstreamOutsideClick(e) {
    if (e.target.closest('#model-upstream-wrapper') || e.target.closest('#model-fetch')) return;
    this._hideUpstreamDropdown();
  },

  _onUpstreamKeydown(e) {
    const list = document.querySelector('#model-upstream-wrapper .upstream-dropdown');
    if (!list || list.style.display === 'none') {
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && this._upstreamOptions?.length) {
        e.preventDefault();
        this._showUpstreamDropdown();
      }
      return;
    }
    const visible = [...list.querySelectorAll('.upstream-option')].filter(el => el.style.display !== 'none');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this._activeUpstreamIndex = Math.min(this._activeUpstreamIndex + 1, visible.length - 1);
      this._updateUpstreamActive();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this._activeUpstreamIndex = Math.max(this._activeUpstreamIndex - 1, -1);
      this._updateUpstreamActive();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (this._activeUpstreamIndex >= 0 && visible[this._activeUpstreamIndex]) {
        this._selectUpstreamOption(visible[this._activeUpstreamIndex].dataset.value);
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      this._hideUpstreamDropdown();
    }
  },

  attachEvents(panel) {
    panel.querySelector('#config-runtime-refresh')?.addEventListener('click', () => this.refreshRuntimeNow());

    // 组表单
    const groupForm = panel.querySelector('#group-form');
    if (groupForm) {
      groupForm.addEventListener('submit', e => this.onGroupSubmit(e));
      panel.querySelector('#speed-test-group-button')?.addEventListener('click', e => this.runSpeedTest(e.currentTarget.dataset.speedTestType, e.currentTarget.dataset.speedTestId));
      panel.querySelector('#group-provider')?.addEventListener('change', () => this.onGroupProviderChange());
      panel.querySelector('#group-waf')?.addEventListener('change', () => { this.syncGroupModeUI(); this.autoSaveGroup(); });
      panel.querySelector('#group-routing-policy')?.addEventListener('change', () => this.onRoutingPolicyChange('group'));
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
      this.bindGroupWorkflowActions(panel);
      ['#group-name', '#group-base', '#group-key'].forEach(selector => {
        panel.querySelector(selector)?.addEventListener('input', () => this.onGroupDraftInput());
      });
      this.bindAutoSave(groupForm);
    }

    // 模型表单
    const modelForm = panel.querySelector('#model-form');
    if (modelForm) {
      modelForm.addEventListener('submit', e => this.onModelSubmit(e));
      panel.querySelector('#model-group')?.addEventListener('change', () => { this.syncModelModeUI(); this.autoSaveModel(); });
      const upstreamInput = panel.querySelector('#model-upstream');
      if (upstreamInput) {
        upstreamInput.addEventListener('focus', () => this._showUpstreamDropdown());
        upstreamInput.addEventListener('input', () => {
          this._showUpstreamDropdown();
          this._filterUpstreamDropdown(upstreamInput.value);
        });
        upstreamInput.addEventListener('blur', () => this._hideUpstreamDropdown());
        upstreamInput.addEventListener('keydown', (e) => this._onUpstreamKeydown(e));
        upstreamInput.addEventListener('change', () => this.autoSaveModel());
      }
      panel.querySelector('#model-delete')?.addEventListener('click', () => this.onModelDelete());
      panel.querySelector('#model-clone')?.addEventListener('click', () => this.onModelClone());
      panel.querySelector('#model-recover')?.addEventListener('click', () => this.onRecoverModel());
      panel.querySelector('#model-risk-recover')?.addEventListener('click', () => this.onReleaseModelRiskIsolation());
      panel.querySelector('#model-risk-diagnosis')?.addEventListener('click', () => this.onOpenRiskDiagnosis());
      panel.querySelector('#model-fetch')?.addEventListener('click', () => this.onFetchUpstream());
      panel.querySelector('#model-test')?.addEventListener('click', () => this.openQuickTest(document.getElementById('model-id')?.value));
      this.bindAutoSave(modelForm);
    }

    // 聚合模型表单
    const aggregateForm = panel.querySelector('#aggregate-form');
    if (aggregateForm) {
      aggregateForm.addEventListener('submit', e => this.onAggregateSubmit(e));
      panel.querySelector('#speed-test-aggregate-button')?.addEventListener('click', e => this.runSpeedTest(e.currentTarget.dataset.speedTestType, e.currentTarget.dataset.speedTestId));
      panel.querySelector('#aggregate-delete')?.addEventListener('click', () => this.onAggregateDelete());
      panel.querySelector('#aggregate-routing-policy')?.addEventListener('change', () => this.onRoutingPolicyChange('aggregate'));
      panel.querySelector('#aggregate-copy-route-key')?.addEventListener('click', () => this.onCopyAggregateRouteKey());
      panel.querySelector('#aggregate-add-members')?.addEventListener('click', () => this.onAddAggregateMembers());
      panel.querySelector('#aggregate-stats-limit')?.addEventListener('change', () => this.refreshAggregateStats());
      this.refreshAggregateStats();
      ['#aggregate-member-filter-group', '#aggregate-member-filter-status'].forEach(selector => {
        panel.querySelector(selector)?.addEventListener('change', () => this.onAggregateMemberFiltersChanged(panel));
      });
      panel.querySelector('#aggregate-member-filter-query')?.addEventListener('input', () => this.onAggregateMemberFiltersChanged(panel));
      panel.querySelector('#aggregate-member-select-all')?.addEventListener('change', event => {
        this.onAggregateMemberSelectAllChanged(event.currentTarget.checked);
      });
      panel.querySelectorAll('.aggregate-member-select').forEach(input => {
        input.addEventListener('change', event => {
          this.onAggregateMemberSelectionChanged(event.currentTarget.dataset.memberId, event.currentTarget.checked);
        });
      });
      panel.querySelectorAll('button[data-aggregate-bulk-action]').forEach(button => {
        button.addEventListener('click', () => this.onAggregateMemberBulkAction(button.dataset.aggregateBulkAction));
      });
      panel.querySelectorAll('.aggregate-member-actions button[data-action]').forEach(el => {
        el.addEventListener('click', () => this.onAggregateMemberAction(el.dataset.action, el.dataset.memberId));
      });
      this.bindAggregateMemberDragAndDrop(panel);
      this.applyAggregateMemberFilters(panel);
      this.bindAutoSave(aggregateForm);
    }

    // 批量导入
    panel.querySelector('#batch-import')?.addEventListener('click', () => this.onBatchImport());

    // 配置导入/导出
    panel.querySelector('#config-export')?.addEventListener('click', () => App.exportConfig());
    panel.querySelector('#config-import')?.addEventListener('click', () => panel.querySelector('#config-import-file')?.click());
    panel.querySelector('#config-import-file')?.addEventListener('change', e => this.onConfigImport(e));
  },

  setSaveStatus(status, message) {
    const el = document.getElementById('save-status');
    if (!el) return;
    el.className = 'save-status';
    if (status === 'draft') {
      el.textContent = '有未保存草稿';
      el.classList.add('draft');
    } else if (status === 'saving') {
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

  // 捕获当前表单中用户已编辑的值，用于重新渲染后恢复

  _captureCurrentDraft() {
    const form = document.querySelector('#panel-config .config-form');
    if (!form) return;
    const selection = {
      type: form.dataset.selectedType || Store.selected.type,
      id: form.dataset.selectedId || null,
    };
    this.captureDraft(form, selection);
  },

  _rememberDraftBaseline(selection, form) {
    if (!form) return;
    const key = ConfigTabForm.draftKey(this, selection);
    this._draftBaselines.set(key, this._captureFormValues(form));
  },

  _draftValues(selection = Store.selected) {
    return this.draftValues(selection);
  },

  _itemWithDraft(selection, item) {
    const values = this._draftValues(selection);
    if (!item || !values) return item;
    if (selection.type === 'group') {
      const provider = values['group-provider'] || item.provider_type;
      const key = values['group-key'] ?? this.groupKeyValue(item);
      const serial = values['__radio:group-request-concurrency'];
      return {
        ...item,
        name: values['group-name'] ?? item.name,
        provider_type: provider,
        base_url: values['group-base'] ?? item.base_url,
        ark_api_key: provider === 'ark' ? key : '',
        api_key: provider === 'proxy' ? key : '',
        auto_model_name: values['group-auto-model-name'] ?? item.auto_model_name,
        auto_model_cooldown_minutes: values['group-cooldown'] ?? item.auto_model_cooldown_minutes,
        stream_idle_timeout: values['group-stream-timeout'] ?? item.stream_idle_timeout,
        waf_compatible: values['group-waf'] ?? item.waf_compatible,
        routing_policy: values['group-routing-policy'] ?? item.routing_policy,
        serial_protection: serial ? serial === 'serial' : item.serial_protection,
        waf_client_mode: values['group-waf-client-mode'] ?? item.waf_client_mode,
        waf_accept_policy: values['group-waf-policy'] ?? item.waf_accept_policy,
      };
    }
    if (selection.type === 'model') {
      const groupId = values['model-group'] || item.group_id;
      const group = Store.getGroup(groupId);
      const upstream = values['model-upstream'] ?? values['model-ep'] ?? item.upstream_model ?? item.ep_id;
      return {
        ...item,
        name: values['model-name'] ?? item.name,
        group_id: groupId,
        ep_id: values['model-ep'] ?? (['relay', 'proxy'].includes(group?.provider_type) ? upstream : item.ep_id),
        upstream_model: values['model-upstream'] ?? item.upstream_model,
        api_key: values['model-key'] ?? item.api_key,
        price_group: values['model-price'] ?? item.price_group,
        price_input: values['model-price-input'] ?? item.price_input,
        price_output: values['model-price-output'] ?? item.price_output,
        usable: values['model-usable'] ?? item.usable,
      };
    }
    if (selection.type === 'aggregate') {
      return {
        ...item,
        name: values['aggregate-name'] ?? item.name,
        display_name: values['aggregate-display-name'] ?? item.display_name,
        client_model_aliases: values['aggregate-client-model-aliases'] !== undefined
          ? String(values['aggregate-client-model-aliases']).split(/[\n,]+/).map(value => value.trim()).filter(Boolean)
          : item.client_model_aliases,
        enabled: values['aggregate-enabled'] ?? item.enabled,
        routing_policy: values['aggregate-routing-policy'] ?? item.routing_policy,
        cooldown_minutes: values['aggregate-cooldown'] ?? item.cooldown_minutes,
        strategy: values['aggregate-strategy'] ?? item.strategy,
      };
    }
    return item;
  },

  isNewGroupDraft(...args) { return ConfigTabForm.isNewGroupDraft(this, ...args); },
  isDefaultRelayBaseUrl(...args) { return ConfigTabForm.isDefaultRelayBaseUrl(this, ...args); },
  groupKeyValue(...args) { return ConfigTabForm.groupKeyValue(this, ...args); },
  groupKeyConfigured(...args) { return ConfigTabForm.groupKeyConfigured(this, ...args); },
  syncUIFromState(...args) { return ConfigTabForm.syncUIFromState(this, ...args); },
  syncAggregateUI(...args) { return ConfigTabForm.syncAggregateUI(this, ...args); },
  updateAggregateCooldownDisplay(...args) { return ConfigTabForm.updateAggregateCooldownDisplay(this, ...args); },
  syncGroupModeUI(...args) { return ConfigTabForm.syncGroupModeUI(this, ...args); },
  updateDefaultRelayBaseUrlHint(...args) { return ConfigTabForm.updateDefaultRelayBaseUrlHint(this, ...args); },
  onGroupProviderChange(...args) { return ConfigTabForm.onGroupProviderChange(this, ...args); },
  groupStateFromForm(...args) { return ConfigTabForm.groupStateFromForm(this, ...args); },
  syncNewGroupDraftFromForm(...args) { return ConfigTabForm.syncNewGroupDraftFromForm(this, ...args); },
  refreshGroupWorkflowFromDraft(...args) { return ConfigTabForm.refreshGroupWorkflowFromDraft(this, ...args); },
  bindGroupWorkflowActions(...args) { return ConfigTabForm.bindGroupWorkflowActions(this, ...args); },
  onGroupDraftInput(...args) { return ConfigTabForm.onGroupDraftInput(this, ...args); },
  captureDraft(...args) { return ConfigTabForm.captureDraft(this, ...args); },
  draftValues(...args) { return ConfigTabForm.draftValues(this, ...args); },
  clearDraft(...args) { return ConfigTabForm.clearDraft(this, ...args); },
  restoreFailedAutoSaveBaseline(...args) { return ConfigTabForm.restoreFailedAutoSaveBaseline(this, ...args); },
  bindAutoSave(...args) { return ConfigTabForm.bindAutoSave(this, ...args); },
  scheduleAutoSave(...args) { return ConfigTabForm.scheduleAutoSave(this, ...args); },
  syncRoutingPolicyUI(...args) { return ConfigTabForm.syncRoutingPolicyUI(this, ...args); },
  _captureFormValues(...args) { return ConfigTabForm._captureFormValues(this, ...args); },
  _restoreFormValues(...args) { return ConfigTabForm._restoreFormValues(this, ...args); },
  clearFieldErrors(...args) { return ConfigTabForm.clearFieldErrors(this, ...args); },
  setFieldError(...args) { return ConfigTabForm.setFieldError(this, ...args); },
  validateGroupForm(...args) { return ConfigTabForm.validateGroupForm(this, ...args); },
  validateModelForm(...args) { return ConfigTabForm.validateModelForm(this, ...args); },
  validateAggregateForm(...args) { return ConfigTabForm.validateAggregateForm(this, ...args); },
  autoSaveGroup(...args) { return ConfigTabForm.autoSaveGroup(this, ...args); },
  autoSaveModel(...args) { return ConfigTabForm.autoSaveModel(this, ...args); },
  autoSaveAggregate(...args) { return ConfigTabForm.autoSaveAggregate(this, ...args); },
  onRuntimeStateUpdate(...args) { return ConfigTabRuntimeView.onRuntimeStateUpdate(this, ...args); },
  patchVisibleRuntimeStatus(...args) { return ConfigTabRuntimeView.patchVisibleRuntimeStatus(this, ...args); },
  refreshRuntimeNow(...args) { return ConfigTabRuntimeView.refreshRuntimeNow(this, ...args); },
  _startCooldownTimer(...args) { return ConfigTabRuntimeView._startCooldownTimer(this, ...args); },
  _stopCooldownTimer(...args) { return ConfigTabRuntimeView._stopCooldownTimer(this, ...args); },
  updateCooldownDisplay(...args) { return ConfigTabRuntimeView.updateCooldownDisplay(this, ...args); },
  onRecoverModel(...args) { return ConfigTabActions.onRecoverModel(this, ...args); },
  onReleaseModelRiskIsolation(...args) { return ConfigTabActions.onReleaseModelRiskIsolation(this, ...args); },
  onOpenRiskDiagnosis(...args) { return ConfigTabActions.onOpenRiskDiagnosis(this, ...args); },
  onGroupWorkflowAction(...args) { return ConfigTabActions.onGroupWorkflowAction(this, ...args); },
  openQuickTest(...args) { return ConfigTabActions.openQuickTest(this, ...args); },
  copyGroupClientConfig(...args) { return ConfigTabActions.copyGroupClientConfig(this, ...args); },
  onRoutingPolicyChange(...args) { return ConfigTabActions.onRoutingPolicyChange(this, ...args); },
  onGroupSubmit(...args) { return ConfigTabActions.onGroupSubmit(this, ...args); },
  onGroupDelete(...args) { return ConfigTabActions.onGroupDelete(this, ...args); },
  onAddModelToGroup(...args) { return ConfigTabActions.onAddModelToGroup(this, ...args); },
  onGroupClone(...args) { return ConfigTabActions.onGroupClone(this, ...args); },
  onModelSubmit(...args) { return ConfigTabActions.onModelSubmit(this, ...args); },
  onModelDelete(...args) { return ConfigTabActions.onModelDelete(this, ...args); },
  onModelClone(...args) { return ConfigTabActions.onModelClone(this, ...args); },
  onAggregateSubmit(...args) { return ConfigTabActions.onAggregateSubmit(this, ...args); },
  onAggregateDelete(...args) { return ConfigTabActions.onAggregateDelete(this, ...args); },
  onAddAggregateMembers(...args) { return ConfigTabActions.onAddAggregateMembers(this, ...args); },
  onAddAggregateMember(...args) { return ConfigTabActions.onAddAggregateMember(this, ...args); },
  onAddAggregateMembersByGroup(...args) { return ConfigTabActions.onAddAggregateMembersByGroup(this, ...args); },
  onAggregateMemberAction(...args) { return ConfigTabActions.onAggregateMemberAction(this, ...args); },
  onAggregateMemberBulkAction(...args) { return ConfigTabActions.onAggregateMemberBulkAction(this, ...args); },
  onBatchUpdateAggregateMembers(...args) { return ConfigTabActions.onBatchUpdateAggregateMembers(this, ...args); },
  onBatchDeleteAggregateMembers(...args) { return ConfigTabActions.onBatchDeleteAggregateMembers(this, ...args); },
  confirmBatchDeleteAggregateMembers(...args) { return ConfigTabActions.confirmBatchDeleteAggregateMembers(this, ...args); },
  bindAggregateMemberDragAndDrop(...args) { return ConfigTabActions.bindAggregateMemberDragAndDrop(this, ...args); },
  onReorderAggregateMembers(...args) { return ConfigTabActions.onReorderAggregateMembers(this, ...args); },
  aggregateChainSummary(...args) { return ConfigTabActions.aggregateChainSummary(this, ...args); },
  confirmAggregateMemberPreview(...args) { return ConfigTabActions.confirmAggregateMemberPreview(this, ...args); },
  reloadAfterAggregateMemberChange(...args) { return ConfigTabActions.reloadAfterAggregateMemberChange(this, ...args); },
  onRecoverAggregateMember(...args) { return ConfigTabActions.onRecoverAggregateMember(this, ...args); },
  onMoveAggregateMember(...args) { return ConfigTabActions.onMoveAggregateMember(this, ...args); },
  onCopyAggregateRouteKey(...args) { return ConfigTabActions.onCopyAggregateRouteKey(this, ...args); },
  onToggleAggregateMember(...args) { return ConfigTabActions.onToggleAggregateMember(this, ...args); },
  onDeleteAggregateMember(...args) { return ConfigTabActions.onDeleteAggregateMember(this, ...args); },
  onFetchUpstream(...args) { return ConfigTabActions.onFetchUpstream(this, ...args); },
  fetchModelsForGroup(...args) { return ConfigTabActions.fetchModelsForGroup(this, ...args); },
  onBatchImport(...args) { return ConfigTabActions.onBatchImport(this, ...args); },
  _showBatchPreview(...args) { return ConfigTabActions._showBatchPreview(this, ...args); },
  onConfigImport(...args) { return ConfigTabActions.onConfigImport(this, ...args); },
  dispose() {
    ConfigTabRuntimeView.dispose(this);
    ConfigTabForm.dispose(this);
  },
};
