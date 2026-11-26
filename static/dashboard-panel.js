/**
 * dashboard-panel.js -- side panel showing the active channel's live dashboard.
 *
 * Agents write blocks via MCP (chat_dashboard_set); the server pushes a full
 * snapshot as a ws "dashboard" event. Humans can Clear, Undo, delete a block or
 * pop the dashboard out into its own window. No in-panel editing: ask an agent.
 */

const DashboardPanel = (() => {
    let snapshot = null;          // latest snapshot for window.activeChannel
    let renderedVersion = null;   // skip srcdoc resets (which lose scroll) when unchanged
    let ageTimer = null;

    const $ = id => document.getElementById(id);
    const isOpen = () => !$('dashboard-panel').classList.contains('hidden');
    const headers = extra => Object.assign({ 'X-Session-Token': window.SESSION_TOKEN }, extra || {});

    async function api(method, path) {
        const r = await fetch(path, { method, headers: headers() });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
        return body;
    }

    async function load(channel = window.activeChannel) {
        try {
            const snap = await api('GET', `/api/dashboards/${encodeURIComponent(channel)}`);
            if (channel === window.activeChannel) { snapshot = snap; render(); }
        } catch (e) {
            console.warn('dashboard load failed:', e);
        }
    }

    function renderChips() {
        const list = $('dashboard-blocks');
        const blocks = (snapshot && snapshot.blocks) || [];
        list.innerHTML = blocks.map(b => {
            const name = DashboardRender.esc(b.name);
            return `<div class="dashboard-chip" title="v${b.version}">
                <span class="dashboard-chip-name">${name}</span>
                <span class="dashboard-chip-meta">${DashboardRender.esc(b.editor)} · ${DashboardRender.relativeAge(b.updated_at)}</span>
                <button class="dashboard-chip-del" data-block="${name}" title="Delete block">×</button>
            </div>`;
        }).join('');
        list.querySelectorAll('.dashboard-chip-del').forEach(btn =>
            btn.addEventListener('click', () => deleteBlock(btn.dataset.block)));
    }

    function render() {
        if (!isOpen()) return;
        $('dashboard-title').textContent = `#${window.activeChannel} dashboard`;
        renderChips();
        const blocks = (snapshot && snapshot.blocks) || [];
        $('dashboard-undo').disabled = !(snapshot && snapshot.can_undo);
        $('dashboard-clear').disabled = !blocks.length;
        const key = snapshot ? `${snapshot.channel}:${snapshot.version}` : null;
        if (key !== renderedVersion) {
            $('dashboard-frame').srcdoc = DashboardRender.buildSrcdoc(snapshot);
            renderedVersion = key;
        }
    }

    function setDot(on) {
        $('dashboard-dot').classList.toggle('hidden', !on);
    }

    function toggle() {
        window._preserveScroll(() => {
            const panel = $('dashboard-panel');
            panel.classList.toggle('hidden');
            const open = isOpen();
            $('dashboard-toggle').classList.toggle('active', open);
            clearInterval(ageTimer);
            if (open) {
                setDot(false);
                renderedVersion = null;
                render();
                load();
                ageTimer = setInterval(renderChips, 30000);
            }
        });
    }

    async function clearAll() {
        const n = ((snapshot && snapshot.blocks) || []).length;
        if (!n || !confirm(`Clear all ${n} block(s) from #${window.activeChannel}'s dashboard? Undo can restore them.`)) return;
        try { await api('POST', `/api/dashboards/${encodeURIComponent(window.activeChannel)}/clear`); }
        catch (e) { alert(`Clear failed: ${e.message}`); }
    }

    async function undo() {
        try { await api('POST', `/api/dashboards/${encodeURIComponent(window.activeChannel)}/undo`); }
        catch (e) { alert(`Undo failed: ${e.message}`); }
    }

    async function deleteBlock(name) {
        const ch = encodeURIComponent(window.activeChannel);
        try { await api('DELETE', `/api/dashboards/${ch}/blocks/${encodeURIComponent(name)}`); }
        catch (e) { alert(`Delete failed: ${e.message}`); }
    }

    function popOut() {
        const ch = window.activeChannel;
        window.open(`/dashboard/${encodeURIComponent(ch)}`, `dash-${ch}`, 'width=760,height=820');
    }

    function onDashboardEvent(ev) {
        if (ev.channel !== window.activeChannel) return;
        // Broadcasts from different threads can arrive out of order: never go back.
        if (snapshot && snapshot.channel === ev.channel && ev.data.version < snapshot.version) return;
        snapshot = ev.data;
        if (isOpen()) render();
        else setDot(true);
    }

    function init() {
        Hub.on('dashboard', onDashboardEvent);
        // settings is re-sent on every ws (re)connect: refetch to cover missed events.
        Hub.on('settings', () => { if (isOpen()) load(); });
        Store.watch('activeChannel', () => {
            snapshot = null;
            setDot(false);
            if (isOpen()) load();   // keep the old frame until the new one arrives (no flicker)
        });
    }

    return { init, toggle, clearAll, undo, popOut };
})();

window.DashboardPanel = DashboardPanel;
window.toggleDashboardPanel = DashboardPanel.toggle;
