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

/* SSE EventSource setup */
/* Placeholder — implemented in Sprint 1 UI pass */

/* PAD Radar Chart */
/* Placeholder — implemented in Sprint 1 UI pass */

/* Stylometric Drift Line Chart */
/* Placeholder — implemented in Sprint 1 UI pass */

/* Auto-scroll */
/* Placeholder — implemented in Sprint 1 UI pass */

/* Command Center controls */
/* Placeholder — implemented in Sprint 1 UI pass */
