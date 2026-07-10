const ConfigTab = {
  onShow() {
    const panel = document.getElementById('panel-config');
    if (!panel) return;
    if (!this._upstreamDropdownBound) {
      document.addEventListener('mousedown', (e) => this._onUpstreamOutsideClick(e));
      this._upstreamDropdownBound = true;
    }
    this.render();
  },

  render() {
    const panel = document.getElementById('panel-config');
    const sel = Store.selected;
    // 清理 pending 的自动保存，避免切换后旧表单的自动保存误写新对象
    clearTimeout(this._autoSaveTimer);
    this._autoSaveTimer = null;
    this.setSaveStatus('');
    // 重新渲染前保留用户正在编辑的表单值，但只在“同一对象重渲染”时恢复，切换对象不恢复旧值
    const oldForm = panel?.querySelector('.config-form');
    const oldSelectedType = oldForm?.dataset.selectedType;
    const oldSelectedId = oldForm?.dataset.selectedId;
    const formValues = this._captureFormValues();
    this._stopCooldownTimer();
    if (!sel.id) {
      panel.innerHTML = this.renderEmptyState();
      this.attachEmptyEvents(panel);
      return;
    }
    const item = sel.type === 'group' ? Store.getGroup(sel.id) : (sel.type === 'model' ? Store.getModel(sel.id) : Store.getAggregate(sel.id));
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
    const sameSelection = oldSelectedType === sel.type && oldSelectedId === sel.id;
    this._restoreFormValues(sameSelection ? formValues : {});
    this.attachEvents(panel);
    this.syncUIFromState();
    this._startCooldownTimer();
  },

  renderEmptyState() {
    return `
      <div class="empty-state">
        <div class="empty-icon">🚀</div>
        <h2>欢迎使用 Lin Router</h2>
        <p class="empty-subtitle">点击左上角 + 新建你的第一个连接组，或导入已有配置</p>
        <div class="empty-actions">
          <button type="button" class="btn-primary" id="empty-new-group">新建连接组</button>
          <button type="button" class="btn-secondary" id="empty-import">导入配置</button>
        </div>
        <p class="empty-hint">客户端请使用连接组 Key（lr-...）或聚合模型 Key（lr-ag-...），旧全局 Key lin-router 已停用</p>
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
    const g = sel.type === 'group' ? Store.getGroup(sel.id) : null;
    const provider = g?.provider_type || 'ark';
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
        <section class="form-card" id="group-advanced-card">
          <h3>高级配置</h3>
          <div class="form-row" id="group-cooldown-row">
            <label>自动冷却分钟</label>
            <input id="group-cooldown" type="number" min="0" step="1" value="${g?.auto_model_cooldown_minutes ?? 5}">
          </div>
          <div class="form-row" id="group-stream-timeout-row">
            <label>流式空闲超时秒</label>
            <input id="group-stream-timeout" type="number" min="0" max="600" step="1" value="${g?.stream_idle_timeout ?? 120}">
          </div>
          <div class="form-row" id="group-waf-row">
            <label class="checkbox">
              <input id="group-waf" type="checkbox" ${g?.waf_compatible ? 'checked' : ''}>
              <span>仅中转站 WAF 兼容</span>
            </label>
          </div>
          <div class="form-row hidden" id="group-waf-policy-row">
            <label>Accept 策略</label>
            <select id="group-waf-policy">
              <option value="default" ${(g?.waf_accept_policy || 'default') === 'default' ? 'selected' : ''}>默认（浏览器 Accept）</option>
              <option value="text_event_stream" ${g?.waf_accept_policy === 'text_event_stream' ? 'selected' : ''}>固定 text/event-stream</option>
              <option value="passthrough" ${g?.waf_accept_policy === 'passthrough' ? 'selected' : ''}>passthrough（透传入站 Accept）</option>
            </select>
            <div class="form-hint">仅在 WAF 兼容开启时生效；passthrough 仅用于 debug 对照。</div>
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
            <label>自动路由模型名</label>
            <input id="group-auto-model-name" value="${Utils.escapeHtml(g?.auto_model_name || '')}" placeholder="lin-router-auto">
            <div class="form-hint">客户端 /v1/models 中显示的自动路由模型 ID；留空则使用 lin-router-auto。</div>
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

  renderModelSection(sel = Store.selected) {
    const m = sel.type === 'model' ? Store.getModel(sel.id) : null;
    const groupId = m?.group_id || Store.state.groups?.[0]?.id || '';
    const group = Store.getGroup(groupId);
    const isArk = group?.provider_type === 'ark';
    const isRelay = group?.provider_type === 'relay';
    const isProxy = group?.provider_type === 'proxy';
    const needUpstream = isRelay || isProxy;
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
            <input id="model-key" type="password" value="${Utils.escapeHtml(m?.api_key || '')}" placeholder="sk-xxxx" ${isRelay ? 'required' : ''}>
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
            <label>冷却截止</label>
            <span id="model-cooldown-display">-</span>
            ${m && m.cooldown_until && m.cooldown_until * 1000 > Date.now() && !m.disabled_by_user ? `<button type="button" id="model-recover" class="btn-recover btn-sm">重试恢复</button>` : ''}
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

  renderAggregateSection(sel = Store.selected) {
    const a = sel.type === 'aggregate' ? Store.getAggregate(sel.id) : null;
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
            <label>描述</label>
            <textarea id="aggregate-description" rows="2" placeholder="可选">${Utils.escapeHtml(a?.description || '')}</textarea>
          </div>
          <div class="form-row">
            <label class="checkbox">
              <input id="aggregate-enabled" type="checkbox" ${a?.enabled !== false ? 'checked' : ''}>
              <span>启用</span>
            </label>
          </div>
          <div class="form-row">
            <label>冷却分钟</label>
            <input id="aggregate-cooldown" type="number" min="0" step="1" value="${a?.cooldown_minutes ?? 5}">
          </div>
          <div class="form-row">
            <label>调度策略</label>
            <select id="aggregate-strategy">
              <option value="priority" ${(a?.strategy || 'priority') === 'priority' ? 'selected' : ''}>手动优先级</option>
              <option value="price_first" ${(a?.strategy || 'priority') === 'price_first' ? 'selected' : ''}>价格优先</option>
            </select>
            <div class="form-hint">当前策略：${(a?.strategy || 'priority') === 'price_first' ? '价格优先，按手动价格从低到高排序，同价按优先级；未填价格排最后。' : '手动优先级，按成员顺序依次尝试；价格仅展示，不参与排序。'}</div>
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
        ${a ? this.renderAggregateGainBoard(a) : ''}
      </form>
    `;
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
      ['平均首包', ms(stats.avg_first_chunk_ms), 'stream_ok 平均耗时'],
    ];
    const risk = (stats.high_risk_members || []).length
      ? `<div class="aggregate-risk-list"><strong>高风险成员</strong>${stats.high_risk_members.map(item => `<div>${Utils.escapeHtml(item.model || item.member_id)}：timeout ${item.timeout_count || 0} / WAF ${item.waf_blocked_count || 0} / 失败 ${item.failure_count || 0}</div>`).join('')}</div>`
      : '<div class="form-hint">暂无高风险成员。</div>';
    return cards.map(([label, value, hint]) => `
      <div class="aggregate-stat-card"><span>${label}</span><strong>${value}</strong><small>${hint}</small></div>
    `).join('') + risk;
  },

  renderAggregateMembers(a) {
    const members = Store.getAggregateMembers(a.id);
    return `
      <section class="form-card aggregate-members-card">
        <div class="aggregate-members-header">
          <h3>聚合成员</h3>
          <button type="button" id="aggregate-add-member" class="btn-secondary btn-sm">+ 添加成员</button>
        </div>
        <div class="aggregate-status-note">成员状态不等于底层真实模型状态：手动停用只影响聚合成员；自动冷却表示上游健康失败；底层停用需要到真实模型配置中恢复。</div>
        ${members.length ? `
        <div class="aggregate-members-table-wrap">
          <table class="aggregate-members-table">
            <thead>
              <tr>
                <th>顺序</th>
                <th>连接组</th>
                <th>模型</th>
                <th>上游模型</th>
                <th>优先级</th>
                <th class="price-col">手动价格</th>
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
    const recoverBtn = isCooling
      ? `<button type="button" class="btn-recover btn-sm" data-action="recover" data-member-id="${member.id}">恢复/启用</button>`
      : '';
    const toggleBtn = member.enabled === false
      ? `<button type="button" class="btn-secondary btn-sm" data-action="enable" data-member-id="${member.id}">启用</button>`
      : `<button type="button" class="btn-secondary btn-sm" data-action="disable" data-member-id="${member.id}">停用</button>`;
    return `
      <tr data-member-id="${member.id}">
        <td class="tiny">${idx + 1}</td>
        <td class="truncate-cell" title="${Utils.escapeHtml(group?.name || '-')}">${Utils.escapeHtml(group?.name || '-')}${warningBadge}</td>
        <td class="truncate-cell" title="${Utils.escapeHtml(model?.name || '-')}">${Utils.escapeHtml(model?.name || '-')}</td>
        <td class="truncate-cell" title="${Utils.escapeHtml(model?.upstream_model || model?.ep_id || '-')}">${Utils.escapeHtml(model?.upstream_model || model?.ep_id || '-')}</td>
        <td class="tiny">${idx + 1}</td>
        <td class="price-col"><input type="number" class="aggregate-member-price" data-member-id="${member.id}" value="${member.manual_price != null ? member.manual_price : ''}" step="0.001" placeholder="继承"></td>
        <td class="tiny" data-member-status-cell="${member.id}"><span class="pill ${status.class}" title="${Utils.escapeHtml(status.title)}">${status.text}</span></td>
        <td class="aggregate-member-actions">
          ${toggleBtn}
          ${recoverBtn}
          <button type="button" class="btn-icon" data-action="up" data-member-id="${member.id}" ${idx === 0 ? 'disabled' : ''} title="上移">↑</button>
          <button type="button" class="btn-icon" data-action="down" data-member-id="${member.id}" ${idx === total - 1 ? 'disabled' : ''} title="下移">↓</button>
          <button type="button" class="btn-icon btn-danger" data-action="delete" data-member-id="${member.id}" title="删除">×</button>
        </td>
      </tr>
    `;
  },

  aggregateMemberStatus(member, model) {
    const derivedMap = {
      manual_disabled: { class: 'warning', text: '已停用', title: member.derived_reason || '该聚合成员已手动停用，不参与调度' },
      cooling: { class: 'cooldown', text: '冷却中', title: member.derived_reason || member.cooldown_reason || '聚合成员正在冷却' },
      underlying_model_disabled: { class: 'warning', text: '底层模型已停用', title: member.derived_reason || '请先启用底层真实模型' },
      underlying_model_cooling: { class: 'cooldown', text: '底层模型冷却中', title: member.derived_reason || '底层真实模型正在冷却' },
      config_error: { class: 'danger', text: '配置异常', title: member.derived_reason || '底层连接组或模型缺失' },
      warning: { class: 'warning', text: '最近错误', title: member.derived_reason || member.last_error || '最近发生错误' },
      healthy: { class: 'success', text: '正常', title: member.derived_reason || '该成员可参与聚合调度' },
    };
    if (member.derived_status && derivedMap[member.derived_status]) return derivedMap[member.derived_status];
    if (member.enabled === false) return { class: 'warning', text: '已停用', title: '该聚合成员已手动停用，不参与调度' };
    if (member.cooldown_until && member.cooldown_until * 1000 > Date.now()) {
      const remainSec = Math.max(0, Math.ceil((member.cooldown_until * 1000 - Date.now()) / 1000));
      const mm = Math.floor(remainSec / 60).toString().padStart(2, '0');
      const ss = (remainSec % 60).toString().padStart(2, '0');
      return { class: 'cooldown', text: `冷却中（剩 ${mm}:${ss}）`, title: member.cooldown_reason || '该聚合成员因上游健康失败进入短期冷却' };
    }
    if (!model) return { class: 'danger', text: '底层模型不存在', title: '底层真实模型已删除或配置异常' };
    if (model.usable === false) return { class: 'warning', text: '底层模型已停用', title: '请先启用底层真实模型' };
    if (model.cooldown_until && model.cooldown_until * 1000 > Date.now()) return { class: 'cooldown', text: '底层模型冷却中', title: model.cooldown_reason || '底层真实模型正在冷却' };
    if (member.last_error) return { class: 'warning', text: '最近错误', title: member.last_error };
    return { class: 'success', text: '正常', title: '该成员可参与聚合调度' };
  },

  renderGroupSide() {
    return `
      ${this.renderBatchImport()}
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

  groupKeyValue(g) {
    if (!g) return '';
    if (g.provider_type === 'proxy') return g.api_key || '';
    return g.ark_api_key || '';
  },

  syncUIFromState() {
    const sel = Store.selected;
    if (sel.type === 'aggregate') this.syncAggregateUI();
    else if (sel.type === 'group' || sel.type === null) this.syncGroupModeUI();
    else this.syncModelModeUI();
  },

  syncAggregateUI() {
    // 聚合模型表单无需动态切换，定时刷新成员状态由 Store 订阅触发 re-render 完成
    this.updateAggregateCooldownDisplay();
  },

  updateAggregateCooldownDisplay() {
    // 可在成员表格中显示冷却倒计时，当前通过重新渲染实现
  },

  syncGroupModeUI() {
    const mode = document.getElementById('group-provider')?.value || 'ark';
    const needsKey = mode === 'ark' || mode === 'proxy';
    const keyRow = document.getElementById('group-key-row');
    const advancedCard = document.getElementById('group-advanced-card');
    const cooldownRow = document.getElementById('group-cooldown-row');
    const streamTimeoutRow = document.getElementById('group-stream-timeout-row');
    const wafRow = document.getElementById('group-waf-row');
    const wafPolicyRow = document.getElementById('group-waf-policy-row');
    const hint = document.getElementById('group-mode-hint');
    const label = document.getElementById('group-key-label');

    if (keyRow) keyRow.classList.toggle('hidden', !needsKey);
    if (advancedCard) advancedCard.classList.remove('hidden');
    if (cooldownRow) cooldownRow.classList.remove('hidden');
    if (streamTimeoutRow) streamTimeoutRow.classList.remove('hidden');
    if (wafRow) wafRow.classList.toggle('hidden', mode !== 'relay');
    if (wafPolicyRow) {
      const wafChecked = document.getElementById('group-waf')?.checked || false;
      wafPolicyRow.classList.toggle('hidden', mode !== 'relay' || !wafChecked);
    }
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
      panel.querySelector('#model-fetch')?.addEventListener('click', () => this.onFetchUpstream());
      this.bindAutoSave(modelForm, () => this.autoSaveModel());
    }

    // 聚合模型表单
    const aggregateForm = panel.querySelector('#aggregate-form');
    if (aggregateForm) {
      aggregateForm.addEventListener('submit', e => this.onAggregateSubmit(e));
      panel.querySelector('#aggregate-delete')?.addEventListener('click', () => this.onAggregateDelete());
      panel.querySelector('#aggregate-copy-route-key')?.addEventListener('click', () => this.onCopyAggregateRouteKey());
      panel.querySelector('#aggregate-add-member')?.addEventListener('click', () => this.onAddAggregateMember());
      panel.querySelector('#aggregate-stats-limit')?.addEventListener('change', () => this.refreshAggregateStats());
      this.refreshAggregateStats();
      panel.querySelectorAll('.aggregate-member-price').forEach(el => {
        const save = () => this.onUpdateAggregateMemberPrice(el.dataset.memberId, el.value);
        el.addEventListener('change', save);
        el.addEventListener('blur', save);
      });
      panel.querySelectorAll('.aggregate-member-actions button[data-action]').forEach(el => {
        el.addEventListener('click', () => this.onAggregateMemberAction(el.dataset.action, el.dataset.memberId));
      });
      this.bindAutoSave(aggregateForm, () => this.autoSaveAggregate());
    }

    // 批量导入
    panel.querySelector('#batch-import')?.addEventListener('click', () => this.onBatchImport());

    // 配置导入/导出
    panel.querySelector('#config-export')?.addEventListener('click', () => App.exportConfig());
    panel.querySelector('#config-import')?.addEventListener('click', () => panel.querySelector('#config-import-file')?.click());
    panel.querySelector('#config-import-file')?.addEventListener('change', e => this.onConfigImport(e));
  },

  isEditingConfigForm() {
    const active = document.activeElement;
    return !!(active && active.closest && active.closest('#panel-config .config-form') && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName));
  },

  onRuntimeStateUpdate() {
    const panel = document.getElementById('panel-config');
    if (!panel || Tabs.current !== 'config') return;
    this.updateCooldownDisplay();
    this.patchVisibleRuntimeStatus();
  },

  patchVisibleRuntimeStatus() {
    const selected = Store.selected;
    if (selected.type === 'aggregate') {
      Store.getAggregateMembers(selected.id).forEach(member => {
        const cell = document.querySelector(`[data-member-status-cell="${member.id}"]`);
        if (!cell) return;
        const status = this.aggregateMemberStatus(member, Store.getModel(member.model_id));
        const next = `<span class="pill ${status.class}" title="${Utils.escapeHtml(status.title)}">${status.text}</span>`;
        if (cell.innerHTML !== next) cell.innerHTML = next;
      });
    }
  },

  async refreshRuntimeNow() {
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
      this.render();
      Toast.success('运行状态已刷新');
    } catch (err) {
      Toast.error('刷新状态失败：' + err.message);
    }
  },

  bindAutoSave(form, callback) {
    if (!form) return;
    form.querySelectorAll('input, select, textarea').forEach(el => {
      // 聚合成员字段有独立保存逻辑，避免 autoSaveAggregate 的 blur 事件与成员保存竞争
      const cls = el.className || '';
      if (cls.includes('aggregate-member-price')) return;
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

  // 捕获当前表单中用户已编辑的值，用于重新渲染后恢复
  _captureFormValues() {
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

  // 恢复用户之前编辑的表单值
  _restoreFormValues(values) {
    if (!values) return;
    Object.entries(values).forEach(([id, value]) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = Boolean(value);
      else el.value = value ?? '';
    });
  },

  // 启动冷却倒计时定时器，实时更新状态信息中的冷却截止时间
  _startCooldownTimer() {
    this._stopCooldownTimer();
    this.updateCooldownDisplay();
    this._cooldownTimer = setInterval(() => this.updateCooldownDisplay(), 1000);
  },

  _stopCooldownTimer() {
    if (this._cooldownTimer) {
      clearInterval(this._cooldownTimer);
      this._cooldownTimer = null;
    }
  },

  updateCooldownDisplay() {
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
  },

  async onRecoverModel() {
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
      if (Tabs.current === 'config' && Store.selected.type === 'model' && Store.selected.id === id) this.render();
      Toast.success(res.message || '模型已重试恢复');
    } catch (err) {
      Toast.error('重试恢复失败：' + err.message);
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
    const id = document.getElementById('group-id').value;
    if (Store.selected.type !== 'group' || Store.selected.id !== id) {
      Toast.error('当前连接组表单状态已过期，请重新选择后再保存');
      this.render();
      return;
    }
    const mode = document.getElementById('group-provider').value;
    const key = document.getElementById('group-key').value.trim();
    const payload = {
      name: document.getElementById('group-name').value.trim(),
      provider_type: mode,
      base_url: document.getElementById('group-base').value.trim() || undefined,
      ark_api_key: mode === 'ark' ? key : '',
      api_key: mode === 'proxy' ? key : '',
      auto_model_name: document.getElementById('group-auto-model-name').value.trim(),
      auto_model_cooldown_minutes: Number(document.getElementById('group-cooldown').value || 0),
      stream_idle_timeout: Math.max(0, Math.min(600, Number(document.getElementById('group-stream-timeout').value || 0))),
      waf_compatible: mode === 'relay' ? document.getElementById('group-waf').checked : false,
      waf_accept_policy: mode === 'relay' && document.getElementById('group-waf').checked
        ? (document.getElementById('group-waf-policy')?.value || 'default')
        : 'default',
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
    const id = document.getElementById('model-id').value;
    if (Store.selected.type !== 'model' || Store.selected.id !== id) {
      Toast.error('当前模型表单状态已过期，请重新选择后再保存');
      this.render();
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

  autoSaveAggregate() {
    const id = document.getElementById('aggregate-id')?.value;
    if (!id) return; // 新建不自动保存
    clearTimeout(this._autoSaveTimer);
    this.setSaveStatus('saving');
    this._autoSaveTimer = setTimeout(() => {
      const form = document.getElementById('aggregate-form');
      if (form) form.dispatchEvent(new Event('submit'));
    }, 500);
  },

  async onAggregateSubmit(e) {
    e.preventDefault();
    const id = document.getElementById('aggregate-id').value;
    if (Store.selected.type !== 'aggregate' || Store.selected.id !== id) {
      Toast.error('当前聚合模型表单状态已过期，请重新选择后再保存');
      this.render();
      return;
    }
    const payload = {
      name: document.getElementById('aggregate-name').value.trim(),
      display_name: document.getElementById('aggregate-display-name').value.trim(),
      description: document.getElementById('aggregate-description').value.trim(),
      enabled: document.getElementById('aggregate-enabled').checked,
      cooldown_minutes: Math.max(0, Number(document.getElementById('aggregate-cooldown').value || 0)),
      strategy: document.getElementById('aggregate-strategy').value,
    };
    try {
      this.setSaveStatus('saving');
      if (id) await API.saveAggregate(id, payload);
      else await API.createAggregate(payload);
      await Store.load();
      this.setSaveStatus('saved');
    } catch (err) {
      this.setSaveStatus('error', '保存失败：' + err.message);
      Toast.error('保存失败：' + err.message);
    }
  },

  async onAggregateDelete() {
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

  async onAddAggregateMember() {
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
          this._updateMemberPreview('', '', preview);
          return;
        }
        const models = Store.getModelsByGroup(groupId).filter(m => m.usable !== false);
        const existing = Store.getAggregateMembers(aggregateId).map(m => m.model_id);
        modelSelect.innerHTML = '<option value="">请选择模型</option>' + models.map(m =>
          `<option value="${m.id}" ${existing.includes(m.id) ? 'disabled' : ''}>${Utils.escapeHtml(m.name)}${m.upstream_model && m.upstream_model !== m.name ? ` (${Utils.escapeHtml(m.upstream_model)})` : ''}</option>`
        ).join('');
        this._updateMemberPreview(groupSelect.value, modelSelect.value, preview);
      };
      priceInput?.addEventListener('input', () => { priceInput.dataset.touched = 'true'; });
      groupSelect.addEventListener('change', () => {
        if (priceInput) { priceInput.dataset.touched = ''; priceInput.value = ''; }
        refresh();
      });
      modelSelect.addEventListener('change', () => {
        applyDefaultPrice();
        this._updateMemberPreview(groupSelect.value, modelSelect.value, preview);
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
      await this.reloadAfterAggregateMemberChange();
      Toast.success('成员已添加');
    } catch (err) {
      Toast.error('添加失败：' + err.message);
    }
  },

  _updateMemberPreview(groupId, modelId, previewEl) {
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

  onAggregateMemberAction(action, memberId) {
    if (action === 'delete') return this.onDeleteAggregateMember(memberId);
    if (action === 'recover') return this.onRecoverAggregateMember(memberId);
    if (action === 'enable') return this.onToggleAggregateMember(memberId, true);
    if (action === 'disable') return this.onToggleAggregateMember(memberId, false);
    if (['up', 'down', 'top', 'bottom'].includes(action)) return this.onMoveAggregateMember(memberId, action);
  },

  aggregateChainSummary(chain) {
    const items = (chain || []).slice(0, 8).map((item, idx) => {
      const status = item.derived_status && item.derived_status !== 'healthy' ? `（${item.derived_reason || item.derived_status}）` : '';
      return `${idx + 1}. ${item.group_name || '-'} / ${item.model_name || '-'}${status}`;
    });
    const suffix = (chain || []).length > 8 ? `\n… 其余 ${(chain || []).length - 8} 个候选` : '';
    return items.join('\n') + suffix;
  },

  async confirmAggregateMemberPreview(title, preview, confirmText) {
    if (!preview?.ok) return false;
    const before = Utils.escapeHtml(this.aggregateChainSummary(preview.candidate_chain_before || []));
    const after = Utils.escapeHtml(this.aggregateChainSummary(preview.candidate_chain_after || []));
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


  async reloadAfterAggregateMemberChange() {
    await Store.load();
    if (Tabs.current === 'config' && Store.selected.type === 'aggregate') {
      this.render();
    }
  },
  async onRecoverAggregateMember(memberId) {
    try {
      const preview = await API.previewAggregateMemberClearCooldown(memberId);
      const ok = await this.confirmAggregateMemberPreview('恢复成员预览', preview, '确认恢复');
      if (!ok) return;
      const res = await API.recoverAggregateMember(memberId);
      await this.reloadAfterAggregateMemberChange();
      Toast.success(res.message || '成员已重试恢复');
    } catch (err) {
      Toast.error('恢复失败：' + err.message);
    }
  },

  async onMoveAggregateMember(memberId, direction) {
    try {
      await API.saveAggregateMember(memberId, { direction });
      await this.reloadAfterAggregateMemberChange();
    } catch (err) {
      Toast.error('排序失败：' + err.message);
    }
  },

  async onCopyAggregateRouteKey() {
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

  async onUpdateAggregateMemberPrice(memberId, value) {
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
      await this.reloadAfterAggregateMemberChange();
    } catch (err) {
      Toast.error('价格更新失败：' + err.message);
    }
  },

  async onToggleAggregateMember(memberId, enabled) {
    const member = Store.state.aggregate_members?.find(m => m.id === memberId);
    if (!member) return;
    try {
      await API.saveAggregateMember(memberId, { enabled, clear_cooldown: enabled });
      await this.reloadAfterAggregateMemberChange();
      Toast.success(enabled ? '聚合成员已启用并清理冷却' : '聚合成员已停用');
    } catch (err) {
      Toast.error('状态更新失败：' + err.message);
    }
  },

  async onDeleteAggregateMember(memberId) {
    const ok = await Modal.confirm({
      title: '删除成员',
      message: '确定从聚合模型中移除该成员吗？',
      confirmText: '确定删除',
      confirmClass: 'btn-danger'
    });
    if (!ok) return;
    try {
      await API.deleteAggregateMember(memberId);
      await this.reloadAfterAggregateMemberChange();
      Toast.success('成员已删除');
    } catch (err) {
      Toast.error('删除失败：' + err.message);
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
      // 获取到新列表后清空旧值，并直接渲染下拉，避免 datalist 在 Safari 中不刷新
      const upstreamInput = document.getElementById('model-upstream');
      if (upstreamInput) upstreamInput.value = '';
      this.renderUpstreamOptions(groupId, true);
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
    const confirmed = await this._showBatchPreview(preview, group);
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

  _showBatchPreview(preview, group) {
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
