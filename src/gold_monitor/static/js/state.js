export const PERIOD_HOURS = Object.freeze({ '1D': 24, '7D': 168, '1M': 720, '6M': 4320, '1Y': 8760, '5Y': 43800 });
export const UNIT_FACTORS = Object.freeze({ OZ: 1, G: 31.1035, KG: 0.0311035 });

/** Existing timestamps without an offset represent UTC, never browser local time. */
export function parseUtc(value) {
    if (value instanceof Date) return new Date(value.getTime());
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return new Date(NaN);
    return new Date(/[zZ]$|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`);
}

export function formatTimestamp(value, options = {}) {
    const date = parseUtc(value);
    return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', options);
}

export function convertUnitPrice(price, { fromUnit = 'OZ', toUnit = 'G', fromCurrency = 'USD', toCurrency = 'CNY', usdCny = 7.2 } = {}) {
    const from = UNIT_FACTORS[fromUnit.toUpperCase()];
    const to = UNIT_FACTORS[toUnit.toUpperCase()];
    if (!from || !to || !['USD', 'CNY'].includes(fromCurrency) || !['USD', 'CNY'].includes(toCurrency) || !Number.isFinite(price) || !Number.isFinite(usdCny) || usdCny <= 0) throw new Error('无效的价格、单位或汇率');
    const currencyRate = fromCurrency === toCurrency ? 1 : (fromCurrency === 'USD' ? usdCny : 1 / usdCny);
    return price * from / to * currencyRate;
}

export function summarize(points) {
    const prices = points.map(point => point.price);
    const current = prices.at(-1) ?? 0;
    const first = prices[0] ?? 0;
    return {
        timestamps: points.map(point => new Date(point.time).toISOString()), prices,
        current_price: current, price_change: current - first,
        price_change_percent: first > 0 ? (current - first) / first * 100 : 0,
        high: prices.length ? prices.reduce((maximum, price) => Math.max(maximum, price), -Infinity) : 0,
        low: prices.length ? prices.reduce((minimum, price) => Math.min(minimum, price), Infinity) : 0,
        average: prices.length ? prices.reduce((total, price) => total + price, 0) / prices.length : 0,
        count: prices.length,
    };
}

export class QuoteState {
    constructor({ period = '1D', now = () => Date.now() } = {}) {
        this.period = period;
        this.now = now;
        this.points = [];
        this.version = 0;
        this.sequence = 0;
        this.events = [];
        this.baseline = null;
        this.livePoints = new Map();
        this.dirty = false;
        this.latestQuote = null;
    }
    beginRequest(period = this.period) {
        if (!PERIOD_HOURS[period]) throw new Error('无效的时间窗口');
        if (period !== this.period) {
            this.points = [];
            this.baseline = null;
            this.livePoints.clear();
            this.dirty = false;
        }
        this.period = period;
        return { version: ++this.version, sequence: this.sequence, period };
    }
    acceptSnapshot(token, data) {
        if (token.version !== this.version || token.period !== this.period) return false;
        if (!Array.isArray(data.timestamps) || !Array.isArray(data.prices) || data.timestamps.length !== data.prices.length) throw new Error('图表数据格式无效');
        const points = data.timestamps.map((value, i) => ({ time: parseUtc(value).getTime(), price: data.prices[i] }));
        if (points.some(point => !Number.isFinite(point.time) || !Number.isFinite(point.price))) throw new Error('图表包含无效的时间或价格');
        const recentEvents = this.events.filter(point => point.sequence > token.sequence);
        this.livePoints.clear();
        this.dirty = false;
        if (Number.isFinite(data.count) && Number.isFinite(data.average)) {
            const fields = ['current_price', 'price_change', 'price_change_percent', 'high', 'low', 'average', 'count'];
            if (fields.some(field => !Number.isFinite(data[field])) || data.count < points.length) throw new Error('图表统计数据无效');
            const windowStart = parseUtc(data.window_start).getTime();
            const windowEnd = parseUtc(data.window_end).getTime();
            if (!Number.isFinite(windowStart) || !Number.isFinite(windowEnd)) throw new Error('图表时间窗口无效');
            // Samples draw the line; only the server knows the full-window statistics.
            this.baseline = {
                points, stats: Object.fromEntries(fields.map(field => [field, data[field]])),
                firstTime: points[0]?.time ?? Infinity, lastTime: points.at(-1)?.time ?? -Infinity,
                windowStart, windowEnd,
            };
            for (const point of recentEvents) if (point.recorded) this.accumulate(point);
            this.points = this.merge([...points, ...this.livePoints.values()]);
        } else {
            // Compatibility with old, unaggregated responses containing all records.
            this.baseline = null;
            this.points = this.merge([...points, ...recentEvents.filter(point => point.recorded)]);
        }
        return true;
    }
    acceptPrice(data) {
        const point = {
            time: parseUtc(data.timestamp).getTime(), price: data.price,
            sequence: ++this.sequence, recorded: data.recorded !== false,
        };
        if (!Number.isFinite(point.time) || !Number.isFinite(point.price)) return false;
        if (!this.latestQuote || point.time >= this.latestQuote.time) this.latestQuote = point;
        this.events.push(point);
        this.events = this.events.slice(-1000);
        if (!point.recorded) return true;
        if (this.baseline) {
            this.accumulate(point);
            this.points = this.merge([...this.baseline.points, ...this.livePoints.values()]);
        } else this.points = this.merge([...this.points, point]);
        return true;
    }
    accumulate(point) {
        if (point.time < this.baseline.windowStart) return;
        if (point.time > this.baseline.lastTime) this.livePoints.set(point.time, point);
        else {
            const existing = this.baseline.points.find(sample => sample.time === point.time);
            // An older unseen record may already be included in the aggregates.
            // Query again rather than guessing whether it changes the full count.
            if (!existing || existing.price !== point.price) this.dirty = true;
        }
    }
    get needsRefresh() {
        if (!this.baseline) return false;
        const start = this.now() - PERIOD_HOURS[this.period] * 3600000;
        return this.dirty || this.baseline.firstTime < start || [...this.livePoints.values()].some(point => point.time < start);
    }
    merge(points) {
        const start = this.now() - PERIOD_HOURS[this.period] * 3600000;
        const unique = new Map();
        for (const point of points) if (point.time >= start) unique.set(point.time, point);
        return [...unique.values()].sort((a, b) => a.time - b.time);
    }
    snapshot() {
        this.points = this.merge(this.points);
        const samples = summarize(this.points);
        if (!this.baseline) return this.withLatestQuote(samples, this.points.at(-1)?.time ?? -Infinity);
        const stats = { ...this.baseline.stats };
        const live = [...this.livePoints.values()].sort((a, b) => a.time - b.time);
        if (live.length) {
            const liveStats = summarize(live);
            const firstPrice = stats.count ? stats.current_price - stats.price_change : live[0].price;
            const count = stats.count + live.length;
            stats.average = (stats.average * stats.count + liveStats.average * live.length) / count;
            stats.high = stats.count ? Math.max(stats.high, liveStats.high) : liveStats.high;
            stats.low = stats.count ? Math.min(stats.low, liveStats.low) : liveStats.low;
            stats.current_price = liveStats.current_price;
            stats.price_change = stats.current_price - firstPrice;
            stats.price_change_percent = firstPrice > 0 ? stats.price_change / firstPrice * 100 : 0;
            stats.count = count;
        }
        return this.withLatestQuote({
            ...samples, ...stats,
            window_start: new Date(this.baseline.windowStart).toISOString(),
            window_end: new Date(Math.max(this.baseline.windowEnd, live.at(-1)?.time ?? 0)).toISOString(),
            needs_refresh: this.needsRefresh,
        }, Math.max(this.baseline.lastTime, live.at(-1)?.time ?? -Infinity));
    }
    withLatestQuote(stats, lastRecordedTime) {
        const quote = this.latestQuote;
        if (!quote || quote.recorded || quote.time <= lastRecordedTime) return stats;
        // Latest received prices may be deduplicated rather than stored. They
        // change the headline only; the history and its aggregates remain exact.
        const firstPrice = stats.count ? stats.current_price - stats.price_change : quote.price;
        return {
            ...stats,
            current_price: quote.price,
            price_change: quote.price - firstPrice,
            price_change_percent: firstPrice > 0 ? (quote.price - firstPrice) / firstPrice * 100 : 0,
        };
    }
}
