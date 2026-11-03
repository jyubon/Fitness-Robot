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
let currentExercise = 'bicep_curls';
let latestEventTimestamp = null;
let robotState = 'READY';

// Bicep curls dual-arm tracking state (8 reps per arm = 1 set)
let bicepCurrentSet = 1;
let bicepLeftInSet = 0;
let bicepRightInSet = 0;
let bicepSetStartLeft = 0;
let bicepSetStartRight = 0;
let bicepHasBaseline = false;
let lastFinishedBicepSet = 0;

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
  const forceStop = document.getElementById('forceStopBtn');
  if (forceStop) forceStop.classList.add('hidden');
  const batt = document.getElementById('batteryIndicator');
  if (batt) batt.classList.add('hidden');
  const cfgBtn = document.getElementById('configMenuBtn');
  if (cfgBtn) cfgBtn.classList.add('hidden');
  const cfgDrawer = document.getElementById('configDrawer');
  if (cfgDrawer) cfgDrawer.classList.add('hidden');

  const desktopNav = document.getElementById('desktopNavMenu');
  if (desktopNav) desktopNav.classList.add('hidden');

  const mobileNav = document.getElementById('mobileNavBar');
  if (mobileNav) mobileNav.classList.add('hidden');
}

async function deleteAccount() {
  if (!auth || !auth.currentUser) {
    alert('No active user logged in.');
    return;
  }
  const confirmation = confirm('Are you sure you want to delete your account? All your workout history, profile details, and smartwatch logs will be permanently erased. This cannot be undone.');
  if (!confirmation) return;

  const doubleCheck = prompt('To confirm permanent deletion, type DELETE:');
  if (doubleCheck !== 'DELETE') {
    alert('Account deletion cancelled: confirmation input did not match "DELETE".');
    return;
  }

  const user = auth.currentUser;
  const uid = user.uid;

  try {
    if (db) {
      const subcollections = ['daily_aggregates', 'sessions', 'watch_data', 'control_commands'];
      for (const coll of subcollections) {
        try {
          const snap = await db.collection('users').doc(uid).collection(coll).get();
          if (!snap.empty) {
            const batch = db.batch();
            snap.forEach(doc => batch.delete(doc.ref));
            await batch.commit();
          }
        } catch (subErr) {
          console.warn(`Could not delete collection ${coll}:`, subErr);
        }
      }

      try {
        await db.collection('users').doc(uid).delete();
      } catch (docErr) {
        console.warn('Could not delete user document:', docErr);
      }
    }

    localStorage.removeItem('fitness_robot_profile_name');
    localStorage.removeItem('fitness_robot_profile_avatar');

    await user.delete();
    alert('Your account has been deleted permanently.');
    await logout();
  } catch (err) {
    console.error('Delete account failed:', err);
    if (err.code === 'auth/requires-recent-login') {
      alert('Security requirement: Please sign out, log back in, and then delete your account.');
    } else {
      alert('Error deleting account: ' + (err.message || err));
    }
  }
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

  const configBtn = document.getElementById('configMenuBtn');
  if (configBtn) configBtn.classList.remove('hidden');

  const desktopNav = document.getElementById('desktopNavMenu');
  if (desktopNav) desktopNav.classList.remove('hidden');

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
  const fDot = document.getElementById('fitnessWatchStatusDot');
  const fText = document.getElementById('fitnessWatchStatusText');
  const drawerBtn = document.getElementById('stepConnectBtn');

  [{ dot, text }, { dot: fDot, text: fText }].forEach(item => {
    if (!item.text) return;
    if (isOnline) {
      item.text.textContent = 'ONLINE';
      item.text.className = item.text.id === 'fitnessWatchStatusText'
        ? 'text-xs sm:text-sm font-extrabold tracking-wide text-emerald-400'
        : 'text-sm sm:text-base font-extrabold tracking-wide text-emerald-400';
      if (item.dot) item.dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse';
    } else {
      item.text.textContent = 'OFFLINE';
      item.text.className = item.text.id === 'fitnessWatchStatusText'
        ? 'text-xs sm:text-sm font-extrabold tracking-wide text-red-400'
        : 'text-sm sm:text-base font-extrabold tracking-wide text-red-400';
      if (item.dot) item.dot.className = 'w-2.5 h-2.5 rounded-full bg-red-400';
    }
  });

  if (drawerBtn) {
    drawerBtn.textContent = isOnline ? 'Disconnect' : 'Connect to Tracker';
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
let isCameraActive = false;

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
    if (isCameraActive) {
      startCameraStreamIfNeeded();
    }
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
  if (!drawer) return;
  drawer.classList.toggle('hidden');
}

function toggleCameraView(forceState) {
  const card = document.getElementById('cameraViewCard');
  const btn = document.getElementById('cameraToggleBtn');
  if (!card) return;

  if (typeof forceState === 'boolean') {
    isCameraActive = forceState;
  } else {
    isCameraActive = card.classList.contains('hidden');
  }

  if (isCameraActive) {
    card.classList.remove('hidden');
    if (btn) btn.classList.add('camera-btn-active');
    startCameraStreamIfNeeded();
  } else {
    card.classList.add('hidden');
    if (btn) btn.classList.remove('camera-btn-active');
  }
}

function onExerciseSelectChange(value) {
  switchMode(value);
}

function selectExerciseFromHome(mode, btn) {
  if (btn) {
    btn.classList.add('selecting-highlight');
    setTimeout(() => {
      btn.classList.remove('selecting-highlight');
    }, 250);
  }
  showTab('steps');
  switchFitnessSubtab('exercises');
  switchMode(mode);
}

function updateBicepArmUI(left, right, setNum) {
  const leftEl = document.getElementById('leftArmReps');
  const rightEl = document.getElementById('rightArmReps');
  const leftBar = document.getElementById('leftArmProgress');
  const rightBar = document.getElementById('rightArmProgress');
  const setEl = document.getElementById('currentSet');

  const l = Math.max(0, Number(left) || 0);
  const r = Math.max(0, Number(right) || 0);

  if (leftEl) leftEl.textContent = l;
  if (rightEl) rightEl.textContent = r;
  if (leftBar) leftBar.style.width = `${Math.min(100, (l / 8) * 100)}%`;
  if (rightBar) rightBar.style.width = `${Math.min(100, (r / 8) * 100)}%`;
  if (setEl && setNum !== undefined) setEl.textContent = setNum;
}

function handleBicepSetComplete(newSet, rawLeft, rawRight) {
  const finishedSet = bicepCurrentSet;
  if (lastFinishedBicepSet === finishedSet && newSet === undefined) {
    return; // Already processed this set completion
  }
  lastFinishedBicepSet = finishedSet;

  if (newSet !== undefined && Number(newSet) > 0) {
    bicepCurrentSet = Math.max(bicepCurrentSet, Number(newSet));
    if (bicepCurrentSet === finishedSet) {
      bicepCurrentSet = finishedSet + 1;
    }
  } else {
    bicepCurrentSet = finishedSet + 1;
  }

  // Left count and right count reset again to 0 upon completing a set (8 reps each)
  bicepLeftInSet = 0;
  bicepRightInSet = 0;

  if (rawLeft !== undefined && rawRight !== undefined) {
    bicepSetStartLeft = rawLeft;
    bicepSetStartRight = rawRight;
  }

  updateBicepArmUI(0, 0, bicepCurrentSet);

  const repsEl = document.getElementById('currentReps');
  if (repsEl) repsEl.textContent = '0';

  const detailEl = document.getElementById('repsDetail');
  if (detailEl) detailEl.textContent = 'Set Complete - Rest';

  appendExerciseLog(`🎉 Bicep Curls: Set ${finishedSet} Complete! Both arms reached 8 reps.`);
}

function syncExerciseUI(exercise) {
  if (!exercise) return;
  const currentWorkoutEl = document.getElementById('currentWorkout');
  if (currentWorkoutEl) currentWorkoutEl.textContent = formatExercise(exercise);

  const heroRoutine = document.getElementById('homeHeroRoutine');
  if (heroRoutine) heroRoutine.textContent = formatExercise(exercise);

  const select = document.getElementById('fitnessExerciseSelect');
  if (select && select.value !== exercise) {
    select.value = exercise;
  }

  // Show plank time when plank is selected; in other times keep only reps and sets
  const plankBox = document.getElementById('plankTimeBox');
  if (plankBox) {
    if (exercise === 'plank') {
      plankBox.classList.remove('hidden');
    } else {
      plankBox.classList.add('hidden');
    }
  }

  // Show dual arm tracking box when bicep curls is selected; in other times keep hidden
  const bicepBox = document.getElementById('bicepArmsBox');
  if (bicepBox) {
    if (exercise === 'bicep_curls') {
      bicepBox.classList.remove('hidden');
      updateBicepArmUI(bicepLeftInSet, bicepRightInSet, bicepCurrentSet);
      const setEl = document.getElementById('currentSet');
      if (setEl && (setEl.textContent === '—' || !setEl.textContent)) {
        setEl.textContent = bicepCurrentSet;
      }
      const repsEl = document.getElementById('currentReps');
      if (repsEl && (repsEl.textContent === '—' || !repsEl.textContent)) {
        repsEl.textContent = Math.min(bicepLeftInSet, bicepRightInSet);
      }
    } else {
      bicepBox.classList.add('hidden');
    }
  }

  updateModeButtons(exercise);
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
  const configMenuBtn = document.getElementById('configMenuBtn');
  const configDrawer = document.getElementById('configDrawer');

  // Both Start Detecting Button and Emergency Stop button only appear in Fitness section ('steps')
  if (btnStart) {
    if (tab === 'steps') {
      btnStart.classList.remove('hidden');
    } else {
      btnStart.classList.add('hidden');
    }
  }

  // Emergency stop button only appears in Fitness section ('steps')
  if (forceStopBtn) {
    if (tab === 'steps') {
      forceStopBtn.classList.remove('hidden');
    } else {
      forceStopBtn.classList.add('hidden');
    }
  }

  // Configuration button in menu is enabled for all tabs
  if (configMenuBtn) {
    configMenuBtn.classList.remove('hidden');
  }
  // Dismiss open config popup when navigating tabs
  if (configDrawer) {
    configDrawer.classList.add('hidden');
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
    syncExerciseUI(currentExercise || 'bicep_curls');
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
    }, (error) => appendSystemLog(`Telemetry listener failed: ${error.message}`, true));

  const today = dateKey(new Date());
  watchUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('watch_data')
    .doc(today)
    .onSnapshot((doc) => {
      if (doc.exists) handleTelemetryMessage({ type: 'watch_steps', ...doc.data() });
    });

  appendSystemLog('Connected to Firebase telemetry');
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
    currentExercise = msg.exercise_type || payload.exercise_type || currentExercise;
    if (currentExercise === 'bicep_curls') {
      bicepCurrentSet = 1;
      bicepLeftInSet = 0;
      bicepRightInSet = 0;
      bicepHasBaseline = false;
      lastFinishedBicepSet = 0;
      updateBicepArmUI(0, 0, 1);
    }
    syncExerciseUI(currentExercise);
    appendExerciseLog(`Session started: ${formatExercise(currentExercise)}`);
    return;
  }

  if (type === 'session_end') {
    const activeSec = Number(payload.active_duration_s || 0);
    appendExerciseLog(`Session ended. Active: ${activeSec.toFixed(1)}s`);
    recordLiveSessionEnd(activeSec);
    currentSessionId = null;
    return;
  }

  if (type === 'robot_state') {
    const pill = document.getElementById('robotState');
    const state = (payload.state || 'STOPPED').toUpperCase();
    if (pill) {
      pill.textContent = state;
      pill.setAttribute('data-state', state);
    }
    robotState = state;

    if (payload.exercise) {
      currentExercise = payload.exercise;
      syncExerciseUI(currentExercise);
    } else if (state === 'READY') {
      currentExercise = null;
      syncExerciseUI(currentExercise);
    }
    return;
  }

  if (type === 'rep') {
    currentExercise = payload.exercise || currentExercise;
    syncExerciseUI(currentExercise);

    if (payload.exercise === 'bicep_curls' || currentExercise === 'bicep_curls') {
      const rawLeft = Number(payload.left ?? payload.count ?? 0);
      const rawRight = Number(payload.right ?? payload.count ?? 0);

      // Check if backend reports an updated set number
      if (payload.set !== undefined && Number(payload.set) > 0) {
        if (Number(payload.set) > bicepCurrentSet) {
          bicepCurrentSet = Number(payload.set);
          bicepSetStartLeft = rawLeft;
          bicepSetStartRight = rawRight;
          bicepLeftInSet = 0;
          bicepRightInSet = 0;
        }
      }

      if (!bicepHasBaseline) {
        if (rawLeft > 8 || rawRight > 8) {
          bicepSetStartLeft = rawLeft - (rawLeft % 8);
          bicepSetStartRight = rawRight - (rawRight % 8);
        } else {
          bicepSetStartLeft = 0;
          bicepSetStartRight = 0;
        }
        bicepHasBaseline = true;
      }

      // Current set reps relative to set start (or explicit in-set counts)
      let lReps = payload.left_in_set !== undefined ? Number(payload.left_in_set) : (rawLeft - bicepSetStartLeft);
      let rReps = payload.right_in_set !== undefined ? Number(payload.right_in_set) : (rawRight - bicepSetStartRight);
      if (lReps < 0) { bicepSetStartLeft = rawLeft; lReps = 0; }
      if (rReps < 0) { bicepSetStartRight = rawRight; rReps = 0; }

      bicepLeftInSet = lReps;
      bicepRightInSet = rReps;

      // Completed reps in set (both arms synchronized towards 8 reps goal)
      const completedSetReps = Math.min(bicepLeftInSet, bicepRightInSet);
      const repsEl = document.getElementById('currentReps');
      const detailEl = document.getElementById('repsDetail');
      if (repsEl) repsEl.textContent = completedSetReps;
      if (detailEl) detailEl.textContent = `L:${bicepLeftInSet}/8  R:${bicepRightInSet}/8`;

      updateBicepArmUI(bicepLeftInSet, bicepRightInSet, bicepCurrentSet);

      // A set should have 8 reps from both arms.
      // Left count and right count reset again to 0 upon completing a set.
      if (bicepLeftInSet >= 8 && bicepRightInSet >= 8) {
        handleBicepSetComplete(bicepCurrentSet + 1, rawLeft, rawRight);
      }

      recordLiveRep(currentExercise, completedSetReps, bicepLeftInSet, bicepRightInSet);
      return;
    } else {
      if (payload.set !== undefined) {
        const setEl = document.getElementById('currentSet');
        if (setEl) setEl.textContent = payload.set;
      }
      const repCount = payload.count ?? payload.reps ?? payload.total ?? 0;
      const repsEl = document.getElementById('currentReps');
      const detailEl = document.getElementById('repsDetail');
      if (repsEl) repsEl.textContent = repCount;
      if (detailEl) detailEl.textContent = '';
      recordLiveRep(currentExercise, repCount);
      return;
    }
  }

  if (type === 'plank_time' || type === 'plank') {
    const val = payload.value ?? payload.plank_timer ?? 0;
    const plankEl = document.getElementById('plankTime');
    if (plankEl) plankEl.textContent = formatSeconds(val);
    recordLivePlank(val);
    return;
  }

  if (type === 'form_error' || type === 'encouragement') {
    appendExerciseLog(payload.message || JSON.stringify(payload), true);
    if (type === 'form_error') {
      recordLiveFormError();
    }
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
    appendExerciseLog(`Set ${payload.set} complete (${payload.reps ?? '?'} reps)`);
    if (payload.exercise === 'bicep_curls' || currentExercise === 'bicep_curls') {
      const nextSet = payload.set !== undefined ? Number(payload.set) + 1 : (bicepCurrentSet + 1);
      handleBicepSetComplete(nextSet);
    }
    return;
  }

  if (['pause', 'resume', 'robot_command'].includes(type)) {
    appendExerciseLog(`${type}: ${JSON.stringify(payload)}`);
  }
}

// System & Configuration Log (Home Tab)
function appendSystemLog(text, isError = false) {
  const container = document.getElementById('systemLog');
  if (!container) return;
  const entry = document.createElement('div');
  entry.className = isError ? 'text-amber-300 font-semibold' : 'text-emerald-300/80';
  entry.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;

  while (container.children.length > 60) {
    container.removeChild(container.firstChild);
  }
}

function clearSystemLog() {
  const container = document.getElementById('systemLog');
  if (container) container.innerHTML = '';
}

// Robot & Exercise Log (Fitness Tab -> Exercises Subtab)
function appendExerciseLog(text, isFlaw = false) {
  const container = document.getElementById('exerciseLog');
  if (!container) return;
  const entry = document.createElement('div');
  entry.className = isFlaw ? 'text-amber-300 font-semibold' : 'text-emerald-300/80';
  entry.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;

  while (container.children.length > 60) {
    container.removeChild(container.firstChild);
  }
}

function clearExerciseLog() {
  const container = document.getElementById('exerciseLog');
  if (container) container.innerHTML = '';
}

// Compatibility Aliases
function appendLog(text, isForm = false) {
  appendExerciseLog(text, isForm);
}

function clearLog() {
  clearExerciseLog();
  clearSystemLog();
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
    appendSystemLog("Connected to local robot control channel");
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
    appendSystemLog("Disconnected from local robot control channel", true);
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
  toggleCameraView(true);
  if (currentExercise === 'bicep_curls') {
    bicepHasBaseline = false;
    bicepLeftInSet = 0;
    bicepRightInSet = 0;
    updateBicepArmUI(0, 0, bicepCurrentSet);
  }
  await sendControlCommand({ action: 'start_exercise' }, 'Start detecting exercise signal sent to robot');
}

async function switchMode(mode) {
  if (robotState === 'ACTIVE' || robotState === 'LOADING') {
    appendExerciseLog("⚠️ Exercise is currently active. Selection disabled.", true);
    const select = document.getElementById('fitnessExerciseSelect');
    if (select && currentExercise) select.value = currentExercise;
    return;
  }

  currentExercise = mode;
  if (mode === 'bicep_curls') {
    bicepHasBaseline = false;
    bicepLeftInSet = 0;
    bicepRightInSet = 0;
  }
  syncExerciseUI(currentExercise);

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
  // Do not keep home target routine buttons in a selected state (highlight only when selecting)
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.classList.remove('active-mode');
  });
}

async function sendControlCommand(command, successMessage) {
  let sentLocally = false;

  // 1. Send via local WebSockets if connected
  if (localControlSocket && localControlSocket.readyState === WebSocket.OPEN) {
    try {
      localControlSocket.send(JSON.stringify(command));
      sentLocally = true;
      appendExerciseLog(`${successMessage} (Direct Local Connection)`);
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
        appendExerciseLog(successMessage);
      }
    } catch (e) {
      if (!sentLocally) {
        appendExerciseLog(`Failed to sync command to cloud: ${e.message}`, true);
      }
    }
  } else if (!sentLocally) {
    appendExerciseLog("⚠️ Command failed: local robot not connected and not synced with cloud", true);
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

function getSevenDaysWindow() {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const days = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    d.setHours(0, 0, 0, 0);
    days.push(d);
  }
  return {
    today,
    minDate: days[0], // 6 days ago
    days
  };
}

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
  const monthStr = d.toLocaleDateString('en-US', { month: 'short' });
  return `${dayStr} ${monthStr}`;
}

function changeSelectedDate(delta) {
  const { minDate, today } = getSevenDaysWindow();
  const nextDate = new Date(selectedDate);
  nextDate.setDate(nextDate.getDate() + delta);
  nextDate.setHours(0, 0, 0, 0);

  // Clamp within the 7-day window (no circular wrap)
  if (nextDate > today || nextDate < minDate) return;

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
  selectedDate.setHours(0, 0, 0, 0);
  currentViewMode = 'today';
  updateViewModeButtons();
  updateProfileViewForSelectedDate();
}

function selectToday() {
  selectedDate = new Date();
  selectedDate.setHours(0, 0, 0, 0);
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
  if (mode === 'today') {
    selectedDate = new Date();
    selectedDate.setHours(0, 0, 0, 0);
  }
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
      weekBtn.className = 'py-2.5 rounded-2xl border border-cyan-400/50 bg-cyan-500/20 text-cyan-300 font-bold text-xs sm:text-sm active:scale-[0.98] transition-all text-center shadow-lg shadow-cyan-500/10';
    }
  } else {
    if (todayBtn) {
      todayBtn.className = 'py-2.5 rounded-2xl border border-cyan-400/50 bg-cyan-500/20 text-cyan-300 font-bold text-xs sm:text-sm active:scale-[0.98] transition-all text-center shadow-lg shadow-cyan-500/10';
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

// Live fitness measurement recording into statistics
function recordLiveRep(exercise, count, left, right) {
  if (!exercise) return;
  const todayStr = dateKey(new Date());
  let dayData = cachedDailyMap.get(todayStr);
  if (!dayData) {
    dayData = emptyDaily(todayStr);
    cachedDailyMap.set(todayStr, dayData);
  }
  if (!dayData.reps) dayData.reps = {};

  let repNum = Number(count || 1);
  if (exercise === 'bicep_curls' && (left !== undefined || right !== undefined)) {
    repNum = Math.min(Number(left || 0), Number(right || 0));
  }

  dayData.reps[exercise] = Math.max(Number(dayData.reps[exercise] || 0), repNum);
  dayData.form_good_count = (dayData.form_good_count || 0) + 1;

  if (db && currentUserId) {
    db.collection('users').doc(currentUserId)
      .collection('daily_aggregates')
      .doc(todayStr)
      .set({
        date: todayStr,
        reps: dayData.reps,
        form_good_count: dayData.form_good_count,
        form_flaw_count: dayData.form_flaw_count || 0,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true }).catch(() => {});
  }

  updateProfileViewForSelectedDate();
}

function recordLivePlank(seconds) {
  const todayStr = dateKey(new Date());
  let dayData = cachedDailyMap.get(todayStr);
  if (!dayData) {
    dayData = emptyDaily(todayStr);
    cachedDailyMap.set(todayStr, dayData);
  }
  dayData.plank_time_s = Math.max(Number(dayData.plank_time_s || 0), Math.round(Number(seconds || 0)));

  if (db && currentUserId) {
    db.collection('users').doc(currentUserId)
      .collection('daily_aggregates')
      .doc(todayStr)
      .set({
        date: todayStr,
        plank_time_s: dayData.plank_time_s,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true }).catch(() => {});
  }

  updateProfileViewForSelectedDate();
}

function recordLiveFormError() {
  const todayStr = dateKey(new Date());
  let dayData = cachedDailyMap.get(todayStr);
  if (!dayData) {
    dayData = emptyDaily(todayStr);
    cachedDailyMap.set(todayStr, dayData);
  }
  dayData.form_flaw_count = (dayData.form_flaw_count || 0) + 1;

  if (db && currentUserId) {
    db.collection('users').doc(currentUserId)
      .collection('daily_aggregates')
      .doc(todayStr)
      .set({
        date: todayStr,
        form_flaw_count: dayData.form_flaw_count,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true }).catch(() => {});
  }

  updateProfileViewForSelectedDate();
}

function recordLiveSessionEnd(activeSec) {
  const todayStr = dateKey(new Date());
  let dayData = cachedDailyMap.get(todayStr);
  if (!dayData) {
    dayData = emptyDaily(todayStr);
    cachedDailyMap.set(todayStr, dayData);
  }
  const sec = Math.round(Number(activeSec || 0));
  dayData.active_time_s = (dayData.active_time_s || 0) + sec;
  dayData.total_time_s = (dayData.total_time_s || 0) + sec;

  if (db && currentUserId) {
    db.collection('users').doc(currentUserId)
      .collection('daily_aggregates')
      .doc(todayStr)
      .set({
        date: todayStr,
        active_time_s: dayData.active_time_s,
        total_time_s: dayData.total_time_s,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true }).catch(() => {});
  }

  updateProfileViewForSelectedDate();
}

/**
 * connectAnalyticsListeners — sets up live onSnapshot listeners for
 * daily_aggregates and sessions.
 */
function connectAnalyticsListeners() {
  if (!db || !currentUserId) return;
  disconnectAnalyticsListeners();

  const { minDate, today } = getSevenDaysWindow();

  function rebuildAndRender() {
    cachedDailyMap.clear();

    const { days } = getSevenDaysWindow();
    days.forEach(d => {
      const k = dateKey(d);
      cachedDailyMap.set(k, emptyDaily(k));
    });

    cachedAggDocs.forEach(data => {
      const k = data.date || data._docId;
      if (k) {
        cachedDailyMap.set(k, { ...emptyDaily(k), ...data });
      }
    });

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
    .where('date', '>=', dateKey(minDate))
    .where('date', '<=', dateKey(today))
    .onSnapshot(snap => {
      cachedAggDocs = snap.docs.map(doc => ({ _docId: doc.id, ...doc.data() }));
      rebuildAndRender();
    }, err => appendSystemLog(`Analytics listener error: ${err.message}`, true));

  // Listener 2: sessions (updates when a session starts or ends)
  analyticsSessionUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('sessions')
    .where('started_at', '>=', minDate)
    .onSnapshot(snap => {
      cachedSessionDocs = snap.docs.map(doc => ({ id: doc.id, ...doc.data() }));
      rebuildAndRender();
    }, err => appendSystemLog(`Session listener error: ${err.message}`, true));

  // One-shot today's watch_data
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
  const { minDate, today, days } = getSevenDaysWindow();
  const selectedKey = dateKey(selectedDate);
  const todayKey = dateKey(today);
  const minKey = dateKey(minDate);

  // 1. Unboxed arrows: disable prev at minDate, disable next at today (no circular wrap)
  const prevBtn = document.getElementById('prevDateBtn');
  if (prevBtn) {
    if (selectedKey <= minKey) {
      prevBtn.classList.add('opacity-20', 'pointer-events-none');
    } else {
      prevBtn.classList.remove('opacity-20', 'pointer-events-none');
    }
  }

  const nextBtn = document.getElementById('nextDateBtn');
  if (nextBtn) {
    if (selectedKey >= todayKey) {
      nextBtn.classList.add('opacity-20', 'pointer-events-none');
    } else {
      nextBtn.classList.remove('opacity-20', 'pointer-events-none');
    }
  }

  // 2. Boxed Date display: "Today", "Yesterday", or "n-th Month"
  const fullTextEl = document.getElementById('selectedDateFullText');
  if (fullTextEl) {
    if (currentViewMode === 'week') {
      fullTextEl.textContent = 'Whole Week';
    } else {
      fullTextEl.textContent = formatCustomDate(selectedDate);
    }
  }

  // 3. Render 7 day name buttons (strictly 7 days total, non-circular)
  const stripEl = document.getElementById('weekDaysStrip');
  let stripHtml = '';
  days.forEach(d => {
    const k = dateKey(d);
    const isSel = (currentViewMode === 'today' && k === selectedKey);
    const isTod = (k === todayKey);
    const dayName = d.toLocaleDateString('en-US', { weekday: 'short' });

    stripHtml += `
      <button onclick="setSelectedDate('${k}')" class="week-day-btn ${isSel ? 'active' : ''} ${isTod ? 'is-today' : ''}" title="${formatCustomDate(d)}">
        ${dayName}
      </button>
    `;
  });
  if (stripEl) stripEl.innerHTML = stripHtml;

  // 4. Update the KPI blocks (Total Reps, Plank Time, Form Accuracy)
  let curls = 0, pushups = 0, squats = 0, totalReps = 0;
  let plankSec = 0, stepsTotal = 0, good = 0, flaw = 0;

  if (currentViewMode === 'week') {
    days.forEach(d => {
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

  const profStepsCount = document.getElementById('profileStepsCount');
  if (profStepsCount) profStepsCount.textContent = stepsTotal.toLocaleString();

  const profStepsPeriod = document.getElementById('profileStepsPeriod');
  if (profStepsPeriod) {
    profStepsPeriod.textContent = (currentViewMode === 'week') ? '7-Day Total' : (selectedKey === todayKey ? 'Today' : formatCustomDate(selectedDate));
  }

  const profStepsSub = document.getElementById('profileStepsSub');
  if (profStepsSub) {
    const estCal = Math.round(stepsTotal * 0.04);
    const estDist = (stepsTotal * 0.0008).toFixed(2);
    profStepsSub.textContent = stepsTotal > 0 ? `~${estCal} kcal • ~${estDist} km` : 'ESP32 Wristband';
  }

  const totalForm = good + flaw;
  const pct = totalForm > 0 ? Math.round((good / totalForm) * 100) : 0;

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

  // 5. Update charts
  if (currentViewMode === 'week') {
    renderRepsChartWeek(days);
    renderTimeChartWeek(days);
  } else {
    const dayData = cachedDailyMap.get(selectedKey) || emptyDaily(selectedKey);
    renderRepsChart(dayData);
    renderTimeChart(dayData);
  }
  renderFormChart({ good, flaw });
}

// Clear History & Share Snapshot Action Handlers
async function clearWorkoutHistory() {
  if (!confirm("Are you sure you want to clear your workout history? This will delete all recorded reps, sessions, and daily performance logs.")) {
    return;
  }

  cachedAggDocs = [];
  cachedSessionDocs = [];
  cachedDailyMap.clear();

  if (db && currentUserId) {
    try {
      const batch = db.batch();
      const aggSnap = await db.collection('users').doc(currentUserId).collection('daily_aggregates').get();
      aggSnap.forEach(doc => batch.delete(doc.ref));

      const sessionSnap = await db.collection('users').doc(currentUserId).collection('sessions').get();
      sessionSnap.forEach(doc => batch.delete(doc.ref));

      await batch.commit();
      appendSystemLog("Workout history cleared from cloud.");
    } catch (err) {
      console.error("Error clearing history in Firestore:", err);
      appendSystemLog(`Error clearing history: ${err.message}`, true);
    }
  }

  const { days } = getSevenDaysWindow();
  days.forEach(d => {
    const k = dateKey(d);
    cachedDailyMap.set(k, emptyDaily(k));
  });

  updateProfileViewForSelectedDate();
  alert("Workout history has been cleared.");
}

// ---------- Workout Snapshot Image & PDF Generation ----------

let currentSnapshotCanvas = null;
let currentSnapshotBlob = null;

function roundRect(ctx, x, y, width, height, radius) {
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + width - radius, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
  ctx.lineTo(x + width, y + height - radius);
  ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  ctx.lineTo(x + radius, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
}

function generateWorkoutSnapshotCanvas() {
  const isWeek = currentViewMode === 'week';
  const timeLabel = isWeek ? 'Whole Week Summary' : formatCustomDate(selectedDate);
  const athleteName = document.getElementById('profileName')?.textContent || 'Athlete User';
  const totalReps = document.getElementById('kpiTotalReps')?.textContent || '0';
  const repsDetail = document.getElementById('kpiRepsBreakdown')?.textContent || 'Curls • Pushups • Squats';
  const plank = document.getElementById('kpiPlankTime')?.textContent || '0s';
  const form = document.getElementById('kpiForm')?.textContent || '0%';
  const formSub = document.getElementById('kpiFormSub')?.textContent || 'Pose Precision Score';
  const steps = document.getElementById('profileStepsCount')?.textContent || '0';
  const stepsSub = document.getElementById('profileStepsSub')?.textContent || 'ESP32 Wristband';

  const width = 1200;
  const height = 1500;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');

  // 1. Dark Cyber Background
  const bgGrad = ctx.createLinearGradient(0, 0, width, height);
  bgGrad.addColorStop(0, '#070913');
  bgGrad.addColorStop(0.5, '#0a0e1c');
  bgGrad.addColorStop(1, '#05070d');
  ctx.fillStyle = bgGrad;
  ctx.fillRect(0, 0, width, height);

  // Ambient glows
  const glow1 = ctx.createRadialGradient(200, 160, 20, 200, 160, 480);
  glow1.addColorStop(0, 'rgba(6, 182, 212, 0.22)');
  glow1.addColorStop(1, 'rgba(6, 182, 212, 0)');
  ctx.fillStyle = glow1;
  ctx.fillRect(0, 0, width, height);

  const glow2 = ctx.createRadialGradient(1000, 1200, 30, 1000, 1200, 520);
  glow2.addColorStop(0, 'rgba(16, 185, 129, 0.18)');
  glow2.addColorStop(1, 'rgba(16, 185, 129, 0)');
  ctx.fillStyle = glow2;
  ctx.fillRect(0, 0, width, height);

  // Outer Border
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
  ctx.lineWidth = 3;
  roundRect(ctx, 30, 30, width - 60, height - 60, 32);
  ctx.stroke();

  // 2. Header
  // Robot Icon Box
  ctx.fillStyle = 'rgba(6, 182, 212, 0.15)';
  roundRect(ctx, 70, 70, 74, 74, 22);
  ctx.fill();
  ctx.strokeStyle = 'rgba(6, 182, 212, 0.4)';
  ctx.lineWidth = 2;
  roundRect(ctx, 70, 70, 74, 74, 22);
  ctx.stroke();

  ctx.font = '38px "Segoe UI Emoji", "Apple Color Emoji", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('🤖', 107, 107);

  // Brand Titles
  ctx.textAlign = 'left';
  ctx.fillStyle = '#ffffff';
  ctx.font = '900 36px system-ui, -apple-system, sans-serif';
  ctx.fillText('FITNESS ROBOT', 165, 96);

  ctx.fillStyle = 'rgba(6, 182, 212, 0.9)';
  ctx.font = '700 15px system-ui, -apple-system, sans-serif';
  ctx.fillText('AUTONOMOUS AI VISION & WEARABLE TRACKER', 167, 128);

  // Athlete & Date Badge on Right
  ctx.textAlign = 'right';
  ctx.fillStyle = '#ffffff';
  ctx.font = '800 24px system-ui, -apple-system, sans-serif';
  ctx.fillText(athleteName, width - 75, 96);

  ctx.fillStyle = '#10b981';
  ctx.font = '600 17px system-ui, -apple-system, sans-serif';
  ctx.fillText(`📅 ${timeLabel}`, width - 75, 128);

  // Divider Line
  const divGrad = ctx.createLinearGradient(70, 175, width - 70, 175);
  divGrad.addColorStop(0, 'rgba(6, 182, 212, 0.6)');
  divGrad.addColorStop(0.5, 'rgba(16, 185, 129, 0.6)');
  divGrad.addColorStop(1, 'rgba(255, 255, 255, 0.1)');
  ctx.fillStyle = divGrad;
  ctx.fillRect(70, 175, width - 140, 3);

  // 3. Metric Cards (2x2 Grid)
  const cardW = 510;
  const cardH = 200;
  const col1X = 70;
  const col2X = 620;
  const row1Y = 210;
  const row2Y = 440;

  function drawMetricCard(x, y, w, h, tag, val, sub, tagColor, valColor) {
    ctx.fillStyle = 'rgba(255, 255, 255, 0.04)';
    roundRect(ctx, x, y, w, h, 24);
    ctx.fill();
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 2;
    roundRect(ctx, x, y, w, h, 24);
    ctx.stroke();

    ctx.textAlign = 'left';
    ctx.font = '800 15px system-ui, -apple-system, sans-serif';
    ctx.fillStyle = tagColor;
    ctx.fillText(tag.toUpperCase(), x + 28, y + 42);

    ctx.font = '900 58px system-ui, -apple-system, sans-serif';
    ctx.fillStyle = valColor;
    ctx.fillText(val, x + 28, y + 115);

    ctx.font = '600 18px system-ui, -apple-system, sans-serif';
    ctx.fillStyle = 'rgba(255, 255, 255, 0.55)';
    ctx.fillText(sub, x + 28, y + 165);
  }

  drawMetricCard(col1X, row1Y, cardW, cardH, 'Total Reps', totalReps, repsDetail, '#06b6d4', '#ffffff');
  drawMetricCard(col2X, row1Y, cardW, cardH, 'Steps Synced', steps, stepsSub, '#f59e0b', '#ffffff');
  drawMetricCard(col1X, row2Y, cardW, cardH, 'Plank Hold Time', plank, 'Core Elevation Hold', '#10b981', '#10b981');
  drawMetricCard(col2X, row2Y, cardW, cardH, 'Form Accuracy', form, formSub, '#c084fc', '#c084fc');

  // 4. Visual Routine & Performance Chart Area
  const chartBoxY = 675;
  const chartBoxH = 680;
  ctx.fillStyle = 'rgba(255, 255, 255, 0.03)';
  roundRect(ctx, 70, chartBoxY, width - 140, chartBoxH, 28);
  ctx.fill();
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
  ctx.lineWidth = 2;
  roundRect(ctx, 70, chartBoxY, width - 140, chartBoxH, 28);
  ctx.stroke();

  ctx.textAlign = 'left';
  ctx.font = '800 24px system-ui, -apple-system, sans-serif';
  ctx.fillStyle = '#ffffff';
  ctx.fillText('📊 Routine & Performance Breakdown', 105, chartBoxY + 50);

  ctx.font = '600 16px system-ui, -apple-system, sans-serif';
  ctx.fillStyle = 'rgba(6, 182, 212, 0.8)';
  ctx.fillText(timeLabel, 105, chartBoxY + 82);

  // Render reps chart image onto canvas
  const repsCanvas = document.getElementById('repsChart');
  if (repsCanvas) {
    try {
      ctx.drawImage(repsCanvas, 100, chartBoxY + 110, width - 200, 260);
    } catch (e) {
      console.warn('Could not render repsCanvas onto snapshot:', e);
    }
  }

  // Render time chart image onto canvas
  const timeCanvas = document.getElementById('timeChart');
  if (timeCanvas) {
    try {
      ctx.drawImage(timeCanvas, 100, chartBoxY + 395, width - 200, 245);
    } catch (e) {
      console.warn('Could not render timeCanvas onto snapshot:', e);
    }
  }

  // 5. Verification Footer
  const footerY = 1410;
  ctx.textAlign = 'left';
  ctx.font = '600 16px system-ui, -apple-system, sans-serif';
  ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
  ctx.fillText('Verified by Raspberry Pi CV & ESP32 Smartwatch Telemetry', 75, footerY);

  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(255, 255, 255, 0.4)';
  const nowStr = new Date().toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  ctx.fillText(`Generated: ${nowStr}`, width - 75, footerY);

  return canvas;
}

function openSnapshotModal(canvas, blob) {
  currentSnapshotCanvas = canvas;
  currentSnapshotBlob = blob;

  const previewImg = document.getElementById('snapshotPreviewImg');
  if (previewImg) {
    previewImg.src = canvas.toDataURL('image/png');
  }

  const modal = document.getElementById('snapshotModal');
  if (modal) {
    modal.classList.remove('hidden');
  }
}

function closeSnapshotModal() {
  const modal = document.getElementById('snapshotModal');
  if (modal) {
    modal.classList.add('hidden');
  }
}

async function triggerSnapshotImageShare() {
  if (!currentSnapshotCanvas) return;

  const isWeek = currentViewMode === 'week';
  const fileName = `fitness-snapshot-${isWeek ? 'week' : dateKey(selectedDate)}.png`;

  if (currentSnapshotBlob) {
    const file = new File([currentSnapshotBlob], fileName, { type: 'image/png' });

    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({
          files: [file],
          title: `Fitness Snapshot - ${isWeek ? 'Whole Week' : formatCustomDate(selectedDate)}`,
          text: `My workout snapshot from Fitness Robot (${isWeek ? 'Whole Week' : formatCustomDate(selectedDate)})`
        });
        closeSnapshotModal();
        return;
      } catch (err) {
        if (err.name === 'AbortError') return;
        console.warn('Share file aborted or failed:', err);
      }
    }
  }

  // Fallback: Download Image directly
  const a = document.createElement('a');
  a.href = currentSnapshotCanvas.toDataURL('image/png');
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function triggerSnapshotPdfExport() {
  if (!currentSnapshotCanvas) return;
  const dataUrl = currentSnapshotCanvas.toDataURL('image/png');
  const isWeek = currentViewMode === 'week';
  const timeLabel = isWeek ? 'Whole Week' : formatCustomDate(selectedDate);

  const printWindow = window.open('', '_blank');
  if (!printWindow) {
    alert('Popup window blocked by browser. Please allow popups or download the PNG image instead.');
    return;
  }

  printWindow.document.write(`
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8">
        <title>Fitness Robot Snapshot - ${timeLabel}</title>
        <style>
          @page {
            size: A4 portrait;
            margin: 0;
          }
          * {
            box-sizing: border-box;
          }
          body {
            margin: 0;
            padding: 0;
            background: #070913;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
          }
          img {
            max-width: 100%;
            height: auto;
            max-height: 100vh;
            display: block;
            margin: auto;
            box-shadow: 0 10px 30px rgba(0,0,0,0.6);
          }
          @media print {
            body {
              background: transparent !important;
            }
            img {
              max-height: 100%;
              width: 100%;
              box-shadow: none;
            }
          }
        </style>
      </head>
      <body>
        <img src="${dataUrl}" alt="Workout Snapshot" onload="setTimeout(() => { window.print(); }, 250);" />
      </body>
    </html>
  `);
  printWindow.document.close();
}

async function shareWorkoutSnapshot() {
  const canvas = generateWorkoutSnapshotCanvas();

  canvas.toBlob(async (blob) => {
    if (!blob) return;

    currentSnapshotCanvas = canvas;
    currentSnapshotBlob = blob;

    const isWeek = currentViewMode === 'week';
    const fileName = `fitness-snapshot-${isWeek ? 'week' : dateKey(selectedDate)}.png`;
    const file = new File([blob], fileName, { type: 'image/png' });

    // 1. Try native Web Share API with File (Supported on mobile devices e.g. WhatsApp, AirDrop, Messages)
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({
          files: [file],
          title: `Fitness Snapshot - ${isWeek ? 'Whole Week' : formatCustomDate(selectedDate)}`,
          text: `My workout snapshot from Fitness Robot (${isWeek ? 'Whole Week' : formatCustomDate(selectedDate)})`
        });
        return;
      } catch (e) {
        if (e.name === 'AbortError') return; // User closed sheet
        console.warn('Native share error, falling back to modal:', e);
      }
    }

    // 2. Open sleek snapshot modal with Image and PDF export options
    openSnapshotModal(canvas, blob);
  }, 'image/png');
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
        const configBtn = document.getElementById('configMenuBtn');
        if (configBtn) configBtn.classList.add('hidden');
        const batt = document.getElementById('batteryIndicator');
        if (batt) batt.classList.add('hidden');
        const cfgDrawer = document.getElementById('configDrawer');
        if (cfgDrawer) cfgDrawer.classList.add('hidden');
        const desktopNav = document.getElementById('desktopNavMenu');
        if (desktopNav) desktopNav.classList.add('hidden');
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

  // Close hardware config drawer when clicking outside
  document.addEventListener('click', (e) => {
    const drawer = document.getElementById('configDrawer');
    const btn = document.getElementById('configMenuBtn');
    if (drawer && !drawer.classList.contains('hidden')) {
      if (!drawer.contains(e.target) && !btn?.contains(e.target)) {
        drawer.classList.add('hidden');
      }
    }
  });
})();