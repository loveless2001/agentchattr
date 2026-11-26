/**
 * dashboard-popout.js -- read-only standalone view of one channel's dashboard
 * (/dashboard/<channel>), kept live over the same websocket the chat uses.
 * Clear / Undo / delete stay in the main chat's Dashboard panel.
 */

(() => {
    const channel = decodeURIComponent(location.pathname.split('/')[2] || 'general');
    const token = window.__SESSION_TOKEN__ || '';
    const frame = document.getElementById('dashboard-frame');
    const status = document.getElementById('dashboard-status');
    let renderedVersion = null;

    document.getElementById('dashboard-title').textContent = `#${channel} dashboard`;
    document.title = `#${channel} dashboard · agentchattr`;
    if (window.marked) window.marked.setOptions({ breaks: true, gfm: true });

    function render(snap) {
        // Skip unchanged (keeps scroll) and out-of-order older snapshots.
        if (renderedVersion !== null && snap.version <= renderedVersion) return;
        renderedVersion = snap.version;
        frame.srcdoc = DashboardRender.buildSrcdoc(snap);
        status.textContent = `v${snap.version} · ${snap.blocks.length} block(s)`;
    }

    async function load() {
        try {
            const r = await fetch(`/api/dashboards/${encodeURIComponent(channel)}`,
                { headers: { 'X-Session-Token': token } });
            if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
            render(await r.json());
        } catch (e) {
            status.textContent = `error: ${e.message}`;
        }
    }

    function connect() {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
        ws.onopen = load;   // (re)load on every connect to cover missed events
        ws.onmessage = e => {
            const ev = JSON.parse(e.data);
            if (ev.type === 'dashboard' && ev.channel === channel) render(ev.data);
        };
        ws.onclose = ev => {
            if (ev.code === 4003) { location.reload(); return; }   // server restarted: new token
            status.textContent = 'reconnecting…';
            setTimeout(connect, 2000);
        };
    }

    connect();
})();
