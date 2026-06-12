/**
 * static/js/dashboard.js — Mission Control.
 *
 * The page is a fixed-viewport workspace; the human is a supervisor, not an
 * editor. State model: the story stream is a list of BEAT BLOCKS keyed by
 * beat_id, each carrying a lifecycle state (drafting → revising → committed).
 * Revisions REPLACE block content; a re-announced beat_id (Tier-4 redraft)
 * clears its stale block. On page load the committed manuscript is rebuilt
 * from GET /codex/manuscript and live state from GET /status — generation
 * persists server-side regardless of this page.
 *
 * Mission Control surfaces (all fed from the same SSE events as before):
 *   - Glass Engine: terminal log of pipeline_status narration so the user
 *     never wonders whether the engine is frozen during time-to-first-token.
 *   - Playhead timeline: one segment per beat + translucent "ghost" segments
 *     for upcoming beats (predictive ghosting).
 *   - Margin Inbox: critic rejections surface as margin dots with a
 *     Confirm/Contradict popover instead of blocking UI.
 *   - Semantic phases: cyan=drafting, purple=planning/reflection,
 *     orange=revision, green=committed — applied to dock, timeline, glass log.
 */
/* global Chart */

(() => {
  const $ = (id) => document.getElementById(id);
  const stream = $('prose-stream');
  const canvas = $('canvas-scroll'); // the ONLY scroller for prose
  const beats = new Map(); // beat_id -> {block, textEl, chipEl, marginEl, seg, outlineEl}
  const beatOrder = []; // insertion order, for timeline segments

  /* ---------------- semantic phase mapping ---------------- */

  // node name → semantic phase (drives dock tint, glass log color, timeline)
  const PHASE = {
    node_plan_global: 'reflect',
    node_plan_arc: 'reflect',
    node_plan_chapter: 'reflect',
    node_plan_beat: 'reflect',
    node_compress_memory: 'reflect',
    node_craft_consultant: 'reflect',
    node_assemble_context: 'draft',
    node_draft_prose: 'draft',
    node_programmatic_audit: 'revise',
    node_adversarial_critics: 'revise',
    node_revise_prose: 'revise',
    node_freeze_and_escalate: 'revise',
    node_commit_transaction: 'locked',
  };

  /* ---------------- glass engine (internal monologue) ---------------- */

  const glass = $('glass-engine');
  const glassLog = $('glass-log');

  function glassLine(text, kind = '') {
    const row = document.createElement('div');
    row.className = 'fw-glass-line';
    if (kind) row.dataset.kind = kind;
    const t = document.createElement('time');
    t.textContent = new Date().toLocaleTimeString([], { hour12: false });
    const span = document.createElement('span');
    span.textContent = text;
    row.append(t, span);
    glassLog.appendChild(row);
    while (glassLog.children.length > 200) glassLog.firstChild.remove();
    glassLog.scrollTop = glassLog.scrollHeight;
  }

  $('glass-toggle').addEventListener('click', () => {
    const open = glass.dataset.open !== 'true';
    glass.dataset.open = String(open);
    $('glass-toggle').setAttribute('aria-expanded', String(open));
  });

  /* ---------------- playhead timeline ---------------- */

  const track = $('timeline-track');
  const GHOST_COUNT = 3; // predictive ghosting: translucent upcoming beats

  function rebuildGhosts() {
    track.querySelectorAll('.ghost').forEach((g) => g.remove());
    if ($('statusbar').dataset.running !== 'true') return;
    for (let i = 0; i < GHOST_COUNT; i++) {
      const g = document.createElement('div');
      g.className = 'fw-timeline-seg ghost';
      g.title = 'Planned beat';
      track.appendChild(g);
    }
  }

  function addTimelineSeg(beatId, state) {
    const seg = document.createElement('button');
    seg.type = 'button';
    seg.className = 'fw-timeline-seg';
    seg.dataset.state = state;
    seg.title = beatId;
    seg.addEventListener('click', () => {
      beats.get(beatId)?.block.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    track.appendChild(seg);
    rebuildGhosts();
    return seg;
  }

  /* ---------------- RAPTOR outline (left rail) ---------------- */

  const outline = $('raptor-outline');
  const outlineScenes = new Set();

  function addOutlineEntry(beatId, sceneId, label, state) {
    $('outline-empty')?.remove();
    if (sceneId && !outlineScenes.has(sceneId)) {
      outlineScenes.add(sceneId);
      const h = document.createElement('div');
      h.className = 'fw-outline-scene';
      h.textContent = sceneId;
      outline.appendChild(h);
    }
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'fw-outline-beat';
    el.dataset.state = state;
    el.textContent = label || beatId;
    el.title = label || beatId;
    el.addEventListener('click', () => {
      beats.get(beatId)?.block.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    outline.appendChild(el);
    return el;
  }

  /* ---------------- margin inbox ---------------- */

  const popover = $('margin-popover');
  let popoverDot = null;

  function addMarginDot(beatId, { kind = 'critic', message = '' } = {}) {
    const entry = beats.get(beatId);
    if (!entry) return;
    const dot = document.createElement('button');
    dot.type = 'button';
    dot.className = 'fw-margin-dot';
    dot.dataset.kind = kind;
    dot.dataset.message = message;
    dot.setAttribute('aria-label', `Annotation: ${message}`);
    dot.addEventListener('click', (e) => {
      e.stopPropagation();
      openPopover(dot);
    });
    entry.marginEl.appendChild(dot);
  }

  function openPopover(dot) {
    popoverDot = dot;
    $('margin-popover-body').textContent = dot.dataset.message;
    popover.hidden = false;
    const r = dot.getBoundingClientRect();
    const w = 280;
    popover.style.left = `${Math.min(window.innerWidth - w - 12, r.right + 10)}px`;
    popover.style.top = `${Math.min(window.innerHeight - 140, r.top - 8)}px`;
  }

  function resolveDot(verdict) {
    if (!popoverDot) return;
    popoverDot.classList.add('resolved');
    feed(`Annotation ${verdict}: ${popoverDot.dataset.message}`, verdict === 'confirmed' ? 'good' : 'warn');
    popoverDot = null;
    popover.hidden = true;
  }
  $('margin-confirm').addEventListener('click', () => resolveDot('confirmed'));
  $('margin-contradict').addEventListener('click', () => resolveDot('contradicted'));
  document.addEventListener('click', (e) => {
    if (!popover.hidden && !popover.contains(e.target)) {
      popover.hidden = true;
      popoverDot = null;
    }
  });

  /* ---------------- beat blocks ---------------- */

  function beatBlock(beatId, { description = '', sceneId = '', state = 'drafting', text = '' } = {}) {
    let entry = beats.get(beatId);
    if (entry) {
      // Re-announced beat (redraft after escalation): clear stale text.
      entry.textEl.textContent = text;
      setState(beatId, state);
      return entry;
    }
    $('stream-empty')?.remove();
    const block = document.createElement('article');
    block.className = 'beat-block';
    block.dataset.beatId = beatId;
    block.dataset.state = state;
    block.innerHTML = `
      <div class="beat-meta">
        <span class="beat-chip"></span>
        <span class="beat-desc"></span>
      </div>
      <div class="beat-text"></div>
      <div class="beat-margin"></div>`;
    block.querySelector('.beat-desc').textContent = description;
    const textEl = block.querySelector('.beat-text');
    textEl.textContent = text;
    stream.appendChild(block);
    entry = {
      block,
      textEl,
      chipEl: block.querySelector('.beat-chip'),
      marginEl: block.querySelector('.beat-margin'),
      seg: addTimelineSeg(beatId, state),
      outlineEl: addOutlineEntry(beatId, sceneId, description, state),
    };
    beats.set(beatId, entry);
    beatOrder.push(beatId);
    setState(beatId, state);
    return entry;
  }

  const CHIP = { drafting: 'drafting', revising: 'revising', committed: 'committed ✓', escalated: 'recovering' };

  function setState(beatId, state) {
    const entry = beats.get(beatId);
    if (!entry) return;
    entry.block.dataset.state = state;
    entry.chipEl.textContent = CHIP[state] || state;
    entry.seg.dataset.state = state;
    entry.outlineEl.dataset.state = state;
  }

  /* auto-scroll pins the CANVAS, not the prose div */
  let autoScroll = true;
  canvas.addEventListener('scroll', () => {
    autoScroll = canvas.scrollTop + canvas.clientHeight >= canvas.scrollHeight - 48;
  });
  function pinScroll() {
    if (autoScroll) canvas.scrollTop = canvas.scrollHeight;
  }

  /* ---------------- charts ---------------- */

  const chartFont = { font: { size: 10, family: 'Inter, sans-serif' }, boxWidth: 10 };
  const padChart = new Chart($('pad-radar'), {
    type: 'radar',
    data: { labels: ['Pleasure', 'Arousal', 'Dominance'], datasets: [] },
    options: {
      scales: { r: { min: -1, max: 1, ticks: { stepSize: 0.5, display: false } } },
      plugins: { legend: { labels: chartFont } },
      animation: false,
    },
  });
  const driftChart = new Chart($('drift-chart'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { label: 'STEL Dc', data: [], borderColor: '#38bdf8', tension: 0.35, pointRadius: 0 },
        { label: "Burrows' Δ (advisory)", data: [], borderColor: '#8b93a9', borderDash: [5, 4], tension: 0.35, pointRadius: 0 },
      ],
    },
    options: {
      scales: { y: { min: 0 } },
      plugins: { legend: { labels: chartFont } },
      animation: false,
    },
  });
  const padColors = ['#38bdf8', '#34d399', '#f0a832', '#f85149', '#a78bfa'];

  /* right-rail collapse (charts need a resize after reflow) */
  $('rail-toggle').addEventListener('click', () => {
    const rail = $('right-rail');
    rail.dataset.collapsed = String(rail.dataset.collapsed !== 'true');
    setTimeout(() => { padChart.resize(); driftChart.resize(); }, 220);
  });

  /* ---------------- dock (command center) ---------------- */

  let activeBeatId = null;
  let startedAt = null;
  let wordTarget = 0;

  function setRunning(running) {
    $('statusbar').dataset.running = String(running);
    $('btn-generate').disabled = running === true;
    glass.dataset.live = String(running === true);
    if (running !== true) rebuildGhosts(); // drop ghosts when idle
  }

  function setStage(label, phase = '') {
    $('stage-label').textContent = label;
    $('statusbar').dataset.phase = phase;
  }

  function setWords(committed, target) {
    if (target) wordTarget = target;
    $('word-count').textContent = committed;
    if (wordTarget) {
      $('word-target').textContent = wordTarget;
      $('word-progress').style.width = `${Math.min(100, (committed / wordTarget) * 100)}%`;
    }
  }

  setInterval(() => {
    if (!startedAt || $('statusbar').dataset.running !== 'true') return;
    const s = Math.floor((Date.now() - startedAt) / 1000);
    $('elapsed').textContent =
      `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }, 1000);

  function feed(text, kind = '') {
    const host = $('activity-feed');
    const row = document.createElement('div');
    if (kind) row.style.color = `var(--fw-${kind})`;
    const t = document.createElement('time');
    t.textContent = new Date().toLocaleTimeString([], { hour12: false });
    const span = document.createElement('span');
    span.textContent = text;
    row.append(t, span);
    host.prepend(row);
    while (host.children.length > 60) host.lastChild.remove();
  }

  /* ---------------- SSE handlers ---------------- */

  Object.assign(window.fwHandlers, {
    pipeline_status(ev) {
      const phase = PHASE[ev.stage] || '';
      setRunning(ev.running !== false);
      setStage(ev.label || ev.stage || 'Working…', phase);
      // Narrate every node start into the Glass Engine — the user always
      // sees the engine thinking, especially before the first token.
      glassLine(ev.label || ev.stage || 'working…', phase);
      if (ev.stage && ev.stage.startsWith('node_')) feed(ev.label || ev.stage);
      if (ev.stage === 'node_freeze_and_escalate' && activeBeatId) setState(activeBeatId, 'escalated');
    },
    beat_start(ev) {
      activeBeatId = ev.beat_id;
      beatBlock(ev.beat_id, {
        description: ev.description || `Beat ${ev.beat_index}`,
        sceneId: ev.scene_id || '',
        state: 'drafting',
        text: '',
      });
      $('beat-counter').textContent = `beat ${ev.beat_index}`;
      $('timeline-chapter').textContent = ev.scene_id || `beat ${ev.beat_index}`;
      glassLine(`beat ${ev.beat_index} → ${ev.description || ev.beat_id}`, 'draft');
      pinScroll();
    },
    draft_chunk(ev) {
      if (!activeBeatId) return;
      beats.get(activeBeatId)?.textEl.append(ev.text);
      pinScroll();
    },
    draft_complete(ev) {
      if (activeBeatId) setState(activeBeatId, 'revising');
      glassLine(`draft complete (${ev.word_count ?? '?'} words) — critics evaluating…`, 'revise');
    },
    draft_replaced(ev) {
      // Revision REPLACES the block's text — stale prose never lingers.
      // Semantic treatment: a slow orange wash, not a blink.
      const entry = beatBlock(ev.beat_id, { state: 'revising' });
      entry.textEl.textContent = ev.text;
      entry.block.classList.remove('revision-pulse');
      void entry.block.offsetWidth; // restart the pulse animation
      entry.block.classList.add('revision-pulse');
      feed(`Revision ${ev.revision} applied`, 'warn');
      glassLine(`revision ${ev.revision} applied to ${ev.beat_id}`, 'revise');
      pinScroll();
    },
    beat_committed(ev) {
      setState(ev.beat_id, 'committed');
      feed(`Beat committed (${ev.word_count} words)`, 'good');
      glassLine(`beat committed — ${ev.word_count} words locked`, 'locked');
    },
    word_count(ev) { setWords(ev.total); },
    status(ev) {
      setStage(ev.status);
      feed(`Status: ${ev.status}`, 'warn');
      glassLine(`status: ${ev.status}`, 'revise');
    },
    critic_result(ev) {
      const ok = ev.error_code === 'NONE';
      feed(`Critic ${ev.critic}: ${ev.error_code}`, ok ? 'good' : 'warn');
      glassLine(`critic ${ev.critic} → ${ev.error_code}`, ok ? 'locked' : 'revise');
      // Margin Inbox: rejections land as a non-blocking margin dot.
      if (!ok && activeBeatId) {
        addMarginDot(activeBeatId, {
          kind: 'critic',
          message: `${ev.critic} flagged ${ev.error_code}. Does this concern hold?`,
        });
      }
    },
    pad_update(ev) {
      let ds = padChart.data.datasets.find((d) => d.label === ev.char_id);
      if (!ds) {
        const color = padColors[padChart.data.datasets.length % padColors.length];
        ds = { label: ev.char_id, data: [0, 0, 0], borderColor: color, backgroundColor: color + '2b' };
        padChart.data.datasets.push(ds);
      }
      ds.data = [ev.pad.pleasure ?? 0, ev.pad.arousal ?? 0, ev.pad.dominance ?? 0];
      padChart.update();
    },
    drift(ev) {
      driftChart.data.labels.push(driftChart.data.labels.length + 1);
      driftChart.data.datasets[0].data.push(ev.stel_dc ?? 0);
      driftChart.data.datasets[1].data.push(ev.burrows_delta ?? 0);
      if (driftChart.data.labels.length > 60) {
        driftChart.data.labels.shift();
        driftChart.data.datasets.forEach((d) => d.data.shift());
      }
      driftChart.update();
    },
    generation_complete(ev) {
      setRunning(false);
      setStage('Complete', 'locked');
      window.fwToast(`Generation complete in ${ev.duration_s}s`, 'info');
      feed(`Generation complete (${ev.duration_s}s)`, 'good');
      glassLine(`run complete in ${ev.duration_s}s`, 'locked');
    },
    generation_error(ev) {
      setRunning(false);
      $('statusbar').dataset.running = 'error';
      setStage('Failed — see activity log');
      window.fwToast(`Generation failed: ${ev.error}`, 'error', 9000);
      feed(`ERROR: ${ev.error}`, 'bad');
      glassLine(`ERROR: ${ev.error}`, 'bad');
    },

    /** Reattach: called by fwResync() with the GET /status snapshot. */
    __resync(snap) {
      setRunning(snap.running);
      setStage(snap.stage_label || (snap.running ? 'Working…' : 'Idle'), PHASE[snap.stage] || '');
      setWords(snap.committed_words ?? 0, snap.word_count_target);
      if (snap.running && snap.started_at) startedAt = Date.parse(snap.started_at);
      if (snap.last_error) feed(`Last error: ${snap.last_error}`, 'bad');
    },
  });

  /* ---------------- controls ---------------- */

  async function post(url) {
    try {
      const res = await fetch(url, { method: 'POST' });
      return [res.status, await res.json().catch(() => ({}))];
    } catch (e) {
      window.fwToast(`Request failed: ${e.message}`, 'error');
      return [0, {}];
    }
  }

  $('btn-generate').addEventListener('click', async () => {
    const [code, body] = await post('/generate');
    if (code === 409) { window.fwToast(body.message || 'Already running', 'warn'); return; }
    if (code === 202) {
      startedAt = Date.now();
      setRunning(true);
      setStage('Starting pipeline…', 'reflect');
      glassLine('pipeline starting — assembling graph…', 'reflect');
      window.fwToast('Generation started — it keeps running even if you leave this page.');
    }
  });
  $('btn-pause').addEventListener('click', async () => { await post('/control/pause'); });
  $('btn-resume').addEventListener('click', async () => { await post('/control/resume'); });
  $('btn-stop').addEventListener('click', async () => {
    const [, body] = await post('/control/stop');
    setRunning(false);
    setStage('Stopped');
    window.fwToast(body.task_cancelled ? 'Generation task cancelled.' : 'Stop signal sent.', 'warn');
  });

  /* ---------------- initial hydration ---------------- */

  (async () => {
    // Rebuild committed prose so a reload never shows an empty screen mid-run.
    try {
      const res = await fetch('/codex/manuscript');
      const text = (await res.text()).trim();
      if (text) {
        $('stream-empty')?.remove();
        text.split(/\n\n+/).forEach((para, i) => {
          beatBlock(`committed_${i}`, { description: 'committed prose', state: 'committed', text: para });
        });
        pinScroll();
      }
    } catch { /* manuscript endpoint empty/unavailable — fine on first run */ }
    window.fwResync();
  })();
})();
