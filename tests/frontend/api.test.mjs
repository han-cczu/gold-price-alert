import test from 'node:test';
import assert from 'node:assert/strict';
import { createApiClient, ApiError } from '../../src/gold_monitor/static/js/api.js';

const json = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), { status, headers });

test('administrator key is opt-in, stays per client and can be cleared', async () => {
    const requests = [];
    const fetchImpl = async (path, options) => { requests.push(options); return json({ providers: [] }); };
    const first = createApiClient({ fetchImpl });
    const second = createApiClient({ fetchImpl });
    await first.request('/api/llm/providers');
    first.setAdminKey(' test-admin-secret ');
    await first.request('/api/llm/providers');
    await second.request('/api/llm/providers');
    first.setAdminKey('');
    await first.request('/api/llm/providers');
    assert.deepEqual(requests.map(request => request.headers.get('X-Admin-Key')), [null, 'test-admin-secret', null, null]);
    assert.equal(requests[1].redirect, 'error');
    assert.equal(requests[1].credentials, 'same-origin');
});

test('credential-bearing requests cannot target another origin', async () => {
    let called = false;
    const client = createApiClient({ fetchImpl: async () => { called = true; } });
    client.setAdminKey('secret');
    await assert.rejects(client.request('https://example.org/api/llm/config'), ApiError);
    await assert.rejects(client.request('//example.org/api/llm/config'), ApiError);
    assert.equal(called, false);
});

test('401 and 429 produce actionable errors even for non-JSON error pages', async () => {
    const errors = [];
    const denied = createApiClient({ fetchImpl: async () => new Response('no', { status: 401 }), onError: error => errors.push(error) });
    await assert.rejects(denied.request('/api/llm/config'), error => error.status === 401 && /管理员凭证/.test(error.message));
    const limited = createApiClient({ fetchImpl: async () => json({}, 429, { 'Retry-After': '12' }) });
    await assert.rejects(limited.request('/api/smart-analysis'), error => error.status === 429 && error.retryAfter === '12' && /12 秒/.test(error.message));
    assert.equal(errors.length, 1);
});

test('timeouts abort requests and do not expose transport errors or credentials', async () => {
    let aborted = false;
    const client = createApiClient({ timeoutMs: 5, fetchImpl: (path, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => { aborted = true; reject(new Error('internal secret')); });
    }) });
    await assert.rejects(client.request('/api/llm/config'), error => /超时/.test(error.message) && !/secret/.test(error.message));
    assert.equal(aborted, true);
});

test('timeout includes a stalled response body, and invalid JSON is an error', async () => {
    const stalled = createApiClient({ timeoutMs: 5, fetchImpl: async (path, { signal }) => ({ ok: true, status: 200, json: () => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')))) }) });
    await assert.rejects(stalled.request('/api/llm/config'), /超时/);
    const invalid = createApiClient({ fetchImpl: async () => new Response('<html>') });
    await assert.rejects(invalid.request('/api/llm/config'), /无效数据/);
});

test('user-request cancellation does not display an error banner', async () => {
    let reported = false;
    const client = createApiClient({ onError: () => { reported = true; }, fetchImpl: (path, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('cancelled')))) });
    const controller = new AbortController();
    const pending = client.request('/api/chart/data', { signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, /取消/);
    assert.equal(reported, false);
});

test('probe payload preserves unsaved fields and redacts echoed credentials', async () => {
    let payload;
    const client = createApiClient({ fetchImpl: async (path, init) => { payload = JSON.parse(init.body); return json({ success: false, message: 'upstream rejected unsaved-secret and admin-secret' }); } });
    client.setAdminKey('admin-secret');
    const response = await client.request('/api/llm/providers/p1/test', { method: 'POST', body: JSON.stringify({ api_key: 'unsaved-secret', base_url: 'https://unsaved.example/v1', model: 'sample' }) });
    assert.deepEqual(payload, { api_key: 'unsaved-secret', base_url: 'https://unsaved.example/v1', model: 'sample' });
    assert.doesNotMatch(response.message, /unsaved-secret|admin-secret/);
});

test('a valid mock administrator key cannot corrupt provider identifiers or URLs', async () => {
    const data = { active_provider_id: 'mock', providers: [{ id: 'mock', name: 'mock provider', base_url: 'https://mock.example/v1', models: ['mock-model'] }] };
    const client = createApiClient({ fetchImpl: async () => json(data) });
    client.setAdminKey('mock');
    assert.deepEqual(await client.request('/api/llm/providers'), data);
});
