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
let robotState = 'READY';

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

  setRobotConnectionStatus(false);
  setWatchConnectionStatus(false);

  document.getElementById('appView').classList.add('hidden');
  document.getElementById('authView').classList.remove('hidden');
  const userChip = document.getElementById('userInfo');
  if (userChip) userChip.classList.add('hidden');
  document.getElementById('forceStopBtn').classList.add('hidden');
  document.getElementById('batteryIndicator').classList.add('hidden');

  const mobileNav = document.getElementById('mobileNavBar');
  if (mobileNav) mobileNav.classList.add('hidden');
  const mobileStop = document.getElementById('mobileForceStopBtn');
  if (mobileStop) mobileStop.classList.add('hidden');
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
  if (userInfo) {
    userInfo.classList.remove('hidden');
    userInfo.classList.add('flex');
  }
  const emailEl = document.getElementById('userEmail');
  if (emailEl) emailEl.textContent = email || 'You';

  const profEmail = document.getElementById('profileEmail');
  if (profEmail) profEmail.textContent = email || 'coach@fitness.com';

  const storedName = localStorage.getItem('fitness_robot_profile_name');
  const profName = document.getElementById('profileName');
  const profAvatar = document.getElementById('profileAvatar');
  let displayName = storedName;
  if (!displayName && email) {
    const userPart = email.split('@')[0];
    displayName = userPart.charAt(0).toUpperCase() + userPart.slice(1);
  }
  displayName = displayName || 'Athlete User';
  if (profName) profName.textContent = displayName;
  if (profAvatar) profAvatar.textContent = displayName.slice(0, 2).toUpperCase();

  const batteryInd = document.getElementById('batteryIndicator');
  if (batteryInd) batteryInd.classList.remove('hidden');

  const mobileNav = document.getElementById('mobileNavBar');
  if (mobileNav) mobileNav.classList.remove('hidden');

  updateGreeting();
  setRobotConnectionStatus(localControlSocket && localControlSocket.readyState === 1);
  setWatchConnectionStatus(stepConnected);
  showTab('live');
}

function setRobotConnectionStatus(isOnline) {
  const dot = document.getElementById('robotStatusDot');
  const text = document.getElementById('robotStatusText');
  if (!text) return;
  if (isOnline) {
    text.textContent = 'ONLINE';
    text.className = 'text-sm sm:text-base font-extrabold tracking-wide text-emerald-400';
    if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse';
  } else {
    text.textContent = 'OFFLINE';
    text.className = 'text-sm sm:text-base font-extrabold tracking-wide text-red-400';
    if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-red-400';
  }
}

function setWatchConnectionStatus(isOnline) {
  const dot = document.getElementById('watchStatusDot');
  const text = document.getElementById('watchStatusText');
  if (!text) return;
  if (isOnline) {
    text.textContent = 'ONLINE';
    text.className = 'text-sm sm:text-base font-extrabold tracking-wide text-emerald-400';
    if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse';
  } else {
    text.textContent = 'OFFLINE';
    text.className = 'text-sm sm:text-base font-extrabold tracking-wide text-red-400';
    if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-red-400';
  }
}

function updateGreeting() {
  const greetingEl = document.getElementById('homeGreetingText');
  const greetingSubEl = document.getElementById('homeGreetingSub');
  const greetingIconEl = document.getElementById('homeGreetingIcon');
  const dateEl = document.getElementById('homeCurrentDateText');
  const badgeEl = document.getElementById('homeTimeOfDayBadge');
  const heroRoutineEl = document.getElementById('homeHeroRoutine');

  if (dateEl) {
    const now = new Date();
    dateEl.textContent = now.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric'
    });
  }

  if (heroRoutineEl) {
    heroRoutineEl.textContent = currentExercise ? formatExercise(currentExercise) : 'Bicep Curls';
  }

  if (!greetingEl) return;

  const hour = new Date().getHours();
  const storedName = localStorage.getItem('fitness_robot_profile_name');
  let name = storedName;
  if (!name && currentUserEmail) {
    const userPart = currentUserEmail.split('@')[0];
    name = userPart.charAt(0).toUpperCase() + userPart.slice(1);
  }
  name = name || 'Athlete';

  let greeting = 'Good Day';
  let badge = 'DAYTIME SESSION';
  let icon = '👋';
  let sub = 'Ready to achieve your daily fitness goals? Your autonomous companion is standing by.';

  if (hour >= 5 && hour < 12) {
    greeting = 'Good Morning';
    badge = 'MORNING ENERGIZER';
    icon = '🌅';
    sub = 'Rise and shine! Ready to kickstart your day with an energized workout?';
  } else if (hour >= 12 && hour < 17) {
    greeting = 'Good Afternoon';
    badge = 'AFTERNOON POWER';
    icon = '☀️';
    sub = 'Keep the momentum going! Great time to power through your workout targets.';
  } else if (hour >= 17 && hour < 22) {
    greeting = 'Good Evening';
    badge = 'EVENING STRENGTH';
    icon = '🌆';
    sub = 'Finish your day strong! Perfect time for a focused strength & form session.';
  } else {
    greeting = 'Good Evening';
    badge = 'NIGHT RECOVERY';
    icon = '🌙';
    sub = 'Late night training? Stay hydrated, maintain strict form, and recover well!';
  }

  greetingEl.textContent = `${greeting}, ${name}!`;
  if (greetingSubEl) greetingSubEl.textContent = sub;
  if (greetingIconEl) greetingIconEl.textContent = icon;
  if (badgeEl) badgeEl.textContent = badge;
}

function editProfileName() {
  const profName = document.getElementById('profileName');
  const current = profName ? profName.textContent : '';
  const newName = prompt('Enter your name:', current);
  if (newName && newName.trim()) {
    const clean = newName.trim();
    localStorage.setItem('fitness_robot_profile_name', clean);
    if (profName) profName.textContent = clean;
    const profAvatar = document.getElementById('profileAvatar');
    if (profAvatar) profAvatar.textContent = clean.slice(0, 2).toUpperCase();
    updateGreeting();
    if (db && currentUserId) {
      db.collection('users').doc(currentUserId).set({ displayName: clean }, { merge: true }).catch(() => { });
    }
  }
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

// ---------- Fitness Sub-tabs & Configuration Drawer ----------

let currentFitnessSubtab = 'exercises';

function switchFitnessSubtab(subtab) {
  currentFitnessSubtab = subtab;
  const exercisesView = document.getElementById('subtabExercisesView');
  const stepsView = document.getElementById('subtabStepsView');
  const btnExercises = document.getElementById('btnSubtabExercises');
  const btnSteps = document.getElementById('btnSubtabSteps');

  if (subtab === 'exercises') {
    if (exercisesView) exercisesView.classList.remove('hidden');
    if (stepsView) stepsView.classList.add('hidden');
    if (btnExercises) {
      btnExercises.classList.add('subtab-btn-active');
      btnExercises.classList.remove('text-white/60');
    }
    if (btnSteps) {
      btnSteps.classList.remove('subtab-btn-active');
      btnSteps.classList.add('text-white/60');
    }
    startCameraStreamIfNeeded();
  } else {
    if (exercisesView) exercisesView.classList.add('hidden');
    if (stepsView) stepsView.classList.remove('hidden');
    if (btnExercises) {
      btnExercises.classList.remove('subtab-btn-active');
      btnExercises.classList.add('text-white/60');
    }
    if (btnSteps) {
      btnSteps.classList.add('subtab-btn-active');
      btnSteps.classList.remove('text-white/60');
    }
    renderStepHistory();
  }
}

function toggleConfigDrawer() {
  const drawer = document.getElementById('configDrawer');
  const txt = document.getElementById('configToggleText');
  const arrow = document.getElementById('configToggleArrow');
  if (!drawer) return;
  drawer.classList.toggle('hidden');
  const isHidden = drawer.classList.contains('hidden');
  if (txt) txt.textContent = isHidden ? 'Show Settings' : 'Hide Settings';
  if (arrow) arrow.textContent = isHidden ? '▾' : '▴';
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
  const forceStopBtn = document.getElementById('forceStopBtn');
  const mobileStopBtn = document.getElementById('mobileForceStopBtn');

  // Both Start Detecting Button and Emergency Stop button only appear in Fitness section ('steps')
  if (btnStart) {
    if (tab === 'steps') {
      btnStart.classList.remove('hidden');
    } else {
      btnStart.classList.add('hidden');
    }
  }

  // Emergency stop buttons (desktop header & mobile floating) only appear in Fitness section ('steps')
  if (tab === 'steps') {
    if (forceStopBtn) forceStopBtn.classList.remove('hidden');
    if (mobileStopBtn) mobileStopBtn.classList.remove('hidden');
  } else {
    if (forceStopBtn) forceStopBtn.classList.add('hidden');
    if (mobileStopBtn) mobileStopBtn.classList.add('hidden');
  }

  // Sync all tab buttons (both desktop and mobile bottom nav)
  document.querySelectorAll('[data-tab-target]').forEach(btn => {
    if (btn.getAttribute('data-tab-target') === tab) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });

  if (tab === 'live') {
    live.classList.remove('hidden');
    steps.classList.add('hidden');
    analytics.classList.add('hidden');
    if (btnLive) btnLive.classList.add('active');
    if (btnSteps) btnSteps.classList.remove('active');
    if (btnAnalytics) btnAnalytics.classList.remove('active');
    updateGreeting();
    // tear down live analytics listeners to save quota
    disconnectAnalyticsListeners();
  } else if (tab === 'steps') {
    live.classList.add('hidden');
    steps.classList.remove('hidden');
    analytics.classList.add('hidden');
    if (btnLive) btnLive.classList.remove('active');
    if (btnSteps) btnSteps.classList.add('active');
    if (btnAnalytics) btnAnalytics.classList.remove('active');
    disconnectAnalyticsListeners();
    switchFitnessSubtab(currentFitnessSubtab || 'exercises');
  } else {
    live.classList.add('hidden');
    steps.classList.add('hidden');
    analytics.classList.remove('hidden');
    if (btnLive) btnLive.classList.remove('active');
    if (btnSteps) btnSteps.classList.remove('active');
    if (btnAnalytics) btnAnalytics.classList.add('active');
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
    updateModeButtons(currentExercise);
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
    robotState = state;

    if (payload.exercise) {
      currentExercise = payload.exercise;
      document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);
      updateModeButtons(currentExercise);
    } else if (state === 'READY') {
      currentExercise = null;
      document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);
      updateModeButtons(null);
    }
    return;
  }

  if (type === 'rep') {
    currentExercise = payload.exercise || currentExercise;
    document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);

    if (payload.set !== undefined) {
      document.getElementById('currentSet').textContent = payload.set;
    }

    if (payload.exercise === 'bicep_curls' && (payload.left !== undefined || payload.right !== undefined)) {
      document.getElementById('currentReps').textContent = Math.min(payload.left || 0, payload.right || 0);
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

  if (type === 'battery_voltage') {
    const v = parseFloat(payload.voltage);
    if (!isNaN(v)) {
      const textEl = document.getElementById('batteryVoltageText');
      if (textEl) textEl.textContent = v.toFixed(2) + ' V';
      const fill = document.getElementById('batteryLevelFill');
      const indicator = document.getElementById('batteryIndicator');
      let percent = ((v - 10.0) / (12.6 - 10.0)) * 100;
      percent = Math.max(0, Math.min(100, percent));
      if (fill) {
        fill.style.height = percent + '%';
        fill.style.width = '100%';

        if (percent < 20) {
          fill.style.background = '#ef4444'; // red
        } else if (percent < 40) {
          fill.style.background = '#f59e0b'; // amber
        } else {
          fill.style.background = '#10b981'; // emerald
        }
      }
      if (indicator) {
        indicator.title = `Robot Battery: ${v.toFixed(2)} V (${Math.round(percent)}%)`;
      }
    }
    return;
  }

  if (type === 'watch_steps') {
    updateStepDisplay({
      steps: Number(payload.steps || 0),
      cal: payload.calories,
      km: payload.distance_km,
      min: payload.active_min
    }, false);
    const lastSyncEl = document.getElementById('watchLastSync');
    if (lastSyncEl) lastSyncEl.textContent = payload.last_synced_at ? formatTime(payload.last_synced_at) : '';
    setWatchConnectionStatus(true);
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
    } catch (e) { }
  }

  console.log(`🔌 Attempting to connect to local robot control at ws://${piIp}:8001`);
  localControlSocket = new WebSocket(`ws://${piIp}:8001`);

  localControlSocket.onopen = () => {
    console.log("✅ Local WebSocket control channel connected");
    appendLog("Connected to local robot control channel");
    setRobotConnectionStatus(true);
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
      setRobotConnectionStatus(true);
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
    setRobotConnectionStatus(false);
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
  if (robotState === 'ACTIVE' || robotState === 'LOADING') {
    appendLog("⚠️ Exercise is currently active. Selection disabled.", true);
    return;
  }

  currentExercise = mode;
  document.getElementById('currentWorkout').textContent = formatExercise(currentExercise);
  const heroRoutine = document.getElementById('homeHeroRoutine');
  if (heroRoutine) heroRoutine.textContent = formatExercise(currentExercise);
  updateModeButtons(currentExercise);

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

function updateModeButtons(exerciseName) {
  const norm = (exerciseName || '').toLowerCase().replace(/[^a-z]/g, '');
  document.querySelectorAll('.mode-btn').forEach(btn => {
    const attr = btn.getAttribute('onclick') || '';
    const cleanAttr = attr.toLowerCase().replace(/[^a-z]/g, '');
    if (norm && cleanAttr.includes(norm)) {
      btn.classList.add('active-mode');
    } else {
      btn.classList.remove('active-mode');
    }
  });
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
  if (cameraUrlInput && cameraUrlInput.value.trim() !== currentCameraUrl) {
    cameraUrlInput.value = currentCameraUrl;
  }
  setCameraStream();
}

function toggleCameraSettings() {
  const drawer = document.getElementById('cameraSettingsDrawer') || document.getElementById('configDrawer');
  if (drawer) {
    drawer.classList.toggle('open');
    drawer.classList.toggle('hidden');
  }
}

function setCameraPreset(url) {
  const input = document.getElementById('cameraUrl');
  if (input) {
    input.value = url;
    setCameraStream();
  }
}

function toggleCameraFullscreen() {
  const container = document.getElementById('cameraContainer');
  if (!container) return;
  if (!document.fullscreenElement && !document.webkitFullscreenElement) {
    if (container.requestFullscreen) {
      container.requestFullscreen();
    } else if (container.webkitRequestFullscreen) {
      container.webkitRequestFullscreen();
    }
  } else {
    if (document.exitFullscreen) {
      document.exitFullscreen();
    } else if (document.webkitExitFullscreen) {
      document.webkitExitFullscreen();
    }
  }
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

// ---------- Profile & Performance Analytics ----------

let selectedDate = new Date();
let cachedAggDocs = [];
let cachedSessionDocs = [];
let watchStepsToday = 0;
let cachedDailyMap = new Map();
let currentViewMode = 'today';

function formatOrdinalDay(day) {
  if (day > 3 && day < 21) return day + 'th';
  switch (day % 10) {
    case 1: return day + 'st';
    case 2: return day + 'nd';
    case 3: return day + 'rd';
    default: return day + 'th';
  }
}

function formatCustomDate(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  const now = new Date();
  now.setHours(0, 0, 0, 0);

  const dKey = dateKey(d);
  const nowKey = dateKey(now);

  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const yKey = dateKey(yesterday);

  if (dKey === nowKey) return 'Today';
  if (dKey === yKey) return 'Yesterday';

  const dayStr = formatOrdinalDay(d.getDate());
  const monthStr = d.toLocaleDateString('en-US', { month: 'long' });
  return `${dayStr} ${monthStr}`;
}

function changeSelectedDate(delta) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const nextDate = new Date(selectedDate);
  nextDate.setDate(nextDate.getDate() + delta);
  nextDate.setHours(0, 0, 0, 0);

  // Clamp forward: do not allow navigating past today (no circular wrap)
  if (nextDate > today) return;

  // Clamp backward: do not allow navigating beyond 35 days ago (no circular wrap)
  const minDate = new Date(today);
  minDate.setDate(today.getDate() - 35);
  if (nextDate < minDate) return;

  selectedDate = nextDate;
  currentViewMode = 'today';
  updateViewModeButtons();
  updateProfileViewForSelectedDate();
}

function setSelectedDate(dateStr) {
  if (!dateStr) return;
  const parts = dateStr.split('-');
  if (parts.length === 3) {
    selectedDate = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  } else {
    selectedDate = new Date(dateStr);
  }
  currentViewMode = 'today';
  updateViewModeButtons();
  updateProfileViewForSelectedDate();
}

function selectToday() {
  selectedDate = new Date();
  currentViewMode = 'today';
  updateViewModeButtons();
  updateProfileViewForSelectedDate();
}

function openCalendarPicker() {
  const picker = document.getElementById('hiddenDatePicker');
  if (picker) {
    picker.value = dateKey(selectedDate);
    picker.max = dateKey(new Date());
    if (picker.showPicker) {
      picker.showPicker();
    } else {
      picker.click();
    }
  }
}

function onCalendarDatePicked(val) {
  if (!val) return;
  setSelectedDate(val);
}

function setViewMode(mode) {
  currentViewMode = mode;
  updateViewModeButtons();
  updateProfileViewForSelectedDate();
}

function updateViewModeButtons() {
  const todayBtn = document.getElementById('viewModeTodayBtn');
  const weekBtn = document.getElementById('viewModeWeekBtn');

  if (currentViewMode === 'week') {
    if (todayBtn) {
      todayBtn.className = 'py-2.5 rounded-2xl border border-white/10 bg-white/5 hover:bg-white/10 text-white/70 font-bold text-xs sm:text-sm active:scale-[0.98] transition-all text-center';
    }
    if (weekBtn) {
      weekBtn.className = 'py-2.5 rounded-2xl border border-cyan-400/50 bg-cyan-500/15 text-cyan-400 font-bold text-xs sm:text-sm active:scale-[0.98] transition-all text-center';
    }
  } else {
    if (todayBtn) {
      todayBtn.className = 'py-2.5 rounded-2xl border border-cyan-400/50 bg-cyan-500/15 text-cyan-400 font-bold text-xs sm:text-sm active:scale-[0.98] transition-all text-center';
    }
    if (weekBtn) {
      weekBtn.className = 'py-2.5 rounded-2xl border border-white/10 bg-white/5 hover:bg-white/10 text-white/70 font-bold text-xs sm:text-sm active:scale-[0.98] transition-all text-center';
    }
  }
}

function getExerciseIcon(exercise) {
  const map = {
    bicep_curls: '💪',
    pushups: '🧘',
    squats: '🏋️',
    plank: '⏱️',
    person_follow: '🚶'
  };
  return map[exercise] || '⚡';
}

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
  fromDate.setDate(today.getDate() - 30); // 30 days history for calendar browsing

  function rebuildAndRender() {
    cachedDailyMap.clear();

    // Pre-populate 35 days in map
    for (let i = 0; i < 35; i++) {
      const day = new Date(fromDate);
      day.setDate(fromDate.getDate() + i);
      const k = dateKey(day);
      cachedDailyMap.set(k, emptyDaily(k));
    }

    cachedAggDocs.forEach(data => {
      const k = data.date || data._docId;
      if (k) {
        cachedDailyMap.set(k, { ...emptyDaily(k), ...data });
      }
    });

    // Merge today's watch steps into the daily aggregate for KPI accuracy
    const todayStr = dateKey(new Date());
    if (watchStepsToday > 0) {
      const todayAgg = cachedDailyMap.get(todayStr);
      if (todayAgg) todayAgg.steps = Math.max(todayAgg.steps || 0, watchStepsToday);
    }

    updateProfileViewForSelectedDate();
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

  // One-shot today's watch_data for step KPI
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
    .catch(() => { });
}

function updateProfileViewForSelectedDate() {
  const selectedKey = dateKey(selectedDate);
  const todayDate = new Date();
  todayDate.setHours(0, 0, 0, 0);
  const todayKey = dateKey(todayDate);
  const isToday = selectedKey === todayKey;

  // Clamp & arrow state: Disable forward arrow if on today
  const nextBtn = document.getElementById('nextDateBtn');
  if (nextBtn) {
    if (selectedKey >= todayKey) {
      nextBtn.classList.add('opacity-30', 'pointer-events-none');
    } else {
      nextBtn.classList.remove('opacity-30', 'pointer-events-none');
    }
  }

  // 1. Boxed Date: "Today", "Yesterday", or "day-th Month"
  const fullTextEl = document.getElementById('selectedDateFullText');
  if (fullTextEl) {
    if (currentViewMode === 'week') {
      fullTextEl.textContent = 'This Week';
    } else {
      fullTextEl.textContent = formatCustomDate(selectedDate);
    }
  }

  // 2. Render 7 boxes indicating day - Just use day name for that
  const stripEl = document.getElementById('weekDaysStrip');
  const curr = new Date(selectedDate);
  const dayOfWeek = (curr.getDay() + 6) % 7; // Monday = 0
  const monday = new Date(curr);
  monday.setDate(curr.getDate() - dayOfWeek);

  const weekDays = [];
  let stripHtml = '';
  for (let i = 0; i < 7; i++) {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    weekDays.push(d);

    const k = dateKey(d);
    const isSel = (currentViewMode === 'today' && k === selectedKey);
    const isTod = (k === todayKey);
    const dayName = d.toLocaleDateString('en-US', { weekday: 'short' });

    stripHtml += `
      <button onclick="setSelectedDate('${k}')" class="week-day-btn ${isSel ? 'active' : ''} ${isTod ? 'is-today' : ''}" title="${formatCustomDate(d)}">
        ${dayName}
      </button>
    `;
  }
  if (stripEl) stripEl.innerHTML = stripHtml;

  // 3. Update the 4 exercise blocks based on selected date or week mode
  let curls = 0, pushups = 0, squats = 0, totalReps = 0;
  let plankSec = 0, stepsTotal = 0, good = 0, flaw = 0;

  if (currentViewMode === 'week') {
    // Sum stats over the entire 7 days of the week
    weekDays.forEach(d => {
      const dData = cachedDailyMap.get(dateKey(d));
      if (dData) {
        curls += Number(dData.reps?.bicep_curls || 0);
        pushups += Number(dData.reps?.pushups || 0);
        squats += Number(dData.reps?.squats || 0);
        plankSec += Number(dData.plank_time_s || 0);
        stepsTotal += Number(dData.steps || 0);
        good += Number(dData.form_good_count || dData.form_good || 0);
        flaw += Number(dData.form_flaw_count || dData.form_flawed || 0);
      }
    });
    totalReps = curls + pushups + squats;
  } else {
    // Specific selected day
    const dayData = cachedDailyMap.get(selectedKey) || emptyDaily(selectedKey);
    curls = Number(dayData.reps?.bicep_curls || 0);
    pushups = Number(dayData.reps?.pushups || 0);
    squats = Number(dayData.reps?.squats || 0);
    totalReps = curls + pushups + squats;
    plankSec = Number(dayData.plank_time_s || 0);
    stepsTotal = Number(dayData.steps || 0);
    good = Number(dayData.form_good_count || dayData.form_good || 0);
    flaw = Number(dayData.form_flaw_count || dayData.form_flawed || 0);
  }

  const kpiTotalReps = document.getElementById('kpiTotalReps');
  if (kpiTotalReps) kpiTotalReps.textContent = totalReps;

  const kpiRepsBreakdown = document.getElementById('kpiRepsBreakdown');
  if (kpiRepsBreakdown) {
    kpiRepsBreakdown.textContent = `Curls: ${curls} • Push: ${pushups} • Squats: ${squats}`;
  }

  const kpiPlank = document.getElementById('kpiPlankTime');
  if (kpiPlank) kpiPlank.textContent = formatSeconds(plankSec);

  const kpiSteps = document.getElementById('kpiSteps');
  if (kpiSteps) kpiSteps.textContent = stepsTotal.toLocaleString();

  const totalForm = good + flaw;
  const pct = totalForm > 0 ? Math.round((good / totalForm) * 100) : 100;

  const kpiForm = document.getElementById('kpiForm');
  if (kpiForm) kpiForm.textContent = `${pct}%`;

  const kpiFormSub = document.getElementById('kpiFormSub');
  if (kpiFormSub) {
    kpiFormSub.textContent = totalForm > 0 ? `${good} Good • ${flaw} Flawed` : 'No Reps Logged';
  }

  const badgeText = currentViewMode === 'week' ? 'Week Total' : formatCustomDate(selectedDate);
  const chartRepsBadge = document.getElementById('chartRepsDateBadge');
  if (chartRepsBadge) chartRepsBadge.textContent = badgeText;

  const chartFormBadge = document.getElementById('chartFormDateBadge');
  if (chartFormBadge) chartFormBadge.textContent = badgeText;

  // 4. Update charts
  if (currentViewMode === 'week') {
    renderRepsChartWeek(weekDays);
    renderTimeChartWeek(weekDays);
  } else {
    const dayData = cachedDailyMap.get(selectedKey) || emptyDaily(selectedKey);
    renderRepsChart(dayData);
    renderTimeChart(dayData);
  }
  renderFormChart({ good, flaw });
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

function renderRepsChart(selectedDayData) {
  const ctx = document.getElementById('repsChart');
  if (!ctx) return;
  if (repsChart) repsChart.destroy();

  const reps = selectedDayData.reps || {};
  const curls = Number(reps.bicep_curls || 0);
  const push = Number(reps.pushups || 0);
  const squats = Number(reps.squats || 0);

  repsChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Curls', 'Push-ups', 'Squats'],
      datasets: [
        {
          label: 'Reps',
          data: [curls, push, squats],
          backgroundColor: ['#06b6d4', '#10b981', '#f59e0b'],
          borderRadius: 8
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.08)' } },
        x: { grid: { color: 'rgba(255,255,255,0.04)' } }
      },
      plugins: {
        legend: { display: false }
      }
    }
  });
}

function renderRepsChartWeek(weekDays) {
  const ctx = document.getElementById('repsChart');
  if (!ctx) return;
  if (repsChart) repsChart.destroy();

  const labels = weekDays.map(d => d.toLocaleDateString('en-US', { weekday: 'short' }));
  const curls = weekDays.map(d => Number(cachedDailyMap.get(dateKey(d))?.reps?.bicep_curls || 0));
  const push = weekDays.map(d => Number(cachedDailyMap.get(dateKey(d))?.reps?.pushups || 0));
  const squats = weekDays.map(d => Number(cachedDailyMap.get(dateKey(d))?.reps?.squats || 0));

  repsChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Curls', data: curls, backgroundColor: '#06b6d4', borderRadius: 6 },
        { label: 'Push-ups', data: push, backgroundColor: '#10b981', borderRadius: 6 },
        { label: 'Squats', data: squats, backgroundColor: '#f59e0b', borderRadius: 6 }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.08)' } },
        x: { grid: { color: 'rgba(255,255,255,0.04)' } }
      },
      plugins: {
        legend: { labels: { color: '#94a3b8' } }
      }
    }
  });
}

function renderTimeChart(selectedDayData) {
  const ctx = document.getElementById('timeChart');
  if (!ctx) return;
  if (timeChart) timeChart.destroy();

  const activeMins = Math.round((Number(selectedDayData.active_time_s || 0)) / 60);
  const totalMins = Math.round((Number(selectedDayData.total_time_s || 0)) / 60);
  const restMins = Math.max(0, totalMins - activeMins);

  timeChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Active Workout', 'Rest / Transition'],
      datasets: [
        {
          label: 'Minutes',
          data: [activeMins, restMins],
          backgroundColor: ['#10b981', '#64748b'],
          borderRadius: 8
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.08)' } },
        x: { grid: { color: 'rgba(255,255,255,0.04)' } }
      },
      plugins: {
        legend: { display: false }
      }
    }
  });
}

function renderTimeChartWeek(weekDays) {
  const ctx = document.getElementById('timeChart');
  if (!ctx) return;
  if (timeChart) timeChart.destroy();

  const labels = weekDays.map(d => d.toLocaleDateString('en-US', { weekday: 'short' }));
  const active = weekDays.map(d => Math.round(Number(cachedDailyMap.get(dateKey(d))?.active_time_s || 0) / 60));
  const total = weekDays.map(d => Math.round(Number(cachedDailyMap.get(dateKey(d))?.total_time_s || 0) / 60));

  timeChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Active (min)', data: active, borderColor: '#10b981', tension: 0.2 },
        { label: 'Total (min)', data: total, borderColor: '#64748b', tension: 0.2 }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.08)' } }
      },
      plugins: {
        legend: { labels: { color: '#94a3b8' } }
      }
    }
  });
}

function renderFormChart(form) {
  const ctx = document.getElementById('formChart');
  if (!ctx) return;
  if (formChart) formChart.destroy();

  const good = Number(form.good || 0);
  const flaw = Number(form.flaw || form.flawed || 0);

  formChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Good Form', 'Flawed Form'],
      datasets: [{
        data: (good === 0 && flaw === 0) ? [1, 0] : [good, flaw],
        backgroundColor: (good === 0 && flaw === 0) ? ['rgba(255,255,255,0.1)', 'transparent'] : ['#10b981', '#ef4444'],
        borderWidth: 0
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#94a3b8' } }
      }
    }
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

    await stepCommandChar.startNotifications();
    stepCommandChar.addEventListener('characteristicvaluechanged', onStepCommandReceived);
    stepLog('Subscribed to command notifications (physical button ready)');

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

function onStepCommandReceived(event) {
  const cmd = new TextDecoder().decode(event.target.value).trim();
  stepLog(`BLE command received from wristband: "${cmd}"`);
  if (cmd !== 'switch') return;

  // FOLLOWING/SEARCHING/STOPPED = person-follow movement mode
  const movementStates = ['LOADING'];
  // LOADING/ACTIVE = exercise detection in progress
  const exerciseStates = ['ACTIVE'];

  if (movementStates.includes(robotState)) {
    stepLog('Physical button — movement mode → Start Detecting');
    sendStartExercise();
  } else if (exerciseStates.includes(robotState)) {
    stepLog('Physical button — exercise mode → Force Stop');
    sendForceStop();
  } else {
    stepLog(`Physical button — state "${robotState}" — ignored`);
  }
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
  setWatchConnectionStatus(status === 'connected');
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