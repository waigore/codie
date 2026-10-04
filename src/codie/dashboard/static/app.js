async function refresh() {
  try {
    const s = await fetch('/api/summary').then(r => r.json());
    document.getElementById('summary').textContent =
      `repo ${s.repo} · daily $${(s.daily_cost_usd || 0).toFixed(3)} · ${s.halted ? 'halted' : s.paused ? 'paused' : 'running'}`;
    const a = await fetch('/api/agents').then(r => r.json());
    const box = document.getElementById('agents');
    box.innerHTML = a.agents.map(ag => `
      <div class="agent">
        <h3>${ag.role}
          <label class="toggle">on<input type="checkbox" ${ag.enabled ? 'checked' : ''} onchange="setEnabled('${ag.role}', this.checked)"/></label>
        </h3>
        <div class="state">${ag.state} — ${ag.current_title || `last: ${ag.last_title}`}</div>
        <div>outcome: ${ag.last_outcome || '—'} · today $${(ag.today_cost || 0).toFixed(3)}</div>
      </div>`).join('');
  } catch (e) { /* backoff on errors */ }
}
async function setEnabled(role, enabled) {
  await fetch(`/api/agents/${role}/enabled`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ enabled }) });
}
async function loadLogs() {
  const logs = await fetch('/api/logs?limit=50').then(r => r.json());
  document.getElementById('logs').textContent = logs.events.map(e => `[${e.ts}] ${e.role} ${e.kind}: ${e.message}`).join('\n');
}
const stream = new EventSource('/api/stream');
stream.onmessage = () => loadLogs();
refresh();
setInterval(refresh, 5000);
setInterval(loadLogs, 5000);
