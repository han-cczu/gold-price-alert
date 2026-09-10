/** Escape both text and quoted attribute contexts used by our small HTML templates. */
export function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

export function renderMarkdown(text, { sanitizer = globalThis.DOMPurify, parser = globalThis.marked } = {}) {
    if (!text) return '';
    try {
        if (!sanitizer?.sanitize || !parser?.parse) throw new Error('Markdown unavailable');
        return sanitizer.sanitize(parser.parse(String(text)));
    } catch {
        return escapeHtml(text).replace(/\n/g, '<br>');
    }
}

export function showError(error, targetId = 'app-status') {
    const target = document.getElementById(targetId);
    if (target) {
        target.textContent = error.message || '操作失败，请稍后重试';
        target.hidden = false;
    }
}

export function showNotification(message) {
    const notification = document.createElement('div');
    notification.className = 'ws-notification';
    const text = document.createElement('span');
    text.textContent = String(message ?? '收到新的告警');
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.setAttribute('aria-label', '关闭通知');
    close.addEventListener('click', () => notification.remove());
    notification.append(text, close);
    document.body.append(notification);
    setTimeout(() => notification.remove(), 5000);
}
