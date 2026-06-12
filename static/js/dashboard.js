/**
 * static/js/dashboard.js — Live Command Center.
 *
 * State model: the story stream is a list of BEAT BLOCKS keyed by beat_id,
 * each carrying a lifecycle state (drafting → revising → committed). Revisions
 * REPLACE block content (the old append-forever stream is gone); a re-announced
 * beat_id (Tier-4 redraft) clears its stale block. On page load the committed
 * manuscript is rebuilt from GET /codex/manuscript and live state from
 * GET /status — generation persists server-side regardless of this page.
 */
/* global Chart */

(() => {
  const $ = (id) => document.getElementById(id);
  const stream = $('prose-stream');
  const beats = new Map(); // beat_id -> {block, textEl, chipEl}

  /* ---------------- beat blocks ---------------- */

  function beatBlock(beatId, { description = '', state = 'drafting', text = '' } = {}) {
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
      <div class="beat-text"></div>`;
    block.querySelector('.beat-desc').textContent = description;
    const textEl = block.querySelector('.beat-text');
    textEl.textContent = text;
    stream.appendChild(block);
    entry = { block, textEl, chipEl: block.querySelector('.beat-chip') };
    beats.set(beatId, entry);
    setState(beatId, state);
    return entry;
  }

  const CHIP = { drafting: 'drafting', revising: 'revising', committed: 'committed ✓', escalated: 'recovering' };

  function setState(beatId, state) {
    const entry = beats.get(beatId);
    if (!entry) return;
    entry.block.dataset.state = state;
    entry.chipEl.textContent = CHIP[state] || state;
  }

  function pinScroll() {
    if (autoScroll) stream.scrollTop = stream.scrollHeight;
  }

  let autoScroll = true;
  stream.addEventListener('scroll', () => {
    autoScroll = stream.scrollTop + stream.clientHeight >= stream.scrollHeight - 32;
  });

  /* ---------------- charts ---------------- */

  const padChart = new Chart($('pad-radar'), {
    type: 'radar',
    data: { labels: ['Pleasure', 'Arousal', 'Dominance'], datasets: [] },
    options: {
      scales: { r: { min: -1, max: 1, ticks: { stepSize: 0.5, display: false } } },
      plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 } } } },
      animation: false,
    },
  });
  const driftChart = new Chart($('drift-chart'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        { label: 'STEL Dc', data: [], borderColor: '#6ea8fe', tension: 0.35, pointRadius: 0 },
        { label: "Burrows' Δ (advisory)", data: [], borderColor: '#8b93a9', borderDash: [5, 4], tension: 0.35, pointRadius: 0 },
      ],
    },
    options: {
      scales: { y: { min: 0 } },
      plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 } } } },
      animation: false,
    },
  });
  const padColors = ['#6ea8fe', '#3fb950', '#d29922', '#f85149', '#bc8cff'];

  /* ---------------- status bar ---------------- */

  let activeBeatId = null;
  let startedAt = null;
  let wordTarget = 0;

  function setRunning(running) {
    $('statusbar').dataset.running = String(running);
    $('btn-generate').disabled = running === true;
  }

  function setStage(label) { $('stage-label').textContent = label; }

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
      setRunning(ev.running !== false);
      setStage(ev.label || ev.stage || 'Working…');
      if (ev.stage && ev.stage.startsWith('node_')) feed(ev.label || ev.stage);
      if (ev.stage === 'node_freeze_and_escalate' && activeBeatId) setState(activeBeatId, 'escalated');
    },
    beat_start(ev) {
      activeBeatId = ev.beat_id;
      beatBlock(ev.beat_id, { description: ev.description || `Beat ${ev.beat_index}`, state: 'drafting', text: '' });
      $('beat-counter').textContent = `beat ${ev.beat_index}`;
      pinScroll();
    },
    draft_chunk(ev) {
      if (!activeBeatId) return;
      beats.get(activeBeatId)?.textEl.append(ev.text);
      pinScroll();
    },
    draft_complete() {
      if (activeBeatId) setState(activeBeatId, 'revising');
    },
    draft_replaced(ev) {
      // Revision REPLACES the block's text — stale prose never lingers.
      const entry = beatBlock(ev.beat_id, { state: 'revising' });
      entry.textEl.textContent = ev.text;
      entry.textEl.classList.remove('swap');
      void entry.textEl.offsetWidth; // restart the swap animation
      entry.textEl.classList.add('swap');
      feed(`Revision ${ev.revision} applied`, 'warn');
      pinScroll();
    },
    beat_committed(ev) {
      setState(ev.beat_id, 'committed');
      feed(`Beat committed (${ev.word_count} words)`, 'good');
    },
    word_count(ev) { setWords(ev.total); },
    status(ev) {
      setStage(ev.status);
      feed(`Status: ${ev.status}`, 'warn');
    },
    critic_result(ev) {
      feed(`Critic ${ev.critic}: ${ev.error_code}`, ev.error_code === 'NONE' ? 'good' : 'warn');
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
      setStage('Complete');
      window.fwToast(`Generation complete in ${ev.duration_s}s`, 'info');
      feed(`Generation complete (${ev.duration_s}s)`, 'good');
    },
    generation_error(ev) {
      setRunning(false);
      $('statusbar').dataset.running = 'error';
      setStage('Failed — see activity log');
      window.fwToast(`Generation failed: ${ev.error}`, 'error', 9000);
      feed(`ERROR: ${ev.error}`, 'bad');
    },

    /** Reattach: called by fwResync() with the GET /status snapshot. */
    __resync(snap) {
      setRunning(snap.running);
      setStage(snap.stage_label || (snap.running ? 'Working…' : 'Idle'));
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
      setStage('Starting pipeline…');
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
