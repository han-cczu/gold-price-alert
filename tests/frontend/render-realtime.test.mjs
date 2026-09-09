import test from 'node:test';
import assert from 'node:assert/strict';
import { escapeHtml, renderMarkdown } from '../../src/gold_monitor/static/js/render.js';
import { createRealtime } from '../../src/gold_monitor/static/js/realtime.js';

test('untrusted provider, alert and source text cannot escape a quoted HTML attribute', () => {
    const payload = '\"><img src=x onerror="alert(1)">&';
    const safe = escapeHtml(payload);
    assert.doesNotMatch(safe, /[<>"']/);
    assert.match(safe, /&quot;/);
    assert.match(safe, /&lt;img/);
});

test('Markdown uses sanitizer output and safely falls back if dependencies are absent', () => {
    let sanitizedInput;
    const text = '<img src=x onerror="bad()">';
    const output = renderMarkdown(text, {
        parser: { parse: value => `<p>${value}</p>` },
        sanitizer: { sanitize: html => { sanitizedInput = html; return '<p>safe</p>'; } },
    });
    assert.equal(sanitizedInput, `<p>${text}</p>`);
    assert.equal(output, '<p>safe</p>');
    assert.equal(renderMarkdown(text, { sanitizer: null, parser: null }), escapeHtml(text));
});

test('realtime filters malformed messages and disposes sockets without reconnecting', async () => {
    const sockets = [];
    class FakeSocket {
        constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
        close() { this.readyState = 3; this.onclose?.(); }
        send() {}
    }
    const messages = [];
    const client = createRealtime({ WebSocketImpl: FakeSocket, location: { protocol: 'https:', host: 'gold.test' }, onMessage: message => messages.push(message), retryMs: 2 });
    client.start();
    client.start();
    assert.equal(sockets.length, 1);
    assert.equal(sockets[0].url, 'wss://gold.test/ws');
    sockets[0].readyState = 1;
    sockets[0].onopen();
    sockets[0].onmessage({ data: '{bad' });
    sockets[0].onmessage({ data: 'null' });
    sockets[0].onmessage({ data: JSON.stringify({ type: 'price_update', data: { price: 2000 } }) });
    assert.equal(messages.length, 1);
    assert.equal(client.connected, true);
    client.stop();
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(client.connected, false);
    assert.equal(sockets.length, 1);
});

test('dropped sockets reconnect and stale socket frames are ignored', async () => {
    const sockets = [];
    class FakeSocket {
        constructor() { this.readyState = 0; sockets.push(this); }
        close() { this.readyState = 3; this.onclose?.(); }
        send() {}
    }
    const messages = [];
    const client = createRealtime({ WebSocketImpl: FakeSocket, location: { protocol: 'http:', host: 'gold.test' }, onMessage: message => messages.push(message), retryMs: 2 });
    try {
        client.start();
        sockets[0].close();
        await new Promise(resolve => setTimeout(resolve, 10));
        assert.equal(sockets.length, 2);
        sockets[0].onmessage({ data: '{"type":"alert"}' });
        sockets[1].onmessage({ data: '{"type":"alert"}' });
        assert.equal(messages.length, 1);
    } finally { client.stop(); }
});
