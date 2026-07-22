const Utils = {
  escapeHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },

  formatDate(ts) {
    if (!ts) return '-';
    const d = new Date(ts * 1000 || ts);
    return d.toLocaleString('zh-CN', { hour12: false });
  },

  formatDuration(ms) {
    if (ms == null) return '-';
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  },

  debounce(fn, wait = 200) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  },

  async copy(text) {
    const clipboard = globalThis.navigator?.clipboard;
    if (clipboard?.writeText) {
      let timeoutId;
      try {
        // 内嵌浏览器可能因剪贴板权限而一直等待；超时后继续走兼容复制，避免界面无反馈。
        const copied = await Promise.race([
          clipboard.writeText(text).then(() => true),
          new Promise(resolve => { timeoutId = setTimeout(() => resolve(false), 1200); }),
        ]);
        if (copied) return true;
      } catch (_) {
        // 继续尝试兼容路径。
      } finally {
        if (timeoutId) clearTimeout(timeoutId);
      }
    }

    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      return document.execCommand?.('copy') === true;
    } catch (_) {
      return false;
    } finally {
      document.body.removeChild(ta);
    }
  },

  redactSensitive(value) {
    if (value == null) return '';
    let text = String(value);
    text = text.replace(/(Authorization\s*[:=]\s*Bearer\s+)[^\s;,&}]+/gi, '$1[REDACTED]');
    text = text.replace(/(Authorization\s*[:=]\s*)[^\s;,&}]+/gi, '$1[REDACTED]');
    text = text.replace(/((?:api[_-]?key|ark[_-]?api[_-]?key|route[_-]?key|access[_-]?token|refresh[_-]?token|secret|token|key)\s*[:=]\s*)[^\s;,&}]+/gi, '$1[REDACTED]');
    text = text.replace(/(sk-[A-Za-z0-9][A-Za-z0-9_\-]{8,})/g, 'sk-[REDACTED]');
    text = text.replace(/(lr-ag-[A-Za-z0-9][A-Za-z0-9_\-]{8,})/g, 'lr-ag-[REDACTED]');
    text = text.replace(/(lr-[A-Za-z0-9][A-Za-z0-9_\-]{8,})/g, 'lr-[REDACTED]');
    text = text.replace(/(body(?:_base64)?\s*[:=]\s*)[^;]{80,}/gi, '$1[REDACTED_BODY]');
    text = text.replace(/((?:messages|prompt|input)\s*[:=]\s*)[^;]{160,}/gi, '$1[REDACTED_BODY]');
    return text;
  },


  generateId() {
    return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
  }
};

const ConnectionStatus = {
  isCooling(model) {
    return Number(model?.cooldown_until || 0) * 1000 > Date.now();
  },

  isSuccessfulLog(log) {
    return String(log?.status || '').startsWith('2')
      && log?.event !== 'skip'
      && log?.event !== 'manual_probe'
      && log?.usage_source !== 'manual_probe';
  },

  isValidBaseUrl(value) {
    try {
      const url = new URL(String(value || '').trim());
      return url.protocol === 'http:' || url.protocol === 'https:';
    } catch (_) {
      return false;
    }
  },

  groupMissingFields(group) {
    const missing = [];
    if (!String(group?.name || '').trim()) missing.push('组名');
    if (!this.isValidBaseUrl(group?.base_url)) missing.push('Base URL');
    const hasGroupKey = group?.provider_type === 'proxy'
      ? Boolean(group?.api_key_configured || String(group?.api_key || '').trim())
      : Boolean(group?.ark_api_key_configured || String(group?.ark_api_key || '').trim());
    if (['ark', 'proxy'].includes(group?.provider_type) && !hasGroupKey) {
      missing.push('API Key');
    }
    return missing;
  },

  modelMissingFields(model, group) {
    const missing = [];
    if (!String(model?.name || '').trim()) missing.push('模型名称');
    if (!String(model?.upstream_model || model?.ep_id || '').trim()) missing.push('上游模型');
    if (group?.provider_type === 'relay' && !Boolean(model?.api_key_configured || String(model?.api_key || '').trim())) missing.push('中转站 API Key');
    return missing;
  },

  group(group, state = Store.state) {
    const models = (state.models || []).filter(model => model.group_id === group.id);
    const missingFields = this.groupMissingFields(group);
    // 验证状态必须来自模型持久化成功证据，不能随当前日志窗口滚动而丢失。
    const verifiedModels = models.filter(model => String(model.last_success_at || '').trim());
    const usableModels = models.filter(model => model.usable !== false && !this.isCooling(model));
    const coolingModels = models.filter(model => this.isCooling(model));
    const representative = verifiedModels.find(model => model.usable !== false && !this.isCooling(model))
      || verifiedModels[0]
      || usableModels[0]
      || models[0]
      || null;

    if (missingFields.length) {
      return {
        code: 'needs_completion', label: '待完善',
        reason: `缺少${missingFields.join('、')}`, impact: '尚未满足测试条件，当前连接组不会发起上游请求。',
        systemAction: '系统保留现有配置，等待你补全必填信息。', action: '补全字段', modelCount: models.length,
        representative, missingFields, verifiedModel: null,
      };
    }
    if (!models.length) {
      return {
        code: 'saved_no_model', label: '已保存，待添加模型',
        reason: '连接组已保存，但还没有模型配置。', impact: '当前连接组还不能处理客户端请求。',
        systemAction: '系统未自动访问上游；你可以获取模型或手动添加。', action: '获取模型', modelCount: 0,
        representative: null, missingFields: [], verifiedModel: null,
      };
    }
    const incompleteModel = models.find(model => this.modelMissingFields(model, group).length);
    if (incompleteModel) {
      return {
        code: 'needs_model_completion', label: '待完善',
        reason: `模型「${incompleteModel.name || '未命名'}」缺少${this.modelMissingFields(incompleteModel, group).join('、')}。`,
        impact: '该模型尚不能用于测试或客户端请求。',
        systemAction: '系统保留该模型配置，等待你补全必填信息。', action: '编辑模型', modelCount: models.length,
        representative: incompleteModel, missingFields: [], verifiedModel: null,
      };
    }
    if (verifiedModels.some(model => model.usable !== false && !this.isCooling(model))) {
      return {
        code: 'ready', label: '可用',
        reason: '存在模型持久化的成功验证证据。', impact: '可用于客户端接入。',
        systemAction: '系统会按当前模型和路由 Key 处理请求。', action: '复制配置', modelCount: models.length,
        representative, missingFields: [], verifiedModel: representative,
      };
    }
    if (coolingModels.length && !usableModels.length) {
      return {
        code: 'cooldown', label: '临时冷却',
        reason: '当前模型正在冷却或等待自动恢复。', impact: '该连接组暂不参与自动选择。',
        systemAction: '系统会在冷却结束后恢复模型参与调度。', action: '重新测试', modelCount: models.length,
        representative, missingFields: [], verifiedModel: null,
      };
    }
    if (!usableModels.length) {
      return {
        code: 'needs_attention', label: '需处理',
        reason: '没有可参与请求的模型。', impact: '当前连接组无法正常处理请求。',
        systemAction: '系统已跳过不可用模型，未修改你的配置。', action: '查看模型', modelCount: models.length,
        representative, missingFields: [], verifiedModel: null,
      };
    }
    return {
      code: 'pending_verify', label: '已添加模型，待验证',
      reason: '模型已添加，但没有成功测试或请求证据。', impact: '尚不建议用于客户端接入。',
      systemAction: '系统不会仅因保存配置而标记为可用。', action: '测试模型', modelCount: models.length,
      representative, missingFields: [], verifiedModel: null,
    };
  },

  draftGroup(group) {
    const status = this.group(
      { ...group, id: '__new_group_draft__' },
      { groups: [], models: [], logs: [] },
    );
    if (status.code === 'needs_completion') return status;
    return {
      ...status,
      code: 'draft_ready', label: '基础字段已填写，待保存',
      reason: '当前内容是未保存草稿。',
      impact: '保存后才能添加或获取模型，当前不会处理客户端请求。',
      systemAction: '系统不会自动保存、获取模型或测试。',
      action: '保存连接组',
    };
  },

  derive(state = Store.state) {
    const groups = state.groups || [];
    const aggregates = state.aggregate_models || [];
    const statuses = groups.map(group => ({ group, ...this.group(group, state) }));
    const ready = statuses.filter(item => item.code === 'ready');
    const models = state.models || [];
    let code = 'S2';
    if (!groups.length && !aggregates.length) code = 'S0';
    else if (!models.length) code = 'S1';
    else if (ready.length >= 2 && !aggregates.length) code = 'S4';
    else if (ready.length) code = 'S3';
    else if (statuses.length && statuses.every(item => ['needs_completion', 'needs_model_completion', 'needs_attention', 'cooldown'].includes(item.code))) code = 'E1';
    return { code, groups: statuses, readyGroups: ready, primary: ready[0] || statuses[0] || null };
  }
};
