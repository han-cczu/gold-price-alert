import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { QuoteState, convertUnitPrice, parseUtc, formatTimestamp, summarize } from '../../src/gold_monitor/static/js/state.js';

const now = Date.parse('2026-09-09T08:00:00Z');
const iso = hours => new Date(now - hours * 3600000).toISOString();

test('oz/g/kg conversion uses the same physical unit in both directions', () => {
    assert.equal(convertUnitPrice(31.1035, { toUnit: 'G', toCurrency: 'USD' }), 1);
    assert.equal(convertUnitPrice(31.1035, { toUnit: 'KG', toCurrency: 'USD' }), 1000);
    for (const fromUnit of ['oz', 'g', 'kg']) for (const toUnit of ['oz', 'g', 'kg']) {
        const forward = convertUnitPrice(2050, { fromUnit, toUnit, usdCny: 7.13 });
        const backward = convertUnitPrice(forward, { fromUnit: toUnit, toUnit: fromUnit, fromCurrency: 'CNY', toCurrency: 'USD', usdCny: 7.13 });
        assert.ok(Math.abs(backward - 2050) < 1e-8);
    }
    assert.throws(() => convertUnitPrice(2000, { toUnit: 'lb' }));
    assert.throws(() => convertUnitPrice(2000, { usdCny: 0 }));
});

test('legacy UTC, Z and explicit offsets denote the same instant', () => {
    const values = ['2026-09-09T08:00:00', '2026-09-09T08:00:00Z', '2026-09-09T16:00:00+08:00'];
    for (const value of values) assert.equal(parseUtc(value).getTime(), now);
    assert.match(formatTimestamp(values[0], { timeZone: 'Asia/Shanghai', hour12: false }), /16:00:00/);
    assert.match(formatTimestamp(values[0], { timeZone: 'UTC', hour12: false }), /08:00:00/);
    assert.equal(formatTimestamp(null), '时间未知');
});

test('browser timezone cannot reinterpret a legacy naive UTC timestamp', () => {
    const moduleUrl = new URL('../../src/gold_monitor/static/js/state.js', import.meta.url).href;
    const script = `import { parseUtc } from ${JSON.stringify(moduleUrl)}; process.stdout.write(String(parseUtc('2026-09-09T08:00:00').getTime()));`;
    for (const TZ of ['UTC', 'Asia/Shanghai']) {
        const value = execFileSync(process.execPath, ['--input-type=module', '-e', script], { env: { ...process.env, TZ }, encoding: 'utf8' });
        assert.equal(Number(value), now);
    }
});

test('HTTP and realtime updates recompute the same complete summary', () => {
    const state = new QuoteState({ now: () => now });
    state.acceptSnapshot(state.beginRequest(), { timestamps: [iso(3), iso(2)], prices: [2000, 2100] });
    state.acceptPrice({ timestamp: iso(1), price: 1900 });
    const actual = state.snapshot();
    assert.equal(actual.current_price, 1900);
    assert.equal(actual.high, 2100);
    assert.equal(actual.low, 1900);
    assert.equal(actual.average, 2000);
    assert.equal(actual.price_change_percent, -5);
    const snapshotOnly = new QuoteState({ now: () => now });
    snapshotOnly.acceptSnapshot(snapshotOnly.beginRequest(), { timestamps: [iso(3), iso(2), iso(1)], prices: [2000, 2100, 1900] });
    assert.deepEqual(actual, snapshotOnly.snapshot());
});

test('late responses for a previously selected period cannot replace current data', () => {
    const state = new QuoteState({ now: () => now });
    const first = state.beginRequest('1D');
    const second = state.beginRequest('7D');
    assert.equal(state.acceptSnapshot(second, { timestamps: [iso(30)], prices: [2200] }), true);
    assert.equal(state.acceptSnapshot(first, { timestamps: [iso(1)], prices: [9999] }), false);
    assert.deepEqual(state.snapshot().prices, [2200]);
});

test('stream updates received during an HTTP request survive its older snapshot', () => {
    const state = new QuoteState({ now: () => now });
    const token = state.beginRequest();
    state.acceptPrice({ timestamp: iso(1), price: 2150 });
    state.acceptSnapshot(token, { timestamps: [iso(2), iso(1)], prices: [2000, 2100] });
    assert.deepEqual(state.snapshot().prices, [2000, 2150]);
});

test('out-of-order updates are sorted and expired samples leave the selected window', () => {
    let clock = now;
    const state = new QuoteState({ now: () => clock });
    state.acceptPrice({ timestamp: iso(23), price: 2000 });
    state.acceptPrice({ timestamp: iso(1), price: 2100 });
    state.acceptPrice({ timestamp: iso(2), price: 2050 });
    assert.deepEqual(state.snapshot().prices, [2000, 2050, 2100]);
    clock += 2 * 3600000;
    assert.deepEqual(state.snapshot().prices, [2050, 2100]);
    assert.equal(state.acceptPrice({ timestamp: 'not-a-date', price: 2222 }), false);
});

test('empty and large histories produce finite summaries without argument overflow', () => {
    assert.equal(summarize([]).price_change_percent, 0);
    const points = Array.from({ length: 150000 }, (_, i) => ({ time: now + i, price: 2000 + i }));
    assert.equal(summarize(points).high, 151999);
});

test('aggregated HTTP metadata remains authoritative even when sampled points differ', () => {
    const state = new QuoteState({ now: () => now });
    const metadata = {
        timestamps: [iso(3), iso(1)], prices: [2000, 2100],
        current_price: 2300, high: 2400, low: 1900, average: 2075, count: 100,
        price_change: 400, price_change_percent: 21.052631578947366,
        window_start: iso(24), window_end: iso(0),
    };
    state.acceptSnapshot(state.beginRequest(), metadata);
    const actual = state.snapshot();
    for (const field of ['current_price', 'high', 'low', 'average', 'count', 'price_change', 'price_change_percent']) assert.equal(actual[field], metadata[field]);
    assert.equal(actual.prices.length, 2);
    assert.notEqual(actual.average, 2050);
    assert.notEqual(actual.price_change_percent, 5);
});

test('realtime extends full-window count, weighted average and price change without recounting samples', () => {
    const state = new QuoteState({ now: () => now });
    state.acceptSnapshot(state.beginRequest(), {
        timestamps: [iso(3), iso(1)], prices: [2000, 2200],
        current_price: 2200, high: 2400, low: 1900, average: 2075, count: 100,
        price_change: 200, price_change_percent: 10,
        window_start: iso(24), window_end: iso(0),
    });
    state.acceptPrice({ timestamp: iso(0), price: 2300 });
    state.acceptPrice({ timestamp: iso(0), price: 2300 });
    const actual = state.snapshot();
    assert.equal(actual.count, 101);
    assert.equal(actual.average, (2075 * 100 + 2300) / 101);
    assert.equal(actual.high, 2400);
    assert.equal(actual.current_price, 2300);
    assert.equal(actual.price_change_percent, 15);
});

test('sliding past a baseline endpoint invalidates the complete aggregate', () => {
    let clock = now;
    const state = new QuoteState({ now: () => clock });
    state.acceptSnapshot(state.beginRequest(), {
        timestamps: [iso(23), iso(1)], prices: [2000, 2200],
        current_price: 2200, high: 2400, low: 1900, average: 2075, count: 100,
        price_change: 200, price_change_percent: 10,
        window_start: iso(24), window_end: iso(0),
    });
    assert.equal(state.needsRefresh, false);
    clock += 2 * 3600000;
    assert.equal(state.snapshot().needs_refresh, true);
    // The old full-window aggregate is retained until the next server response.
    assert.equal(state.snapshot().count, 100);
});

test('unrecorded quotes update the headline but survive HTTP refresh without changing stored aggregates', () => {
    const state = new QuoteState({ now: () => now });
    const baseline = {
        timestamps: [iso(3), iso(2)], prices: [2000, 2100],
        current_price: 2100, high: 2100, low: 2000, average: 2050, count: 2,
        price_change: 100, price_change_percent: 5,
        window_start: iso(24), window_end: iso(0),
    };
    state.acceptSnapshot(state.beginRequest(), baseline);
    state.acceptPrice({ timestamp: iso(1), price: 2100.001, recorded: false });
    let actual = state.snapshot();
    assert.equal(actual.current_price, 2100.001);
    assert.ok(Math.abs(actual.price_change - 100.001) < 1e-9);
    assert.deepEqual(actual.prices, [2000, 2100]);
    for (const field of ['average', 'count', 'high', 'low']) assert.equal(actual[field], baseline[field]);
    // A refresh started after the deduplicated quote still retains its headline.
    state.acceptSnapshot(state.beginRequest(), baseline);
    actual = state.snapshot();
    assert.equal(actual.current_price, 2100.001);
    assert.equal(actual.count, 2);
    assert.equal(actual.average, 2050);
    state.acceptPrice({ timestamp: iso(0), price: 2200, recorded: true });
    actual = state.snapshot();
    assert.equal(actual.current_price, 2200);
    assert.equal(actual.count, 3);
    assert.equal(actual.average, 2100);
    assert.equal(actual.high, 2200);
    assert.equal(actual.price_change_percent, 10);
    assert.deepEqual(actual.prices, [2000, 2100, 2200]);
});

test('mixed recorded and unrecorded events merge correctly during an HTTP request', () => {
    const state = new QuoteState({ now: () => now });
    const token = state.beginRequest();
    state.acceptPrice({ timestamp: iso(2), price: 2100, recorded: true });
    state.acceptPrice({ timestamp: iso(1), price: 2100.001, recorded: false });
    state.acceptSnapshot(token, {
        timestamps: [iso(3)], prices: [2000], current_price: 2000,
        high: 2000, low: 2000, average: 2000, count: 1,
        price_change: 0, price_change_percent: 0, window_start: iso(24), window_end: iso(0),
    });
    const actual = state.snapshot();
    assert.equal(actual.current_price, 2100.001);
    assert.equal(actual.count, 2);
    assert.equal(actual.average, 2050);
    assert.deepEqual(actual.prices, [2000, 2100]);
});

test('old events without recorded retain the existing persisted-price behavior', () => {
    const state = new QuoteState({ now: () => now });
    state.acceptPrice({ timestamp: iso(2), price: 2000 });
    state.acceptPrice({ timestamp: iso(1), price: 2100.001, recorded: false });
    assert.equal(state.snapshot().count, 1);
    assert.equal(state.snapshot().current_price, 2100.001);
    state.acceptPrice({ timestamp: iso(0), price: 2200 });
    assert.equal(state.snapshot().count, 2);
    assert.equal(state.snapshot().current_price, 2200);
    assert.deepEqual(state.snapshot().prices, [2000, 2200]);
});
