import { convertUnitPrice, formatTimestamp } from './state.js';
import { escapeHtml, showError } from './render.js';

const BANK_COLORS = Object.freeze({ '工商银行': '#C80000', '中国银行': '#C2000B', '建设银行': '#0066B3', '农业银行': '#009966', '交通银行': '#003087', '招商银行': '#C41230', '兴业银行': '#0055A5', '民生银行': '#00A0E9', '浦发银行': '#003399', '光大银行': '#7B2D8E', '平安银行': '#FA6400', '中信银行': '#E60012' });
const ALERT_ICONS = Object.freeze({ threshold_upper: '🔴', threshold_lower: '🟡', volatility: '⚡' });

export function createMarket({ request, onExchangeRate }) {
    let exchangeRate = 7.2;
    let alerts = [];

    function doConvert() {
        const input = document.getElementById('convert-input').value;
        const price = Number(input);
        const output = document.getElementById('convert-output');
        if (input.trim() === '' || !Number.isFinite(price) || price < 0) { output.value = ''; return; }
        const toCurrency = document.getElementById('convert-to-currency').value;
        const value = convertUnitPrice(price, {
            fromUnit: document.getElementById('convert-from-unit').value,
            toUnit: document.getElementById('convert-to-unit').value,
            fromCurrency: document.getElementById('convert-from-currency').value,
            toCurrency, usdCny: exchangeRate,
        });
        output.value = (toCurrency === 'USD' ? '$' : '¥') + value.toFixed(2);
    }

    async function fetchExchangeRate() {
        const status = document.getElementById('exchange-rate-status');
        try {
            const data = await request('/api/exchange-rate');
            if (!Number.isFinite(data.usd_cny) || data.usd_cny <= 0) throw new Error('汇率数据无效');
            exchangeRate = data.usd_cny;
            const fallback = data.is_fallback || !data.updated_at;
            const label = fallback ? '参考回退值' : (data.is_stale ? '缓存已过期' : '已更新');
            status.textContent = `${label}${data.updated_at ? ` · ${formatTimestamp(data.updated_at)}` : ''}${data.source ? ` · ${data.source}` : ''}`;
            document.getElementById('current-rate').textContent = exchangeRate.toFixed(4);
            onExchangeRate(exchangeRate);
            doConvert();
            renderAlerts();
        } catch (error) {
            status.textContent = `获取失败，继续使用参考值 ${exchangeRate.toFixed(4)}`;
            showError(error);
        }
    }

    async function fetchHistory() {
        const body = document.getElementById('history-table');
        try {
            const data = await request('/api/price/history?limit=10');
            body.replaceChildren();
            if (!data.data.length) {
                const row = body.insertRow();
                const cell = row.insertCell();
                cell.colSpan = 3;
                cell.textContent = '暂无数据';
                return;
            }
            for (const price of data.data) {
                const row = body.insertRow();
                row.insertCell().textContent = formatTimestamp(price.timestamp);
                const currency = String(price.currency ?? '未知币种');
                const prefix = currency === 'USD' ? '$' : currency === 'CNY' ? '¥' : `${currency} `;
                row.insertCell().textContent = `${prefix}${Number(price.price).toFixed(2)}`;
                row.insertCell().textContent = String(price.source ?? '');
            }
        } catch (error) { showError(error); }
    }

    async function fetchBankPrices() {
        const container = document.getElementById('bank-cards');
        const baseline = document.getElementById('london-gold-price');
        try {
            const data = await request('/api/bank-prices');
            const validPrice = value => Number.isFinite(value) && value > 0;
            if (!Array.isArray(data.data) || !data.data.length || !validPrice(data.london_gold_cny)
                || data.data.some(bank => !validPrice(bank.buy_price) || !validPrice(bank.sell_price) || bank.sell_price < bank.buy_price)) {
                container.textContent = '银行参考报价暂不可用';
                baseline.textContent = '暂不可用';
                return;
            }
            const stale = data.is_fallback || data.is_stale;
            const status = stale ? `<div class="bank-quote-status">参考缓存 · 上次成功更新：${formatTimestamp(data.updated_at)}</div>` : '';
            container.innerHTML = status + data.data.map(bank => {
                const color = BANK_COLORS[bank.bank_name] || '#666';
                return `<div class="bank-card">
                    <div class="bank-name" style="color:${color};border-color:${color}">${escapeHtml(bank.bank_name)}</div>
                    <div class="price-row"><span class="price-label">买入价</span><span class="price-value">¥${bank.buy_price.toFixed(2)}</span></div>
                    <div class="price-row"><span class="price-label">卖出价</span><span class="price-value">¥${bank.sell_price.toFixed(2)}</span></div>
                    <div class="spread">价差: ¥${(bank.sell_price - bank.buy_price).toFixed(2)}</div>
                </div>`;
            }).join('');
            baseline.textContent = `¥${data.london_gold_cny.toFixed(2)}/克${stale ? '（参考缓存）' : ''}`;
        } catch (error) {
            showError(error);
            container.textContent = '银行参考报价暂不可用';
            baseline.textContent = '暂不可用';
        }
    }

    function renderAlerts() {
        const container = document.getElementById('alerts-container');
        if (!alerts.length) { container.innerHTML = '<div class="no-alerts">暂无告警记录</div>'; return; }
        container.innerHTML = alerts.map(alert => {
            const type = Object.hasOwn(ALERT_ICONS, alert.alert_type) ? alert.alert_type : 'unknown';
            const price = convertUnitPrice(alert.price, { usdCny: exchangeRate });
            return `<div class="alert-item ${type}"><span class="alert-icon">${ALERT_ICONS[type] || '⚠️'}</span>
                <div class="alert-content"><div class="alert-message">${escapeHtml(alert.message)}</div>
                <div class="alert-time">${formatTimestamp(alert.triggered_at)}</div></div>
                <div class="alert-price">¥${price.toFixed(2)}/克</div></div>`;
        }).join('');
    }
    async function fetchAlerts() {
        try { alerts = await request('/api/alerts?limit=5'); renderAlerts(); }
        catch (error) { showError(error); }
    }
    function init() {
        for (const id of ['convert-input', 'convert-from-unit', 'convert-from-currency', 'convert-to-unit', 'convert-to-currency']) {
            document.getElementById(id).addEventListener(id === 'convert-input' ? 'input' : 'change', doConvert);
        }
        doConvert();
    }
    return { init, fetchExchangeRate, fetchHistory, fetchBankPrices, fetchAlerts };
}
