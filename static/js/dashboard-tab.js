const DashboardTab = {
  _openClientTemplates: new Set(),

  refresh() {
    const panel = document.getElementById('panel-dashboard');
    if (!panel) return;
    panel.innerHTML = this.render();
    this.attachEvents(panel);
  },

  render() {
    const state = Store.state || {};
    const groups = state.groups || [];
    const models = state.models || [];
    const aggregates = state.aggregate_models || [];
    const members = state.aggregate_members || [];
    const logs = state.logs || [];
    const recent = logs.filter(log => !this.isConfigSkip(log)).slice(0, 30);
    const success = recent.filter(l => String(l.status || '').startsWith('2')).length;
    const successRate = recent.length ? `${Math.round((success / recent.length) * 100)}%` : '-';
    const fallbackCount = recent.filter(l => ['fallback', 'retry_ok'].includes(String(l.event || ''))).length;
    const busyCount = recent.filter(l => {
      const text = `${l.event || ''};${l.detail || ''}`;
      return text.includes('candidate_busy') || text.includes('large_task_in_progress') || text.includes('serial_protection_timeout') || text.includes('waf_lock_timeout');
    }).length;
    const upstreamTimeoutCount = recent.filter(l => `${l.event || ''};${l.detail || ''}`.includes('upstream_timeout') || `${l.detail || ''}`.includes('read_timeout') || `${l.detail || ''}`.includes('stream_idle_timeout')).length;
    const wafBlockedCount = recent.filter(l => `${l.event || ''};${l.detail || ''}`.includes('waf_blocked')).length;
    const enabledAggregates = aggregates.filter(a => a.enabled !== false).length;
    const baseUrl = `${window.location.origin}/v1`;
    const enabledAggregateList = aggregates.filter(a => a.enabled !== false);
    const flow = ConnectionStatus.derive(state);
    const showOperational = ['S3', 'S4'].includes(flow.code);
    const aggregateSummary = this.aggregateSummaryCards(aggregates, recent);
    const liveRequests = state.live_requests || [];

    return `
      <div class="dashboard-page">
        <div class="dashboard-hero">
          <div>
            <div class="dashboard-eyebrow">Lin Router 正在运行</div>
            <h2>${flow.code === 'S0' ? '从添加连接组开始' : '连接与接入状态'}</h2>
            <p>${flow.code === 'S0' ? 'Lin Router 已启动，但还没有可处理请求的连接组。' : '按当前配置和真实请求记录给出下一步。'}</p>
          </div>
          <div class="dashboard-hero-actions">
            <button type="button" class="btn-primary" data-dashboard-action="new-group">添加连接组</button>
            ${groups.length ? '<button type="button" class="btn-secondary" data-dashboard-action="config">管理连接组</button>' : '<button type="button" class="btn-secondary" data-dashboard-action="import">导入已有配置</button>'}
          </div>
        </div>
        ${this.renderFlowCard(flow)}
        ${showOperational ? `
          <div class="dashboard-grid">
            ${this.metricCard('服务状态', state.log_write_error ? '日志异常' : '运行中', state.log_write_error || '本地代理服务已启动')}
            ${this.metricCard('可用连接组', `${flow.readyGroups.length} / ${groups.length}`, `${models.length} 个模型配置`)}
            ${this.metricCard('智能路由', `${enabledAggregates} / ${aggregates.length}`, `${members.length} 个聚合成员`)}
            ${this.metricCard('最近成功率', successRate, `最近 ${recent.length} 条请求`)}
            ${this.metricCard('Fallback', `${fallbackCount} 次`, '最近请求自动切换次数')}
            ${this.metricCard('候选忙', `${busyCount} 次`, '大上下文或锁等待切换')}
            ${this.metricCard('上游超时 / WAF', `${upstreamTimeoutCount} / ${wafBlockedCount}`, '最近请求健康信号')}
          </div>
          ${this.renderLiveRequests(liveRequests)}
          ${aggregateSummary}
          <div class="dashboard-two-col">
            <section class="dashboard-card dashboard-access-section">
              <h3>客户端接入</h3>
              ${this.renderDirectAccessCards(flow.readyGroups, baseUrl)}
            </section>
            <section class="dashboard-card">
              <h3>智能路由（可选）</h3>
              ${this.renderAggregateAccessCards(enabledAggregateList, baseUrl)}
            </section>
          </div>
        ` : this.renderNextSteps(flow)}
      </div>
    `;
  },

  renderFlowCard(flow) {
    const item = flow.primary;
    const group = item?.group;
    const model = item?.representative;
    const mode = { relay: '中转站', ark: '火山方舟', proxy: '通用 OpenAI 代理' }[group?.provider_type] || group?.provider_type || '';
    const content = {
      S0: {
        title: '还没有连接组',
        facts: '添加一个连接组后，你可以获取模型、测试请求，并将本机地址配置到 Codex、Hermes 或其他 OpenAI 兼容客户端。',
        actions: '<button type="button" class="btn-primary" data-dashboard-action="new-group">添加连接组</button><button type="button" class="btn-secondary" data-dashboard-action="import">导入已有配置</button>',
      },
      S1: {
        title: '连接组已添加，尚未有模型',
        facts: `${Utils.escapeHtml(group?.name || '当前连接组')}（${Utils.escapeHtml(mode)}）已保存，模型数为 0。`,
        actions: `${group?.provider_type !== 'ark' ? `<button type="button" class="btn-primary" data-dashboard-action="fetch-models" data-group-id="${group?.id || ''}">获取模型</button>` : ''}<button type="button" class="btn-secondary" data-dashboard-action="add-model" data-group-id="${group?.id || ''}">手动添加模型</button>`,
      },
      S2: {
        title: '模型已添加，建议先验证',
        facts: `${Utils.escapeHtml(model?.name || '当前模型')} 属于 ${Utils.escapeHtml(group?.name || '当前连接组')}，当前状态为待验证。`,
        actions: `<button type="button" class="btn-primary" data-dashboard-action="test-model" data-model-id="${model?.id || ''}">测试模型</button><button type="button" class="btn-secondary" data-dashboard-action="select-group" data-group-id="${group?.id || ''}">编辑连接组</button>`,
      },
      S3: {
        title: '已具备客户端接入条件',
        facts: `最近成功模型为 ${Utils.escapeHtml(model?.name || '当前模型')}，属于 ${Utils.escapeHtml(group?.name || '当前连接组')}。`,
        actions: `<button type="button" class="btn-primary" data-dashboard-action="copy-client" data-group-id="${group?.id || ''}">复制客户端配置</button><button type="button" class="btn-secondary" data-dashboard-action="test-model" data-model-id="${model?.id || ''}">再次测试</button>`,
      },
      S4: {
        title: '多个连接组已可用，可选创建智能路由',
        facts: `已有 ${flow.readyGroups.length} 个可用连接组。智能路由可在候选之间自动切换，但不会影响现有直连接入。`,
        actions: '<button type="button" class="btn-primary" data-dashboard-action="new-aggregate">创建智能路由</button><button type="button" class="btn-secondary" data-dashboard-action="copy-client" data-group-id="' + (group?.id || '') + '">复制客户端配置</button>',
      },
      E1: {
        title: '连接组需要处理',
        facts: `${Utils.escapeHtml(group?.name || '当前连接组')}：${Utils.escapeHtml(item?.reason || '当前没有可用模型')}。`,
        actions: `<button type="button" class="btn-primary" data-dashboard-action="select-group" data-group-id="${group?.id || ''}">查看并处理</button>`,
      },
    }[flow.code] || {};
    return `
      <section class="dashboard-flow-card status-${Utils.escapeHtml(item?.code || flow.code)}">
        <div class="dashboard-flow-status">下一步</div>
        <h3>${content.title || '检查连接状态'}</h3>
        <p>${content.facts || ''}</p>
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
        <div class="dashboard-flow-actions">${content.actions || ''}</div>
      </section>`;
  },

  renderNextSteps(flow) {
    const rows = flow.groups.map(item => `
      <div class="connection-summary-row">
        <div><strong>${Utils.escapeHtml(item.group.name)}</strong><span>${Utils.escapeHtml(item.label)} · ${item.modelCount} 个模型</span></div>
        <span>${Utils.escapeHtml(item.reason)}</span>
        <button type="button" class="btn-secondary btn-sm" data-dashboard-action="select-group" data-group-id="${item.group.id}">查看</button>
      </div>`).join('');
    return `<section class="dashboard-card connection-summary-card"><h3>连接组状态</h3>${rows || '<div class="empty-hint">暂无连接组。</div>'}</section>`;
  },

  metricCard(label, value, hint) {
    return `<section class="dashboard-metric"><span>${Utils.escapeHtml(label)}</span><strong>${Utils.escapeHtml(value)}</strong><small>${Utils.escapeHtml(hint || '')}</small></section>`;
  },

  renderLiveRequests(items) {
    const rows = (items || []).slice(0, 8).map(item => {
      const slow = item.slow ? ' slow' : '';
      const elapsed = this.formatElapsed(item.elapsed_ms || 0);
      const hint = item.possible_reason || (item.slow ? '请求耗时较长，请关注当前阶段' : '处理中');
      const stageLabel = item.stage_label || this.liveStageLabel(item.stage);
      return `
        <div class="live-request-row${slow}">
          <div><strong>${Utils.escapeHtml(item.request_id_short || String(item.request_id || '').slice(0, 8))}</strong><span>${Utils.escapeHtml(item.requested_model || item.model || '-')}</span></div>
          <div>${Utils.escapeHtml(item.group || item.candidate || '-')}</div>
          <div><span class="pill ${item.slow ? 'warning' : 'info'}">${Utils.escapeHtml(stageLabel)}</span></div>
          <div>${Utils.escapeHtml(elapsed)}</div>
          <small title="${Utils.escapeHtml(hint)}">${Utils.escapeHtml(hint)}</small>
          <div class="live-request-action">${item.cancellable === false ? '<span class="pill warning">终止中…</span>' : `<button type="button" class="btn-secondary btn-sm" data-dashboard-action="cancel-request" data-request-id="${Utils.escapeHtml(item.request_id || '')}" data-request-short="${Utils.escapeHtml(item.request_id_short || String(item.request_id || '').slice(0, 8))}" data-request-model="${Utils.escapeHtml(item.requested_model || item.model || '-')}" data-request-group="${Utils.escapeHtml(item.group || item.candidate || '-')}" data-request-stage="${Utils.escapeHtml(stageLabel)}" data-request-elapsed="${Utils.escapeHtml(elapsed)}">终止请求</button>`}</div>
        </div>`;
    }).join('');
    return `
      <section class="dashboard-card live-requests-card">
        <div class="section-title-row">
          <h3>实时请求观测</h3>
          <span class="pill ${items.length ? 'warning' : 'success'}">${items.length ? `${items.length} 个进行中` : '空闲'}</span>
        </div>
        ${rows || '<div class="empty-hint">当前没有正在处理的请求。</div>'}
      </section>
    `;
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
      candidate_busy: '候选忙/串行保护等待超时'
    };
    return map[stage] || stage || '处理中';
  },

  renderDirectAccessCards(groups, baseUrl) {
    return `<div class="dashboard-access-grid">${groups.map(item => {
      const model = item.verifiedModel || item.representative;
      const text = this.clientConfigText(item.group, model, baseUrl);
      return `
        <div class="dashboard-access-card">
          <div class="dashboard-access-title">${Utils.escapeHtml(item.group.name)} <span>已验证直连</span></div>
          <div class="dashboard-access-scope">适用范围：当前连接组。客户端使用本地路由 Key，不是上游 API Key。</div>
          <div class="copy-row"><span>Base URL</span><code>${Utils.escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(baseUrl)}">复制</button></div>
          <div class="copy-row"><span>API Key</span><code>${Utils.escapeHtml(item.group.route_key || '未生成')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(item.group.route_key || '')}" ${item.group.route_key ? '' : 'disabled'}>复制</button></div>
          <div class="copy-row"><span>Model</span><code>${Utils.escapeHtml(model?.name || '-')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(model?.name || '')}" ${model?.name ? '' : 'disabled'}>复制</button></div>
          <button type="button" class="btn-secondary btn-sm" data-copy-client-group="${item.group.id}">复制完整通用配置</button>
          ${this.renderClientTemplates(item.group, model, baseUrl)}
        </div>`;
    }).join('')}</div>`;
  },

  renderClientTemplates(group, model, baseUrl) {
    const routeKey = group?.route_key || '';
    const modelName = model?.name || '';
    const codex = `# PowerShell：启动 Codex 前设置本地路由 Key\n$env:OPENAI_API_KEY = "${routeKey}"\ncodex\n\n# ~/.codex/config.toml\nmodel_provider = "lin-router"\nmodel = "${modelName}"\n\n[model_providers.lin-router]\nname = "Lin Router"\nbase_url = "${baseUrl}"\nwire_api = "responses"\nrequires_openai_auth = false`;
    const hermes = `Base URL: ${baseUrl}\nAPI Key: ${routeKey}\nModel: ${modelName}`;
    const openai = `from openai import OpenAI\n\nclient = OpenAI(\n    base_url="${baseUrl}",\n    api_key="${routeKey}",\n)\n\nresponse = client.chat.completions.create(\n    model="${modelName}",\n    messages=[{"role": "user", "content": "你好"}],\n)\nprint(response.choices[0].message.content)`;
    const template = (name, value, hint) => `
      <div class="client-template">
        <div><strong>${name}</strong><span>${Utils.escapeHtml(hint)}</span></div>
        <button type="button" class="btn-secondary btn-sm" data-copy-template="${Utils.escapeHtml(value)}">复制模板</button>
      </div>`;
    return `
      <details class="client-templates" data-client-template-group="${Utils.escapeHtml(group?.id || '')}" ${this._openClientTemplates.has(group?.id) ? 'open' : ''}>
        <summary>客户端配置模板</summary>
        <div class="client-templates-body">
          ${template('Codex', codex, '使用 Responses 协议与本地 route key')}
          ${template('Hermes', hermes, '在 OpenAI 兼容连接参数中填写')}
          ${template('通用 OpenAI', openai, 'Python SDK 调用示例')}
        </div>
      </details>`;
  },

  renderAggregateAccessCards(aggregates, baseUrl) {
    if (!aggregates.length) {
      return `
        <div class="dashboard-empty-access">
          <p>尚未创建智能路由。连接组直连已可用；智能路由仅用于多候选自动切换。</p>
          <button type="button" class="btn-secondary btn-sm" data-dashboard-action="new-aggregate">创建智能路由</button>
        </div>
      `;
    }
    return `<div class="dashboard-access-grid">${aggregates.map(aggregate => `
      <div class="dashboard-access-card">
        <div class="dashboard-access-title">${Utils.escapeHtml(aggregate.display_name || aggregate.name)} <span>智能路由</span></div>
        <div class="copy-row"><span>Base URL</span><code>${Utils.escapeHtml(baseUrl)}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(baseUrl)}">复制</button></div>
        <div class="copy-row"><span>API Key</span><code>${Utils.escapeHtml(aggregate.route_key || '未生成')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregate.route_key || '')}" ${aggregate.route_key ? '' : 'disabled'}>复制</button></div>
        <div class="copy-row"><span>Model</span><code>${Utils.escapeHtml(aggregate.name || '-')}</code><button type="button" class="btn-secondary btn-sm" data-copy-value="${Utils.escapeHtml(aggregate.name || '')}" ${aggregate.name ? '' : 'disabled'}>复制</button></div>
      </div>
    `).join('')}</div>`;
  },

  aggregateSummaryCards(aggregates, logs) {
    if (!aggregates.length) return '';
    const cards = aggregates.slice(0, 4).map(aggregate => {
      const related = logs.filter(log => log.aggregate_id === aggregate.id || log.aggregate_model === aggregate.name || log.model === aggregate.name);
      const real = related.filter(log => !this.isConfigSkip(log));
      const success = real.filter(log => String(log.status || '').startsWith('2') && ['ok', 'stream_done', 'retry_ok', 'stream_ok'].includes(String(log.event || ''))).length;
      const busy = related.filter(log => `${log.event || ''};${log.detail || ''}`.includes('candidate_busy') || `${log.detail || ''}`.includes('large_task_in_progress')).length;
      const cached = real.reduce((sum, log) => sum + Number(log.cached_tokens || 0), 0);
      const prompt = real.reduce((sum, log) => sum + Number(log.prompt_tokens || 0), 0);
      const rate = real.length ? `${Math.round((success / real.length) * 100)}%` : '暂无数据';
      const cacheRate = prompt ? `${Math.round((cached / prompt) * 100)}%` : '暂无数据';
      return `
        <section class="dashboard-card dashboard-aggregate-card" data-dashboard-action="aggregate" data-aggregate-id="${aggregate.id}">
          <h3>${Utils.escapeHtml(aggregate.display_name || aggregate.name)}</h3>
          <div class="dashboard-mini-grid">
            <span>成功率 <strong>${rate}</strong></span>
            <span>候选忙 <strong>${busy}</strong></span>
            <span>Cache <strong>${cacheRate}</strong></span>
          </div>
        </section>
      `;
    }).join('');
    return `<div class="dashboard-aggregate-summary"><h3>聚合收益摘要</h3><div class="dashboard-aggregate-grid">${cards}</div></div>`;
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
    panel.querySelectorAll('[data-copy-value]').forEach(btn => {
      btn.addEventListener('click', () => {
        const value = (btn.dataset.copyValue || '').trim();
        if (!value) return Toast.warning('暂无可复制内容');
        Utils.copy(value).then(ok => ok ? Toast.success('已复制') : Toast.error('复制失败'));
      });
    });
    panel.querySelectorAll('[data-copy-client-group]').forEach(btn => {
      btn.addEventListener('click', () => {
        const group = Store.getGroup(btn.dataset.copyClientGroup);
        const status = group ? ConnectionStatus.group(group) : null;
        const value = this.clientConfigText(group, status?.verifiedModel || status?.representative);
        Utils.copy(value).then(ok => ok ? Toast.success('客户端配置已复制') : Toast.error('复制失败'));
      });
    });
    panel.querySelectorAll('[data-copy-template]').forEach(btn => {
      btn.addEventListener('click', () => {
        Utils.copy(btn.dataset.copyTemplate || '').then(ok => ok ? Toast.success('客户端模板已复制') : Toast.error('复制失败'));
      });
    });
    panel.querySelectorAll('[data-client-template-group]').forEach(details => {
      details.addEventListener('toggle', () => {
        const groupId = details.dataset.clientTemplateGroup;
        if (!groupId) return;
        if (details.open) this._openClientTemplates.add(groupId);
        else this._openClientTemplates.delete(groupId);
      });
    });
    panel.querySelectorAll('[data-dashboard-action]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const action = btn.dataset.dashboardAction;
        if (action === 'new-group') return App.createGroup();
        if (action === 'new-aggregate') return App.createAggregate();
        if (action === 'import') return App.importConfig();
        if (action === 'config') return Tabs.switch('config');
        if (action === 'test') return Tabs.switch('test');
        if (action === 'logs') return Tabs.switch('logs');
        if (action === 'select-group') { Store.select('group', btn.dataset.groupId); return Tabs.switch('config'); }
        if (action === 'add-model') return ConfigTab.onAddModelToGroup(btn.dataset.groupId);
        if (action === 'fetch-models') return ConfigTab.fetchModelsForGroup(btn.dataset.groupId);
        if (action === 'test-model') { Store.select('model', btn.dataset.modelId); return Tabs.switch('test'); }
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
          btn.textContent = '终止中…';
          try {
            const result = await API.cancelLiveRequest(btn.dataset.requestId);
            Toast.success(result.message || '已发送终止指令，正在释放本地请求资源…');
            await API.getRuntimeState({ silent: true }).then(data => Store.update({ live_requests: data.live_requests || [] }));
          } catch (err) {
            btn.disabled = false;
            btn.textContent = '终止请求';
            Toast.error(err.message || '终止指令未能确认，请刷新实时列表；如仍卡住可查看日志。');
          }
          return;
        }
        if (action === 'aggregate') { Store.select('aggregate', btn.dataset.aggregateId); return Tabs.switch('config'); }
      });
    });
  }
};
