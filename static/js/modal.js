const Modal = {
  /**
   * 显示二次确认弹窗
   * @param {Object} options
   * @param {string} options.title 弹窗标题
   * @param {string} options.message 弹窗内容（支持 HTML）
   * @param {string} [options.cancelText='取消']
   * @param {string} [options.confirmText='确定']
   * @param {string} [options.confirmClass='btn-primary']
   * @returns {Promise<boolean>} 点击确定返回 true，取消/关闭返回 false
   */
  confirm({ title = '确认', message = '', cancelText = '取消', confirmText = '确定', confirmClass = 'btn-primary' }) {
    return new Promise(resolve => {
      let overlay = document.getElementById('modal-overlay');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'modal-overlay';
        overlay.className = 'modal-overlay hidden';
        document.body.appendChild(overlay);
      }

      overlay.innerHTML = `
        <div class="modal-dialog">
          <div class="modal-header">
            <h3>${Utils.escapeHtml(title)}</h3>
            <button type="button" class="modal-close" data-action="cancel">×</button>
          </div>
          <div class="modal-body">${message}</div>
          <div class="modal-footer">
            <button type="button" class="btn-secondary" data-action="cancel">${Utils.escapeHtml(cancelText)}</button>
            <button type="button" class="${confirmClass}" data-action="confirm">${Utils.escapeHtml(confirmText)}</button>
          </div>
        </div>
      `;

      const cleanup = (result) => {
        overlay.classList.add('hidden');
        overlay.innerHTML = '';
        resolve(result);
      };

      const onClick = e => {
        const action = e.target.dataset.action;
        if (action === 'confirm') cleanup(true);
        if (action === 'cancel') cleanup(false);
      };

      overlay.addEventListener('click', onClick);
      overlay.addEventListener('click', e => {
        if (e.target === overlay) cleanup(false);
      });
      document.addEventListener('keydown', function escHandler(e) {
        if (e.key === 'Escape') {
          document.removeEventListener('keydown', escHandler);
          cleanup(false);
        }
      });

      overlay.classList.remove('hidden');
    });
  }
};
