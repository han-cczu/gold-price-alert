import { createApiClient } from './api.js';
import { createChart } from './chart.js';
import { createMarket } from './market.js';
import { createAnalysis } from './analysis.js';
import { createSettings } from './settings.js';
import { createRealtime } from './realtime.js';
import { showError, showNotification } from './render.js';

const api = createApiClient({ onError: showError });
const chart = createChart(api);
const market = createMarket({ request: api.request, onExchangeRate: chart.setExchangeRate });
let authEnabled = false;
const canAdministrate = () => !authEnabled || api.hasAdminKey();
const analysis = createAnalysis({ request: api.request, onOpenSettings: () => settings.openSettings(), canAdministrate });
const refreshAnalysisConfig = () => Promise.allSettled([analysis.fetchAIStatus(), analysis.loadAnalysisModels(), analysis.fetchSmartAnalysis()]);
const settings = createSettings({ request: api.request, onChanged: refreshAnalysisConfig });

function updateConnectionStatus(connected) {
    let target = document.getElementById('ws-status');
    if (!target) { target = document.createElement('div'); target.id = 'ws-status'; target.setAttribute('role', 'status'); document.body.append(target); }
    target.textContent = connected ? '● 实时连接' : '● 重连中，使用备用轮询';
    target.dataset.connected = String(connected);
}

const realtime = createRealtime({
    onStatus(connected) {
        updateConnectionStatus(connected);
        // Reconnects recover the samples missed while the page was disconnected.
        if (connected) chart.fetchChartData();
    },
    onMessage(message) {
        if (message.type === 'price_update' && message.data) chart.onPriceUpdate(message.data);
        else if (message.type === 'alert' && message.data) { showNotification(message.data.message); market.fetchAlerts(); }
        else if (message.type === 'config_update') chart.fetchChartData();
    },
});

async function applyAdminKey() {
    const input = document.getElementById('admin-key');
    api.setAdminKey(input.value);
    input.value = '';
    document.getElementById('admin-key-status').textContent = api.hasAdminKey() ? '凭证已应用到当前页面' : '管理员凭证已清除';
    document.getElementById('app-status').hidden = true;
    await Promise.allSettled([settings.loadProviders(), refreshAnalysisConfig()]);
}
async function clearAdminKey() {
    api.setAdminKey('');
    document.getElementById('admin-key').value = '';
    document.getElementById('admin-key-status').textContent = '管理员凭证已清除';
    await Promise.allSettled([settings.loadProviders(), analysis.fetchAIStatus(), analysis.loadAnalysisModels()]);
}

const actions = { ...analysis, ...settings, applyAdminKey, clearAdminKey };
document.addEventListener('click', event => {
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (Object.hasOwn(actions, action)) {
        event.preventDefault();
        Promise.resolve().then(() => actions[action]()).catch(showError);
    }
});
document.getElementById('admin-key').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); applyAdminKey().catch(showError); }
});
document.getElementById('provider-url').addEventListener('input', settings.updateProviderUrlPreview);
document.getElementById('provider-model').addEventListener('change', () => { document.getElementById('provider-model-custom').value = ''; });
for (const [id, close] of [['settings-modal', settings.closeSettings], ['analysis-modal', analysis.closeModal]]) {
    document.getElementById(id).addEventListener('click', event => { if (event.target.id === id) close(); });
}
document.addEventListener('keydown', event => { if (event.key === 'Escape') { settings.closeSettings(); analysis.closeModal(); } });

function updateDateTime() {
    const now = new Date();
    document.getElementById('current-date').textContent = now.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' });
    document.getElementById('current-time').textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
}

chart.init();
market.init();
updateDateTime();
async function loadSecurityStatus() {
    // Public: tells anonymous pages not to call administrator-only endpoints.
    try { authEnabled = Boolean((await api.request('/api/security/status')).auth_enabled); }
    catch { authEnabled = false; }
}
// Each section can load even if an independent section fails.
Promise.allSettled([chart.fetchChartData(), market.fetchExchangeRate(), market.fetchHistory(), market.fetchBankPrices(), market.fetchAlerts()]);
loadSecurityStatus().then(refreshAnalysisConfig);
realtime.start();

const intervals = [
    setInterval(updateDateTime, 1000),
    setInterval(() => { chart.render(); if (!realtime.connected) chart.fetchChartData(); }, 30000),
    setInterval(market.fetchHistory, 60000),
    setInterval(market.fetchExchangeRate, 1800000),
    setInterval(market.fetchBankPrices, 60000),
    setInterval(market.fetchAlerts, 60000),
];
window.addEventListener('pagehide', () => { intervals.forEach(clearInterval); realtime.stop(); chart.dispose(); });
window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
