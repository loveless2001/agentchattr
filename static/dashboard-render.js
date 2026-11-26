/**
 * dashboard-render.js -- builds the sandboxed srcdoc for a channel dashboard.
 *
 * Shared by the side panel (dashboard-panel.js) and the pop-out page
 * (dashboard-popout.js). Agent HTML is untrusted: the iframe gets sandbox=""
 * (opaque origin, no scripts, no forms, no popups) and a CSP that blocks every
 * subresource except inline styles and data: images. Each block is first
 * re-serialized through an inert DOMParser so an unclosed tag in one block
 * can't swallow the next, and dangerous elements are dropped.
 */

const DashboardRender = (() => {
    const CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        + "base-uri 'none'; form-action 'none'";
    // Raw-text elements (plaintext, xmp, …) serialize without an end tag and would
    // swallow every later block, so they go too.
    const STRIP = 'script, meta, base, link, iframe, frame, object, embed, form, noscript, '
        + 'plaintext, xmp, listing, noembed, noframes, template, title';
    const THEME_VARS = ['--bg', '--bg-header', '--bg-msg', '--border', '--text', '--text-dim',
        '--accent', '--accent-soft', '--success', '--success-soft', '--warning', '--danger',
        '--danger-soft', '--info'];

    // Built-in component classes agents can use (listed in chat_dashboard_set's docstring).
    const BUILTIN_CSS = `
* { box-sizing: border-box; }
body { margin: 0; padding: 10px; background: var(--bg); color: var(--text);
  font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
a { color: var(--info); }
h1, h2, h3, h4 { margin: 0.3em 0; color: var(--text); line-height: 1.25; }
h1 { font-size: 1.35em; } h2 { font-size: 1.2em; } h3 { font-size: 1.05em; }
p { margin: 0.4em 0; }
.dash-block { background: var(--bg-msg); border: 1px solid var(--border); border-radius: 8px;
  margin: 0 0 10px; overflow: hidden; }
.dash-head { padding: 5px 10px; font-size: 11px; color: var(--text-dim);
  border-bottom: 1px solid var(--border); }
.dash-head b { color: var(--text); font-weight: 600; }
.dash-body { padding: 10px; overflow-x: auto; }
.grid { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
.stat { background: var(--bg-header); border: 1px solid var(--border); border-radius: 6px;
  padding: 8px 10px; }
.stat b { display: block; font-size: 1.7em; line-height: 1.15; color: var(--text); }
.stat span { font-size: 11px; color: var(--text-dim); }
.progress { height: 8px; background: var(--bg-header); border: 1px solid var(--border);
  border-radius: 99px; overflow: hidden; margin: 4px 0; }
.progress > span { display: block; height: 100%; background: var(--accent); }
progress { width: 100%; accent-color: var(--accent); }
.badge { display: inline-block; padding: 1px 7px; border-radius: 99px; font-size: 11px;
  font-weight: 600; background: var(--accent-soft); color: var(--text); }
.badge.ok { background: var(--success-soft); color: var(--success); }
.badge.warn { background: rgba(251, 191, 36, 0.15); color: var(--warning); }
.badge.err { background: var(--danger-soft); color: var(--danger); }
.checklist { list-style: none; padding-left: 0; margin: 0.3em 0; }
.checklist li::before { content: "\\2610"; margin-right: 6px; color: var(--text-dim); }
.checklist li.done { color: var(--text-dim); text-decoration: line-through; }
.checklist li.done::before { content: "\\2611"; color: var(--success); }
li:has(> input[type=checkbox]) { list-style: none; margin-left: -1.2em; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { border: 1px solid var(--border); padding: 4px 7px; text-align: left; }
th { background: var(--bg-header); color: var(--text); }
tr:nth-child(even) td { background: rgba(255, 255, 255, 0.02); }
pre, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
pre { background: var(--bg-header); padding: 8px; border-radius: 6px; overflow-x: auto; }
svg, img { max-width: 100%; height: auto; }
.dash-empty { color: var(--text-dim); text-align: center; padding: 30px 10px; }
`;

    function esc(s) {
        return String(s ?? '').replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function relativeAge(ts) {
        const secs = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0)));
        if (secs >= 86400) return `${Math.floor(secs / 86400)}d ago`;
        if (secs >= 3600) return `${Math.floor(secs / 3600)}h ago`;
        if (secs >= 60) return `${Math.floor(secs / 60)}m ago`;
        return 'just now';
    }

    function clockTime(ts) {
        return new Date((ts || 0) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function sanitizeBlock(block) {
        let src = block.html || '';
        if (block.kind === 'markdown' && window.marked) {
            try { src = window.marked.parse(src); } catch (e) { src = `<pre>${esc(src)}</pre>`; }
        }
        const doc = new DOMParser().parseFromString(`<body>${src}</body>`, 'text/html');
        doc.querySelectorAll(STRIP).forEach(el => el.remove());
        // Links can't open anything from the sandbox; they'd only load a page inside the frame.
        doc.querySelectorAll('a[href]').forEach(a => a.removeAttribute('href'));
        return doc.body.innerHTML;
    }

    function themeVars() {
        const cs = getComputedStyle(document.documentElement);
        return THEME_VARS.map(v => `${v}: ${cs.getPropertyValue(v).trim() || 'initial'};`).join(' ');
    }

    function buildSrcdoc(snapshot) {
        const blocks = (snapshot && snapshot.blocks) || [];
        const body = blocks.length
            ? blocks.map(b => `<section class="dash-block"><header class="dash-head"><b>${esc(b.name)}</b>`
                + ` · ${esc(b.editor)} · ${esc(clockTime(b.updated_at))}</header>`
                + `<div class="dash-body">${sanitizeBlock(b)}</div></section>`).join('\n')
            : '<div class="dash-empty">No dashboard yet. Agents add blocks with chat_dashboard_set.</div>';
        return '<!doctype html><html><head><meta charset="utf-8">'
            + `<meta http-equiv="Content-Security-Policy" content="${CSP}">`
            + `<style>:root { ${themeVars()} } ${BUILTIN_CSS}</style></head>`
            + `<body>${body}</body></html>`;
    }

    return { buildSrcdoc, relativeAge, esc };
})();

window.DashboardRender = DashboardRender;
