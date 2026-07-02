/**
 * Fitness Robot Dashboard - Firebase frontend.
 * Auth, telemetry, analytics, and robot control all use Firebase directly.
 */

const DEFAULT_CAMERA_URL = localStorage.getItem('currentCameraUrl') || 'http://fitness.local:8081/stream';
let currentCameraUrl = DEFAULT_CAMERA_URL;
let currentUserId = null;
let currentUserEmail = null;
let telemetryUnsubscribe = null;
let watchUnsubscribe = null;

let repsChart = null;
let timeChart = null;
let formChart = null;

let currentSessionId = null;
let currentExercise = null;
let latestEventTimestamp = null;

// Analytics real-time listeners (unsubscribed when tab is hidden)
let analyticsAggUnsubscribe = null;
let analyticsSessionUnsubscribe = null;

const STEP_SERVICE_UUID = '12345678-1234-1234-1234-123456789001';
const STEP_COUNT_UUID = '12345678-1234-1234-1234-123456789002';
const STEP_TIME_SYNC_UUID = '12345678-1234-1234-1234-123456789003';
const STEP_DAILY_DATA_UUID = '12345678-1234-1234-1234-123456789004';
const STEP_COMMAND_UUID = '12345678-1234-1234-1234-123456789005';
const STEP_HISTORY_KEY = 'fitness_robot_step_history';
const STEP_GOAL = 10000;

let stepDevice = null;
let stepServer = null;
let stepCountChar = null;
let stepTimeSyncChar = null;
let stepDailyDataChar = null;
let stepCommandChar = null;
let stepConnected = false;
let lastSyncedStepCount = -1;

if (!window.firebaseConfig || window.firebaseConfig.apiKey === 'PASTE_FIREBASE_WEB_API_KEY') {
  console.warn('Firebase web config is not set. Edit frontend/firebase-config.js.');
} else {
  firebase.initializeApp(window.firebaseConfig);
}

const auth = firebase.apps.length ? firebase.auth() : null;
const db = firebase.apps.length ? firebase.firestore() : null;

// ---------- Auth ----------

async function register() {
  clearAuthError();
  const email = document.getElementById('regEmail').value.trim();
  const password = document.getElementById('regPassword').value;

  if (!auth || !db) {
    showAuthError('Firebase config is missing. Edit frontend/firebase-config.js first.');
    return;
  }
  if (!email || !password) {
    showAuthError('Please enter email and password');
    return;
  }

  try {
    const result = await auth.createUserWithEmailAndPassword(email, password);
    await ensureUserDocument(result.user);
  } catch (e) {
    showAuthError(firebaseAuthMessage(e));
  }
}

async function login() {
  clearAuthError();
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;

  if (!auth) {
    showAuthError('Firebase config is missing. Edit frontend/firebase-config.js first.');
    return;
  }
  if (!email || !password) {
    showAuthError('Please enter email and password');
    return;
  }

  try {
    await auth.signInWithEmailAndPassword(email, password);
  } catch (e) {
    showAuthError(firebaseAuthMessage(e));
  }
}

async function logout() {
  disconnectRealtime();
  disconnectAnalyticsListeners();
  if (stepConnected) disconnectStepTracker();
  if (auth) await auth.signOut();
  currentUserId = null;
  currentUserEmail = null;

  document.getElementById('appView').classList.add('hidden');
  document.getElementById('authView').classList.remove('hidden');
  document.getElementById('userInfo').classList.add('hidden');
  document.getElementById('forceStopBtn').classList.add('hidden');
}

async function ensureUserDocument(user) {
  if (!db || !user) return;
  await db.collection('users').doc(user.uid).set({
    email: user.email,
    updated_at: firebase.firestore.FieldValue.serverTimestamp(),
    created_at: firebase.firestore.FieldValue.serverTimestamp()
  }, { merge: true });
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
  document.getElementById('userEmail').textContent = email || 'You';

  document.getElementById('forceStopBtn').classList.remove('hidden');
  showTab('live');
  startCameraStreamIfNeeded();
}

function firebaseAuthMessage(error) {
  const map = {
    'auth/email-already-in-use': 'Email already registered',
    'auth/invalid-email': 'Invalid email address',
    'auth/invalid-login-credentials': 'Incorrect email or password',
    'auth/user-not-found': 'Incorrect email or password',
    'auth/wrong-password': 'Incorrect email or password',
    'auth/weak-password': 'Password should be at least 6 characters'
  };
  return map[error.code] || error.message || 'Authentication failed';
}

// ---------- Tabs ----------

function showTab(tab) {
  const live = document.getElementById('liveView');
  const steps = document.getElementById('stepsView');
  const analytics = document.getElementById('analyticsView');
  const btnLive = document.getElementById('tab-live');
  const btnSteps = document.getElementById('tab-steps');
  const btnAnalytics = document.getElementById('tab-analytics');
  const btnStart = document.getElementById('startExerciseBtn');

  if (btnStart) {
    if (tab === 'live') {
      btnStart.classList.remove('hidden');
    } else {
      btnStart.classList.add('hidden');
    }
  }

  if (tab === 'live') {
    live.classList.remove('hidden');
    steps.classList.add('hidden');
    analytics.classList.add('hidden');
    btnLive.classList.add('active');
    btnSteps.classList.remove('active');
    btnAnalytics.classList.remove('active');
    // tear down live analytics listeners to save quota
    disconnectAnalyticsListeners();
  } else if (tab === 'steps') {
    live.classList.add('hidden');
    steps.classList.remove('hidden');
    analytics.classList.add('hidden');
    btnLive.classList.remove('active');
    btnSteps.classList.add('active');
    btnAnalytics.classList.remove('active');
    disconnectAnalyticsListeners();
    renderStepHistory();
  } else {
    live.classList.add('hidden');
    steps.classList.add('hidden');
    analytics.classList.remove('hidden');
    btnLive.classList.remove('active');
    btnSteps.classList.remove('active');
    btnAnalytics.classList.add('active');
    connectAnalyticsListeners();   // live real-time updates
  }
}

function disconnectAnalyticsListeners() {
  if (analyticsAggUnsubscribe) { analyticsAggUnsubscribe(); analyticsAggUnsubscribe = null; }
  if (analyticsSessionUnsubscribe) { analyticsSessionUnsubscribe(); analyticsSessionUnsubscribe = null; }
}

// ---------- Firestore realtime ----------

function connectRealtime() {
  if (!db || !currentUserId) return;
  disconnectRealtime();

  latestEventTimestamp = new Date();

  telemetryUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('events')
    .orderBy('ts', 'desc')
    .limit(50)
    .onSnapshot((snapshot) => {
      const changes = snapshot.docChanges()
        .filter(change => change.type === 'added')
        .map(change => ({ id: change.doc.id, ...change.doc.data() }))
        .sort((a, b) => toMillis(a.ts) - toMillis(b.ts));

      changes.forEach((msg) => {
        if (toMillis(msg.ts) >= latestEventTimestamp.getTime() - 1000) {
          handleTelemetryMessage(msg);
        }
      });
    }, (error) => appendLog(`Telemetry listener failed: ${error.message}`, true));

  const today = dateKey(new Date());
  watchUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('watch_data')
    .doc(today)
    .onSnapshot((doc) => {
      if (doc.exists) handleTelemetryMessage({ type: 'watch_steps', ...doc.data() });
    });

  appendLog('Connected to Firebase telemetry');
}

function disconnectRealtime() {
  if (telemetryUnsubscribe) telemetryUnsubscribe();
  if (watchUnsubscribe) watchUnsubscribe();
  telemetryUnsubscribe = null;
  watchUnsubscribe = null;
}

function handleTelemetryMessage(msg) {
  const type = msg.type;
  const payload = msg.payload || msg;

  if (type === 'session_start') {
    currentSessionId = msg.session_id || payload.session_id || msg.id;
    currentExercise = msg.exercise_type || payload.exercise_type;
    document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);
    appendLog(`Session started: ${formatExercise(currentExercise)}`);
    return;
  }

  if (type === 'session_end') {
    appendLog(`Session ended. Active: ${Number(payload.active_duration_s || 0).toFixed(1)}s`);
    currentSessionId = null;
    return;
  }

  if (type === 'robot_state') {
    const pill = document.getElementById('robotState');
    const state = (payload.state || 'STOPPED').toUpperCase();
    pill.textContent = state;
    pill.setAttribute('data-state', state);
    return;
  }

  if (type === 'rep') {
    currentExercise = payload.exercise || currentExercise;
    document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);

    if (payload.set !== undefined) {
      document.getElementById('currentSet').textContent = payload.set;
    }

    if (payload.exercise === 'bicep_curls' && (payload.left !== undefined || payload.right !== undefined)) {
      document.getElementById('currentReps').textContent = Math.max(payload.left || 0, payload.right || 0);
      document.getElementById('repsDetail').textContent = `L:${payload.left ?? 0}  R:${payload.right ?? 0}`;
    } else {
      document.getElementById('currentReps').textContent = payload.count ?? payload.reps ?? payload.total ?? 0;
      document.getElementById('repsDetail').textContent = '';
    }
    return;
  }

  if (type === 'plank_time' || type === 'plank') {
    const val = payload.value ?? payload.plank_timer ?? 0;
    document.getElementById('plankTime').textContent = formatSeconds(val);
    return;
  }

  if (type === 'form_error' || type === 'encouragement') {
    appendLog(payload.message || JSON.stringify(payload), true);
    return;
  }

  if (type === 'watch_steps') {
    updateStepDisplay({
      steps: Number(payload.steps || 0),
      cal: payload.calories,
      km: payload.distance_km,
      min: payload.active_min
    }, false);
    document.getElementById('watchLastSync').textContent = payload.last_synced_at ? formatTime(payload.last_synced_at) : '';
    return;
  }

  if (type === 'set_complete') {
    appendLog(`Set ${payload.set} complete (${payload.reps ?? '?'} reps)`);
    return;
  }

  if (['pause', 'resume', 'robot_command'].includes(type)) {
    appendLog(`${type}: ${JSON.stringify(payload)}`);
  }
}

function appendLog(text, isForm = false) {
  const container = document.getElementById('formLog');
  const entry = document.createElement('div');
  entry.className = isForm ? 'text-amber-300' : 'text-emerald-300/80';
  entry.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;

  while (container.children.length > 60) {
    container.removeChild(container.firstChild);
  }
}

function clearLog() {
  document.getElementById('formLog').innerHTML = '';
}

// ---------- Control ----------

let localControlSocket = null;

function connectLocalWebSocket(piIp) {
  if (!piIp) return;

  if (localControlSocket) {
    try {
      localControlSocket.close();
    } catch (e) {}
  }

  console.log(`🔌 Attempting to connect to local robot control at ws://${piIp}:8001`);
  localControlSocket = new WebSocket(`ws://${piIp}:8001`);

  localControlSocket.onopen = () => {
    console.log("✅ Local WebSocket control channel connected");
    appendLog("Connected to local robot control channel");
    const pill = document.getElementById('robotState');
    if (pill) {
      pill.textContent = "CONNECTED";
      pill.setAttribute('data-state', 'CONNECTED');
    }
  };

  localControlSocket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      console.log("📩 Received response from local robot:", data);
      if (data.type) {
        handleTelemetryMessage(data);
      }
    } catch (e) {
      console.warn("⚠️ Failed to parse local robot response:", event.data);
    }
  };

  localControlSocket.onclose = () => {
    console.log("❌ Local WebSocket control channel closed");
    appendLog("Disconnected from local robot control channel", true);
    localControlSocket = null;
    const pill = document.getElementById('robotState');
    if (pill) {
      pill.textContent = "OFFLINE";
      pill.setAttribute('data-state', 'OFFLINE');
    }
  };
}

async function sendForceStop() {
  await sendControlCommand({ action: 'force_stop' }, 'Force Stop sent to robot');
}

async function sendStartExercise() {
  await sendControlCommand({ action: 'start_exercise' }, 'Start detecting exercise signal sent to robot');
}

async function switchMode(mode) {
  const mapping = {
    bicep_curls: 'BICEP_CURLS',
    pushups: 'PUSHUPS',
    squats: 'SQUATS',
    plank: 'PLANK',
    person_follow: 'PERSON_FOLLOW'
  };
  await sendControlCommand({
    action: 'switch_mode',
    mode: mapping[mode] || mode.toUpperCase()
  }, `Requested mode: ${mode}`);
}

async function sendControlCommand(command, successMessage) {
  let sentLocally = false;

  // 1. Send via local WebSockets if connected
  if (localControlSocket && localControlSocket.readyState === WebSocket.OPEN) {
    try {
      localControlSocket.send(JSON.stringify(command));
      sentLocally = true;
      appendLog(`${successMessage} (Direct Local Connection)`);
    } catch (err) {
      console.warn("⚠️ Failed to send direct local command:", err);
    }
  }

  // 2. Send via Firestore if database and session exist
  if (db && currentUserId) {
    try {
      await db.collection('users').doc(currentUserId).collection('control_commands').add({
        ...command,
        from: 'web',
        status: 'pending',
        created_at: firebase.firestore.FieldValue.serverTimestamp()
      });
      if (!sentLocally) {
        appendLog(successMessage);
      }
    } catch (e) {
      if (!sentLocally) {
        appendLog(`Failed to sync command to cloud: ${e.message}`, true);
      }
    }
  } else if (!sentLocally) {
    appendLog("⚠️ Command failed: local robot not connected and not synced with cloud", true);
  }
}

// ---------- Camera Stream ----------

function normalizeUrl(input) {
  let url = input.trim();
  if (!url) return '';
  
  // If it doesn't have a protocol, prepend http://
  if (!/^https?:\/\//i.test(url)) {
    url = 'http://' + url;
  }
  
  try {
    const parsed = new URL(url);
    // If no port is specified, default to 8081 for the stream
    if (!parsed.port) {
      parsed.port = '8081';
    }
    // If no path is specified or it is just a root slash, default to "/stream"
    if (parsed.pathname === '/') {
      parsed.pathname = '/stream';
    }
    return parsed.toString();
  } catch (e) {
    return url;
  }
}

function setCameraStream() {
  const rawUrl = document.getElementById('cameraUrl').value.trim();
  if (!rawUrl) return;

  const url = normalizeUrl(rawUrl);
  // Update the input field with the normalized URL
  document.getElementById('cameraUrl').value = url;

  const img = document.getElementById('cameraFeed');
  const placeholder = document.getElementById('cameraPlaceholder');

  currentCameraUrl = url;
  localStorage.setItem('currentCameraUrl', url); // Save to localStorage

  img.src = url;
  img.onload = () => {
    img.classList.remove('hidden');
    placeholder.classList.add('hidden');
  };
  img.onerror = () => {
    img.classList.add('hidden');
    placeholder.classList.remove('hidden');
    placeholder.innerHTML = '<div class="text-red-400">Failed to load stream.<br>Check Pi MJPEG server is running.</div>';
  };

  // Extract hostname or IP address to connect to WebSocket control server
  try {
    const parsedUrl = new URL(url);
    const hostname = parsedUrl.hostname;
    if (hostname) {
      connectLocalWebSocket(hostname);
    }
  } catch (e) {
    console.warn("⚠️ Failed to parse local connection URL:", e);
  }
}

function startCameraStreamIfNeeded() {
  if (!currentCameraUrl) return;
  const cameraUrlInput = document.getElementById('cameraUrl');
  if (cameraUrlInput.value.trim() !== currentCameraUrl) {
    cameraUrlInput.value = currentCameraUrl;
  }
  setCameraStream();
}

async function generateDemoData() {
  if (!db || !currentUserId) {
    alert("Please log in first before generating demo data.");
    return;
  }

  const btn = document.querySelector("button[onclick='generateDemoData()']");
  const originalText = btn ? btn.textContent : "Generate Demo Data";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Generating...";
  }

  try {
    const batch = db.batch();
    const today = new Date();

    // Generate daily aggregates and session data for the past 7 days
    for (let i = 0; i < 7; i++) {
      const dayDate = new Date(today);
      dayDate.setDate(today.getDate() - i);
      const dateStr = dateKey(dayDate);

      // 1. Daily Aggregates doc — exact same schema as person_raspi.py writes
      const aggRef = db.collection('users').doc(currentUserId)
        .collection('daily_aggregates')
        .doc(dateStr);

      const reps = {
        bicep_curls: Math.floor(Math.random() * 25) + 15,
        pushups: Math.floor(Math.random() * 20) + 10,
        squats: Math.floor(Math.random() * 30) + 20
      };

      const plank_time = Math.floor(Math.random() * 90) + 40;
      const active_time = Math.floor(Math.random() * 900) + 500;
      const total_time = active_time + Math.floor(Math.random() * 500) + 200;

      batch.set(aggRef, {
        date: dateStr,
        steps: Math.floor(Math.random() * 7000) + 3000,
        reps: reps,
        plank_time_s: plank_time,
        active_time_s: active_time,
        total_time_s: total_time,
        form_good_count: Math.floor(Math.random() * 60) + 40,
        form_flaw_count: Math.floor(Math.random() * 10) + 1,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()   // matches Pi field name
      }, { merge: true });

      // 2. Workout session doc — exact same schema as person_raspi.py publish_session_end
      const sessionRef = db.collection('users').doc(currentUserId)
        .collection('sessions')
        .doc();

      const exercises = ['bicep_curls', 'pushups', 'squats', 'plank'];
      const chosenExercise = exercises[Math.floor(Math.random() * exercises.length)];

      const sessionTime = new Date(dayDate);
      sessionTime.setHours(8 + Math.floor(Math.random() * 10), Math.floor(Math.random() * 60));

      // Build summary matching the exact structure person_raspi.py produces
      let sessionSummary = {};
      if (chosenExercise === 'plank') {
        sessionSummary = { total_plank_time_s: plank_time };
      } else if (chosenExercise === 'bicep_curls') {
        const leftReps = Math.floor(reps.bicep_curls / 2);
        const rightReps = reps.bicep_curls - leftReps;
        sessionSummary = { left_reps: leftReps, right_reps: rightReps };
      } else {
        sessionSummary = { total_reps: reps[chosenExercise] || 15 };
      }

      batch.set(sessionRef, {
        exercise_type: chosenExercise,
        started_at: firebase.firestore.Timestamp.fromDate(sessionTime),
        ended_at: firebase.firestore.Timestamp.fromDate(new Date(sessionTime.getTime() + 20 * 60000)),
        active_duration_s: active_time,
        total_duration_s: total_time,
        summary: sessionSummary,
        robot_involved: false
      });
    }

    await batch.commit();

    // 3. watch_data for today — exact same schema as syncStepsToFirestore
    const watchRef = db.collection('users').doc(currentUserId)
      .collection('watch_data')
      .doc(dateKey(today));

    await watchRef.set({
      steps: 8420,
      calories: 336.8,
      distance_km: 6.31,
      active_min: 42,
      last_synced_at: firebase.firestore.FieldValue.serverTimestamp()
    }, { merge: true });

    alert("Demo data generated! Charts will update automatically.");
  } catch (error) {
    console.error("Error generating demo data: ", error);
    alert("Failed to generate demo data: " + error.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalText;
    }
  }
}

// ---------- Analytics ----------

/**
 * connectAnalyticsListeners — sets up live onSnapshot listeners for
 * daily_aggregates and sessions. Charts re-render automatically whenever
 * the Pi writes new reps, a BLE watch syncs steps, or a session ends.
 * Safe to call multiple times (tears down old listeners first).
 */
function connectAnalyticsListeners() {
  if (!db || !currentUserId) return;
  disconnectAnalyticsListeners();   // clean up any previous listeners

  const today = new Date();
  const fromDate = new Date(today);
  fromDate.setDate(today.getDate() - 6);

  // Snapshot storage shared between the two listeners
  let cachedAggDocs = [];
  let cachedSessionDocs = [];
  let watchStepsToday = 0;

  function rebuildAndRender() {
    const dailyByDate = new Map();
    for (let i = 0; i < 7; i++) {
      const day = new Date(fromDate);
      day.setDate(fromDate.getDate() + i);
      dailyByDate.set(dateKey(day), emptyDaily(dateKey(day)));
    }

    cachedAggDocs.forEach(data => {
      dailyByDate.set(data.date || data._docId, { ...emptyDaily(data.date || data._docId), ...data });
    });

    const daily = Array.from(dailyByDate.values()).sort((a, b) => a.date.localeCompare(b.date));

    // Merge today's watch steps into the daily aggregate for KPI accuracy
    const todayStr = dateKey(new Date());
    if (watchStepsToday > 0) {
      const todayAgg = daily.find(d => d.date === todayStr);
      if (todayAgg) todayAgg.steps = Math.max(todayAgg.steps || 0, watchStepsToday);
    }

    const form = {
      good: daily.reduce((sum, d) => sum + Number(d.form_good_count || d.form_good || 0), 0),
      flawed: daily.reduce((sum, d) => sum + Number(d.form_flaw_count || d.form_flawed || 0), 0)
    };
    const totals = daily.reduce((acc, d) => {
      acc.steps += Number(d.steps || 0);
      acc.plank_time_s += Number(d.plank_time_s || 0);
      acc.active_time_s += Number(d.active_time_s || 0);
      acc.total_time_s += Number(d.total_time_s || 0);
      Object.entries(d.reps || {}).forEach(([exercise, count]) => {
        acc.reps[exercise] = (acc.reps[exercise] || 0) + Number(count || 0);
      });
      return acc;
    }, { steps: 0, plank_time_s: 0, active_time_s: 0, total_time_s: 0, reps: {} });

    document.getElementById('kpiTotalReps').textContent = Object.values(totals.reps).reduce((a, b) => a + b, 0) || '0';
    document.getElementById('kpiPlankTime').textContent = formatSeconds(totals.plank_time_s);
    document.getElementById('kpiSteps').textContent = totals.steps ? totals.steps.toLocaleString() : '0';

    const totalForm = form.good + form.flawed;
    document.getElementById('kpiForm').textContent = `${totalForm > 0 ? Math.round((form.good / totalForm) * 100) : 100}% good`;

    renderRepsChart(daily);
    renderTimeChart(daily);
    renderFormChart(form);

    // Re-sort sessions descending by start time
    const sessionHistory = [...cachedSessionDocs].sort((a, b) => toMillis(b.started_at) - toMillis(a.started_at));
    renderSessionHistory(sessionHistory);
  }

  // Listener 1: daily_aggregates (updates when Pi writes a rep or session ends)
  analyticsAggUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('daily_aggregates')
    .where('date', '>=', dateKey(fromDate))
    .where('date', '<=', dateKey(today))
    .onSnapshot(snap => {
      cachedAggDocs = snap.docs.map(doc => ({ _docId: doc.id, ...doc.data() }));
      rebuildAndRender();
    }, err => appendLog(`Analytics listener error: ${err.message}`, true));

  // Listener 2: sessions (updates when a session starts or ends)
  analyticsSessionUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('sessions')
    .where('started_at', '>=', fromDate)
    .onSnapshot(snap => {
      cachedSessionDocs = snap.docs.map(doc => ({ id: doc.id, ...doc.data() }));
      rebuildAndRender();
    }, err => appendLog(`Session listener error: ${err.message}`, true));

  // One-shot today's watch_data for step KPI (not worth a persistent listener)
  db.collection('users').doc(currentUserId)
    .collection('watch_data')
    .doc(dateKey(today))
    .get()
    .then(doc => {
      if (doc.exists) {
        watchStepsToday = Number(doc.data().steps || 0);
        rebuildAndRender();
      }
    })
    .catch(() => {});
}

// Kept for backward compatibility (called after generateDemoData)
async function loadAnalytics() {
  connectAnalyticsListeners();
}

function emptyDaily(date) {
  return {
    date,
    steps: 0,
    reps: {},
    plank_time_s: 0,
    active_time_s: 0,
    total_time_s: 0,
    form_good_count: 0,
    form_flaw_count: 0
  };
}

function renderRepsChart(daily) {
  const ctx = document.getElementById('repsChart');
  const labels = daily.map(d => d.date.slice(5));
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
  if (formChart) formChart.destroy();
  formChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Good Form', 'Flawed Form'],
      datasets: [{ data: [form.good || 0, form.flawed || 0], backgroundColor: ['#34d399', '#f87171'] }]
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
    const dur = s.active_duration_s != null ? `${Number(s.active_duration_s).toFixed(0)}s active` : '';
    div.innerHTML = `
      <div>
        <span class="font-medium">${formatExercise(s.exercise_type)}</span>
        <span class="text-white/40 ml-2">${formatDate(s.started_at)}</span>
      </div>
      <div class="text-white/50">${dur}</div>
    `;
    container.appendChild(div);
  });
}

// ---------- Step Tracker ----------

async function toggleStepConnection() {
  if (stepConnected) {
    disconnectStepTracker();
  } else {
    await connectStepTracker();
  }
}

async function connectStepTracker() {
  if (!navigator.bluetooth) {
    stepLog('Web Bluetooth is not supported in this browser. Use Chrome or Edge.');
    setStepStatus('disconnected');
    return;
  }

  try {
    setStepStatus('connecting');
    stepLog('Opening Bluetooth device picker...');

    stepDevice = await requestStepDevice();
    stepLog(`Selected: ${stepDevice.name || 'Unknown Device'}`);
    stepDevice.addEventListener('gattserverdisconnected', onStepDisconnected);

    stepServer = await stepDevice.gatt.connect();
    stepLog('GATT connected');

    const service = await stepServer.getPrimaryService(STEP_SERVICE_UUID);
    stepCountChar = await service.getCharacteristic(STEP_COUNT_UUID);
    stepTimeSyncChar = await service.getCharacteristic(STEP_TIME_SYNC_UUID);
    stepDailyDataChar = await service.getCharacteristic(STEP_DAILY_DATA_UUID);
    stepCommandChar = await service.getCharacteristic(STEP_COMMAND_UUID);
    stepLog('Tracker characteristics ready');

    await stepCountChar.startNotifications();
    stepCountChar.addEventListener('characteristicvaluechanged', onStepCountChanged);

    await stepDailyDataChar.startNotifications();
    stepDailyDataChar.addEventListener('characteristicvaluechanged', onStepDailyDataChanged);

    stepConnected = true;
    setStepStatus('connected');
    stepLog('Connected successfully');

    setTimeout(async () => {
      await syncStepTime();
      await requestStepData();
    }, 500);
  } catch (error) {
    stepLog(`Connection error: ${error.message}`);
    setStepStatus('disconnected');
  }
}

async function requestStepDevice() {
  try {
    return await navigator.bluetooth.requestDevice({
      filters: [{ services: [STEP_SERVICE_UUID] }],
      optionalServices: [STEP_SERVICE_UUID]
    });
  } catch (serviceError) {
    stepLog(`Service filter failed: ${serviceError.message}`);
  }

  try {
    return await navigator.bluetooth.requestDevice({
      filters: [{ name: 'StepTracker' }, { namePrefix: 'Step' }],
      optionalServices: [STEP_SERVICE_UUID]
    });
  } catch (nameError) {
    stepLog(`Name filter failed: ${nameError.message}`);
  }

  return navigator.bluetooth.requestDevice({
    acceptAllDevices: true,
    optionalServices: [STEP_SERVICE_UUID]
  });
}

function disconnectStepTracker() {
  if (stepDevice && stepDevice.gatt.connected) {
    stepDevice.gatt.disconnect();
  }
  stepConnected = false;
  setStepStatus('disconnected');
  stepLog('Disconnected');
}

function onStepDisconnected() {
  stepConnected = false;
  setStepStatus('disconnected');
  stepLog('Device disconnected');
}

function onStepCountChanged(event) {
  const value = new TextDecoder().decode(event.target.value);
  const steps = parseInt(value, 10) || 0;
  
  // Calculate metrics using watch coefficients (calPerStep=0.04, metersPerStep=0.75)
  const cal = steps * 0.04;
  const km = (steps * 0.75) / 1000.0;
  
  // Read current active minutes from UI if present, to keep it consistent
  const activeMinEl = document.getElementById('stepActiveMin');
  const min = activeMinEl ? parseInt(activeMinEl.textContent, 10) || 0 : 0;
  
  const data = {
    steps: steps,
    cal: cal,
    km: km,
    min: min
  };
  
  // Update local UI immediately in real time so the user sees changes instantly
  updateStepDisplay(data, true);
  
  // Only sync to Firestore on the initial connection (when lastSyncedStepCount is -1)
  // or when the steps have increased by at least 10 since the last write.
  if (lastSyncedStepCount !== -1 && Math.abs(steps - lastSyncedStepCount) < 10) {
    return;
  }
  
  lastSyncedStepCount = steps;
  syncStepsToFirestore(data);
}

function onStepDailyDataChanged(event) {
  const value = new TextDecoder().decode(event.target.value);
  stepLog(`Data: ${value}`);

  try {
    const data = JSON.parse(value);
    
    // Keep lastSyncedStepCount updated to prevent duplicate step notifications
    lastSyncedStepCount = Number(data.steps || 0);
    
    updateStepDisplay(data, true);
    saveStepHistory(data);
    syncStepsToFirestore(data);
  } catch (error) {
    stepLog(`Parse error: ${error.message}`);
  }
}

async function syncStepTime() {
  if (!stepConnected || !stepTimeSyncChar) {
    stepLog('Not connected');
    return;
  }

  try {
    const now = Math.floor(Date.now() / 1000);
    const buffer = new ArrayBuffer(4);
    const view = new DataView(buffer);
    view.setUint32(0, now, true);
    await stepTimeSyncChar.writeValue(buffer);
    stepLog(`Time synced: ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    stepLog(`Time sync error: ${error.message}`);
  }
}

async function requestStepData() {
  if (!stepConnected || !stepCommandChar) {
    stepLog('Not connected');
    return;
  }

  try {
    await stepCommandChar.writeValue(new TextEncoder().encode('SYNC'));
    stepLog('Data requested');
  } catch (error) {
    stepLog(`Request error: ${error.message}`);
  }
}

async function resetStepCount() {
  if (!stepConnected || !stepCommandChar) {
    stepLog('Not connected');
    return;
  }

  if (!confirm('Reset step count to 0?')) return;

  try {
    await stepCommandChar.writeValue(new TextEncoder().encode('RESET'));
    stepLog('Steps reset command sent');
  } catch (error) {
    stepLog(`Reset error: ${error.message}`);
  }
}

function exportStepData() {
  const history = getStepHistory();
  if (!history.length) {
    stepLog('No data to export');
    return;
  }

  let csv = 'Date,Steps,Calories,Distance(km),ActiveMinutes\n';
  history.forEach(entry => {
    csv += `${entry.date},${entry.steps || 0},${entry.cal || 0},${entry.km || 0},${entry.min || 0}\n`;
  });

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `step_tracker_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  stepLog('Data exported as CSV');
}

function updateStepDisplay(data, updateSyncTime) {
  const steps = Number(data.steps || 0);
  const cal = Number(data.cal ?? data.calories ?? 0);
  const km = Number(data.km ?? data.distance_km ?? 0);
  const min = Number(data.min ?? data.active_min ?? 0);
  const goal = Number(data.goal || STEP_GOAL);
  const pct = Math.min(100, Math.round(Number(data.pct ?? ((steps / goal) * 100))));

  setTextIfPresent('stepCount', steps.toLocaleString());
  setTextIfPresent('stepCalories', Math.round(cal).toString());
  setTextIfPresent('stepDistance', km.toFixed(2));
  setTextIfPresent('stepActiveMin', min.toString());
  setTextIfPresent('stepGoalText', `${pct}% of ${goal.toLocaleString()} goal`);
  setTextIfPresent('watchSteps', steps ? steps.toLocaleString() : '-');
  setTextIfPresent('watchExtra', `${Math.round(cal)} cal - ${km.toFixed(2)} km`);

  const bar = document.getElementById('stepProgressBar');
  if (bar) bar.style.width = `${pct}%`;
  if (updateSyncTime) setTextIfPresent('watchLastSync', new Date().toLocaleTimeString());
}

function setStepStatus(status) {
  const badge = document.getElementById('stepStatusBadge');
  const btn = document.getElementById('stepConnectBtn');
  if (!badge || !btn) return;

  badge.className = `step-status ${status}`;
  if (status === 'connected') {
    badge.textContent = 'Connected';
    btn.textContent = 'Disconnect';
    btn.disabled = false;
  } else if (status === 'connecting') {
    badge.textContent = 'Connecting...';
    btn.textContent = 'Connecting...';
    btn.disabled = true;
  } else {
    badge.textContent = 'Disconnected';
    btn.textContent = 'Connect to Tracker';
    btn.disabled = false;
  }
}

function stepLog(msg) {
  const container = document.getElementById('stepLogContainer');
  if (!container) return;

  const entry = document.createElement('div');
  entry.className = 'text-white/70';
  entry.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;

  while (container.children.length > 50) {
    container.removeChild(container.firstChild);
  }
}

function clearStepLog() {
  const container = document.getElementById('stepLogContainer');
  if (container) container.innerHTML = '';
}

function getStepHistory() {
  const data = localStorage.getItem(STEP_HISTORY_KEY);
  return data ? JSON.parse(data) : [];
}

function saveStepHistory(entry) {
  const history = getStepHistory();
  const today = new Date().toLocaleDateString();
  const existingIndex = history.findIndex(h => h.date === today);
  const next = {
    date: today,
    steps: Number(entry.steps || 0),
    cal: Number(entry.cal ?? entry.calories ?? 0),
    km: Number(entry.km ?? entry.distance_km ?? 0),
    min: Number(entry.min ?? entry.active_min ?? 0),
    pct: Number(entry.pct || 0)
  };

  if (existingIndex >= 0) {
    history[existingIndex] = next;
  } else {
    history.unshift(next);
  }

  while (history.length > 30) history.pop();
  localStorage.setItem(STEP_HISTORY_KEY, JSON.stringify(history));
  renderStepHistory();
}

function renderStepHistory() {
  const container = document.getElementById('stepHistoryList');
  if (!container) return;

  const history = getStepHistory();
  if (!history.length) {
    container.innerHTML = '<div class="text-white/40 text-xs">No data yet.</div>';
    return;
  }

  container.innerHTML = history.map(entry => `
    <div class="flex items-center justify-between border-b border-white/10 pb-2">
      <span class="text-white/60">${entry.date}</span>
      <span class="font-medium">${Number(entry.steps || 0).toLocaleString()} steps</span>
      <span class="text-white/45">${Number(entry.km || 0).toFixed(2)} km</span>
    </div>
  `).join('');
}

async function syncStepsToFirestore(data) {
  if (!db || !currentUserId) return;

  try {
    const today = dateKey(new Date());
    const steps = Number(data.steps || 0);
    const cal = Number(data.cal ?? data.calories ?? 0);
    const km = Number(data.km ?? data.distance_km ?? 0);
    const min = Number(data.min ?? data.active_min ?? 0);
    
    await db.collection('users').doc(currentUserId)
      .collection('watch_data')
      .doc(today)
      .set({
        steps: steps,
        calories: cal,
        distance_km: km,
        active_min: min,
        last_synced_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true });
      
    stepLog(`Firestore synced: ${steps.toLocaleString()} steps, ${cal.toFixed(1)} cal, ${km.toFixed(2)} km, ${min} active min`);
  } catch (error) {
    stepLog(`Firestore sync failed: ${error.message}`);
  }
}

function setTextIfPresent(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// ---------- Helpers ----------

function formatExercise(ex) {
  if (!ex) return '-';
  const map = {
    bicep_curls: 'Bicep Curls',
    pushups: 'Push-ups',
    squats: 'Squats',
    plank: 'Plank',
    person_follow: 'Person Following'
  };
  return map[ex] || ex;
}

function formatSeconds(sec) {
  if (sec == null || isNaN(sec)) return '-';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m}:${s.toString().padStart(2, '0')}` : `${s}s`;
}

function dateKey(date) {
  return date.toISOString().slice(0, 10);
}

function toDate(value) {
  if (!value) return null;
  if (value.toDate) return value.toDate();
  if (value instanceof Date) return value;
  return new Date(value);
}

function toMillis(value) {
  const date = toDate(value);
  return date ? date.getTime() : 0;
}

function formatTime(value) {
  const date = toDate(value);
  return date ? date.toLocaleTimeString() : '';
}

function formatDate(value) {
  const date = toDate(value);
  return date ? date.toLocaleDateString() : '';
}

// ---------- Boot ----------

(function init() {
  renderStepHistory();
  if (!navigator.bluetooth) {
    const button = document.getElementById('stepConnectBtn');
    if (button) button.disabled = true;
    stepLog('Web Bluetooth is not supported. Use Chrome or Edge on a secure origin.');
  }

  if (auth) {
    auth.onAuthStateChanged(async (user) => {
      if (!user) {
        disconnectRealtime();
        document.getElementById('authView').classList.remove('hidden');
        document.getElementById('appView').classList.add('hidden');
        return;
      }

      currentUserId = user.uid;
      currentUserEmail = user.email;
      await ensureUserDocument(user);
      showAppView(currentUserEmail);
      connectRealtime();
    });
  } else {
    showAuthError('Firebase config is missing. Edit frontend/firebase-config.js first.');
  }

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
