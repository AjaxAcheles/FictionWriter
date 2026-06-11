/**
 * static/js/dashboard.js — Live Command Center wiring.
 * SSE consumption is in main.js (shared EventSource); this file owns the
 * dashboard's charts and control buttons.
 */
/* global Chart */

const padChart = new Chart(document.getElementById('pad-radar'), {
  type: 'radar',
  data: {
    labels: ['Pleasure', 'Arousal', 'Dominance'],
    datasets: [],
  },
  options: {
    scales: { r: { min: -1, max: 1, ticks: { stepSize: 0.5 } } },
    plugins: { legend: { labels: { boxWidth: 12 } } },
    animation: false,
  },
});

const driftChart = new Chart(document.getElementById('drift-chart'), {
  type: 'line',
  data: {
    labels: [],
    datasets: [
      { label: 'STEL Dc (gates routing)', data: [], borderColor: '#58a6ff', tension: 0.3 },
      { label: "Burrows' Delta (advisory)", data: [], borderColor: '#8b949e', borderDash: [6, 4], tension: 0.3 },
    ],
  },
  options: { scales: { y: { min: 0 } }, animation: false },
});

const padColors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff'];

window.fwHandlers = {
  draft_chunk(ev) {
    const host = document.getElementById('prose-stream');
    host.insertAdjacentText('beforeend', ev.text);
    host.scrollTop = host.scrollHeight;
  },
  draft_complete() { document.getElementById('fsm-status').textContent = 'auditing'; },
  beat_committed(ev) { document.getElementById('fsm-status').textContent = `committed ${ev.beat_id}`; },
  word_count(ev) { document.getElementById('word-count').textContent = ev.total; },
  status(ev) { document.getElementById('fsm-status').textContent = ev.status; },
  critic_result(ev) {
    const feed = document.getElementById('critic-feed');
    feed.insertAdjacentHTML('afterbegin',
      `<div><span class="badge bg-dark border">${ev.critic}</span> ${ev.error_code}</div>`);
  },
  pad_update(ev) {
    let ds = padChart.data.datasets.find(d => d.label === ev.char_id);
    if (!ds) {
      const color = padColors[padChart.data.datasets.length % padColors.length];
      ds = { label: ev.char_id, data: [0, 0, 0], borderColor: color, backgroundColor: color + '33' };
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
      driftChart.data.datasets.forEach(d => d.data.shift());
    }
    driftChart.update();
  },
};

const post = (url) => fetch(url, { method: 'POST' });
document.getElementById('btn-generate').addEventListener('click', () => {
  document.getElementById('prose-stream').textContent = '';
  document.getElementById('fsm-status').textContent = 'generating';
  post('/generate');
});
document.getElementById('btn-pause').addEventListener('click', () => post('/control/pause'));
document.getElementById('btn-resume').addEventListener('click', () => post('/control/resume'));
document.getElementById('btn-stop').addEventListener('click', () => post('/control/stop'));
