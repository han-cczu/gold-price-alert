import { escapeHtml, renderMarkdown } from "./render.js";
import { formatTimestamp } from "./state.js";

export function createAnalysis({ request, onOpenSettings }) {
    // 智能分析数据
    let smartAnalysisData = null;
    let requestVersion = 0;

    // These reads use saved metadata; live provider probes belong to settings.
    async function loadAnalysisModels() {
        try {
            const config = await request('/api/llm/providers');
            const active = (config.providers || []).find(provider => provider.id === config.active_provider_id);
            const select = document.getElementById('analysis-model-select');
            select.replaceChildren(new Option('默认模型', ''));
            for (const id of active?.models || []) select.appendChild(new Option(id, id));
            if (config.active_model) {
                if (![...select.options].some(option => option.value === config.active_model)) select.appendChild(new Option(config.active_model, config.active_model));
                select.value = config.active_model;
            }
        } catch (error) {
            document.getElementById('analysis-model-select').replaceChildren(new Option(error.message, ''));
        }
    }

    async function fetchAIStatus() {
        const text = document.getElementById('ai-status-text');
        try {
            const status = await request('/api/llm/status');
            const color = status.enabled ? '#4CAF50' : '#ff9800';
            document.getElementById('ai-status-icon').style.color = color;
            document.getElementById('ai-status-bar').style.background = status.enabled ? 'rgba(76,175,80,0.3)' : 'rgba(255,152,0,0.3)';
            text.textContent = status.enabled ? `AI 已启用: ${status.provider_name} - ${status.model}` : `${status.message} `;
            if (!status.enabled) {
                const link = document.createElement('a');
                link.href = '#';
                link.style.color = 'inherit';
                link.textContent = '去配置';
                link.addEventListener('click', event => { event.preventDefault(); onOpenSettings(); });
                text.appendChild(link);
            }
        } catch (error) { text.textContent = error.message; }
    }

    function acceptReport(data) {
        smartAnalysisData = data;
        renderSmartAnalysis(data);
        const age = data.is_cached ? data.cache_age_minutes || 0 : 0;
        document.getElementById('analysis-cache-info').textContent = age > 0
            ? `${Math.floor(age / 60) ? `${Math.floor(age / 60)}小时` : ''}${age % 60}分钟前` : '刚刚更新';
        document.getElementById('show-full-btn').style.display = 'inline-block';
    }
    async function fetchSmartAnalysis() {
        const version = ++requestVersion;
        try {
            const data = await request('/api/smart-analysis', { timeoutMs: 120000 });
            if (version === requestVersion) acceptReport(data);
        } catch (error) {
            if (version === requestVersion) document.getElementById('smart-analysis-content').textContent = error.message;
        }
    }

    // 渲染智能分析
    function renderSmartAnalysis(data) {
        const content = document.getElementById('smart-analysis-content');
        const modelInfo = data.model_used ? `<span style="font-size:11px; opacity:0.6; margin-left:10px;">(${escapeHtml(data.model_used)})</span>` : '';
        const searchBadge = data.web_search_used
            ? '<span style="font-size:11px; color:#2e7d32; margin-left:8px;">🌐 已联网检索</span>'
            : '<span style="font-size:11px; color:#999; margin-left:8px;">📚 基于模型知识（未联网）</span>';

        content.innerHTML = `
            <div style="display:grid; gap:15px;">
                <div>
                    <div style="font-size:12px; opacity:0.7; margin-bottom:5px;">📈 市场概况 ${modelInfo}${searchBadge}</div>
                    <div class="markdown-content" style="font-size:14px;">${renderMarkdown(data.market_overview)}</div>
                </div>
                <div>
                    <div style="font-size:12px; opacity:0.7; margin-bottom:5px;">⏰ 买入时机</div>
                    <div class="markdown-content" style="font-size:14px;">${renderMarkdown(data.buy_timing)}</div>
                </div>
                <div>
                    <div style="font-size:12px; opacity:0.7; margin-bottom:5px;">💡 操作建议</div>
                    <div class="markdown-content" style="font-size:14px;">${renderMarkdown(data.recommendation)}</div>
                </div>
            </div>
        `;
    }

    async function refreshSmartAnalysis() {
        const version = ++requestVersion;
        const button = document.getElementById('refresh-analysis-btn');
        const originalText = button.textContent;
        const model = document.getElementById('analysis-model-select').value;
        button.textContent = '⏳ 分析中…';
        button.disabled = true;
        document.getElementById('smart-analysis-content').textContent = `AI 正在分析，请稍候…${model ? ` 使用模型: ${model}` : ''}`;
        try {
            const result = await request('/api/smart-analysis/refresh', {
                timeoutMs: 120000, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: model || null }),
            });
            if (version !== requestVersion) return;
            if (result.success === false) throw new Error(result.message || '分析失败');
            acceptReport(result.data);
            const select = document.getElementById('analysis-model-select');
            if (result.data.model_used && [...select.options].some(option => option.value === result.data.model_used)) select.value = result.data.model_used;
        } catch (error) {
            if (version === requestVersion) document.getElementById('smart-analysis-content').textContent = error.message;
        } finally { button.textContent = originalText; button.disabled = false; }
    }

    // 显示完整分析报告
    function showFullAnalysis() {
        if (!smartAnalysisData) return;

        const modal = document.getElementById('analysis-modal');
        const content = document.getElementById('modal-content');

        const factors = smartAnalysisData.key_factors.map(f => `<li style="margin-bottom:8px;">${renderMarkdown(f)}</li>`).join('');

        // 是否有原始响应
        const hasRawResponse = smartAnalysisData.raw_response && smartAnalysisData.raw_response.length > 0;
        const rawResponseSection = hasRawResponse ? `
            <div class="raw-response-section">
                <button class="raw-response-toggle" data-action="toggleRawResponse">
                    <span id="raw-toggle-icon">▶</span>
                    <span>查看 AI 原始响应</span>
                    <span style="font-size:11px; color:#999;">(${smartAnalysisData.raw_response.length} 字符)</span>
                </button>
                <div id="raw-response-content" class="raw-response-content" style="display:none;">${escapeHtml(smartAnalysisData.raw_response)}</div>
            </div>
        ` : '';

        content.innerHTML = `
            <h3 style="color:#667eea; margin-bottom:15px;">${escapeHtml(smartAnalysisData.title)}</h3>

            <div style="margin-bottom:20px;">
                <h4 style="color:#333; margin-bottom:10px;">📊 市场概况</h4>
                <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.market_overview)}</div>
            </div>

            <div style="margin-bottom:20px;">
                <h4 style="color:#333; margin-bottom:10px;">📈 近期走势</h4>
                <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.recent_trend)}</div>
            </div>

            <div style="margin-bottom:20px;">
                <h4 style="color:#333; margin-bottom:10px;">🔍 影响因素</h4>
                <ul class="markdown-content" style="color:#666; margin-left:20px;">${factors}</ul>
            </div>

            <div style="margin-bottom:20px;">
                <h4 style="color:#333; margin-bottom:10px;">🔮 价格预测</h4>
                <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.price_prediction)}</div>
            </div>

            <div style="margin-bottom:20px;">
                <h4 style="color:#333; margin-bottom:10px;">⏰ 买入时机</h4>
                <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.buy_timing)}</div>
            </div>

            <div style="margin-bottom:20px;">
                <h4 style="color:#333; margin-bottom:10px;">💡 操作建议</h4>
                <div class="markdown-content" style="color:#666;">${renderMarkdown(smartAnalysisData.recommendation)}</div>
            </div>

            <div style="background:#fff3e0; padding:15px; border-radius:8px; margin-top:20px;">
                <h4 style="color:#e65100; margin-bottom:10px;">⚠️ 风险提示</h4>
                <div class="markdown-content" style="color:#e65100; font-size:13px;">${renderMarkdown(smartAnalysisData.risk_warning)}</div>
            </div>

            ${rawResponseSection}

            ${(() => {
                const srcs = smartAnalysisData.sources || [];
                if (!srcs.length) return '';
                const items = srcs.map(s => `<li style="margin-bottom:4px;">${escapeHtml(s.title || s.url)} <span style="color:#999;">${escapeHtml(s.url)}</span></li>`).join('');
                return `<div style="margin-top:18px; font-size:12px; color:#666;">
                    <div style="font-weight:bold; margin-bottom:6px;">🔗 参考来源（联网检索）</div>
                    <ul style="margin-left:18px;">${items}</ul>
                </div>`;
            })()}

            <p style="color:#999; font-size:12px; margin-top:20px; text-align:right;">
                ${smartAnalysisData.web_search_used ? '🌐 已联网检索实时数据' : '📚 基于模型已有知识（未联网，数据可能过时）'}<br>
                生成时间: ${formatTimestamp(smartAnalysisData.generated_at)}
                ${smartAnalysisData.model_used ? ` · 模型: ${escapeHtml(smartAnalysisData.model_used)}` : ''}
            </p>
        `;

        modal.style.display = 'block';
    }

    // 切换原始响应显示
    function toggleRawResponse() {
        const content = document.getElementById('raw-response-content');
        const icon = document.getElementById('raw-toggle-icon');
        if (content.style.display === 'none') {
            content.style.display = 'block';
            icon.textContent = '▼';
        } else {
            content.style.display = 'none';
            icon.textContent = '▶';
        }
    }
    function closeModal() { document.getElementById("analysis-modal").style.display = "none"; }
    return { loadAnalysisModels, fetchAIStatus, fetchSmartAnalysis, refreshSmartAnalysis, showFullAnalysis, toggleRawResponse, closeModal };
}
