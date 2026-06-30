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
      const data = await API.getState();
      this.state = data;
      this.ensureSelection();
      this.emit();
      return data;
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
    const exists = this.selected.type === 'group'
      ? groups.find(g => g.id === this.selected.id)
      : models.find(m => m.id === this.selected.id);
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

  update(patch) {
    this.state = { ...this.state, ...patch };
    this.emit();
  }
};
