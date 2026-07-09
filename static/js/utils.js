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
