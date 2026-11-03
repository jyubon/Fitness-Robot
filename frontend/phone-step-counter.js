/**
 * PhoneStepTracker - Smartphone Pedometer Module
 * 
 * Replaces or supplements the ESP32-C3 wristband using the smartphone's
 * built-in motion sensors (DeviceMotionEvent / Accelerometer).
 * 
 * Features:
 * - Low-pass filtered 3D acceleration magnitude calculation
 * - Dynamic peak/valley step detection with debounce & arming window
 * - Screen WakeLock to keep pedometer running while walking with screen on
 * - Haptic feedback support on step / milestones
 * - LocalStorage state preservation per calendar day (auto midnight reset)
 * - Desktop/Laptop simulation mode for testing without a mobile device
 * - Seamless integration bridge to Firebase Firestore (watch_data) and Fitness Robot UI
 */

(function (global) {
  'use strict';

  class PhoneStepTracker {
    constructor(options = {}) {
      this.stepGoal = options.stepGoal || 10000;
      this.calPerStep = options.calPerStep || 0.04; // Matches ESP32 firmware
      this.metersPerStep = options.metersPerStep || 0.75; // Matches ESP32 firmware
      this.minStepIntervalMs = options.minStepIntervalMs || 270; // Debounce window
      this.minArmWindowMs = options.minArmWindowMs || 45; // False peak rejection
      this.hapticEnabled = options.hapticEnabled !== false;

      // Sensitivity presets (thresholds in m/s²)
      this.sensitivities = {
        high: { peak: 10.8, valley: 9.2, label: 'High (Pocket / Gentle)' },
        normal: { peak: 11.8, valley: 9.0, label: 'Normal (Hand / Standard)' },
        low: { peak: 13.0, valley: 8.6, label: 'Low (Armband / Vigorous)' }
      };
      this.sensitivity = options.sensitivity || 'normal';

      // Low-pass filter factor (EMA alpha)
      this.alpha = options.alpha || 0.28;

      // Filtered sensor values
      this.filteredAx = 0;
      this.filteredAy = 9.8;
      this.filteredAz = 0;

      // Detection state
      this.isTracking = false;
      this.stepFlag = false;
      this.stepArmed = false;
      this.stepArmTime = 0;
      this.lastStepTime = 0;
      this.lastActiveCheck = 0;
      this.activeSeconds = 0;

      // Metrics
      this.steps = 0;
      this.calories = 0;
      this.distanceKm = 0;
      this.activeMin = 0;

      // Background / Wake Lock / Simulation
      this.wakeLock = null;
      this.simInterval = null;
      this._handleMotionBound = this._handleMotion.bind(this);
      this._onVisibilityChange = this._handleVisibilityChange.bind(this);

      // Callbacks
      this.onStepCallbacks = [];
      this.onStatusChangeCallbacks = [];
      this.onLogCallbacks = [];

      // Load today's persisted data
      this._loadStoredData();

      // Listen for tab visibility to re-acquire wake lock if needed
      if (typeof document !== 'undefined') {
        document.addEventListener('visibilitychange', this._onVisibilityChange);
      }
    }

    // ==========================================
    // DATE & STORAGE HELPERS
    // ==========================================

    _getTodayKey() {
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }

    _storageKey() {
      return `phone_steps_${this._getTodayKey()}`;
    }

    _loadStoredData() {
      try {
        const raw = localStorage.getItem(this._storageKey());
        if (raw) {
          const parsed = JSON.parse(raw);
          this.steps = Number(parsed.steps) || 0;
          this.calories = Number(parsed.calories) || Number((this.steps * this.calPerStep).toFixed(1));
          this.distanceKm = Number(parsed.distanceKm) || Number(((this.steps * this.metersPerStep) / 1000.0).toFixed(2));
          this.activeSeconds = Number(parsed.activeSeconds) || 0;
          this.activeMin = Math.floor(this.activeSeconds / 60);
          this.log(`Loaded persisted step data for ${this._getTodayKey()}: ${this.steps} steps`);
        } else {
          this.steps = 0;
          this.calories = 0;
          this.distanceKm = 0;
          this.activeSeconds = 0;
          this.activeMin = 0;
        }
      } catch (e) {
        this.log(`Storage load error: ${e.message}`);
      }
    }

    _saveStoredData() {
      try {
        const payload = {
          date: this._getTodayKey(),
          steps: this.steps,
          calories: this.calories,
          distanceKm: this.distanceKm,
          activeSeconds: this.activeSeconds,
          activeMin: this.activeMin,
          lastUpdated: new Date().toISOString()
        };
        localStorage.setItem(this._storageKey(), JSON.stringify(payload));
      } catch (e) {
        // LocalStorage quota or private mode
      }
    }

    /**
     * Adopt baseline steps from cloud sync or ESP32 wristband if higher.
     * Enables seamless switching between wristband and mobile phone.
     */
    setInitialSteps(steps, calories, distanceKm, activeMin) {
      const numSteps = Number(steps) || 0;
      if (numSteps > this.steps) {
        this.steps = numSteps;
        this.calories = typeof calories === 'number' ? calories : Number((this.steps * this.calPerStep).toFixed(1));
        this.distanceKm = typeof distanceKm === 'number' ? distanceKm : Number(((this.steps * this.metersPerStep) / 1000.0).toFixed(2));
        if (typeof activeMin === 'number' && activeMin > this.activeMin) {
          this.activeMin = activeMin;
          this.activeSeconds = activeMin * 60;
        }
        this._saveStoredData();
        this.log(`Adopted external step baseline: ${this.steps} steps`);
      }
    }

    // ==========================================
    // PERMISSIONS & HARDWARE
    // ==========================================

    /**
     * Check if DeviceMotionEvent is available in this browser.
     */
    isSupported() {
      return typeof window !== 'undefined' && 'DeviceMotionEvent' in window;
    }

    /**
     * Request permission on iOS 13+ devices.
     * Must be invoked from a user click / gesture.
     */
    async requestPermission() {
      if (!this.isSupported()) {
        this.log('DeviceMotionEvent is not supported on this device/browser');
        return false;
      }

      if (typeof DeviceMotionEvent.requestPermission === 'function') {
        try {
          const response = await DeviceMotionEvent.requestPermission();
          if (response === 'granted') {
            this.log('Motion sensor permission granted (iOS)');
            return true;
          } else {
            this.log(`Motion sensor permission denied: ${response}`);
            this._notifyStatus('denied');
            return false;
          }
        } catch (err) {
          this.log(`Permission request error: ${err.message}`);
          this._notifyStatus('error');
          return false;
        }
      }

      // Android / standard desktop Chrome does not require explicit requestPermission
      return true;
    }

    // ==========================================
    // TRACKING CONTROLS
    // ==========================================

    /**
     * Start live motion tracking.
     */
    async start() {
      if (this.isTracking) return true;

      const perm = await this.requestPermission();
      if (!perm) return false;

      // Check if new day started since initialization
      this._loadStoredData();

      try {
        window.addEventListener('devicemotion', this._handleMotionBound, { passive: true });
        this.isTracking = true;
        this.stepFlag = false;
        this.stepArmed = false;
        this.lastStepTime = Date.now();
        this.lastActiveCheck = Date.now();

        await this._acquireWakeLock();

        this.log('Phone step tracking started');
        this._notifyStatus('tracking');
        this._emitStepData(false); // Emit current counts
        return true;
      } catch (err) {
        this.log(`Failed to start motion listener: ${err.message}`);
        this.isTracking = false;
        this._notifyStatus('error');
        return false;
      }
    }

    /**
     * Stop live motion tracking.
     */
    stop() {
      if (!this.isTracking && !this.simInterval) return;

      if (typeof window !== 'undefined') {
        window.removeEventListener('devicemotion', this._handleMotionBound);
      }

      this.stopSimulation();
      this._releaseWakeLock();

      this.isTracking = false;
      this.stepFlag = false;
      this.stepArmed = false;

      this.log('Phone step tracking stopped');
      this._notifyStatus('stopped');
    }

    /**
     * Reset step counts for today.
     */
    reset() {
      this.steps = 0;
      this.calories = 0;
      this.distanceKm = 0;
      this.activeSeconds = 0;
      this.activeMin = 0;
      this._saveStoredData();
      this.log('Steps reset to 0');
      this._emitStepData(true);
    }

    /**
     * Set sensitivity ('high' | 'normal' | 'low').
     */
    setSensitivity(level) {
      if (this.sensitivities[level]) {
        this.sensitivity = level;
        this.log(`Sensitivity set to ${level} (${this.sensitivities[level].label})`);
      }
    }

    // ==========================================
    // MOTION PROCESSING & STEP ALGORITHM
    // ==========================================

    _handleMotion(event) {
      if (!this.isTracking) return;

      const acc = event.accelerationIncludingGravity || event.acceleration;
      if (!acc) return;

      const rawX = acc.x || 0;
      const rawY = acc.y || 0;
      const rawZ = acc.z || 0;

      // Low-pass filter (Exponential Moving Average)
      this.filteredAx = this.alpha * rawX + (1 - this.alpha) * this.filteredAx;
      this.filteredAy = this.alpha * rawY + (1 - this.alpha) * this.filteredAy;
      this.filteredAz = this.alpha * rawZ + (1 - this.alpha) * this.filteredAz;

      // Compute total acceleration magnitude: |a| = sqrt(ax^2 + ay^2 + az^2)
      const magnitude = Math.sqrt(
        this.filteredAx * this.filteredAx +
        this.filteredAy * this.filteredAy +
        this.filteredAz * this.filteredAz
      );

      this._processStepDetection(magnitude);
      this._trackActiveTime(magnitude);
    }

    _processStepDetection(magnitude) {
      const now = Date.now();
      const thresholds = this.sensitivities[this.sensitivity] || this.sensitivities.normal;
      const peakThreshold = thresholds.peak;
      const valleyThreshold = thresholds.valley;

      // Detect upward acceleration peak
      if (!this.stepFlag && magnitude > peakThreshold) {
        // Enforce minimum time interval between steps (debounce)
        if (now - this.lastStepTime >= this.minStepIntervalMs) {
          if (!this.stepArmed) {
            this.stepArmed = true;
            this.stepArmTime = now;
          } else if ((now - this.stepArmTime) >= this.minArmWindowMs) {
            // STEP VALIDATED!
            this._recordStep(now);
            this.stepFlag = true;
            this.stepArmed = false;
          }
        }
      }

      // Reset step flag when motion enters downward trough / valley
      if (this.stepFlag && magnitude < valleyThreshold) {
        this.stepFlag = false;
        this.stepArmed = false;
      }

      // Expire armed state if peak was a false transient spike
      if (this.stepArmed && (now - this.stepArmTime > 260)) {
        this.stepArmed = false;
      }
    }

    _recordStep(now) {
      this.steps += 1;
      this.lastStepTime = now;

      // Recalculate derived metrics
      this.calories = Number((this.steps * this.calPerStep).toFixed(1));
      this.distanceKm = Number(((this.steps * this.metersPerStep) / 1000.0).toFixed(2));

      // Haptic pulse if supported
      if (this.hapticEnabled && typeof navigator !== 'undefined' && navigator.vibrate) {
        try {
          // Subtle pulse on each step, double pulse on 100-step milestones
          if (this.steps % 100 === 0) {
            navigator.vibrate([40, 60, 40]);
          } else {
            navigator.vibrate(20);
          }
        } catch (_) { }
      }

      // Persist locally
      this._saveStoredData();

      // Broadcast step event
      this._emitStepData(true);
    }

    _trackActiveTime(magnitude) {
      // Active motion threshold: deviated from baseline gravity (~9.8 m/s²)
      const isMotionActive = magnitude > 11.0 || magnitude < 8.6;
      const now = Date.now();

      if (now - this.lastActiveCheck >= 1000) {
        if (isMotionActive) {
          this.activeSeconds += 1;
          const newMin = Math.floor(this.activeSeconds / 60);
          if (newMin !== this.activeMin) {
            this.activeMin = newMin;
            this._saveStoredData();
            this._emitStepData(false);
          }
        }
        this.lastActiveCheck = now;
      }
    }

    // ==========================================
    // SIMULATION MODE (TESTING ON DESKTOP)
    // ==========================================

    startSimulation(stepCadenceMs = 520) {
      if (this.simInterval) return;

      this.log(`Simulation started (${stepCadenceMs}ms cadence)`);
      this._notifyStatus('simulating');

      this.simInterval = setInterval(() => {
        this._recordStep(Date.now());
        this.activeSeconds += Math.round(stepCadenceMs / 1000) || 1;
        this.activeMin = Math.floor(this.activeSeconds / 60);
      }, stepCadenceMs);
    }

    stopSimulation() {
      if (this.simInterval) {
        clearInterval(this.simInterval);
        this.simInterval = null;
        this.log('Simulation stopped');
        if (!this.isTracking) {
          this._notifyStatus('stopped');
        }
      }
    }

    simulateSingleStep() {
      this._recordStep(Date.now());
      this.activeSeconds += 1;
      this.activeMin = Math.floor(this.activeSeconds / 60);
      this.log(`Simulated 1 step (total: ${this.steps})`);
    }

    // ==========================================
    // WAKE LOCK & VISIBILITY
    // ==========================================

    async _acquireWakeLock() {
      if ('wakeLock' in navigator && !this.wakeLock) {
        try {
          this.wakeLock = await navigator.wakeLock.request('screen');
          this.wakeLock.addEventListener('release', () => {
            this.wakeLock = null;
          });
          this.log('Screen WakeLock active (keeps tracking alive in pocket)');
        } catch (e) {
          this.log(`WakeLock request skipped: ${e.message}`);
        }
      }
    }

    _releaseWakeLock() {
      if (this.wakeLock) {
        try {
          this.wakeLock.release();
        } catch (_) { }
        this.wakeLock = null;
      }
    }

    async _handleVisibilityChange() {
      if (document.visibilityState === 'visible' && this.isTracking) {
        await this._acquireWakeLock();
      }
    }

    // ==========================================
    // DATA EMISSION & EVENT SUBSCRIBERS
    // ==========================================

    getStepData() {
      const pct = Math.min(100, Math.round((this.steps / this.stepGoal) * 100));
      return {
        date: this._getTodayKey(),
        steps: this.steps,
        calories: this.calories,
        cal: this.calories,
        distance_km: this.distanceKm,
        km: this.distanceKm,
        active_min: this.activeMin,
        min: this.activeMin,
        active_seconds: this.activeSeconds,
        goal: this.stepGoal,
        pct: pct,
        source: 'phone',
        synced_at: new Date().toISOString()
      };
    }

    onStep(cb) {
      if (typeof cb === 'function') this.onStepCallbacks.push(cb);
      return this;
    }

    onStatusChange(cb) {
      if (typeof cb === 'function') this.onStatusChangeCallbacks.push(cb);
      return this;
    }

    onLog(cb) {
      if (typeof cb === 'function') this.onLogCallbacks.push(cb);
      return this;
    }

    log(msg) {
      const formatted = `[PhoneStepTracker] ${msg}`;
      // console.log(formatted);
      this.onLogCallbacks.forEach(cb => {
        try { cb(formatted); } catch (_) { }
      });
    }

    _notifyStatus(status) {
      this.onStatusChangeCallbacks.forEach(cb => {
        try { cb(status); } catch (_) { }
      });
    }

    _emitStepData(isStepIncrement) {
      const data = this.getStepData();
      this.onStepCallbacks.forEach(cb => {
        try { cb(data, isStepIncrement); } catch (_) { }
      });
    }
  }

  // Expose to window / global scope
  global.PhoneStepTracker = PhoneStepTracker;

  // Create default singleton instance
  if (!global.phoneStepTracker) {
    global.phoneStepTracker = new PhoneStepTracker();
  }

})(typeof window !== 'undefined' ? window : this);

