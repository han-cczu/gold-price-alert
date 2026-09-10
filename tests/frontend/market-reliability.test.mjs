import test from 'node:test';
import assert from 'node:assert/strict';
import { QuoteState } from '../../src/gold_monitor/static/js/state.js';
import { createMarket } from '../../src/gold_monitor/static/js/market.js';

const now = Date.parse('2026-09-09T08:00:01Z');
const baseline = {
    timestamps: ['2026-09-09T07:59:59Z'], prices: [2000], current_price: 2000,
    high: 2000, low: 2000, average: 2000, count: 1,
    price_change: 0, price_change_percent: 0,
    window_start: '2026-09-08T08:00:01Z', window_end: '2026-09-09T08:00:01Z',
};

test('microsecond records preserve the HTTP curve and live full-window statistics', () => {
    const high = '2026-09-09T08:00:00.123001Z';
    const last = '2026-09-09T08:00:00.123999Z';
    const live = new QuoteState({ now: () => now });
    live.acceptSnapshot(live.beginRequest(), baseline);
    // Deliberately reversed arrival order in the same browser millisecond.
    live.acceptPrice({ timestamp: last, price: 1900, recorded: true });
    live.acceptPrice({ timestamp: high, price: 2500, recorded: true });
    const actual = live.snapshot();
    assert.deepEqual(actual.prices, [2000, 2500, 1900]);
    assert.deepEqual(actual.timestamps.slice(1), [high, last]);
    assert.equal(actual.count, 3);
    assert.equal(actual.high, 2500);
    assert.equal(actual.average, (2000 + 2500 + 1900) / 3);
    assert.equal(actual.current_price, 1900);
    assert.equal(actual.needs_refresh, false);

    const snapshot = new QuoteState({ now: () => now });
    snapshot.acceptSnapshot(snapshot.beginRequest(), {
        ...baseline, ...actual, timestamps: [...baseline.timestamps, high, last],
    });
    assert.deepEqual(snapshot.snapshot().prices, [2000, 2500, 1900]);
    assert.deepEqual(snapshot.snapshot().timestamps.slice(1), [high, last]);
});

test('microsecond ordering distinguishes an HTTP endpoint from later realtime records', () => {
    const first = '2026-09-09T08:00:00.123001Z';
    const next = '2026-09-09T08:00:00.123999Z';
    const state = new QuoteState({ now: () => now });
    const token = state.beginRequest();
    state.acceptPrice({ timestamp: next, price: 2200, recorded: true });
    state.acceptSnapshot(token, { ...baseline, timestamps: [first] });
    // Equivalent UTC, offset and legacy-naive timestamps are the same record.
    state.acceptPrice({ timestamp: '2026-09-09T16:00:00.123999+08:00', price: 2200, recorded: true });
    state.acceptPrice({ timestamp: '2026-09-09T08:00:00.123999', price: 2200, recorded: true });
    assert.equal(state.snapshot().count, 2);
    assert.equal(state.snapshot().needs_refresh, false);
    assert.deepEqual(state.snapshot().prices, [2000, 2200]);

    state.acceptPrice({ timestamp: '2026-09-09T08:00:00.124999Z', price: 2200.002, recorded: false });
    state.acceptPrice({ timestamp: '2026-09-09T08:00:00.124001Z', price: 2200.001, recorded: false });
    const actual = state.snapshot();
    assert.equal(actual.current_price, 2200.002, 'older unrecorded quotes cannot replace the newer headline');
    assert.equal(actual.count, 2);
    assert.equal(actual.average, 2100);
    assert.deepEqual(actual.prices, [2000, 2200]);
});

test('an unrecorded quote later in the same millisecond changes only the headline', () => {
    const state = new QuoteState({ now: () => now });
    const snapshot = { ...baseline, timestamps: ['2026-09-09T08:00:00.123001Z'] };
    state.acceptSnapshot(state.beginRequest(), snapshot);
    state.acceptPrice({ timestamp: '2026-09-09T08:00:00.123999Z', price: 2000.001, recorded: false });
    state.acceptSnapshot(state.beginRequest(), snapshot);
    const actual = state.snapshot();
    assert.equal(actual.current_price, 2000.001);
    assert.equal(actual.count, 1);
    assert.equal(actual.average, 2000);
    assert.deepEqual(actual.prices, [2000]);
});

function mockDocument(t) {
    const nodes = new Map();
    const previous = globalThis.document;
    globalThis.document = { getElementById(id) {
        if (!nodes.has(id)) {
            let html = '', text = '';
            nodes.set(id, {
                get innerHTML() { return html; }, set innerHTML(value) { html = value; text = ''; },
                get textContent() { return text; }, set textContent(value) { text = value; html = ''; },
            });
        }
        return nodes.get(id);
    } };
    t.after(() => { globalThis.document = previous; });
    return id => globalThis.document.getElementById(id);
}

const bankData = {
    data: [{ bank_name: '工商银行', buy_price: 549.5, sell_price: 550.5 }],
    london_gold_cny: 550, updated_at: '2026-09-09T08:00:00Z',
};

test('missing and invalid bank prices replace old cards with unavailable instead of zero', async t => {
    const element = mockDocument(t);
    let data = bankData;
    const market = createMarket({ onExchangeRate() {}, request: async () => data });
    await market.fetchBankPrices();
    assert.match(element('bank-cards').innerHTML, /¥549\.50/);
    for (const value of [null, undefined, NaN, Infinity, -5, 0, '550']) {
        data = { ...bankData, data: [{ ...bankData.data[0], buy_price: value }] };
        await market.fetchBankPrices();
        assert.equal(element('bank-cards').textContent, '银行参考报价暂不可用');
        assert.equal(element('bank-cards').innerHTML, '');
        assert.equal(element('london-gold-price').textContent, '暂不可用');
    }
    for (const unavailable of [
        { data: [], london_gold_cny: null, updated_at: null, is_fallback: true, is_stale: true },
        { ...bankData, london_gold_cny: null },
        { ...bankData, data: [{ ...bankData.data[0], sell_price: null }] },
        { ...bankData, data: [{ ...bankData.data[0], sell_price: 500 }] },
    ]) {
        data = unavailable;
        await market.fetchBankPrices();
        assert.equal(element('bank-cards').textContent, '银行参考报价暂不可用');
        assert.equal(element('london-gold-price').textContent, '暂不可用');
    }
});

test('bank cache retains prices and visibly labels the original successful update', async t => {
    const element = mockDocument(t);
    const market = createMarket({ onExchangeRate() {}, request: async () => ({
        ...bankData, is_fallback: true, is_stale: true,
    }) });
    await market.fetchBankPrices();
    assert.match(element('bank-cards').innerHTML, /参考缓存 · 上次成功更新/);
    assert.match(element('bank-cards').innerHTML, /¥549\.50/);
    assert.match(element('bank-cards').innerHTML, /¥550\.50/);
    assert.equal(element('london-gold-price').textContent, '¥550.00/克（参考缓存）');
});
