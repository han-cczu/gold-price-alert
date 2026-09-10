import test from 'node:test';
import assert from 'node:assert/strict';
import { createSettings } from '../../src/gold_monitor/static/js/settings.js';
import { createAnalysis } from '../../src/gold_monitor/static/js/analysis.js';
import { createChart } from '../../src/gold_monitor/static/js/chart.js';
import { createMarket } from '../../src/gold_monitor/static/js/market.js';

function mockDocument(t) {
    const nodes = new Map();
    const createNode = () => ({
        value: '', textContent: '', innerHTML: '', style: {}, options: [], disabled: false,
        appendChild(child) { this.options.push(child); },
        replaceChildren(...children) { this.options = children; },
        querySelectorAll() { return []; }, addEventListener() {},
    });
    const document = {
        getElementById(id) { if (!nodes.has(id)) nodes.set(id, createNode()); return nodes.get(id); },
        querySelector() { return createNode(); }, createElement: createNode,
    };
    const previousDocument = globalThis.document;
    const previousOption = globalThis.Option;
    globalThis.document = document;
    globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
    t.after(() => { globalThis.document = previousDocument; globalThis.Option = previousOption; });
    return id => document.getElementById(id);
}

const providerConfig = { providers: [{ id: 'p1', name: 'Original', base_url: 'https://saved.test/v1', has_api_key: true, api_key: 'saved...', models: ['old-model'] }], active_provider_id: 'p1', active_model: 'old-model' };
const response = data => data;

test('history preserves original mixed currencies and renders unknown currency as text', async t => {
    const element = mockDocument(t);
    const rows = [];
    element('history-table').insertRow = () => {
        const cells = [];
        rows.push(cells);
        return { insertCell() {
            const cell = { textContent: '' };
            Object.defineProperty(cell, 'innerHTML', { set() { assert.fail('history values must use textContent'); } });
            cells.push(cell);
            return cell;
        } };
    };
    const data = [
        { currency: 'USD', price: 2010.25 },
        { currency: 'CNY', price: 450.6 },
        { currency: 'EUR', price: 1900 },
        { currency: '<img src=x onerror=bad()>', price: 12.34 },
    ].map(price => ({ ...price, timestamp: '2026-09-09T08:00:00Z', source: '<script>bad()</script>' }));
    const market = createMarket({ request: async path => {
        assert.equal(path, '/api/price/history?limit=10');
        return { data };
    }, onExchangeRate: () => {} });
    await market.fetchHistory();
    assert.deepEqual(rows.map(cells => cells[1].textContent), [
        '$2010.25', '¥450.60', 'EUR 1900.00', '<img src=x onerror=bad()> 12.34',
    ]);
    assert.ok(rows.every(cells => cells[2].textContent === '<script>bad()</script>'));
});

test('model probe uses unsaved form credentials and leaves them in the form', async t => {
    const element = mockDocument(t);
    let sent;
    const settings = createSettings({ onChanged: async () => {}, request: async (path, init) => {
        if (path.endsWith('/models')) { sent = JSON.parse(init.body); return response({ success: true, count: 1, models: [{ id: 'new-model' }] }); }
        return response(providerConfig);
    } });
    await settings.loadProviders();
    settings.selectProvider('p1');
    element('provider-url').value = 'https://unsaved.test/v1';
    element('provider-key').value = 'unsaved-secret';
    element('provider-model-custom').value = 'my-custom-model';
    await settings.fetchProviderModels();
    assert.deepEqual(sent, { api_key: 'unsaved-secret', base_url: 'https://unsaved.test/v1' });
    assert.equal(element('provider-url').value, 'https://unsaved.test/v1');
    assert.equal(element('provider-key').value, 'unsaved-secret');
    assert.equal(element('provider-model-custom').value, 'my-custom-model');
});

test('connection probe sends custom model and reports failure as text', async t => {
    const element = mockDocument(t);
    let sent;
    const settings = createSettings({ onChanged: async () => {}, request: async (path, init) => {
        if (path.endsWith('/test')) { sent = JSON.parse(init.body); return response({ success: false, message: '<img src=x onerror=bad()>' }); }
        return response(providerConfig);
    } });
    await settings.loadProviders();
    settings.selectProvider('p1');
    element('provider-model-custom').value = 'custom';
    element('provider-key').value = '';
    await settings.testProviderConnection();
    assert.equal(sent.model, 'custom');
    assert.equal(sent.api_key, null);
    assert.equal(element('settings-status').textContent, '<img src=x onerror=bad()>');
    assert.equal(element('settings-status').innerHTML, '');
    assert.equal(element('settings-status').style.background, '#ffebee');
});

test('failed save cannot display a success message or refresh active analysis', async t => {
    const element = mockDocument(t);
    let refreshed = false;
    const settings = createSettings({ onChanged: async () => { refreshed = true; }, request: async (path, init) => {
        if (init?.method === 'PUT') throw new Error('保存失败');
        return response(providerConfig);
    } });
    await settings.loadProviders();
    settings.selectProvider('p1');
    await settings.saveProviderConfig();
    assert.equal(element('settings-status').textContent, '保存失败');
    assert.equal(refreshed, false);
});

test('a cached analysis response cannot overwrite a later manual refresh', async t => {
    const element = mockDocument(t);
    let releaseOld;
    const oldResponse = new Promise(resolve => { releaseOld = resolve; });
    const report = title => ({ title, market_overview: title, buy_timing: 'timing', recommendation: 'recommendation', key_factors: [], model_used: null, web_search_used: false });
    const analysis = createAnalysis({ onOpenSettings: () => {}, request: async path => path.endsWith('/refresh') ? response({ success: true, data: report('new analysis') }) : oldResponse });
    const loading = analysis.fetchSmartAnalysis();
    await analysis.refreshSmartAnalysis();
    releaseOld(response(report('outdated analysis')));
    await loading;
    assert.match(element('smart-analysis-content').innerHTML, /new analysis/);
    assert.doesNotMatch(element('smart-analysis-content').innerHTML, /outdated analysis/);
});

test('chart refreshes full-window statistics after the rolling boundary expires', async t => {
    const element = mockDocument(t);
    let clock = Date.parse('2026-09-09T08:00:00Z');
    const timestamp = hours => new Date(Date.parse('2026-09-09T08:00:00Z') - hours * 3600000).toISOString();
    const baseline = {
        timestamps: [timestamp(23), timestamp(1)], prices: [2000, 2200],
        current_price: 2200, high: 2400, low: 1900, average: 2075, count: 100,
        price_change: 200, price_change_percent: 10,
        window_start: timestamp(24), window_end: timestamp(0),
    };
    let requests = 0;
    let releaseRefresh;
    const refresh = new Promise(resolve => { releaseRefresh = resolve; });
    const chart = createChart({ now: () => clock, request: async () => ++requests === 1 ? baseline : refresh });
    await chart.fetchChartData();
    assert.equal(element('stat-count').textContent, 100);
    const expectedAverageCny = (2075 * 7.2 / 31.1035).toFixed(2);
    assert.equal(element('stat-avg').textContent, `¥${expectedAverageCny}`);
    clock += 2 * 3600000;
    chart.render();
    chart.render();
    assert.equal(requests, 2, 'one refresh is shared while the window is stale');
    releaseRefresh({
        ...baseline, timestamps: [timestamp(1)], prices: [2200],
        high: 2200, low: 2200, average: 2200, count: 1, price_change: 0, price_change_percent: 0,
        window_start: timestamp(22), window_end: timestamp(-2),
    });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(element('stat-count').textContent, 1);
    assert.equal(element('price-change').textContent, '+0.00%');
});
