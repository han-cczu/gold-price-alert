import { QuoteState, PERIOD_HOURS, convertUnitPrice, formatTimestamp } from "./state.js";
import { showError } from "./render.js";

export function createChart({ request, now }) {
    const state = new QuoteState({ now });
    let chart = null;
    let currentCurrency = "CNY";
    let currentUnit = "G";
    let exchangeRate = 7.2;
    const convertPrice = price => convertUnitPrice(price, { toUnit: currentUnit, toCurrency: currentCurrency, usdCny: exchangeRate });
    function updateChart(data) {
        const prices = data.prices.map(p => convertPrice(p));
        const currencySymbol = currentCurrency === 'USD' ? '$' : '¥';
        const unitLabel = currentUnit === 'OZ' ? '盎司' : (currentUnit === 'G' ? '克' : '公斤');

        const option = {
            tooltip: {
                trigger: 'axis',
                formatter: function(params) {
                    if (!params.length) return '';
                    const time = formatTimestamp(params[0].axisValue);
                    const price = params[0].data;
                    return `${time}<br/>价格: ${currencySymbol}${price.toFixed(2)}/${unitLabel}`;
                },
                backgroundColor: 'rgba(255,255,255,0.95)',
                borderColor: '#eee',
                borderWidth: 1,
                textStyle: { color: '#333' }
            },
            grid: {
                left: '3%',
                right: '4%',
                bottom: '3%',
                top: '5%',
                containLabel: true
            },
            xAxis: {
                type: 'category',
                data: data.timestamps,
                boundaryGap: false,
                axisLine: { lineStyle: { color: '#ddd' } },
                axisLabel: {
                    color: '#999', fontSize: 11,
                    formatter: value => formatTimestamp(value, state.period === '1D'
                        ? { hour: '2-digit', minute: '2-digit' }
                        : { month: '2-digit', day: '2-digit', ...(PERIOD_HOURS[state.period] > 168 ? { year: 'numeric' } : {}) }),
                },
                axisTick: { show: false }
            },
            yAxis: {
                type: 'value',
                axisLine: { show: false },
                axisLabel: {
                    color: '#999',
                    fontSize: 11,
                    formatter: val => currencySymbol + val.toFixed(2)
                },
                splitLine: { lineStyle: { color: '#f0f0f0', type: 'dashed' } }
            },
            series: [{
                name: '金价',
                type: 'line',
                data: prices,
                smooth: true,
                symbol: 'none',
                lineStyle: {
                    color: '#4ecdc4',
                    width: 2
                },
                areaStyle: {
                    color: {
                        type: 'linear',
                        x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: 'rgba(78, 205, 196, 0.4)' },
                            { offset: 1, color: 'rgba(78, 205, 196, 0.05)' }
                        ]
                    }
                },
                markLine: {
                    silent: true,
                    symbol: 'none',
                    data: prices.length ? [{
                        yAxis: prices[prices.length - 1],
                        lineStyle: { color: '#4ecdc4', type: 'dashed', width: 1 },
                        label: {
                            position: 'end',
                            formatter: currencySymbol + prices[prices.length - 1]?.toFixed(2),
                            color: '#4ecdc4',
                            backgroundColor: 'rgba(78, 205, 196, 0.1)',
                            padding: [4, 8],
                            borderRadius: 4
                        }
                    }] : []
                }
            }]
        };

        chart.setOption(option);
    }

    function render() {
        const data = state.snapshot();
        if (chart) updateChart(data);
        const symbol = currentCurrency === 'USD' ? '$' : '¥';
        document.getElementById('current-price').textContent = symbol + convertPrice(data.current_price).toFixed(2);
        const change = document.getElementById('price-change');
        change.textContent = (data.price_change_percent >= 0 ? '+' : '') + data.price_change_percent.toFixed(2) + '%';
        change.className = 'price-change ' + (data.price_change_percent >= 0 ? 'up' : 'down');
        for (const [id, value] of [['stat-high', data.high], ['stat-low', data.low], ['stat-avg', data.average]]) {
            document.getElementById(id).textContent = symbol + convertPrice(value).toFixed(2);
        }
        document.getElementById('stat-count').textContent = data.count;
        if (data.needs_refresh && !pending) fetchChartData();
    }

    let pending = null;
    async function fetchChartData(period = state.period) {
        pending?.abort();
        const controller = new AbortController();
        pending = controller;
        const token = state.beginRequest(period);
        render();
        try {
            const data = await request(`/api/chart/data?hours=${PERIOD_HOURS[period]}`, { signal: controller.signal });
            if (state.acceptSnapshot(token, data)) render();
        } catch (error) {
            if (!controller.signal.aborted) showError(error);
        } finally {
            if (pending === controller) pending = null;
        }
    }
    const resize = () => chart?.resize();
    function init() {
        if (globalThis.echarts) {
            chart = globalThis.echarts.init(document.getElementById('chart'));
            window.addEventListener('resize', resize);
        } else showError(new Error('图表组件加载失败，请刷新页面；价格数据仍会更新'));
        for (const [container, attribute] of [['currency-switch', 'currency'], ['unit-switch', 'unit']]) {
            document.getElementById(container).addEventListener('click', event => {
                const button = event.target.closest(`[data-${attribute}]`);
                if (!button) return;
                document.querySelectorAll(`#${container} .switch-btn`).forEach(item => item.classList.toggle('active', item === button));
                if (attribute === 'currency') currentCurrency = button.dataset.currency;
                else currentUnit = button.dataset.unit;
                render();
            });
        }
        document.querySelector('.period-group').addEventListener('click', event => {
            const button = event.target.closest('[data-period]');
            if (!button) return;
            document.querySelectorAll('.period-btn').forEach(item => item.classList.toggle('active', item === button));
            fetchChartData(button.dataset.period);
        });
        render();
    }
    return {
        init, fetchChartData, render,
        onPriceUpdate(data) { if (state.acceptPrice(data)) render(); },
        setExchangeRate(rate) { exchangeRate = rate; render(); },
        dispose() { pending?.abort(); window.removeEventListener('resize', resize); chart?.dispose(); },
    };
}
