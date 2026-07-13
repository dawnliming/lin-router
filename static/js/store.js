const Store = {
  state: {},
  selected: { type: 'group', id: null },
  listeners: [],

  subscribe(fn) {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter(l => l !== fn);
    };
  },

  emit() {
    this.listeners.forEach(fn => fn(this.state, this.selected));
  },

  async load() {
    try {
      const [data, settings] = await Promise.all([
        API.getState(),
        API.getSettings(),
      ]);
      this.state = { ...data, settings: settings || data.settings || {} };
      this.ensureSelection();
      this.emit();
      return this.state;
    } catch (err) {
      Toast.error('加载状态失败：' + err.message);
      throw err;
    }
  },

  ensureSelection() {
    // v0.4.1：默认不自动选中任何节点，首次打开显示空白引导页
    if (!this.selected.id) return;
    const groups = this.state.groups || [];
    const models = this.state.models || [];
    const aggregates = this.state.aggregate_models || [];
    let exists = false;
    if (this.selected.type === 'group') exists = groups.find(g => g.id === this.selected.id);
    else if (this.selected.type === 'model') exists = models.find(m => m.id === this.selected.id);
    else if (this.selected.type === 'aggregate') exists = aggregates.find(a => a.id === this.selected.id);
    if (!exists) {
      this.selected = { type: 'group', id: null };
    }
  },

  select(type, id) {
    this.selected = { type, id };
    this.emit();
  },

  getGroup(id) {
    return (this.state.groups || []).find(g => g.id === id);
  },

  getModel(id) {
    return (this.state.models || []).find(m => m.id === id);
  },

  getModelsByGroup(groupId) {
    return (this.state.models || []).filter(m => m.group_id === groupId);
  },

  getAggregate(id) {
    return (this.state.aggregate_models || []).find(a => a.id === id);
  },

  getAggregateMembers(aggregateId) {
    return (this.state.aggregate_members || [])
      .filter(m => m.aggregate_id === aggregateId)
      .sort((a, b) => (a.priority || 0) - (b.priority || 0));
  },

  getAggregateMemberRevision(aggregateId) {
    return Number(this.state.aggregate_member_revisions?.[aggregateId] || 0);
  },

  update(patch) {
    this.state = { ...this.state, ...patch };
    this.emit();
  }
};
