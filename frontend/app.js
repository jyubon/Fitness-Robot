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
let batteryUnsubscribe = null;
let lastKnownBatteryVoltage = null;
let batteryDisplayMode = 'percent';

let repsChart = null;
let timeChart = null;
let formChart = null;

let currentSessionId = null;
let currentExercise = 'bicep_curls';
let latestEventTimestamp = null;
let robotState = 'READY';

// Persistent daily per-exercise state (resets ONLY on Clear History or day rollover)
let dailyExerciseState = null;
let lastFormFlawRecordedAt = 0;

// Legacy aliases synced with dailyExerciseState.exercises.bicep_curls
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

  if (localReconnectTimer) {
    clearTimeout(localReconnectTimer);
    localReconnectTimer = null;
  }
  isCameraStreamLoaded = false;
  lastRobotTelemetryAt = 0;

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
  const cameraUrlInput = document.getElementById('cameraUrl');
  if (cameraUrlInput && !cameraUrlInput.value.trim() && currentCameraUrl) {
    cameraUrlInput.value = currentCameraUrl;
  }
  ensureLocalWebSocketConnected();
  refreshRobotOnlineStatus();
  setWatchConnectionStatus(stepConnected);

  if (localControlSocket && localControlSocket.readyState === 1 && currentUserId) {
    try {
      localControlSocket.send(JSON.stringify({
        action: 'identify',
        user_id: currentUserId,
        user_email: currentUserEmail
      }));
    } catch (e) { }
  }

  updateMobileTrackerLink();

  if (!navigateFromUrl()) {
    showTab('live');
  }
}

function navigateFromUrl() {
  try {
    const params = new URLSearchParams(window.location.search);
    const hash = (window.location.hash || '').toLowerCase().replace(/^#+/, '');
    const tabParam = params.get('tab') || params.get('section') || params.get('view') || hash;
    const subtabParam = params.get('subtab');

    if (tabParam === 'steps' || tabParam === 'step' || tabParam === 'steptracker' || tabParam === 'step-tracker' || tabParam === 'stepsview') {
      currentFitnessSubtab = 'steps';
      showTab('steps');
      switchFitnessSubtab('steps');
      return true;
    }
    if (tabParam === 'fitness' || tabParam === 'exercises') {
      currentFitnessSubtab = (subtabParam === 'steps' || subtabParam === 'step') ? 'steps' : 'exercises';
      showTab('steps');
      switchFitnessSubtab(currentFitnessSubtab);
      return true;
    }
    if (tabParam === 'analytics' || tabParam === 'profile') {
      showTab('analytics');
      return true;
    }
    if (tabParam === 'live' || tabParam === 'home') {
      showTab('live');
      return true;
    }
  } catch (e) {
    console.warn("Error parsing URL navigation params:", e);
  }
  return false;
}

let isCameraStreamLoaded = false;
let lastRobotTelemetryAt = 0;

function isRobotCurrentlyOnline() {
  const wsOpen = Boolean(localControlSocket && localControlSocket.readyState === WebSocket.OPEN);
  const recentTelemetry = lastRobotTelemetryAt > 0 && (Date.now() - lastRobotTelemetryAt < 65000);
  return wsOpen || isCameraStreamLoaded || recentTelemetry;
}

function refreshRobotOnlineStatus() {
  setRobotConnectionStatus(isRobotCurrentlyOnline());
}

function setRobotConnectionStatus(isOnline) {
  const dot = document.getElementById('robotStatusDot');
  const text = document.getElementById('robotStatusText');
  const pill = document.getElementById('robotState');

  if (text) {
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

  if (pill) {
    if (isOnline) {
      const currentPillState = (pill.getAttribute('data-state') || pill.textContent || '').toUpperCase();
      if (!currentPillState || currentPillState === 'OFFLINE') {
        const displayState = (robotState && robotState !== 'OFFLINE') ? robotState : 'CONNECTED';
        pill.textContent = displayState;
        pill.setAttribute('data-state', displayState);
      }
    } else {
      pill.textContent = 'OFFLINE';
      pill.setAttribute('data-state', 'OFFLINE');
    }
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

  const startBtn = document.getElementById('startExerciseBtn');

  if (subtab === 'exercises') {
    if (startBtn) startBtn.classList.remove('hidden');
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
    if (isCameraActive || isDesktop()) {
      startCameraStreamIfNeeded();
    }
  } else {
    if (startBtn) startBtn.classList.add('hidden');
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

function isDesktop() {
  return window.innerWidth >= 1024;
}

function toggleCameraView(forceState) {
  const card = document.getElementById('cameraViewCard');
  const wrapper = document.getElementById('cameraViewCardWrapper');
  const btn = document.getElementById('cameraToggleBtn');
  if (!card) return;

  if (isDesktop()) {
    // Camera view is permanently fixed in the left column on desktop
    isCameraActive = true;
    startCameraStreamIfNeeded();
    return;
  }

  // Mobile toggle behavior
  if (typeof forceState === 'boolean') {
    isCameraActive = forceState;
  } else {
    const isCurrentlyActive = wrapper ? wrapper.classList.contains('mobile-active') : isCameraActive;
    isCameraActive = !isCurrentlyActive;
  }

  if (isCameraActive) {
    if (wrapper) wrapper.classList.add('mobile-active');
    if (btn) btn.classList.add('camera-btn-active');
    startCameraStreamIfNeeded();
  } else {
    if (wrapper) wrapper.classList.remove('mobile-active');
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

function getDailyExerciseStorageKey() {
  return `fitness_daily_exercises_${currentUserId || 'anon'}`;
}

function createEmptyDailyExercises(dateStr) {
  return {
    date: dateStr,
    exercises: {
      bicep_curls: {
        set: 1,
        repsInSet: 0,
        totalReps: 0,
        leftInSet: 0,
        rightInSet: 0,
        totalLeft: 0,
        totalRight: 0,
        sessionRawLeft: 0,
        sessionRawRight: 0
      },
      pushups: {
        set: 1,
        repsInSet: 0,
        totalReps: 0,
        sessionRawCount: 0
      },
      squats: {
        set: 1,
        repsInSet: 0,
        totalReps: 0,
        sessionRawCount: 0
      },
      plank: {
        plankTimeS: 0,
        sessionRawPlank: 0
      }
    }
  };
}

function saveDailyExerciseState() {
  if (!dailyExerciseState) return;
  try {
    localStorage.setItem(getDailyExerciseStorageKey(), JSON.stringify(dailyExerciseState));
  } catch (e) { }
}

function ensureDailyExerciseState() {
  const todayStr = dateKey(new Date());
  const storageKey = getDailyExerciseStorageKey();

  if (!dailyExerciseState || dailyExerciseState._storageKey !== storageKey) {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.date === todayStr && parsed.exercises) {
          const base = createEmptyDailyExercises(todayStr);
          dailyExerciseState = {
            _storageKey: storageKey,
            date: todayStr,
            exercises: {
              bicep_curls: { ...base.exercises.bicep_curls, ...(parsed.exercises.bicep_curls || {}) },
              pushups: { ...base.exercises.pushups, ...(parsed.exercises.pushups || {}) },
              squats: { ...base.exercises.squats, ...(parsed.exercises.squats || {}) },
              plank: { ...base.exercises.plank, ...(parsed.exercises.plank || {}) }
            }
          };
        }
      }
    } catch (e) { }
  }

  // Automatic rollover when the calendar day has passed (or first init)
  if (!dailyExerciseState || dailyExerciseState.date !== todayStr) {
    dailyExerciseState = {
      _storageKey: storageKey,
      ...createEmptyDailyExercises(todayStr)
    };
    saveDailyExerciseState();
  }

  const bc = dailyExerciseState.exercises.bicep_curls;
  bicepCurrentSet = bc.set || 1;
  bicepLeftInSet = bc.leftInSet || 0;
  bicepRightInSet = bc.rightInSet || 0;

  return dailyExerciseState.exercises;
}

function resetSessionBaselines(exercise) {
  const exMap = ensureDailyExerciseState();
  if (!exercise || exercise === 'bicep_curls') {
    exMap.bicep_curls.sessionRawLeft = 0;
    exMap.bicep_curls.sessionRawRight = 0;
  }
  if (!exercise || exercise === 'pushups') {
    exMap.pushups.sessionRawCount = 0;
  }
  if (!exercise || exercise === 'squats') {
    exMap.squats.sessionRawCount = 0;
  }
  if (!exercise || exercise === 'plank') {
    exMap.plank.sessionRawPlank = 0;
  }
  saveDailyExerciseState();
}

function resetAllExercisesDailyState() {
  const todayStr = dateKey(new Date());
  dailyExerciseState = {
    _storageKey: getDailyExerciseStorageKey(),
    ...createEmptyDailyExercises(todayStr)
  };
  saveDailyExerciseState();
  bicepCurrentSet = 1;
  bicepLeftInSet = 0;
  bicepRightInSet = 0;
  bicepSetStartLeft = 0;
  bicepSetStartRight = 0;
  bicepHasBaseline = false;
  lastFinishedBicepSet = 0;
  syncExerciseUI(currentExercise);
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

function resetExerciseUI(exercise) {
  // Only reset per-session robot counter baselines; preserve daily sets/reps unless history is cleared or day passes
  resetSessionBaselines(exercise);
  syncExerciseUI(exercise || currentExercise);
}

function handleBicepSetComplete(newSet, rawLeft, rawRight) {
  const exMap = ensureDailyExerciseState();
  const bc = exMap.bicep_curls;
  const finishedSet = bc.set || 1;
  bc.set = finishedSet + 1;
  bc.leftInSet = 0;
  bc.rightInSet = 0;
  bc.repsInSet = 0;
  if (rawLeft !== undefined) bc.sessionRawLeft = Number(rawLeft) || 0;
  if (rawRight !== undefined) bc.sessionRawRight = Number(rawRight) || 0;
  saveDailyExerciseState();

  bicepCurrentSet = bc.set;
  bicepLeftInSet = 0;
  bicepRightInSet = 0;

  updateBicepArmUI(0, 0, bc.set);

  const repsEl = document.getElementById('currentReps');
  if (repsEl) repsEl.textContent = '0';

  const detailEl = document.getElementById('repsDetail');
  if (detailEl) detailEl.textContent = `Set ${finishedSet} Complete • Total today: ${bc.totalReps}`;

  appendExerciseLog(`🎉 Bicep Curls: Set ${finishedSet} Complete! Both arms reached 8 reps.`);
}

function syncExerciseUI(exercise) {
  if (!exercise) return;
  const exMap = ensureDailyExerciseState();

  const currentWorkoutEl = document.getElementById('currentWorkout');
  if (currentWorkoutEl) currentWorkoutEl.textContent = formatExercise(exercise);

  const heroRoutine = document.getElementById('homeHeroRoutine');
  if (heroRoutine) heroRoutine.textContent = formatExercise(exercise);

  const select = document.getElementById('fitnessExerciseSelect');
  if (select && select.value !== exercise) {
    select.value = exercise;
  }

  const setEl = document.getElementById('currentSet');
  const repsEl = document.getElementById('currentReps');
  const detailEl = document.getElementById('repsDetail');
  const plankBox = document.getElementById('plankTimeBox');
  const plankEl = document.getElementById('plankTime');
  const bicepBox = document.getElementById('bicepArmsBox');

  if (exercise === 'plank') {
    if (plankBox) plankBox.classList.remove('hidden');
    if (bicepBox) bicepBox.classList.add('hidden');
    if (setEl) setEl.textContent = '—';
    if (repsEl) repsEl.textContent = '—';
    if (detailEl) detailEl.textContent = '';
    const pTime = Math.round(Number(exMap.plank?.plankTimeS || 0));
    if (plankEl) plankEl.textContent = pTime > 0 ? formatSeconds(pTime) : '0s';
  } else if (exercise === 'bicep_curls') {
    if (plankBox) plankBox.classList.add('hidden');
    if (bicepBox) bicepBox.classList.remove('hidden');
    const bc = exMap.bicep_curls;
    if (setEl) setEl.textContent = bc.set || 1;
    if (repsEl) repsEl.textContent = bc.repsInSet || 0;
    if (detailEl) {
      detailEl.textContent = bc.totalReps > 0
        ? `L:${bc.leftInSet}/8  R:${bc.rightInSet}/8 • Total today: ${bc.totalReps}`
        : `L:${bc.leftInSet}/8  R:${bc.rightInSet}/8`;
    }
    updateBicepArmUI(bc.leftInSet, bc.rightInSet, bc.set);
  } else if (exercise === 'pushups' || exercise === 'squats') {
    if (plankBox) plankBox.classList.add('hidden');
    if (bicepBox) bicepBox.classList.add('hidden');
    const st = exMap[exercise] || { set: 1, repsInSet: 0, totalReps: 0 };
    if (setEl) setEl.textContent = st.set || 1;
    if (repsEl) repsEl.textContent = st.repsInSet || 0;
    if (detailEl) {
      detailEl.textContent = st.totalReps > 0 ? `Total today: ${st.totalReps} reps` : '';
    }
  } else {
    if (plankBox) plankBox.classList.add('hidden');
    if (bicepBox) bicepBox.classList.add('hidden');
    if (setEl) setEl.textContent = '—';
    if (repsEl) repsEl.textContent = '—';
    if (detailEl) detailEl.textContent = '';
  }

  updateModeButtons(exercise);
  updateExerciseButtonStates();
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
  const configMenuBtn = document.getElementById('configMenuBtn');
  const configDrawer = document.getElementById('configDrawer');

  // Start button in Fitness section
  if (btnStart) {
    if (tab === 'steps') {
      btnStart.classList.remove('hidden');
    } else {
      btnStart.classList.add('hidden');
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
    if (isDesktop() || isCameraActive) {
      startCameraStreamIfNeeded();
    }
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

  batteryUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('device_status')
    .doc('battery')
    .onSnapshot((doc) => {
      if (doc.exists && doc.data() && doc.data().voltage !== undefined) {
        const data = doc.data();
        updateBatteryUI(data.voltage);
        const updatedMs = toMillis(data.updated_at);
        if (!updatedMs || (Date.now() - updatedMs < 65000)) {
          lastRobotTelemetryAt = Date.now();
          setRobotConnectionStatus(true);
        }
      }
    });

  appendSystemLog('Connected to Firebase telemetry');
}

function disconnectRealtime() {
  if (telemetryUnsubscribe) telemetryUnsubscribe();
  if (watchUnsubscribe) watchUnsubscribe();
  if (batteryUnsubscribe) batteryUnsubscribe();
  telemetryUnsubscribe = null;
  watchUnsubscribe = null;
  batteryUnsubscribe = null;
}

function handleTelemetryMessage(msg) {
  const type = msg.type;
  const payload = msg.payload || msg;

  if (type && type !== 'watch_steps') {
    lastRobotTelemetryAt = Date.now();
    setRobotConnectionStatus(true);
  }

  if (type === 'session_start') {
    const sid = msg.session_id || payload.session_id || msg.id;
    const ex = msg.exercise_type || payload.exercise_type || currentExercise;
    if (sid && sid === currentSessionId && ex === currentExercise) {
      return;
    }
    currentSessionId = sid;
    currentExercise = ex;
    resetSessionBaselines(currentExercise);
    syncExerciseUI(currentExercise);
    appendExerciseLog(`Session started: ${formatExercise(currentExercise)}`);
    return;
  }

  if (type === 'session_end') {
    const activeSec = Number(payload.active_duration_s || 0);
    appendExerciseLog(`Session ended. Active: ${activeSec.toFixed(1)}s`);
    resetSessionBaselines(currentExercise);
    recordLiveSessionEnd(activeSec, payload);
    syncExerciseUI(currentExercise);
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
    const prevState = robotState;
    robotState = state;

    if (payload.exercise) {
      if (payload.exercise !== currentExercise) {
        currentExercise = payload.exercise;
      }
      syncExerciseUI(currentExercise);
    } else if (state === 'READY' || state === 'STOPPED') {
      const select = document.getElementById('fitnessExerciseSelect');
      if (select && select.value && select.value !== currentExercise) {
        currentExercise = select.value;
      }
      syncExerciseUI(currentExercise);
    }

    if (state === 'FOLLOWING' && prevState !== 'FOLLOWING') {
      appendExerciseLog(`🤖 Robot is in FOLLOWING mode for ${formatExercise(currentExercise)}. Get in position and press START.`);
    }

    updateExerciseButtonStates();
    return;
  }

  if (type === 'rep') {
    const ex = payload.exercise || currentExercise;
    if (ex && ex !== currentExercise) {
      currentExercise = ex;
    }
    const exMap = ensureDailyExerciseState();

    if (ex === 'bicep_curls') {
      const bc = exMap.bicep_curls;
      const rawLeft = Number(payload.left ?? payload.count ?? 0);
      const rawRight = Number(payload.right ?? payload.count ?? 0);

      if (rawLeft < bc.sessionRawLeft || rawRight < bc.sessionRawRight) {
        bc.sessionRawLeft = 0;
        bc.sessionRawRight = 0;
      }

      const deltaL = Math.max(0, rawLeft - bc.sessionRawLeft);
      const deltaR = Math.max(0, rawRight - bc.sessionRawRight);
      if (deltaL === 0 && deltaR === 0) {
        return; // Duplicate telemetry packet
      }

      bc.sessionRawLeft = rawLeft;
      bc.sessionRawRight = rawRight;

      if (bc.leftInSet >= 8 && bc.rightInSet >= 8) {
        bc.set = (bc.set || 1) + 1;
        bc.leftInSet = 0;
        bc.rightInSet = 0;
        bc.repsInSet = 0;
      }

      bc.totalLeft = (bc.totalLeft || 0) + deltaL;
      bc.totalRight = (bc.totalRight || 0) + deltaR;
      bc.leftInSet = Math.min(8, (bc.leftInSet || 0) + deltaL);
      bc.rightInSet = Math.min(8, (bc.rightInSet || 0) + deltaR);

      const prevRepsInSet = bc.repsInSet || 0;
      bc.repsInSet = Math.min(bc.leftInSet, bc.rightInSet);
      const newFullReps = Math.max(0, bc.repsInSet - prevRepsInSet);
      bc.totalReps = (bc.totalReps || 0) + newFullReps;

      if (bc.leftInSet >= 8 && bc.rightInSet >= 8) {
        const finishedSet = bc.set || 1;
        bc.set = finishedSet + 1;
        bc.leftInSet = 0;
        bc.rightInSet = 0;
        bc.repsInSet = 0;
        appendExerciseLog(`🎉 Bicep Curls: Set ${finishedSet} Complete! Both arms reached 8 reps.`);
      }

      saveDailyExerciseState();
      syncExerciseUI('bicep_curls');

      if (newFullReps > 0 || payload.is_full_rep) {
        recordLiveRep('bicep_curls', bc.totalReps, bc.leftInSet, bc.rightInSet, Math.max(1, newFullReps));
      }
      return;
    } else if (ex === 'pushups' || ex === 'squats') {
      const st = exMap[ex] || (exMap[ex] = { set: 1, repsInSet: 0, totalReps: 0, sessionRawCount: 0 });
      const rawCount = Number(payload.count ?? payload.total ?? payload.in_set ?? payload.reps ?? 0);

      if (rawCount < st.sessionRawCount) {
        st.sessionRawCount = 0;
      }

      const delta = Math.max(0, rawCount - st.sessionRawCount);
      if (delta === 0) {
        return; // Duplicate telemetry packet
      }

      st.sessionRawCount = rawCount;
      st.totalReps = (st.totalReps || 0) + delta;
      st.repsInSet = (st.repsInSet || 0) + delta;

      while (st.repsInSet >= 8) {
        const finishedSet = st.set || 1;
        st.set = finishedSet + 1;
        st.repsInSet -= 8;
        appendExerciseLog(`🎉 ${formatExercise(ex)}: Set ${finishedSet} Complete! (8 reps)`);
      }

      saveDailyExerciseState();
      syncExerciseUI(ex);
      recordLiveRep(ex, st.totalReps, undefined, undefined, delta);
      return;
    }
  }

  if (type === 'plank_time' || type === 'plank') {
    const rawVal = Number(payload.value ?? payload.plank_timer ?? 0);
    const exMap = ensureDailyExerciseState();
    const pl = exMap.plank;

    if (rawVal < pl.sessionRawPlank) {
      pl.sessionRawPlank = 0;
    }

    const deltaSec = Math.max(0, rawVal - pl.sessionRawPlank);
    if (deltaSec <= 0) {
      return;
    }

    pl.sessionRawPlank = rawVal;
    pl.plankTimeS = Math.round(((pl.plankTimeS || 0) + deltaSec) * 10) / 10;
    saveDailyExerciseState();

    const plankEl = document.getElementById('plankTime');
    if (plankEl) plankEl.textContent = formatSeconds(Math.round(pl.plankTimeS));
    recordLivePlank(pl.plankTimeS);
    return;
  }

  if (type === 'form_error' || type === 'encouragement') {
    const msgText = payload.message || JSON.stringify(payload);
    appendExerciseLog(msgText, true);
    if (type === 'form_error') {
      recordLiveFormError(msgText);
    }
    return;
  }

  if (type === 'battery_voltage') {
    updateBatteryUI(payload.voltage);
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
    const ex = payload.exercise || currentExercise;
    const exMap = ensureDailyExerciseState();
    appendExerciseLog(`Set ${payload.set} complete (${payload.reps ?? '?'} reps)`);
    if (ex === 'bicep_curls') {
      const bc = exMap.bicep_curls;
      if (bc.leftInSet > 0 || bc.rightInSet > 0) {
        bc.set = (bc.set || 1) + 1;
        bc.leftInSet = 0;
        bc.rightInSet = 0;
        bc.repsInSet = 0;
        saveDailyExerciseState();
      }
      syncExerciseUI('bicep_curls');
    } else if (ex === 'pushups' || ex === 'squats') {
      const st = exMap[ex];
      if (st && st.repsInSet > 0) {
        st.set = (st.set || 1) + 1;
        st.repsInSet = 0;
        saveDailyExerciseState();
      }
      syncExerciseUI(ex);
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
let localControlHost = null;
let localReconnectTimer = null;

function ensureLocalWebSocketConnected() {
  const urlToUse = currentCameraUrl || DEFAULT_CAMERA_URL;
  if (!urlToUse) return;
  try {
    const parsed = new URL(normalizeUrl(urlToUse));
    if (parsed.hostname) {
      connectLocalWebSocket(parsed.hostname, false);
    }
  } catch (e) { }
}

function connectLocalWebSocket(piIp, forceReconnect = false) {
  if (!piIp) return;

  if (localReconnectTimer) {
    clearTimeout(localReconnectTimer);
    localReconnectTimer = null;
  }

  // Reuse existing socket if already open or connecting to the same host
  if (
    !forceReconnect &&
    localControlSocket &&
    localControlHost === piIp &&
    (localControlSocket.readyState === WebSocket.OPEN || localControlSocket.readyState === WebSocket.CONNECTING)
  ) {
    if (localControlSocket.readyState === WebSocket.OPEN) {
      setRobotConnectionStatus(true);
    }
    return;
  }

  if (localControlSocket) {
    try {
      const oldSocket = localControlSocket;
      oldSocket.onopen = null;
      oldSocket.onmessage = null;
      oldSocket.onerror = null;
      oldSocket.onclose = null;
      oldSocket.close();
    } catch (e) { }
    localControlSocket = null;
  }

  localControlHost = piIp;
  console.log(`🔌 Attempting to connect to local robot control at ws://${piIp}:8001`);
  let ws;
  try {
    ws = new WebSocket(`ws://${piIp}:8001`);
  } catch (err) {
    console.warn("⚠️ Could not create local WebSocket:", err);
    refreshRobotOnlineStatus();
    return;
  }
  localControlSocket = ws;

  ws.onopen = () => {
    if (localControlSocket !== ws) return;
    console.log("✅ Local WebSocket control channel connected");
    appendSystemLog("Connected to local robot control channel");
    lastRobotTelemetryAt = Date.now();
    setRobotConnectionStatus(true);
    if (currentUserId) {
      try {
        ws.send(JSON.stringify({
          action: 'identify',
          user_id: currentUserId,
          user_email: currentUserEmail
        }));
      } catch (err) { }
    }
    updateExerciseButtonStates();
  };

  ws.onmessage = (event) => {
    if (localControlSocket !== ws) return;
    try {
      const data = JSON.parse(event.data);
      console.log("📩 Received response from local robot:", data);
      lastRobotTelemetryAt = Date.now();
      setRobotConnectionStatus(true);
      if (data.type) {
        handleTelemetryMessage(data);
      }
    } catch (e) {
      console.warn("⚠️ Failed to parse local robot response:", event.data);
    }
  };

  ws.onclose = () => {
    if (localControlSocket !== ws) return;
    console.log("❌ Local WebSocket control channel closed");
    localControlSocket = null;
    refreshRobotOnlineStatus();

    // Automatically attempt to reconnect while user is signed in
    if (currentUserId && localControlHost) {
      localReconnectTimer = setTimeout(() => {
        connectLocalWebSocket(localControlHost, false);
      }, 4000);
    }
  };
}

let hasShownLowBatteryModal = false;

function checkBatteryWarning(v, percent) {
  if (v <= 9.8) {
    if (!hasShownLowBatteryModal) {
      showBatteryChargeModal(v, percent);
      hasShownLowBatteryModal = true;
    }
  } else if (v >= 10.1) {
    // Reset warning threshold once recharged above 10.1V
    hasShownLowBatteryModal = false;
  }
}

function showBatteryChargeModal(v, percent) {
  const modal = document.getElementById('batteryChargeModal');
  if (!modal) return;
  const voltSpan = document.getElementById('chargeModalVoltage');
  const pctSpan = document.getElementById('chargeModalPercent');
  if (voltSpan) voltSpan.textContent = `${v.toFixed(2)}V`;
  if (pctSpan) pctSpan.textContent = `${percent}%`;
  modal.classList.remove('hidden');
}

function closeBatteryChargeModal() {
  const modal = document.getElementById('batteryChargeModal');
  if (modal) modal.classList.add('hidden');
}

function openMobileTracker(e) {
  try {
    const returnUrl = window.location.origin + window.location.pathname + '?tab=steps&subtab=steps#steps';
    const target = new URL('https://admirable-duckanoo-81d784.netlify.app/phone_tracker.html');
    target.searchParams.set('dashboard', returnUrl);
    window.location.href = target.toString();
    if (e) e.preventDefault();
  } catch (_) {}
}

function updateMobileTrackerLink() {
  const btn = document.getElementById('mobileTrackerBtn');
  if (btn) {
    try {
      const returnUrl = window.location.origin + window.location.pathname + '?tab=steps&subtab=steps#steps';
      btn.href = `https://admirable-duckanoo-81d784.netlify.app/phone_tracker.html?dashboard=${encodeURIComponent(returnUrl)}`;
    } catch (_) {}
  }
}

function updateBatteryUI(voltage) {
  const v = parseFloat(voltage);
  if (isNaN(v)) return;
  lastKnownBatteryVoltage = v;

  const textEl = document.getElementById('batteryVoltageText');
  const fill = document.getElementById('batteryLevelFill');
  const indicator = document.getElementById('batteryIndicator');

  // Robot battery range: 9.0V (0%) to 10.6V (100%)
  const minV = 9.0;
  const maxV = 10.6;
  let percent = Math.round(((v - minV) / (maxV - minV)) * 100);
  percent = Math.max(0, Math.min(100, percent));

  if (textEl) {
    textEl.textContent = (batteryDisplayMode === 'voltage') ? `${v.toFixed(1)}V` : `${percent}%`;
  }

  if (fill) {
    fill.style.height = `${percent}%`;
    if (v <= 9.8 || percent < 20) {
      fill.style.background = '#ef4444'; // red
      if (textEl) textEl.style.color = '#f87171';
    } else if (percent < 50) {
      fill.style.background = '#f59e0b'; // amber
      if (textEl) textEl.style.color = '#fbbf24';
    } else {
      fill.style.background = '#10b981'; // emerald
      if (textEl) textEl.style.color = '#34d399';
    }
  }

  if (indicator) {
    indicator.classList.remove('hidden');
    indicator.title = `Robot Battery: ${v.toFixed(2)} V (${percent}%) - Range: 9.0V (0%) to 10.6V (100%) - Click to toggle V/%`;
  }

  // Pop up a warning message to charge the robot when battery drops to 9.8V or below
  checkBatteryWarning(v, percent);
}

function toggleBatteryDisplayMode() {
  batteryDisplayMode = (batteryDisplayMode === 'percent') ? 'voltage' : 'percent';
  if (lastKnownBatteryVoltage !== null) {
    updateBatteryUI(lastKnownBatteryVoltage);
  }
}

function updateExerciseButtonStates() {
  const startBtn = document.getElementById('startExerciseBtn');
  const stopBtn = document.getElementById('fitnessStopBtn');
  const upperState = (robotState || '').toUpperCase();
  const select = document.getElementById('fitnessExerciseSelect');
  const isActive = (upperState === 'ACTIVE');

  if (startBtn) {
    startBtn.classList.remove('opacity-50', 'opacity-60', 'cursor-not-allowed');
    startBtn.classList.add('opacity-100', 'cursor-pointer');
    startBtn.title = isActive ? 'Restart / trigger start detecting' : `Start detecting ${formatExercise(currentExercise || select?.value || 'exercise')}`;
  }

  if (stopBtn) {
    stopBtn.classList.remove('opacity-50', 'cursor-not-allowed');
    stopBtn.classList.add('opacity-100', 'cursor-pointer');
    stopBtn.title = isActive ? 'Stop current exercise' : 'Stop exercise / reset robot';
  }
}

async function sendForceStop() {
  const upperState = (robotState || '').toUpperCase();

  appendExerciseLog(`⛔ Stopping exercise (${formatExercise(currentExercise)})...`);

  robotState = 'STOPPED';
  const pill = document.getElementById('robotState');
  if (pill) {
    pill.textContent = 'STOPPED';
    pill.setAttribute('data-state', 'STOPPED');
  }
  resetExerciseUI(currentExercise);
  updateExerciseButtonStates();

  await sendControlCommand({ action: 'force_stop' }, 'Exercise stopped');
}

async function sendStartExercise() {
  const select = document.getElementById('fitnessExerciseSelect');
  const selectedExercise = currentExercise || (select ? select.value : 'bicep_curls');

  // 1. Must have an exercise selected
  if (!selectedExercise) {
    appendExerciseLog("⚠️ Please select an exercise first before starting.", true);
    return;
  }

  toggleCameraView(true);
  resetExerciseUI(selectedExercise);

  appendExerciseLog(`🚀 Starting ${formatExercise(selectedExercise)}! Detecting form and counting reps...`);

  robotState = 'ACTIVE';
  const pill = document.getElementById('robotState');
  if (pill) {
    pill.textContent = 'ACTIVE';
    pill.setAttribute('data-state', 'ACTIVE');
  }
  updateExerciseButtonStates();

  const mapping = {
    bicep_curls: 'BICEP_CURLS',
    pushups: 'PUSHUPS',
    squats: 'SQUATS',
    plank: 'PLANK',
    person_follow: 'PERSON_FOLLOW'
  };

  const upperState = (robotState || '').toUpperCase();
  const isWaitingInExercise = (upperState === 'FOLLOWING' || upperState === 'LOADING');
  if (!isWaitingInExercise) {
    await sendControlCommand({
      action: 'switch_mode',
      mode: mapping[selectedExercise] || selectedExercise.toUpperCase()
    }, `Selected ${formatExercise(selectedExercise)}. Launching exercise...`);
  }

  await sendControlCommand({
    action: 'start_exercise',
    exercise: selectedExercise
  }, `Started ${formatExercise(selectedExercise)}`);
}

async function switchMode(mode) {
  if (robotState === 'ACTIVE' && currentExercise === mode) {
    return;
  }

  currentExercise = mode;
  resetExerciseUI(currentExercise);
  syncExerciseUI(currentExercise);

  robotState = 'ACTIVE';
  const pill = document.getElementById('robotState');
  if (pill) {
    pill.textContent = 'ACTIVE';
    pill.setAttribute('data-state', 'ACTIVE');
  }
  updateExerciseButtonStates();

  const mapping = {
    bicep_curls: 'BICEP_CURLS',
    pushups: 'PUSHUPS',
    squats: 'SQUATS',
    plank: 'PLANK',
    person_follow: 'PERSON_FOLLOW'
  };
  const targetMode = mapping[mode] || mode.toUpperCase();

  await sendControlCommand({
    action: 'switch_mode',
    mode: targetMode,
    exercise: targetMode
  }, `Selected ${formatExercise(mode)}. Robot switching to exercise counting...`);
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

function setCameraStream(forceReload = false) {
  const cameraUrlEl = document.getElementById('cameraUrl');
  const rawUrl = (cameraUrlEl ? cameraUrlEl.value.trim() : '') || currentCameraUrl;
  if (!rawUrl) return;

  const url = normalizeUrl(rawUrl);
  // Update the input field with the normalized URL
  if (cameraUrlEl) cameraUrlEl.value = url;

  const img = document.getElementById('cameraFeed');
  const placeholder = document.getElementById('cameraPlaceholder');

  const urlChanged = (currentCameraUrl !== url);
  currentCameraUrl = url;
  localStorage.setItem('currentCameraUrl', url); // Save to localStorage

  if (img && (forceReload || urlChanged || img.getAttribute('data-stream-url') !== url || img.classList.contains('hidden'))) {
    img.setAttribute('data-stream-url', url);
    img.src = url;
    img.onload = () => {
      isCameraStreamLoaded = true;
      img.classList.remove('hidden');
      if (placeholder) placeholder.classList.add('hidden');
      setRobotConnectionStatus(true);
    };
    img.onerror = () => {
      isCameraStreamLoaded = false;
      img.classList.add('hidden');
      if (placeholder) {
        placeholder.classList.remove('hidden');
        placeholder.innerHTML = '<div class="text-red-400">Failed to load stream.<br>Check Pi MJPEG server is running.</div>';
      }
      refreshRobotOnlineStatus();
    };
  }

  // Extract hostname or IP address to connect to WebSocket control server
  try {
    const parsedUrl = new URL(url);
    const hostname = parsedUrl.hostname;
    if (hostname) {
      connectLocalWebSocket(hostname, urlChanged);
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
  setCameraStream(false);
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
    setCameraStream(true);
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
function recordLiveRep(exercise, count, left, right, goodIncrement = 1) {
  if (!exercise) return;
  const todayStr = dateKey(new Date());
  let dayData = cachedDailyMap.get(todayStr);
  if (!dayData) {
    dayData = emptyDaily(todayStr);
    cachedDailyMap.set(todayStr, dayData);
  }
  if (!dayData.reps) dayData.reps = {};

  const repNum = Math.max(0, Number(count || 1));
  dayData.reps[exercise] = Math.max(Number(dayData.reps[exercise] || 0), repNum);
  const inc = Math.max(1, Number(goodIncrement || 1));
  const totalDayReps = Number(dayData.reps.bicep_curls || 0) + Number(dayData.reps.pushups || 0) + Number(dayData.reps.squats || 0);
  dayData.form_good_count = Math.max(Number(dayData.form_good_count || 0) + inc, totalDayReps);

  if (db && currentUserId) {
    db.collection('users').doc(currentUserId)
      .collection('daily_aggregates')
      .doc(todayStr)
      .set({
        date: todayStr,
        reps: dayData.reps,
        form_good_count: dayData.form_good_count,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true }).catch(() => { });
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
      }, { merge: true }).catch(() => { });
  }

  updateProfileViewForSelectedDate();
}

function recordLiveFormError(message) {
  const msgUpper = String(message || '').toUpperCase();
  // Ignore camera positioning instructions so stepping into frame doesn't mark exercises as flawed
  if (msgUpper.includes('GET IN POSITION') || msgUpper.includes('POSITIONING') || msgUpper.includes('STAND IN FRONT')) {
    return;
  }

  const now = Date.now();
  if (now - lastFormFlawRecordedAt < 3000) {
    return;
  }
  lastFormFlawRecordedAt = now;

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
      }, { merge: true }).catch(() => { });
  }

  updateProfileViewForSelectedDate();
}

function recordLiveSessionEnd(activeSec, payload) {
  const todayStr = dateKey(new Date());
  let dayData = cachedDailyMap.get(todayStr);
  if (!dayData) {
    dayData = emptyDaily(todayStr);
    cachedDailyMap.set(todayStr, dayData);
  }
  const sec = Math.round(Number(activeSec || 0));
  dayData.active_time_s = (dayData.active_time_s || 0) + sec;
  dayData.total_time_s = (dayData.total_time_s || 0) + sec;

  // Sync with today's persistent per-exercise state
  const exMap = ensureDailyExerciseState();
  if (!dayData.reps) dayData.reps = {};
  dayData.reps.bicep_curls = Math.max(Number(dayData.reps.bicep_curls || 0), Number(exMap.bicep_curls?.totalReps || 0));
  dayData.reps.pushups = Math.max(Number(dayData.reps.pushups || 0), Number(exMap.pushups?.totalReps || 0));
  dayData.reps.squats = Math.max(Number(dayData.reps.squats || 0), Number(exMap.squats?.totalReps || 0));
  dayData.plank_time_s = Math.max(Number(dayData.plank_time_s || 0), Math.round(Number(exMap.plank?.plankTimeS || 0)));

  // Extract from summary if present
  if (payload && payload.summary) {
    const sum = payload.summary;
    const ex = payload.exercise_type || currentExercise;
    if (ex === 'plank') {
      let pTime = Number(sum.total_plank_time_s || sum.plank_time_s || 0);
      if (!pTime && Array.isArray(sum.metrics)) {
        for (const [mName, mVal] of sum.metrics) {
          if (String(mName).includes('Hold Time')) {
            pTime = parseFloat(String(mVal).replace('s', '')) || 0;
          }
        }
      }
      if (pTime > 0) {
        dayData.plank_time_s = Math.max(Number(dayData.plank_time_s || 0), Math.round(pTime));
      }
    } else if (sum.total_reps !== undefined && ex) {
      dayData.reps[ex] = Math.max(Number(dayData.reps[ex] || 0), Number(sum.total_reps));
    }
  }

  const totalDayReps = Number(dayData.reps.bicep_curls || 0) + Number(dayData.reps.pushups || 0) + Number(dayData.reps.squats || 0);
  if (totalDayReps > 0) {
    dayData.form_good_count = Math.max(Number(dayData.form_good_count || 0), totalDayReps);
  }

  if (db && currentUserId) {
    db.collection('users').doc(currentUserId)
      .collection('daily_aggregates')
      .doc(todayStr)
      .set({
        date: todayStr,
        active_time_s: dayData.active_time_s,
        total_time_s: dayData.total_time_s,
        reps: dayData.reps || {},
        plank_time_s: dayData.plank_time_s || 0,
        form_good_count: dayData.form_good_count || 0,
        updated_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true }).catch(() => { });
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
    const todayStr = dateKey(new Date());
    const prevToday = cachedDailyMap.get(todayStr);

    cachedDailyMap.clear();

    const { days } = getSevenDaysWindow();
    days.forEach(d => {
      const k = dateKey(d);
      cachedDailyMap.set(k, emptyDaily(k));
    });

    cachedAggDocs.forEach(data => {
      const k = data.date || data._docId;
      if (k) {
        const base = emptyDaily(k);
        const mergedReps = {
          bicep_curls: Math.max(Number(data.reps?.bicep_curls || 0), Number(data['reps.bicep_curls'] || 0)),
          pushups: Math.max(Number(data.reps?.pushups || 0), Number(data['reps.pushups'] || 0)),
          squats: Math.max(Number(data.reps?.squats || 0), Number(data['reps.squats'] || 0))
        };
        cachedDailyMap.set(k, {
          ...base,
          ...data,
          reps: mergedReps
        });
      }
    });

    // Group and SUM all completed sessions by day
    const sessionsByDay = new Map();
    cachedSessionDocs.forEach(s => {
      if (s.cleared) return;
      const rawDate = s.started_at || s.ended_at;
      if (!rawDate) return;
      const sDate = rawDate.toDate ? rawDate.toDate() : new Date(rawDate);
      const sKey = dateKey(sDate);
      if (!sessionsByDay.has(sKey)) {
        sessionsByDay.set(sKey, {
          reps: { bicep_curls: 0, pushups: 0, squats: 0 },
          plank_time_s: 0,
          active_time_s: 0,
          total_time_s: 0
        });
      }
      const daySum = sessionsByDay.get(sKey);
      const sum = s.summary || {};
      const ex = s.exercise_type || sum.exercise;

      if (ex === 'plank') {
        let pSec = Number(sum.total_plank_time_s || sum.plank_time_s || 0);
        if (!pSec && Array.isArray(sum.metrics)) {
          for (const [mName, mVal] of sum.metrics) {
            if (String(mName).includes('Hold Time')) {
              pSec = parseFloat(String(mVal).replace('s', '')) || 0;
            }
          }
        }
        daySum.plank_time_s += pSec;
      } else if (ex && (sum.total_reps !== undefined || sum.reps !== undefined)) {
        const rCount = Number(sum.total_reps ?? sum.reps ?? 0);
        daySum.reps[ex] = (daySum.reps[ex] || 0) + rCount;
      }

      if (s.active_duration_s) {
        daySum.active_time_s += Number(s.active_duration_s || 0);
      }
      if (s.total_duration_s) {
        daySum.total_time_s += Number(s.total_duration_s || 0);
      }
    });

    // Merge sessions sums with daily_aggregates (take max of incremental vs summed sessions to ensure no reps missed)
    days.forEach(d => {
      const k = dateKey(d);
      const day = cachedDailyMap.get(k) || emptyDaily(k);
      if (!day.reps) day.reps = { bicep_curls: 0, pushups: 0, squats: 0 };
      const sSum = sessionsByDay.get(k);
      if (sSum) {
        day.reps.bicep_curls = Math.max(Number(day.reps.bicep_curls || 0), Number(sSum.reps.bicep_curls || 0));
        day.reps.pushups = Math.max(Number(day.reps.pushups || 0), Number(sSum.reps.pushups || 0));
        day.reps.squats = Math.max(Number(day.reps.squats || 0), Number(sSum.reps.squats || 0));
        day.plank_time_s = Math.max(Number(day.plank_time_s || 0), Math.round(Number(sSum.plank_time_s || 0)));
        day.active_time_s = Math.max(Number(day.active_time_s || 0), Math.round(Number(sSum.active_time_s || 0)));
        day.total_time_s = Math.max(Number(day.total_time_s || 0), Math.round(Number(sSum.total_time_s || 0)));
      }

      const dayTotalReps = Number(day.reps.bicep_curls || 0) + Number(day.reps.pushups || 0) + Number(day.reps.squats || 0);
      if (dayTotalReps > 0) {
        day.form_good_count = Math.max(Number(day.form_good_count || 0), dayTotalReps);
      }
      cachedDailyMap.set(k, day);
    });

    // Preserve live today progress and sync with persistent daily exercise state
    const todayAgg = cachedDailyMap.get(todayStr) || emptyDaily(todayStr);
    const exMap = ensureDailyExerciseState();
    if (!todayAgg.reps) todayAgg.reps = { bicep_curls: 0, pushups: 0, squats: 0 };

    if (prevToday) {
      todayAgg.reps.bicep_curls = Math.max(Number(todayAgg.reps.bicep_curls || 0), Number(prevToday.reps?.bicep_curls || 0));
      todayAgg.reps.pushups = Math.max(Number(todayAgg.reps.pushups || 0), Number(prevToday.reps?.pushups || 0));
      todayAgg.reps.squats = Math.max(Number(todayAgg.reps.squats || 0), Number(prevToday.reps?.squats || 0));
      todayAgg.plank_time_s = Math.max(Number(todayAgg.plank_time_s || 0), Number(prevToday.plank_time_s || 0));
      todayAgg.form_good_count = Math.max(Number(todayAgg.form_good_count || 0), Number(prevToday.form_good_count || 0));
      todayAgg.form_flaw_count = Math.max(Number(todayAgg.form_flaw_count || 0), Number(prevToday.form_flaw_count || 0));
    }

    todayAgg.reps.bicep_curls = Math.max(Number(todayAgg.reps.bicep_curls || 0), Number(exMap.bicep_curls?.totalReps || 0));
    todayAgg.reps.pushups = Math.max(Number(todayAgg.reps.pushups || 0), Number(exMap.pushups?.totalReps || 0));
    todayAgg.reps.squats = Math.max(Number(todayAgg.reps.squats || 0), Number(exMap.squats?.totalReps || 0));
    todayAgg.plank_time_s = Math.max(Number(todayAgg.plank_time_s || 0), Math.round(Number(exMap.plank?.plankTimeS || 0)));

    const totalTodayReps = Number(todayAgg.reps.bicep_curls || 0) + Number(todayAgg.reps.pushups || 0) + Number(todayAgg.reps.squats || 0);
    if (totalTodayReps > 0) {
      todayAgg.form_good_count = Math.max(Number(todayAgg.form_good_count || 0), totalTodayReps);
    }

    // Hydrate dailyExerciseState if cloud has higher totals for today (e.g. after page reload)
    let stateUpdated = false;
    ['bicep_curls', 'pushups', 'squats'].forEach(exKey => {
      const cloudReps = Number(todayAgg.reps[exKey] || 0);
      const localSt = exMap[exKey];
      if (localSt && cloudReps > (localSt.totalReps || 0)) {
        localSt.totalReps = cloudReps;
        localSt.set = Math.floor(cloudReps / 8) + 1;
        localSt.repsInSet = cloudReps % 8;
        if (exKey === 'bicep_curls') {
          localSt.leftInSet = localSt.repsInSet;
          localSt.rightInSet = localSt.repsInSet;
          localSt.totalLeft = Math.max(localSt.totalLeft || 0, cloudReps);
          localSt.totalRight = Math.max(localSt.totalRight || 0, cloudReps);
        }
        stateUpdated = true;
      }
    });
    if (Number(todayAgg.plank_time_s || 0) > Number(exMap.plank?.plankTimeS || 0)) {
      exMap.plank.plankTimeS = Number(todayAgg.plank_time_s || 0);
      stateUpdated = true;
    }
    if (stateUpdated) {
      saveDailyExerciseState();
      syncExerciseUI(currentExercise);
    }

    if (watchStepsToday > 0) {
      todayAgg.steps = Math.max(todayAgg.steps || 0, watchStepsToday);
    }
    cachedDailyMap.set(todayStr, todayAgg);

    updateProfileViewForSelectedDate();
  }

  // Listener 1: daily_aggregates (updates when Pi writes a rep or session ends)
  analyticsAggUnsubscribe = db.collection('users').doc(currentUserId)
    .collection('daily_aggregates')
    .where('date', '>=', dateKey(minDate))
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

  // 1. Clear in-memory caches immediately
  cachedAggDocs = [];
  cachedSessionDocs = [];
  cachedDailyMap.clear();
  watchStepsToday = 0;

  // 2. Clear local storage step history & update step history list
  localStorage.removeItem(STEP_HISTORY_KEY);
  if (typeof renderStepHistory === 'function') {
    renderStepHistory();
  }

  // 3. Clear cloud collections in Firestore
  if (db && currentUserId) {
    try {
      const userDocRef = db.collection('users').doc(currentUserId);
      const collectionsToClear = ['daily_aggregates', 'sessions', 'events', 'workout_sessions', 'watch_data'];

      for (const collName of collectionsToClear) {
        try {
          const snap = await userDocRef.collection(collName).get();
          if (!snap.empty) {
            const docs = snap.docs;
            for (let i = 0; i < docs.length; i += 400) {
              const batch = db.batch();
              const slice = docs.slice(i, i + 400);
              slice.forEach(d => batch.delete(d.ref));
              try {
                await batch.commit();
              } catch (delErr) {
                console.warn(`Direct delete failed for ${collName}, falling back to zero-resetting:`, delErr);
                // Fallback in case delete permission is blocked: reset values to zero so they don't load again
                const resetBatch = db.batch();
                slice.forEach(d => {
                  if (collName === 'daily_aggregates') {
                    resetBatch.set(d.ref, emptyDaily(d.id));
                  } else if (collName === 'sessions' || collName === 'workout_sessions') {
                    resetBatch.set(d.ref, { summary: {}, cleared: true, total_duration_s: 0, active_duration_s: 0 }, { merge: true });
                  } else if (collName === 'watch_data') {
                    resetBatch.set(d.ref, { steps: 0 });
                  }
                });
                await resetBatch.commit().catch(() => {});
              }
            }
          }
        } catch (subErr) {
          console.warn(`Error clearing collection ${collName}:`, subErr);
        }
      }
      appendSystemLog("Workout history cleared from cloud.");
    } catch (err) {
      console.error("Error clearing history in Firestore:", err);
      appendSystemLog(`Error clearing history: ${err.message}`, true);
    }
  }

  // 4. Re-populate 7-day window with zeroed daily maps
  const { days } = getSevenDaysWindow();
  days.forEach(d => {
    const k = dateKey(d);
    cachedDailyMap.set(k, emptyDaily(k));
  });

  // 5. Update KPI and charts
  updateProfileViewForSelectedDate();

  // 6. Reset live exercise metrics and logs in the Fitness tab
  resetAllExercisesDailyState();
  const exLog = document.getElementById('exerciseLog');
  if (exLog) exLog.innerHTML = '';

  appendSystemLog("Workout history has been cleared.");
  appendExerciseLog("Workout history has been cleared.");

  // Non-intrusive feedback on the Clear History button
  const clearBtn = document.getElementById('clearHistoryBtn');
  if (clearBtn) {
    const originalContent = clearBtn.innerHTML;
    clearBtn.innerHTML = `
      <svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path>
      </svg>
      <span class="text-emerald-400 font-bold">Cleared!</span>
    `;
    setTimeout(() => {
      clearBtn.innerHTML = originalContent;
    }, 2500);
  }
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
  const form = document.getElementById('kpiForm')?.textContent || '100%';
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

function renderWristbandDeviceUI(device, status) {
  const container = document.getElementById('wristbandDeviceList');
  const emptyNotice = document.getElementById('wristbandEmptyNotice');
  const scanStatus = document.getElementById('wristbandScanStatus');
  if (!container) return;

  if (emptyNotice) emptyNotice.remove();
  if (scanStatus) scanStatus.textContent = status || 'paired';

  const devId = (device.id || 'bt-wristband').replace(/[^a-zA-Z0-9_-]/g, '_');
  let card = document.getElementById(`wb-device-${devId}`);
  if (!card) {
    card = document.createElement('div');
    card.id = `wb-device-${devId}`;
    card.className = 'p-2 rounded-xl bg-white/5 border border-white/10 flex items-center justify-between transition-all';
    container.appendChild(card);
  }

  const isConn = (status === 'connected');
  const devName = device.name || 'ESP32 Wristband';

  card.innerHTML = `
    <div class="flex items-center gap-2 min-w-0">
      <span class="text-base">${isConn ? '⌚' : '📡'}</span>
      <div class="min-w-0">
        <div class="font-bold text-xs text-white truncate">${escapeHtml(devName)}</div>
        <div class="text-[10px] text-white/50 flex items-center gap-1.5 font-mono">
          <span class="w-1.5 h-1.5 rounded-full ${isConn ? 'bg-emerald-400 animate-pulse' : 'bg-white/40'}"></span>
          <span>${isConn ? 'Connected' : 'Paired'}</span>
        </div>
      </div>
    </div>
    <div class="flex items-center gap-1.5">
      ${isConn ? `
        <button onclick="disconnectStepTracker()" class="px-2 py-0.5 rounded-lg bg-red-500/20 hover:bg-red-500/30 text-red-300 text-[10px] font-bold border border-red-500/30 transition-all active:scale-95">
          Disconnect
        </button>
      ` : `
        <button onclick="connectStepTracker()" class="px-2 py-0.5 rounded-lg bg-cyan-500/20 hover:bg-cyan-500/30 text-cyan-300 text-[10px] font-bold border border-cyan-500/30 transition-all active:scale-95">
          Connect
        </button>
      `}
    </div>
  `;
}

async function loadPermittedWristbandDevices() {
  if (!navigator.bluetooth || !navigator.bluetooth.getDevices) return;
  try {
    const devices = await navigator.bluetooth.getDevices();
    if (devices && devices.length > 0) {
      devices.forEach(dev => {
        renderWristbandDeviceUI(dev, (dev.gatt && dev.gatt.connected) ? 'connected' : 'paired');
      });
    }
  } catch (err) {
    console.debug('getDevices check:', err);
  }
}

async function connectStepTracker() {
  if (!navigator.bluetooth) {
    stepLog('Web Bluetooth is not supported in this browser. Use Chrome or Edge.');
    setStepStatus('disconnected');
    const scanStatus = document.getElementById('wristbandScanStatus');
    if (scanStatus) scanStatus.textContent = 'unsupported';
    return;
  }

  try {
    setStepStatus('connecting');
    const scanStatus = document.getElementById('wristbandScanStatus');
    if (scanStatus) scanStatus.textContent = 'showing available devices...';
    stepLog('Opening Bluetooth pairing prompt (showing all available devices)...');

    // Shows ALL available nearby Bluetooth devices for pairing
    stepDevice = await requestStepDevice();
    stepLog(`Selected: ${stepDevice.name || 'Unknown Device'}`);
    renderWristbandDeviceUI(stepDevice, 'connecting');
    stepDevice.addEventListener('gattserverdisconnected', onStepDisconnected);

    stepServer = await stepDevice.gatt.connect();
    stepLog('GATT connected');

    try {
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
    } catch (svcErr) {
      stepLog(`Service notice: ${svcErr.message || svcErr}`);
    }

    stepConnected = true;
    setStepStatus('connected');
    setWatchConnectionStatus(true);
    renderWristbandDeviceUI(stepDevice, 'connected');
    stepLog('Connected successfully');

    setTimeout(async () => {
      await syncStepTime();
      await requestStepData();
    }, 500);
  } catch (error) {
    stepLog(`Connection error: ${error.message}`);
    setStepStatus('disconnected');
    const scanStatus = document.getElementById('wristbandScanStatus');
    if (scanStatus) scanStatus.textContent = 'idle';
  }
}

async function requestStepDevice() {
  // Directly use acceptAllDevices: true so Chrome/Edge displays ALL available Bluetooth devices in the pairing popup
  return await navigator.bluetooth.requestDevice({
    acceptAllDevices: true,
    optionalServices: [
      STEP_SERVICE_UUID,
      'battery_service',
      'heart_rate',
      'device_information'
    ]
  });
}

function disconnectStepTracker() {
  if (stepDevice && stepDevice.gatt && stepDevice.gatt.connected) {
    stepDevice.gatt.disconnect();
  }
  stepConnected = false;
  setStepStatus('disconnected');
  setWatchConnectionStatus(false);
  const scanStatus = document.getElementById('wristbandScanStatus');
  if (scanStatus) scanStatus.textContent = 'disconnected';
  if (stepDevice) {
    renderWristbandDeviceUI(stepDevice, 'disconnected');
  }
  stepLog('Disconnected from wristband');
}

function onStepDisconnected() {
  stepConnected = false;
  setStepStatus('disconnected');
  setWatchConnectionStatus(false);
  const scanStatus = document.getElementById('wristbandScanStatus');
  if (scanStatus) scanStatus.textContent = 'disconnected';
  if (stepDevice) {
    renderWristbandDeviceUI(stepDevice, 'disconnected');
  }
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
  if (!date) return '';
  if (typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return date;
  }
  const d = (date instanceof Date) ? date : (date && date.toDate ? date.toDate() : new Date(date));
  if (isNaN(d.getTime())) return '';
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
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
  updateMobileTrackerLink();
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

  // Initialize Header Scroll transformation (transforms into solid non-transparent menu with bottom line)
  initHeaderScrollListener();

  // Handle back/forward and hash navigation (e.g. from Phone Tracker "Go to Dashboard")
  window.addEventListener('hashchange', () => {
    if (auth && auth.currentUser) {
      navigateFromUrl();
    }
  });
  window.addEventListener('popstate', () => {
    if (auth && auth.currentUser) {
      navigateFromUrl();
    }
  });

  // Load any previously permitted wristband Bluetooth devices
  loadPermittedWristbandDevices();
})();

function initHeaderScrollListener() {
  const header = document.querySelector('.app-header');
  if (!header) return;
  const updateScroll = () => {
    if (isDesktop() && window.scrollY > 15) {
      header.classList.add('header-scrolled');
    } else {
      header.classList.remove('header-scrolled');
    }
  };
  window.addEventListener('scroll', updateScroll, { passive: true });
  updateScroll();

  // Keep camera state & header scroll synchronized on viewport resize between desktop & mobile
  window.addEventListener('resize', () => {
    updateScroll();
    const wrapper = document.getElementById('cameraViewCardWrapper');
    const btn = document.getElementById('cameraToggleBtn');
    if (isDesktop()) {
      if (btn) btn.classList.remove('camera-btn-active');
      startCameraStreamIfNeeded();
    } else {
      if (isCameraActive) {
        if (wrapper) wrapper.classList.add('mobile-active');
        if (btn) btn.classList.add('camera-btn-active');
      } else {
        if (wrapper) wrapper.classList.remove('mobile-active');
        if (btn) btn.classList.remove('camera-btn-active');
      }
    }
  });
}