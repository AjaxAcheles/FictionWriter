/*
  static/js/main.js

  Purpose:
    Shared frontend plumbing for ALL FictionWriter pages (Mission Control
    dashboard, Alignment, Codex, Settings):
    1. EventSource connection to GET /stream (SSE endpoint) with automatic
       browser reconnects. Events are dispatched by event.type into the
       page-registered window.fwHandlers map — pages only register handlers;
       the transport lives here.
    2. window.fwResync(): the background-persistence contract. Generation runs
       server-side in core/generation_manager.py; on load, on SSE (re)open, and
       on tab refocus this pulls GET /status and hands the snapshot to the
       page's __resync handler so the UI reattaches to an in-flight run.
    3. window.fwToast(): small, self-dismissing, non-blocking notifications.
    4. Global nav highlighting via [data-nav] (works for both the legacy top
       navbar and the dashboard's left-rail links).

  Pattern:
    All DOM interactions are vanilla ES2020. No jQuery. EventSource is the
    primary data transport; fetch() is used for control actions.
    Page-specific DOM logic (beat blocks, charts, Glass Engine, timeline,
    margin inbox) lives in static/js/dashboard.js.
*/

/* ------------------------------------------------------------------ */
/* Shared SSE consumer + page-reattach plumbing (all pages)           */
/* ------------------------------------------------------------------ */

window.fwHandlers = window.fwHandlers || {};

/** Toast notifications — small, self-dismissing, non-blocking. */
window.fwToast = function fwToast(message, kind = 'info', ttlMs = 4200) {
  const host = document.getElementById('fw-toasts');
  if (!host) return;
  const el = document.createElement('div');
  el.className = `fw-toast ${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity 250ms';
    setTimeout(() => el.remove(), 260);
  }, ttlMs);
};

/** Global nav: highlight the active page + status dot on every page. */
(function initNav() {
  const here = window.location.pathname;
  document.querySelectorAll('[data-nav]').forEach((a) => {
    const target = a.dataset.nav;
    if (target === here || (target !== '/' && here.startsWith(target))) a.classList.add('active');
  });
})();

/**
 * Status resync — THE background-persistence contract.
 * Generation lives server-side in core/generation_manager.py; the page is only
 * a viewport. On load, on SSE reconnect, and whenever the tab becomes visible
 * again, we pull GET /status and let pages re-render from authoritative state.
 */
window.fwResync = async function fwResync() {
  try {
    const res = await fetch('/status');
    if (!res.ok) return null;
    const snapshot = await res.json();
    if (typeof window.fwHandlers.__resync === 'function') window.fwHandlers.__resync(snapshot);
    const dot = document.getElementById('nav-status-dot');
    if (dot) dot.style.background = snapshot.running ? 'var(--fw-good)' : 'var(--fw-text-dim)';
    return snapshot;
  } catch { return null; }
};

(function initSse() {
  const source = new EventSource('/stream');
  const dispatch = (raw) => {
    let ev;
    try { ev = JSON.parse(raw); } catch { return; }
    if (ev.type && typeof window.fwHandlers[ev.type] === 'function') window.fwHandlers[ev.type](ev);
  };
  source.onmessage = (e) => dispatch(e.data);
  for (const type of [
    'pipeline_status', 'beat_start', 'draft_chunk', 'draft_complete', 'draft_replaced',
    'beat_committed', 'word_count', 'status', 'critic_result', 'pad_update', 'drift',
    'generation_complete', 'generation_error', 'ingestion_progress', 'ingestion_complete',
  ]) source.addEventListener(type, (e) => dispatch(e.data));

  // The browser auto-reconnects EventSource; after a gap we may have missed
  // events, so resync from /status on every (re)open and on tab refocus.
  source.onopen = () => window.fwResync();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) window.fwResync();
  });
})();

/* Bootstrap tooltips (global) */
document.addEventListener('DOMContentLoaded', () => {
  if (window.bootstrap) {
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new bootstrap.Tooltip(el));
  }
  window.fwResync();
});
