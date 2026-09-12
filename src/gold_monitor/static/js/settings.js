import { escapeHtml } from './render.js';

export function createSettings({ request: apiRequest, onChanged }) {
    const element = id => document.getElementById(id);
    const request = (path, body, method = 'POST') => apiRequest(path, {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    let llmProviders = [];
    let activeProviderId = '';
    let activeModel = '';
    let editingProviderId = null;
    let isAddingNew = false;
    let editVersion = 0;
    let loadVersion = 0;
    const busy = new Set();

    function showSettingsStatus(message, success) {
        const target = element('settings-status');
        target.style.display = 'block';
        target.style.background = success ? '#e8f5e9' : '#ffebee';
        target.style.color = success ? '#2e7d32' : '#c62828';
        target.textContent = message;
    }
    async function run(action, task) {
        if (busy.has(action)) return;
        busy.add(action);
        const button = document.querySelector(`[data-action="${action}"]`);
        if (button) button.disabled = true;
        try { await task(); }
        catch (error) { showSettingsStatus(error.message, false); }
        finally { busy.delete(action); if (button) button.disabled = false; }
    }
    async function loadProviders() {
        const version = ++loadVersion;
        try {
            const data = await apiRequest('/api/llm/providers');
            if (version !== loadVersion) return;
            llmProviders = data.providers;
            activeProviderId = data.active_provider_id;
            activeModel = data.active_model;
            renderProvidersList();
        } catch (error) {
            if (version !== loadVersion) return;
            llmProviders = [];
            element('providers-list').textContent = error.message;
            showSettingsStatus(error.message, false);
        }
    }
    async function openSettings() {
        element('settings-modal').style.display = 'block';
        await loadProviders();
    }
    function closeSettings() {
        element('settings-modal').style.display = 'none';
        element('settings-status').style.display = 'none';
        element('provider-config-section').style.display = 'none';
        editingProviderId = null;
        isAddingNew = false;
        editVersion++;
    }
    function renderProvidersList() {
        const container = document.getElementById('providers-list');
        if (llmProviders.length === 0) {
            container.innerHTML = '<div style="padding:20px; text-align:center; color:#999;">暂无平台配置</div>';
            return;
        }

        container.innerHTML = llmProviders.map(p => {
            const isActive = p.id === activeProviderId;
            const hasKey = p.has_api_key;
            const safeId = escapeHtml(p.id);
            const safeName = escapeHtml(p.name);
            const safeBaseUrl = escapeHtml(p.base_url || '(无 URL)');
            return `
                <div class="provider-item" style="display:flex; align-items:center; padding:12px 15px; border-bottom:1px solid #eee; cursor:pointer; ${isActive ? 'background:#f0f7ff;' : ''}"
                     data-provider-id="${safeId}">
                    <div style="flex:1;">
                        <div style="font-weight:500; color:#333; display:flex; align-items:center; gap:8px;">
                            ${safeName}
                            ${isActive ? '<span style="background:#4CAF50; color:#fff; font-size:10px; padding:2px 6px; border-radius:10px;">当前使用</span>' : ''}
                        </div>
                        <div style="font-size:12px; color:#999; margin-top:3px;">
                            ${safeBaseUrl}
                            ${hasKey ? ' • <span style="color:#4CAF50;">已配置 Key</span>' : ' • <span style="color:#ff9800;">未配置 Key</span>'}
                        </div>
                    </div>
                    <div style="color:#ccc; font-size:20px;">›</div>
                </div>
            `;
        }).join('');
        container.querySelectorAll('.provider-item').forEach(item => {
            item.addEventListener('click', () => selectProvider(item.dataset.providerId));
        });
    }

    function fillModels(models, selected = '') {
        const select = element('provider-model');
        select.replaceChildren(new Option('-- 选择或手动输入 --', ''));
        for (const model of models) {
            const id = typeof model === 'string' ? model : model.id;
            const label = typeof model === 'string' || !model.owned_by ? id : `${id} (${model.owned_by})`;
            select.appendChild(new Option(label, id));
        }
        const found = [...select.options].some(option => option.value === selected);
        select.value = found ? selected : '';
        element('provider-model-custom').value = found ? '' : selected;
    }
    function selectProvider(providerId) {
        const provider = llmProviders.find(item => item.id === providerId);
        if (!provider) return;
        editVersion++;
        isAddingNew = false;
        editingProviderId = providerId;
        element('provider-config-section').style.display = 'block';
        element('config-title').textContent = `配置: ${provider.name}`;
        element('provider-name').value = provider.name;
        element('provider-url').value = provider.base_url || '';
        element('provider-key').value = '';
        element('provider-key').placeholder = provider.has_api_key ? `已配置 (${provider.api_key})` : '输入 API Key';
        fillModels(provider.models || [], providerId === activeProviderId ? activeModel : '');
        element('delete-provider-btn').style.display = providerId === 'mock' ? 'none' : 'inline-block';
        updateProviderUrlPreview();
    }
    function showAddProviderForm() {
        editVersion++;
        isAddingNew = true;
        editingProviderId = null;
        element('provider-config-section').style.display = 'block';
        element('config-title').textContent = '添加新平台';
        for (const id of ['provider-name', 'provider-url', 'provider-key']) element(id).value = '';
        element('provider-key').placeholder = '输入 API Key';
        fillModels([]);
        element('delete-provider-btn').style.display = 'none';
        updateProviderUrlPreview();
    }
    function updateProviderUrlPreview() {
        let url = element('provider-url').value.trim().replace(/[/]+$/, '');
        if (!url) { element('provider-url-preview').textContent = ''; return; }
        if (!/\/v\d+\w*(\/|$)/.test(url)) url += '/v1';
        element('provider-url-preview').textContent = `预览: ${url}/chat/completions`;
    }
    function probeFields() {
        return { api_key: element('provider-key').value || null, base_url: element('provider-url').value.trim() || null };
    }
    function selectedModel() { return element('provider-model-custom').value.trim() || element('provider-model').value || ''; }
    function needSavedProvider() {
        if (editingProviderId && !isAddingNew) return true;
        showSettingsStatus('请先保存平台，再探测当前表单中的配置', false);
        return false;
    }
    async function saveProviderConfig() {
        const name = element('provider-name').value.trim();
        if (!name) { showSettingsStatus('请输入平台名称', false); return; }
        const version = editVersion;
        const adding = isAddingNew;
        const providerId = editingProviderId;
        if (!adding && !providerId) return;
        const body = { name, base_url: element('provider-url').value.trim(), api_key: element('provider-key').value || null };
        await run('saveProviderConfig', async () => {
            const result = await request(adding ? '/api/llm/providers' : `/api/llm/providers/${encodeURIComponent(providerId)}`, body, adding ? 'POST' : 'PUT');
            if (result.success === false) throw new Error(result.message || '保存失败');
            await loadProviders();
            if (version === editVersion) selectProvider(result.provider.id);
            showSettingsStatus('保存成功', true);
            if (providerId === activeProviderId) await onChanged();
        });
    }
    async function fetchProviderModels() {
        if (!needSavedProvider()) return;
        const version = editVersion;
        const providerId = editingProviderId;
        const body = probeFields();
        const model = selectedModel();
        await run('fetchProviderModels', async () => {
            showSettingsStatus('正在获取模型列表…', true);
            const result = await request(`/api/llm/providers/${encodeURIComponent(providerId)}/models`, body);
            if (result.success === false) throw new Error(result.message || '获取模型失败');
            if (version === editVersion) fillModels(result.models, model);
            await loadProviders();
            showSettingsStatus(`获取到 ${result.count} 个模型`, true);
        });
    }
    async function testProviderConnection() {
        if (!needSavedProvider()) return;
        const providerId = editingProviderId;
        const body = { ...probeFields(), model: selectedModel() || null };
        await run('testProviderConnection', async () => {
            showSettingsStatus('正在测试连接…', true);
            const result = await request(`/api/llm/providers/${encodeURIComponent(providerId)}/test`, body);
            showSettingsStatus(`${result.message || (result.success ? '连接成功' : '连接失败')}${result.response ? '\n' + result.response : ''}`, result.success === true);
        });
    }
    async function useThisProvider() {
        if (!editingProviderId) return;
        const body = { provider_id: editingProviderId, model: selectedModel() };
        await run('useThisProvider', async () => {
            const result = await request('/api/llm/active', body);
            if (result.success === false) throw new Error(result.message || '切换失败');
            await loadProviders();
            await onChanged();
            showSettingsStatus('已切换到此平台', true);
        });
    }
    async function deleteCurrentProvider() {
        if (!editingProviderId || editingProviderId === 'mock') return;
        const provider = llmProviders.find(item => item.id === editingProviderId);
        if (!confirm(`确定要删除平台 "${provider?.name}" 吗？`)) return;
        const providerId = editingProviderId;
        const version = editVersion;
        await run('deleteCurrentProvider', async () => {
            await apiRequest(`/api/llm/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
            if (version === editVersion) { element('provider-config-section').style.display = 'none'; editingProviderId = null; editVersion++; }
            await loadProviders();
            await onChanged();
            showSettingsStatus('平台已删除', true);
        });
    }
    async function resetLLMConfig() {
        if (!confirm('确定要重置所有配置吗？将恢复默认设置。')) return;
        await run('resetLLMConfig', async () => {
            await apiRequest('/api/llm/config', { method: 'DELETE' });
            element('provider-config-section').style.display = 'none';
            editingProviderId = null;
            editVersion++;
            await loadProviders();
            await onChanged();
            showSettingsStatus('配置已重置', true);
        });
    }
    return { openSettings, closeSettings, loadProviders, selectProvider, showAddProviderForm, saveProviderConfig, fetchProviderModels, testProviderConnection, useThisProvider, deleteCurrentProvider, resetLLMConfig, updateProviderUrlPreview, showSettingsStatus };
}
