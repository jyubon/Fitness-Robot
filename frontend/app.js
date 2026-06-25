/**
 * Fitness Robot Dashboard - Frontend (vanilla JS)
 * All data is real and comes from the backend / Pi.
 * No dummy data is ever generated here.
 */

const API_BASE = ''; // Same origin or configure if backend is on different port
let currentToken = null;
let currentUserId = null;
let telemetryWs = null;
let controlWs = null;

// Charts (lazy init)
let repsChart = null;
let timeChart = null;
let formChart = null;

// Live state
let currentSessionId = null;
let currentExercise = null;

// ---------- Auth ----------

async function register() {
  clearAuthError();
  const email = document.getElementById('regEmail').value.trim();
  const password = document.getElementById('regPassword').value;

  if (!email || !password) {
    showAuthError('Please enter email and password');
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Registration failed');

    // Auto-login after register
    await doLogin(email, password);
  } catch (e) {
    showAuthError(e.message);
  }
}

async function login() {
  clearAuthError();
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  await doLogin(email, password);
}

async function doLogin(email, password) {
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Login failed');

    currentToken = data.access_token;
    currentUserId = data.user_id;

    localStorage.setItem('fr_token', currentToken);
    localStorage.setItem('fr_user_id', currentUserId);

    showAppView(email);
    connectWebSockets();
  } catch (e) {
    showAuthError(e.message);
  }
}

function logout() {
  disconnectWebSockets();
  localStorage.removeItem('fr_token');
  localStorage.removeItem('fr_user_id');
  currentToken = null;
  currentUserId = null;

  document.getElementById('appView').classList.add('hidden');
  document.getElementById('authView').classList.remove('hidden');
  document.getElementById('userInfo').classList.add('hidden');
  document.getElementById('forceStopBtn').classList.add('hidden');
}

function showAuthError(msg) {
  const el = document.getElementById('authError');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function clearAuthError() {
  const el = document.getElementById('authError');
  el.textContent = '';
  el.classList.add('hidden');
}

function showAppView(email) {
  document.getElementById('authView').classList.add('hidden');
  document.getElementById('appView').classList.remove('hidden');

  const userInfo = document.getElementById('userInfo');
  userInfo.classList.remove('hidden');
  userInfo.classList.add('flex');
  document.getElementById('userEmail').textContent = email;

  document.getElementById('forceStopBtn').classList.remove('hidden');

  // Default to live tab
  showTab('live');
}

// ---------- Tabs ----------

function showTab(tab) {
  const live = document.getElementById('liveView');
  const analytics = document.getElementById('analyticsView');
  const btnLive = document.getElementById('tab-live');
  const btnAnalytics = document.getElementById('tab-analytics');

  if (tab === 'live') {
    live.classList.remove('hidden');
    analytics.classList.add('hidden');
    btnLive.classList.add('active');
    btnAnalytics.classList.remove('active');
  } else {
    live.classList.add('hidden');
    analytics.classList.remove('hidden');
    btnLive.classList.remove('active');
    btnAnalytics.classList.add('active');
    loadAnalytics();
  }
}

// ---------- WebSocket ----------

function connectWebSockets() {
  if (!currentToken) return;

  disconnectWebSockets();

  // Telemetry (receives reps, form, robot_state, watch, etc.)
  const telemetryUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/telemetry?token=${currentToken}`;
  telemetryWs = new WebSocket(telemetryUrl);

  telemetryWs.onopen = () => {
    appendLog('Connected to live telemetry');
  };

  telemetryWs.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleTelemetryMessage(msg);
    } catch (e) {
      console.warn('Bad telemetry message', event.data);
    }
  };

  telemetryWs.onclose = () => {
    // Silent - will reconnect on next action or manual refresh
  };

  // Control (for receiving acks or raw from Pi)
  const controlUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/control?token=${currentToken}`;
  controlWs = new WebSocket(controlUrl);

  controlWs.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.action || msg.raw) {
        appendLog(`Control: ${msg.action || msg.raw}`);
      }
    } catch {}
  };
}

function disconnectWebSockets() {
  if (telemetryWs) {
    try { telemetryWs.close(); } catch {}
    telemetryWs = null;
  }
  if (controlWs) {
    try { controlWs.close(); } catch {}
    controlWs = null;
  }
}

function handleTelemetryMessage(msg) {
  const type = msg.type;

  if (type === 'session_start') {
    currentSessionId = msg.session_id;
    currentExercise = msg.exercise_type;
    document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);
    appendLog(`Session started: ${formatExercise(currentExercise)}`);
    return;
  }

  if (type === 'session_end') {
    appendLog(`Session ended. Active: ${msg.active_duration_s?.toFixed?.(1) || '?'}s`);
    currentSessionId = null;
    return;
  }

  if (type === 'robot_state') {
    const pill = document.getElementById('robotState');
    const state = (msg.payload?.state || msg.state || 'STOPPED').toUpperCase();
    pill.textContent = state;
    pill.setAttribute('data-state', state);
    return;
  }

  if (type === 'rep') {
    const p = msg.payload || msg;
    currentExercise = p.exercise || currentExercise;

    document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);

    if (p.set !== undefined) {
      document.getElementById('currentSet').textContent = p.set;
    }

    let repText = '';
    if (p.exercise === 'bicep_curls' && (p.left !== undefined || p.right !== undefined)) {
      repText = `L:${p.left ?? 0}  R:${p.right ?? 0}`;
      document.getElementById('currentReps').textContent = Math.max(p.left || 0, p.right || 0);
    } else {
      const count = p.count ?? p.reps ?? p.total ?? 0;
      document.getElementById('currentReps').textContent = count;
      repText = '';
    }
    document.getElementById('repsDetail').textContent = repText;
    return;
  }

  if (type === 'plank_time' || type === 'plank') {
    const val = msg.payload?.value ?? msg.value ?? msg.plank_timer ?? 0;
    document.getElementById('plankTime').textContent = formatSeconds(val);
    return;
  }

  if (type === 'form_error' || type === 'encouragement') {
    const text = msg.payload?.message || msg.message || JSON.stringify(msg.payload || msg);
    appendLog(text, true);
    return;
  }

  if (type === 'watch_steps') {
    document.getElementById('watchSteps').textContent = msg.steps ?? '—';
    document.getElementById('watchLastSync').textContent = msg.synced_at ? new Date(msg.synced_at).toLocaleTimeString() : '';
    if (msg.calories != null || msg.distance_km != null) {
      document.getElementById('watchExtra').textContent =
        `${msg.calories ?? '?'} cal • ${(msg.distance_km ?? 0).toFixed(2)} km`;
    }
    return;
  }

  if (type === 'set_complete') {
    const p = msg.payload || msg;
    appendLog(`Set ${p.set} complete (${p.reps ?? '?'} reps)`);
    return;
  }

  // Fallback: show raw interesting events
  if (['pause', 'resume', 'robot_command'].includes(type)) {
    appendLog(`${type}: ${JSON.stringify(msg.payload || msg)}`);
  }
}

function appendLog(text, isForm = false) {
  const container = document.getElementById('formLog');
  const entry = document.createElement('div');
  entry.className = isForm ? 'text-amber-300' : 'text-emerald-300/80';
  entry.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;

  // Keep only last 60 lines
  while (container.children.length > 60) {
    container.removeChild(container.firstChild);
  }
}

function clearLog() {
  const container = document.getElementById('formLog');
  container.innerHTML = '';
}

// ---------- Control ----------

async function sendForceStop() {
  if (!currentToken) return;
  try {
    await fetch(`${API_BASE}/api/control`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${currentToken}`
      },
      body: JSON.stringify({ action: 'force_stop' })
    });
    appendLog('Force Stop sent to robot');
  } catch (e) {
    appendLog('Failed to send Force Stop: ' + e.message);
  }
}

async function switchMode(mode) {
  if (!currentToken) return;

  const mapping = {
    'bicep_curls': 'BICEP_CURLS',
    'pushups': 'PUSHUPS',
    'squats': 'SQUATS',
    'plank': 'PLANK',
    'person_follow': 'PERSON_FOLLOW'
  };

  const payloadMode = mapping[mode] || mode.toUpperCase();

  try {
    await fetch(`${API_BASE}/api/control`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${currentToken}`
      },
      body: JSON.stringify({ action: 'switch_mode', mode: payloadMode })
    });
    appendLog(`Requested mode: ${mode}`);
  } catch (e) {
    appendLog('Failed to switch mode: ' + e.message);
  }
}

// ---------- Camera Stream ----------

function setCameraStream() {
  const url = document.getElementById('cameraUrl').value.trim();
  const img = document.getElementById('cameraFeed');
  const placeholder = document.getElementById('cameraPlaceholder');

  if (!url) return;

  img.src = url;
  img.onload = () => {
    img.classList.remove('hidden');
    placeholder.classList.add('hidden');
  };
  img.onerror = () => {
    img.classList.add('hidden');
    placeholder.classList.remove('hidden');
    placeholder.innerHTML = `<div class="text-red-400">Failed to load stream.<br>Check Pi MJPEG server is running.</div>`;
  };
}

// ---------- Analytics ----------

async function loadAnalytics() {
  if (!currentToken) return;

  try {
    const res = await fetch(`${API_BASE}/api/analytics/weekly`, {
      headers: { 'Authorization': `Bearer ${currentToken}` }
    });
    if (!res.ok) throw new Error('Failed to load analytics');
    const data = await res.json();

    // KPIs
    document.getElementById('kpiTotalReps').textContent = Object.values(data.totals?.reps || {}).reduce((a, b) => a + b, 0) || '0';
    const plank = data.totals?.plank_time_s ?? 0;
    document.getElementById('kpiPlankTime').textContent = formatSeconds(plank);
    document.getElementById('kpiSteps').textContent = data.totals?.steps ?? '0';

    const form = data.form_accuracy || { good: 0, flawed: 0 };
    const totalForm = (form.good || 0) + (form.flawed || 0);
    const pct = totalForm > 0 ? Math.round((form.good / totalForm) * 100) : 100;
    document.getElementById('kpiForm').textContent = `${pct}% good`;

    // Charts
    renderRepsChart(data.daily);
    renderTimeChart(data.daily);
    renderFormChart(form);

    // Session history
    renderSessionHistory(data.session_history || []);
  } catch (e) {
    console.error(e);
  }
}

function renderRepsChart(daily) {
  const ctx = document.getElementById('repsChart');
  const labels = daily.map(d => d.date.slice(5)); // MM-DD

  const curls = daily.map(d => (d.reps && d.reps.bicep_curls) || 0);
  const push = daily.map(d => (d.reps && d.reps.pushups) || 0);
  const squats = daily.map(d => (d.reps && d.reps.squats) || 0);

  if (repsChart) repsChart.destroy();

  repsChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Curls', data: curls, backgroundColor: '#34d399' },
        { label: 'Push-ups', data: push, backgroundColor: '#60a5fa' },
        { label: 'Squats', data: squats, backgroundColor: '#fbbf24' }
      ]
    },
    options: {
      responsive: true,
      scales: { y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.1)' } }, x: { grid: { color: 'rgba(255,255,255,0.06)' } } },
      plugins: { legend: { labels: { color: '#a1a1aa' } } }
    }
  });
}

function renderTimeChart(daily) {
  const ctx = document.getElementById('timeChart');
  const labels = daily.map(d => d.date.slice(5));

  const active = daily.map(d => d.active_time_s || 0);
  const total = daily.map(d => d.total_time_s || 0);

  if (timeChart) timeChart.destroy();

  timeChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Active (s)', data: active, borderColor: '#34d399', tension: 0.2 },
        { label: 'Total (s)', data: total, borderColor: '#64748b', tension: 0.2 }
      ]
    },
    options: {
      responsive: true,
      scales: { y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.1)' } } },
      plugins: { legend: { labels: { color: '#a1a1aa' } } }
    }
  });
}

function renderFormChart(form) {
  const ctx = document.getElementById('formChart');
  const good = form.good || 0;
  const flawed = form.flawed || 0;

  if (formChart) formChart.destroy();

  formChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Good Form', 'Flawed Form'],
      datasets: [{
        data: [good, flawed],
        backgroundColor: ['#34d399', '#f87171']
      }]
    },
    options: {
      plugins: { legend: { position: 'bottom', labels: { color: '#a1a1aa' } } }
    }
  });
}

function renderSessionHistory(sessions) {
  const container = document.getElementById('sessionHistory');
  container.innerHTML = '';

  if (!sessions.length) {
    container.innerHTML = '<div class="text-white/40 text-xs">No sessions yet this week.</div>';
    return;
  }

  sessions.forEach(s => {
    const div = document.createElement('div');
    div.className = 'flex justify-between text-xs border-b border-white/10 py-1';
    const dur = s.active_duration_s != null ? `${s.active_duration_s.toFixed(0)}s active` : '';
    div.innerHTML = `
      <div>
        <span class="font-medium">${formatExercise(s.exercise_type)}</span>
        <span class="text-white/40 ml-2">${new Date(s.started_at).toLocaleDateString()}</span>
      </div>
      <div class="text-white/50">${dur}</div>
    `;
    container.appendChild(div);
  });
}

// ---------- Helpers ----------

function formatExercise(ex) {
  if (!ex) return '—';
  const map = {
    'bicep_curls': 'Bicep Curls',
    'pushups': 'Push-ups',
    'squats': 'Squats',
    'plank': 'Plank',
    'person_follow': 'Person Following'
  };
  return map[ex] || ex;
}

function formatSeconds(sec) {
  if (sec == null || isNaN(sec)) return '—';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}:${s.toString().padStart(2, '0')}` : `${s}s`;
}

// ---------- Boot ----------

(function init() {
  // Try restore session
  const savedToken = localStorage.getItem('fr_token');
  const savedUser = localStorage.getItem('fr_user_id');

  if (savedToken && savedUser) {
    currentToken = savedToken;
    currentUserId = savedUser;

    // We don't have email stored, so fetch a minimal profile or just show logged-in state
    // For simplicity we show a generic "logged in" until they interact
    document.getElementById('authView').classList.add('hidden');
    document.getElementById('appView').classList.remove('hidden');

    const userInfo = document.getElementById('userInfo');
    userInfo.classList.remove('hidden');
    userInfo.classList.add('flex');
    document.getElementById('userEmail').textContent = 'You';

    document.getElementById('forceStopBtn').classList.remove('hidden');

    connectWebSockets();
    showTab('live');
  }

  // Allow pressing Enter in auth fields
  ['loginEmail', 'loginPassword', 'regEmail', 'regPassword'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          if (id.startsWith('login')) login();
          else register();
        }
      });
    }
  });
})();
