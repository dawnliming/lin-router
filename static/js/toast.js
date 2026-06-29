const Toast = {
  container: null,

  init() {
    this.container = document.getElementById('toast-container');
  },

  show(message, type = 'info', duration = 3000) {
    if (!this.container) this.init();
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = message;
    this.container.appendChild(el);

    if (duration > 0) {
      setTimeout(() => {
        el.classList.add('fade-out');
        setTimeout(() => el.remove(), 200);
      }, duration);
    }
  },

  success(msg) { this.show(msg, 'success'); },
  warning(msg) { this.show(msg, 'warning'); },
  error(msg) { this.show(msg, 'error'); },
  info(msg) { this.show(msg, 'info'); }
};
