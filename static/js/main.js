/*
  static/js/main.js

  Purpose:
    Core frontend JavaScript for the FictionWriter SSE-driven UI. Handles:
    1. EventSource connection to GET /stream (SSE endpoint).
    2. Event type routing: dispatches incoming SSE events to the correct UI panel
       based on the event.type field (token, beat_start, word_count, critic, planning,
       pad_update, status).
    3. beat_start handler: wraps subsequent token events in a data-beat-id div.
       Clears the prose panel content for re-used beat_ids (revision cycle display).
    4. Chart.js PAD radar chart updates: receives pad_update SSE events and updates
       the three-axis radar chart (pleasure, arousal, dominance) in the telemetry sidebar.
    5. Stylometric drift line graph: receives stylometric_distance SSE events and
       appends to the Chart.js time-series line graph. Advisory only (Burrows' Delta).
    6. Auto-scroll: keeps the prose stream panel scrolled to the bottom as tokens arrive.
       Pauses auto-scroll when the author manually scrolls up (inspecting prior text).
    7. Live Command Center button bindings: pause, resume, stop, patch submission.
    8. Settings page: slider value display updates, endpoint test button handler.
    9. Codex page: Kanban drag-and-drop priority reordering, branch restore modal.

  Dependencies (loaded by base.html before this script):
    - Bootstrap 5.3 (modal, collapse, drag-and-drop via SortableJS if added)
    - Alpine.js 3.x (reactive directives on individual components)
    - Chart.js (PAD radar, drift line graph)
    - marked.js (Markdown → HTML for planning bullet stream)

  Pattern:
    All DOM interactions are vanilla ES2020. No jQuery. EventSource is the primary
    data transport; fetch() is used for control actions (pause/resume/stop/patch).

  Sections (to be implemented in Sprint 1 UI pass):
    - SSE EventSource initialization and event dispatch
    - beat_start and token handlers
    - PAD radar chart initialization and update function
    - Stylometric drift line chart initialization and update function
    - Auto-scroll logic with manual-scroll detection
    - Command Center button event listeners
    - Settings slider live-value display
    - Codex Kanban drag-and-drop (SortableJS or HTML5 DnD API)
    - Branch restore modal submission
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
