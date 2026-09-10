/** Same-origin JSON requests. Administrator credentials live only in this page. */
export class ApiError extends Error {
    constructor(message, status = 0, retryAfter = null) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.retryAfter = retryAfter;
    }
}

export function createApiClient({ fetchImpl = globalThis.fetch, onError = () => {}, timeoutMs = 20000 } = {}) {
    let adminKey = '';
    function setAdminKey(value) { adminKey = String(value || '').trim(); }

    async function request(path, options = {}) {
        if (typeof path !== 'string' || !path.startsWith('/api/') || path.includes('\\')) {
            throw new ApiError('仅允许访问本应用的 API');
        }
        const { timeoutMs: deadline = timeoutMs, signal, ...init } = options;
        const controller = new AbortController();
        const cancel = () => controller.abort();
        if (signal?.aborted) cancel();
        signal?.addEventListener('abort', cancel, { once: true });
        let timedOut = false;
        const timer = setTimeout(() => { timedOut = true; controller.abort(); }, deadline);
        const headers = new Headers(init.headers);
        if (adminKey) headers.set('X-Admin-Key', adminKey);
        const secrets = [adminKey];
        try {
            const body = typeof init.body === 'string' ? JSON.parse(init.body) : null;
            if (body?.api_key) secrets.push(String(body.api_key));
        } catch { /* Non-JSON bodies contain no known form credentials. */ }
        const redact = value => secrets.filter(Boolean).reduce((text, key) => text.split(key).join('[已隐藏]'), String(value));
        const diagnosticFields = new Set(['message', 'detail', 'error_message', 'response', 'raw_response']);
        const redactDiagnostics = (value, field) => {
            if (typeof value === 'string') return diagnosticFields.has(field) ? redact(value) : value;
            if (Array.isArray(value)) return value.map(item => redactDiagnostics(item, field));
            if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactDiagnostics(item, key)]));
            return value;
        };
        try {
            const response = await fetchImpl(path, { ...init, headers, signal: controller.signal, credentials: 'same-origin', redirect: 'error' });
            let data;
            try { data = await response.json(); }
            catch {
                if (controller.signal.aborted) throw new ApiError(timedOut ? '请求超时，请稍后重试' : '请求已取消');
                if (response.ok) throw new ApiError('服务器返回了无效数据', response.status);
            }
            if (!response.ok) {
                const retryAfter = response.headers.get('Retry-After');
                let message;
                if (response.status === 401 || response.status === 403) message = '需要有效的管理员凭证，请在设置中填写';
                else if (response.status === 429) message = `请求过于频繁，请${/^\d+$/.test(retryAfter || '') ? ` ${retryAfter} 秒后` : '稍后'}重试`;
                else if (response.status >= 500) message = '服务暂时不可用，请稍后重试';
                else message = typeof data?.detail === 'string' ? redact(data.detail) : `请求失败 (${response.status})`;
                throw new ApiError(message, response.status, retryAfter);
            }
            return redactDiagnostics(data);
        } catch (error) {
            const safeError = error instanceof ApiError ? error : new ApiError(
                controller.signal.aborted ? (timedOut ? '请求超时，请稍后重试' : '请求已取消') : '网络连接失败，请检查连接后重试'
            );
            if (!signal?.aborted) onError(safeError);
            throw safeError;
        } finally {
            clearTimeout(timer);
            signal?.removeEventListener('abort', cancel);
        }
    }
    return { request, setAdminKey, hasAdminKey: () => Boolean(adminKey) };
}
