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
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        return true;
      } catch (_) {
        return false;
      } finally {
        document.body.removeChild(ta);
      }
    }
  },

  generateId() {
    return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join('');
  }
};
