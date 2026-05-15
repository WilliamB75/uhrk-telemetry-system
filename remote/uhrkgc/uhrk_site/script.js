/*
 * UHRK Flight Monitor front-end logic.
 */

(function() {
  'use strict';

  const OFFLINE_THRESHOLD = 10000;
  const POLL_INTERVAL = 2000;
  const MAX_HISTORY = 80;
  const SHUTDOWN_PHRASE = 'SHUTDOWN UHRK';
  const SHUTDOWN_HOLD_MS = 3000;
  const CONTROL_ORIGIN = window.location.protocol + '//' + window.location.hostname + ':8090';
  const CONTROL_API = CONTROL_ORIGIN + '/api/shutdown';
  const SETTINGS_API = CONTROL_ORIGIN + '/api/settings';
  const PAD_STATE_API = CONTROL_ORIGIN + '/api/pad-state';
  const ALTITUDE_ZERO_API = CONTROL_ORIGIN + '/api/altitude-zero';
  const TIME_SYNC_API = CONTROL_ORIGIN + '/api/time-sync';
  const HEALTH_API = CONTROL_ORIGIN + '/api/health';
  const THEME_KEY = 'uhrk-dashboard-theme';
  const GRAVITY_REFERENCE = 9.80665;
  const LINEAR_ACCEL_DEADBAND = 0.25;

  const STAGE_NAMES = ['Booster', 'Sustainer', 'Payload'];
  const STAGE_COLORS = ['#2dd4bf', '#60a5fa', '#f59e0b'];
  const EVENT_NAMES = [
    'Burn active',
    'Burnout',
    'Stage separation',
    'Drogue deployed',
    'Main deployed',
    'Landed',
    'On Pad Idle',
    'On Pad Launch Ready'
  ];
  const DEFAULT_SETTINGS = {
    version: 1,
    sensor: {
      gravityMps2: 9.80665,
      linearAccelDeadbandMps2: 0.25,
      velocitySmoothingAlpha: 0.22,
      altitudeNoiseDeadbandM: 0.35,
      stationaryVelocityDeadbandMps: 0.35,
      maxBaroStepM: 25.0,
      maxGpsStepM: 35.0,
      maxGpsAltStepM: 30.0,
      kalmanBaroVarianceM2: 2.25,
      kalmanAccelVarianceMps2: 4.0,
      kalmanProcessAltitude: 0.08,
      kalmanProcessVelocity: 1.0,
      kalmanProcessAccel: 4.0
    },
    events: [
      { id: 'launch', label: 'Launch detect', stage: 'All', accelAboveG: 2.5, minDurationMs: 120 },
      { id: 'booster_burnout', label: 'Booster burnout', stage: 'Booster', accelBelowG: 0.35, minDurationMs: 250 },
      { id: 'second_motor_ignition', label: 'Second motor ignition', stage: 'Sustainer', accelAboveG: 2.0, minDurationMs: 120 },
      { id: 'sustainer_burnout', label: 'Sustainer burnout', stage: 'Sustainer', accelBelowG: 0.35, minDurationMs: 250 },
      { id: 'apogee', label: 'Apogee', stage: 'All', verticalVelocityBelowMps: 0.0, altitudeDropM: 8.0 },
      { id: 'landing', label: 'Landing', stage: 'All', altitudeBelowM: 5.0, verticalSpeedBelowMps: 1.0 }
    ],
    chutes: [
      { id: 'booster_drogue', stage: 'Booster', name: 'Drogue', deployAt: 'Apogee', altitudeM: null },
      { id: 'booster_main', stage: 'Booster', name: 'Main', deployAt: 'Altitude', altitudeM: 500.0 },
      { id: 'sustainer_drogue', stage: 'Sustainer', name: 'Drogue', deployAt: 'Apogee', altitudeM: null },
      { id: 'sustainer_main', stage: 'Sustainer', name: 'Main', deployAt: 'Altitude', altitudeM: 500.0 },
      { id: 'payload_drogue', stage: 'Payload', name: 'Drogue', deployAt: 'Apogee', altitudeM: null },
      { id: 'payload_main', stage: 'Payload', name: 'Main', deployAt: 'Altitude', altitudeM: 500.0 }
    ]
  };

  // Client-side state mirrors telemetry_latest.json. The backend remains the
  // source of truth; the browser only keeps enough history for charts.
  const stages = STAGE_NAMES.map((name, id) => ({
    id,
    name,
    connected: false,
    lastUpdate: 0,
    data: null,
    history: {
      time: [],
      gpsAlt: [],
      baroAlt: [],
      imuAlt: [],
      fusedAlt: [],
      gpsRelAlt: [],
      baroRelAlt: [],
      imuRelAlt: [],
      fusedRelAlt: [],
      velocity: [],
      imuVelocity: [],
      kalmanAlt: [],
      kalmanRelAlt: [],
      kalmanVelocity: [],
      kalmanAccel: [],
      ax: [],
      ay: [],
      az: [],
      accelMag: [],
      linearAccel: [],
      rawLinearAccel: [],
      gx: [],
      gy: [],
      gz: [],
      gyroMag: []
    }
  }));

  let activeView = 'general';
  let groundStation = {
    gpsStatus: null,
    sats: null,
    lat: null,
    lon: null,
    altitudeM: null
  };
  let systemInfo = {};
  let systemWarnings = [];

  const tabs = document.querySelectorAll('#stage-tabs button');
  const viewContentEl = document.getElementById('view-content');
  const chartsEl = document.querySelector('.charts');
  const gsGpsStatusEl = document.getElementById('gs-gps-status');
  const gsSatsEl = document.getElementById('gs-sats');
  const gsPositionEl = document.getElementById('gs-position');
  const gsAltitudeEl = document.getElementById('gs-altitude');
  const systemVersionEl = document.getElementById('system-version');
  const systemUpdatedEl = document.getElementById('system-updated');
  const systemWarningsEl = document.getElementById('system-warnings');
  const footerVersionEl = document.getElementById('footer-version');
  const exportLogEl = document.getElementById('export-log');
  const exportCsvEl = document.getElementById('export-csv');
  const exportKmlEl = document.getElementById('export-kml');
  const exportHealthEl = document.getElementById('export-health');
  const themeToggleEl = document.getElementById('theme-toggle');
  const settingsToggleEl = document.getElementById('settings-toggle');
  const settingsCloseEl = document.getElementById('settings-close');
  const settingsBackdropEl = document.getElementById('settings-backdrop');
  const settingsDrawerEl = document.getElementById('settings-drawer');
  const settingsContentEl = document.getElementById('settings-content');
  const settingsSaveEl = document.getElementById('settings-save');
  const settingsResetEl = document.getElementById('settings-reset');
  const settingsStatusEl = document.getElementById('settings-status');
  const shutdownArmEl = document.getElementById('shutdown-arm');
  const shutdownConfirmEl = document.getElementById('shutdown-confirm');
  const shutdownTestEl = document.getElementById('shutdown-test');
  const shutdownHoldEl = document.getElementById('shutdown-hold');
  const shutdownProgressEl = document.getElementById('shutdown-progress');
  const shutdownStatusEl = document.getElementById('shutdown-status');

  let altChart;
  let velocityChart;
  let imuVelocityChart;
  let accelChart;
  let gyroChart;
  let settingsModel = typeof structuredClone === 'function'
    ? structuredClone(DEFAULT_SETTINGS)
    : JSON.parse(JSON.stringify(DEFAULT_SETTINGS));

  function fmt(value, digits, suffix) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
    return Number(value).toFixed(digits) + (suffix || '');
  }

  function fmtRaw(value) {
    return value === null || value === undefined || value === '' ? '-' : String(value);
  }

  function fmtPosition(lat, lon) {
    if (lat === null || lat === undefined || lon === null || lon === undefined) return '-';
    return Number(lat).toFixed(5) + ', ' + Number(lon).toFixed(5);
  }

  function signalQuality(data) {
    return fmt(data.rssi, 0, ' dBm') + ' | ' + fmt(data.snr, 1, ' dB');
  }

  function satelliteSummary(data) {
    const used = data.satsUsed ?? null;
    const view = data.satsInView ?? data.sats ?? null;
    if (used == null && view == null) return '-';
    if (used == null) return '- | ' + view;
    if (view == null) return used + ' | -';
    return used + ' | ' + view;
  }

  function readinessText(data) {
    const readiness = data.readiness || {};
    if (readiness.readyForDroneTest) return 'Drone test ready';
    const missing = [];
    if (!readiness.gps3d) missing.push('GPS 3D');
    if (!readiness.altitudeZero) missing.push('zero');
    if (!readiness.link) missing.push('link');
    return missing.length ? 'Needs ' + missing.join(' + ') : 'Warming up';
  }

  function zeroText(data) {
    return data.altitudeZero && data.altitudeZero.setUtc ? 'Set' : 'Not set';
  }

  function packetRateText(data) {
    if (data.packetRateHz == null) return '-';
    const rate = Number(data.packetRateHz);
    if (!Number.isFinite(rate)) return '-';
    if (rate < 1) return rate.toFixed(2) + ' Hz';
    return rate.toFixed(1) + ' Hz';
  }

  function warningClass(warning) {
    const severity = warning && warning.severity ? warning.severity : 'info';
    if (severity === 'critical') return 'critical';
    if (severity === 'warning') return 'warning';
    return 'info';
  }

  function warningItem(warning) {
    const message = warning && warning.message ? warning.message : 'Unknown warning';
    return '<li class="' + warningClass(warning) + '">' + esc(message) + '</li>';
  }

  function warningList(warnings, emptyText) {
    if (!Array.isArray(warnings) || warnings.length === 0) {
      return '<p class="status-note ok">' + esc(emptyText || 'No active warnings') + '</p>';
    }
    return '<ul class="warning-list compact">' + warnings.map(warningItem).join('') + '</ul>';
  }

  function cssVar(name, fallback) {
    const value = getComputedStyle(document.body).getPropertyValue(name).trim();
    return value || fallback;
  }

  function accelMag(sample) {
    if (sample.ax == null || sample.ay == null || sample.az == null) return null;
    return Math.sqrt(sample.ax * sample.ax + sample.ay * sample.ay + sample.az * sample.az);
  }

  function linearAccel(sample) {
    // Prefer backend-filtered acceleration. The fallback keeps old log files
    // readable when they predate the Kalman fields.
    if (sample.linearAccel != null) return sample.linearAccel;
    const mag = sample.accelMagnitude != null ? sample.accelMagnitude : accelMag(sample);
    if (mag == null) return null;
    const value = mag - GRAVITY_REFERENCE;
    return Math.abs(value) < LINEAR_ACCEL_DEADBAND ? 0 : value;
  }

  function gyroMag(sample) {
    if (sample.gx == null || sample.gy == null || sample.gz == null) return null;
    return Math.sqrt(sample.gx * sample.gx + sample.gy * sample.gy + sample.gz * sample.gz);
  }

  function velocityFromHistory(recent, index) {
    const sample = recent[index];
    if (sample.verticalVelocity != null) return sample.verticalVelocity;
    if (sample.velocity != null) return sample.velocity;
    if (index === 0) return null;
    const prev = recent[index - 1];
    const alt = sample.fusedAlt ?? sample.baroAlt;
    const prevAlt = prev.fusedAlt ?? prev.baroAlt;
    if (alt == null || prevAlt == null) return null;
    return alt - prevAlt;
  }

  function imuVelocityFromHistory(recent, index) {
    const sample = recent[index];
    if (sample.imuVelocity != null) return sample.imuVelocity;
    if (index === 0) return null;
    const prev = recent[index - 1];
    if (sample.imuAlt == null || prev.imuAlt == null) return null;
    return sample.imuAlt - prev.imuAlt;
  }

  function currentEventFromFlags(flags) {
    const flightPriority = [5, 4, 3, 2, 1, 0];
    const padPriority = [7, 6];
    const active = (idx) => {
      if (typeof flags === 'number') return Boolean((flags >> idx) & 1);
      if (flags && typeof flags === 'object') return Boolean(flags[EVENT_NAMES[idx]]);
      return false;
    };
    const bit = flightPriority.find(active) ?? padPriority.find(active);
    return bit == null ? null : EVENT_NAMES[bit];
  }

  function eventList(data) {
    if (!data) return [];
    if (data.currentEvent) return [data.currentEvent];
    const fromFlags = currentEventFromFlags(data.eventFlags);
    if (fromFlags) return [fromFlags];
    if (Array.isArray(data.events) && data.events.length > 0) return [data.events[data.events.length - 1]];
    return [];
  }

  function statusClass(stage) {
    return stage.connected ? 'connected' : 'disconnected';
  }

  function statusText(stage) {
    if (stage.connected) return 'Connected';
    return stage.data ? 'Stale' : 'No data';
  }

  function applyChartTheme() {
    const text = cssVar('--chart-text', '#e5e7eb');
    const muted = cssVar('--chart-muted', '#9ca3af');
    const grid = cssVar('--chart-grid', 'rgba(148, 163, 184, 0.2)');
    [altChart, velocityChart, imuVelocityChart, accelChart, gyroChart].forEach((chart) => {
      if (!chart) return;
      chart.options.plugins.legend.labels = {
        color: text,
        boxWidth: 12,
        boxHeight: 8,
        usePointStyle: true
      };
      chart.options.scales.x.ticks = { color: muted };
      chart.options.scales.x.grid = { color: grid };
      chart.options.scales.y.ticks = { color: muted };
      chart.options.scales.y.grid = { color: grid };
    });
  }

  function setTheme(theme) {
    const nextTheme = theme === 'light' ? 'light' : 'dark';
    document.body.dataset.theme = nextTheme;
    try {
      localStorage.setItem(THEME_KEY, nextTheme);
    } catch (err) {
      // Storage can be unavailable in privacy modes; the live toggle still works.
    }
    if (themeToggleEl) {
      themeToggleEl.textContent = nextTheme === 'light' ? 'Dark' : 'Light';
      themeToggleEl.setAttribute('aria-pressed', nextTheme === 'light' ? 'true' : 'false');
    }
    applyChartTheme();
    if (altChart) updateCharts();
  }

  function initTheme() {
    let saved = 'dark';
    try {
      saved = localStorage.getItem(THEME_KEY) || 'dark';
    } catch (err) {
      saved = 'dark';
    }
    setTheme(saved);
    if (themeToggleEl) {
      themeToggleEl.addEventListener('click', () => {
        setTheme(document.body.dataset.theme === 'light' ? 'dark' : 'light');
      });
    }
  }

  function initCharts() {
    if (typeof Chart === 'undefined') {
      if (chartsEl) chartsEl.innerHTML = '<div class="chart-error">Chart library failed to load from the ground station.</div>';
      throw new Error('Chart.js is not available');
    }
    const baseOptions = () => ({
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: true, position: 'top', labels: {} } },
      scales: { x: { display: false, ticks: {}, grid: {} }, y: { beginAtZero: false, ticks: {}, grid: {} } }
    });
    altChart = new Chart(document.getElementById('alt-chart').getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: baseOptions()
    });
    velocityChart = new Chart(document.getElementById('velocity-chart').getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: baseOptions()
    });
    imuVelocityChart = new Chart(document.getElementById('imu-velocity-chart').getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: baseOptions()
    });
    accelChart = new Chart(document.getElementById('accel-chart').getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: baseOptions()
    });
    gyroChart = new Chart(document.getElementById('gyro-chart').getContext('2d'), {
      type: 'line',
      data: { labels: [], datasets: [] },
      options: baseOptions()
    });
    applyChartTheme();
  }

  function dataset(label, data, color, hidden, options) {
    const opts = options || {};
    return {
      label,
      data,
      borderColor: color,
      backgroundColor: color + '22',
      borderWidth: opts.borderWidth || 2,
      pointRadius: opts.pointRadius == null ? 1.5 : opts.pointRadius,
      tension: 0.18,
      hidden: Boolean(hidden),
      order: opts.order || 0
    };
  }

  function longestLabels() {
    return stages.reduce((best, stage) => {
      return stage.history.time.length > best.length ? stage.history.time : best;
    }, []);
  }

  function updateTabs() {
    tabs.forEach((btn) => btn.classList.toggle('active', btn.dataset.view === String(activeView)));
  }

  function updateCharts() {
    // General view compares stages; single-stage views expose the richer raw
    // and filtered sensor signals for debugging.
    if (activeView === 'general') {
      const labels = longestLabels();
      altChart.data.labels = labels;
      velocityChart.data.labels = labels;
      imuVelocityChart.data.labels = labels;
      accelChart.data.labels = labels;
      gyroChart.data.labels = labels;
      altChart.data.datasets = stages.map((stage, idx) => dataset(stage.name, stage.history.fusedRelAlt, STAGE_COLORS[idx]));
      velocityChart.data.datasets = stages.map((stage, idx) => dataset(stage.name, stage.history.kalmanVelocity, STAGE_COLORS[idx]));
      imuVelocityChart.data.datasets = stages.map((stage, idx) => dataset(stage.name, stage.history.imuVelocity, STAGE_COLORS[idx]));
      accelChart.data.datasets = stages.map((stage, idx) => dataset(stage.name, stage.history.kalmanAccel, STAGE_COLORS[idx]));
      gyroChart.data.datasets = stages.map((stage, idx) => dataset(stage.name, stage.history.gyroMag, STAGE_COLORS[idx]));
    } else {
      const stage = stages[Number(activeView)];
      const h = stage.history;
      altChart.data.labels = h.time;
      velocityChart.data.labels = h.time;
      imuVelocityChart.data.labels = h.time;
      accelChart.data.labels = h.time;
      gyroChart.data.labels = h.time;
      altChart.data.datasets = [
        dataset('Kalman fused rel', h.fusedRelAlt, cssVar('--alt-fused', '#f8fafc'), false, { borderWidth: 2.4, pointRadius: 1.5 }),
        dataset('GPS rel', h.gpsRelAlt, cssVar('--alt-gps', '#60a5fa'), false, { borderWidth: 1.8, pointRadius: 1.2 }),
        dataset('Baro rel', h.baroRelAlt, cssVar('--alt-baro', '#34d399'), false, { borderWidth: 4, pointRadius: 2.4, order: -1 }),
        dataset('IMU rel', h.imuRelAlt, cssVar('--alt-imu', '#f59e0b'), true, { borderWidth: 1.8, pointRadius: 1.2 }),
        dataset('Fused abs', h.fusedAlt, '#94a3b8', true, { borderWidth: 1.2, pointRadius: 0.8 })
      ];
      velocityChart.data.datasets = [dataset('Kalman vertical', h.kalmanVelocity, '#0f766e')];
      imuVelocityChart.data.datasets = [dataset('IMU vertical', h.imuVelocity, cssVar('--alt-imu', '#f59e0b'))];
      accelChart.data.datasets = [
        dataset('Kalman linear accel', h.kalmanAccel, '#34d399', false, { borderWidth: 3, pointRadius: 2 }),
        dataset('Raw |a|-g', h.rawLinearAccel, '#f59e0b', true),
        dataset('Raw |a|', h.accelMag, '#f8fafc', true),
        dataset('ax raw', h.ax, '#be123c', true),
        dataset('ay raw', h.ay, '#94a3b8', true),
        dataset('az raw', h.az, '#0e7490', true)
      ];
      gyroChart.data.datasets = [
        dataset('gx', h.gx, '#7c3aed'),
        dataset('gy', h.gy, '#2563eb'),
        dataset('gz', h.gz, '#16a34a'),
        dataset('|g|', h.gyroMag, '#111827', true)
      ];
    }
    [altChart, velocityChart, imuVelocityChart, accelChart, gyroChart].forEach((chart) => chart.update('none'));
  }

  function stageCard(stage) {
    const data = stage.data || {};
    const events = eventList(data);
    return [
      '<article class="node-card">',
      '<div class="card-title-row">',
      '<h2>' + stage.name + '</h2>',
      '<span class="status ' + statusClass(stage) + '">' + statusText(stage) + '</span>',
      '</div>',
      '<dl class="metric-grid compact">',
      '<div><dt>Seq</dt><dd>' + fmtRaw(data.seq) + '</dd></div>',
      '<div><dt>GPS</dt><dd>' + fmtRaw(data.gpsStatus) + '</dd></div>',
      '<div><dt>Sats used | view</dt><dd>' + satelliteSummary(data) + '</dd></div>',
      '<div><dt>Rel altitude</dt><dd>' + fmt(data.fusedRelAlt ?? data.fusedAlt, 1, ' m') + '</dd></div>',
      '<div><dt>Kalman velocity</dt><dd>' + fmt(data.kalmanVelocity ?? data.verticalVelocity, 2, ' m/s') + '</dd></div>',
      '<div><dt>IMU velocity</dt><dd>' + fmt(data.imuVelocity, 2, ' m/s') + '</dd></div>',
      '<div><dt>Kalman accel</dt><dd>' + fmt(data.kalmanAccel ?? data.linearAccel, 2, ' m/s^2') + '</dd></div>',
      '<div><dt>RSSI | SNR</dt><dd>' + signalQuality(data) + '</dd></div>',
      '<div><dt>Packet rate</dt><dd>' + packetRateText(data) + '</dd></div>',
      '<div><dt>Pad zero</dt><dd>' + zeroText(data) + '</dd></div>',
      '<div><dt>Readiness</dt><dd>' + readinessText(data) + '</dd></div>',
      '</dl>',
      '<p class="events-line">' + (events.length ? events.join(', ') : 'No events') + '</p>',
      warningList(data.warnings, 'No node warnings'),
      '</article>'
    ].join('');
  }

  function detailRow(label, value) {
    return '<div><dt>' + label + '</dt><dd>' + value + '</dd></div>';
  }

  function renderGeneral() {
    viewContentEl.innerHTML = [
      '<section class="overview-grid">',
      stages.map(stageCard).join(''),
      '</section>'
    ].join('');
  }

  function renderStageDetail(stage) {
    const data = stage.data || {};
    const events = eventList(data);
    viewContentEl.innerHTML = [
      '<section class="detail-card">',
      '<div class="card-title-row">',
      '<h2>' + stage.name + ' Node</h2>',
      '<span class="status ' + statusClass(stage) + '">' + statusText(stage) + '</span>',
      '</div>',
      '<dl class="metric-grid">',
      detailRow('Device ID', fmtRaw(data.deviceId)),
      detailRow('Sequence', fmtRaw(data.seq)),
      detailRow('Last seen', data.lastSeenMs != null ? fmt(data.lastSeenMs / 1000, 1, ' s ago') : '-'),
      detailRow('Last update UTC', fmtRaw(data.lastUpdateUtc)),
      detailRow('Packets received', fmtRaw(data.packetsReceived)),
      detailRow('Packet rate', packetRateText(data)),
      detailRow('GPS status', fmtRaw(data.gpsStatus)),
      detailRow('Satellites used | view', satelliteSummary(data)),
      detailRow('GPS quality', fmtRaw(data.gpsQuality)),
      detailRow('Position', fmtPosition(data.lat, data.lon)),
      detailRow('Readiness', readinessText(data)),
      detailRow('Pad zero', zeroText(data)),
      detailRow('Relative fused altitude', fmt(data.fusedRelAlt ?? data.fusedAlt, 1, ' m')),
      detailRow('Relative Kalman altitude', fmt(data.kalmanRelAlt ?? data.kalmanAlt, 1, ' m')),
      detailRow('Relative GPS altitude', fmt(data.gpsRelAlt ?? data.gpsAlt, 1, ' m')),
      detailRow('Relative baro altitude', fmt(data.baroRelAlt ?? data.baroAlt, 1, ' m')),
      detailRow('GPS altitude', fmt(data.gpsAlt, 1, ' m')),
      detailRow('Barometric altitude', fmt(data.baroAlt, 1, ' m')),
      detailRow('IMU altitude', fmt(data.imuAlt, 1, ' m')),
      detailRow('Fused altitude', fmt(data.fusedAlt, 1, ' m')),
      detailRow('Kalman velocity', fmt(data.kalmanVelocity ?? data.verticalVelocity, 2, ' m/s')),
      detailRow('IMU velocity', fmt(data.imuVelocity, 2, ' m/s')),
      detailRow('Kalman acceleration', fmt(data.kalmanAccel ?? data.linearAccel, 2, ' m/s^2')),
      detailRow('Raw linear acceleration', fmt(data.rawLinearAccel, 2, ' m/s^2')),
      detailRow('Accel magnitude', fmt(data.accelMagnitude, 2, ' m/s^2')),
      detailRow('Raw accel X', fmt(data.ax, 2, ' m/s^2')),
      detailRow('Raw accel Y', fmt(data.ay, 2, ' m/s^2')),
      detailRow('Raw accel Z', fmt(data.az, 2, ' m/s^2')),
      detailRow('Gyro X', fmt(data.gx, 2, ' deg/s')),
      detailRow('Gyro Y', fmt(data.gy, 2, ' deg/s')),
      detailRow('Gyro Z', fmt(data.gz, 2, ' deg/s')),
      detailRow('RSSI | SNR', signalQuality(data)),
      detailRow('Frequency', fmt(data.frequencyMHz, 3, ' MHz')),
      detailRow('Data rate', fmtRaw(data.dataRate)),
      detailRow('Events', events.length ? events.join(', ') : '-'),
      '</dl>',
      '<h2 class="subsection-title">Node Warnings</h2>',
      warningList(data.warnings, 'No node warnings'),
      '</section>'
    ].join('');
  }

  function updateGroundStation() {
    gsGpsStatusEl.textContent = groundStation.gpsStatus || '-';
    gsSatsEl.textContent = groundStation.sats != null ? groundStation.sats : '-';
    gsPositionEl.textContent = fmtPosition(groundStation.lat, groundStation.lon);
    gsAltitudeEl.textContent = fmt(groundStation.altitudeM, 1);
  }

  function updateSystemPanel() {
    const version = systemInfo.version || '-';
    if (systemVersionEl) systemVersionEl.textContent = version;
    if (systemUpdatedEl) systemUpdatedEl.textContent = systemInfo.updatedUtc || systemInfo.backendUtc || '-';
    if (footerVersionEl) footerVersionEl.textContent = version && version !== '-' ? ' | ' + version : '';
    if (systemWarningsEl) systemWarningsEl.innerHTML = warningList(systemWarnings, 'No system warnings');
  }

  function updateUI() {
    updateTabs();
    if (activeView === 'general') {
      renderGeneral();
    } else {
      renderStageDetail(stages[Number(activeView)]);
    }
    updateGroundStation();
    updateSystemPanel();
    updateCharts();
  }

  function processTelemetry(data) {
    // Transform the backend's per-stage history into Chart.js friendly arrays.
    // Nulls are kept as gaps so missing nodes do not draw misleading lines.
    const now = Date.now();
    systemInfo = Object.assign({}, data.system || {}, { updatedUtc: data.updatedUtc });
    systemWarnings = Array.isArray(data.warnings) ? data.warnings : [];
    if (data.ground_station) {
      const gs = data.ground_station;
      groundStation = {
        gpsStatus: gs.gpsStatus || gs.gps_status,
        sats: gs.sats,
        lat: gs.lat,
        lon: gs.lon,
        altitudeM: gs.altitudeM ?? gs.altitude_m ?? gs.altitude ?? gs.alt
      };
    }
    if (Array.isArray(data.stages)) {
      data.stages.forEach((rec) => {
        const id = rec.id != null ? rec.id : rec.deviceId;
        const stage = stages[id];
        if (!stage) return;
        stage.data = rec;
        if (window.updateMapPosition) {
          window.updateMapPosition(
            stage.id,
            rec.lat,
            rec.lon
          );
        }
        stage.connected = typeof rec.lastSeenMs === 'number' && rec.lastSeenMs <= OFFLINE_THRESHOLD;
        stage.lastUpdate = stage.connected ? now - rec.lastSeenMs : 0;
        const recent = Array.isArray(rec.history) ? rec.history.slice(-MAX_HISTORY) : [];
        stage.history.time = recent.map((sample) => sample.t || '');
        stage.history.gpsAlt = recent.map((sample) => sample.gpsAlt ?? null);
        stage.history.baroAlt = recent.map((sample) => sample.baroAlt ?? null);
        stage.history.imuAlt = recent.map((sample) => sample.imuAlt ?? null);
        stage.history.fusedAlt = recent.map((sample) => sample.fusedAlt ?? sample.baroAlt ?? null);
        stage.history.gpsRelAlt = recent.map((sample) => sample.gpsRelAlt ?? sample.gpsAlt ?? null);
        stage.history.baroRelAlt = recent.map((sample) => sample.baroRelAlt ?? sample.baroAlt ?? null);
        stage.history.imuRelAlt = recent.map((sample) => sample.imuRelAlt ?? sample.imuAlt ?? null);
        stage.history.fusedRelAlt = recent.map((sample) => sample.fusedRelAlt ?? sample.fusedAlt ?? sample.baroRelAlt ?? sample.baroAlt ?? null);
        stage.history.velocity = recent.map((_, index) => velocityFromHistory(recent, index));
        stage.history.imuVelocity = recent.map((_, index) => imuVelocityFromHistory(recent, index));
        stage.history.kalmanAlt = recent.map((sample) => sample.kalmanAlt ?? sample.fusedAlt ?? null);
        stage.history.kalmanRelAlt = recent.map((sample) => sample.kalmanRelAlt ?? sample.fusedRelAlt ?? null);
        stage.history.kalmanVelocity = recent.map((sample, index) => sample.kalmanVelocity ?? velocityFromHistory(recent, index));
        stage.history.kalmanAccel = recent.map((sample) => sample.kalmanAccel ?? linearAccel(sample));
        stage.history.ax = recent.map((sample) => sample.ax ?? null);
        stage.history.ay = recent.map((sample) => sample.ay ?? null);
        stage.history.az = recent.map((sample) => sample.az ?? null);
        stage.history.accelMag = recent.map((sample) => sample.accelMagnitude ?? accelMag(sample));
        stage.history.linearAccel = recent.map(linearAccel);
        stage.history.rawLinearAccel = recent.map((sample) => sample.rawLinearAccel ?? linearAccel(sample));
        stage.history.gx = recent.map((sample) => sample.gx ?? null);
        stage.history.gy = recent.map((sample) => sample.gy ?? null);
        stage.history.gz = recent.map((sample) => sample.gz ?? null);
        stage.history.gyroMag = recent.map(gyroMag);
      });
    }
    stages.forEach((stage) => {
      if (!stage.data || now - stage.lastUpdate > OFFLINE_THRESHOLD) stage.connected = false;
    });
  }

  async function fetchTelemetry() {
    try {
      const res = await fetch('/telemetry_latest.json', { cache: 'no-cache' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      processTelemetry(await res.json());
    } catch (err) {
      const now = Date.now();
      stages.forEach((stage) => {
        if (now - stage.lastUpdate > OFFLINE_THRESHOLD) stage.connected = false;
      });
    } finally {
      updateUI();
    }
  }

  function shutdownSafetyOk() {
    if (!shutdownArmEl || !shutdownConfirmEl) return false;
    return shutdownArmEl.checked && shutdownConfirmEl.value === SHUTDOWN_PHRASE;
  }

  function updateShutdownControls(message) {
    if (!shutdownHoldEl || !shutdownStatusEl) return;
    const ready = shutdownSafetyOk();
    shutdownHoldEl.disabled = !ready;
    if (shutdownProgressEl) shutdownProgressEl.style.width = '0%';
    if (message) {
      shutdownStatusEl.textContent = message;
    } else if (ready) {
      shutdownStatusEl.textContent = 'Armed. Hold the shutdown button for 3 seconds.';
    } else {
      shutdownStatusEl.textContent = 'Not armed';
    }
  }

  async function sendShutdownRequest(dryRun, holdMs) {
    const res = await fetch(CONTROL_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        armed: shutdownArmEl.checked,
        confirmation: shutdownConfirmEl.value,
        holdMs,
        dryRun
      })
    });
    const json = await res.json();
    if (!res.ok || !json.ok) throw new Error(json.error || 'shutdown request failed');
    return json;
  }

  function cloneSettings(settings) {
    return typeof structuredClone === 'function'
      ? structuredClone(settings)
      : JSON.parse(JSON.stringify(settings));
  }

  function esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fieldLabel(key) {
    return key
      .replace(/([A-Z])/g, ' $1')
      .replace(/^./, (ch) => ch.toUpperCase())
      .replace('Mps2', 'm/s^2')
      .replace('Mps', 'm/s')
      .replace('Ms', 'ms')
      .replace('M', 'm')
      .replace('G', 'g');
  }

  function settingsInput(path, label, value, step) {
    const isNumber = typeof value === 'number' || value === null;
    return [
      '<label class="settings-field">',
      '<span>' + esc(label) + '</span>',
      '<input data-settings-path="' + esc(path) + '" type="' + (isNumber ? 'number' : 'text') + '"',
      isNumber ? ' step="' + esc(step || '0.01') + '"' : '',
      ' value="' + esc(value == null ? '' : value) + '">',
      '</label>'
    ].join('');
  }

  function renderSettings() {
    if (!settingsContentEl) return;
    const sensor = settingsModel.sensor || {};
    const events = Array.isArray(settingsModel.events) ? settingsModel.events : [];
    const chutes = Array.isArray(settingsModel.chutes) ? settingsModel.chutes : [];
    // Settings are intentionally rendered from the current model so new GC-side
    // parameters can be added without creating static HTML for each one.
    const sensorFields = [
      settingsInput('sensor.gravityMps2', 'Gravity (m/s^2)', sensor.gravityMps2 ?? GRAVITY_REFERENCE, '0.0001'),
      settingsInput('sensor.linearAccelDeadbandMps2', 'Stationary accel deadband (m/s^2)', sensor.linearAccelDeadbandMps2 ?? LINEAR_ACCEL_DEADBAND, '0.01'),
      settingsInput('sensor.velocitySmoothingAlpha', 'Velocity smoothing alpha', sensor.velocitySmoothingAlpha ?? 0.22, '0.01'),
      settingsInput('sensor.altitudeNoiseDeadbandM', 'Altitude noise deadband (m)', sensor.altitudeNoiseDeadbandM ?? 0.35, '0.01'),
      settingsInput('sensor.stationaryVelocityDeadbandMps', 'Stationary velocity deadband (m/s)', sensor.stationaryVelocityDeadbandMps ?? 0.35, '0.01'),
      settingsInput('sensor.maxBaroStepM', 'Max baro step (m)', sensor.maxBaroStepM ?? 25.0, '1'),
      settingsInput('sensor.maxGpsStepM', 'Max GPS step (m)', sensor.maxGpsStepM ?? 35.0, '1'),
      settingsInput('sensor.maxGpsAltStepM', 'Max GPS altitude step (m)', sensor.maxGpsAltStepM ?? 30.0, '1'),
      settingsInput('sensor.kalmanBaroVarianceM2', 'Kalman baro variance (m^2)', sensor.kalmanBaroVarianceM2 ?? 2.25, '0.01'),
      settingsInput('sensor.kalmanAccelVarianceMps2', 'Kalman accel variance', sensor.kalmanAccelVarianceMps2 ?? 4.0, '0.01'),
      settingsInput('sensor.kalmanProcessAltitude', 'Kalman altitude process', sensor.kalmanProcessAltitude ?? 0.08, '0.01'),
      settingsInput('sensor.kalmanProcessVelocity', 'Kalman velocity process', sensor.kalmanProcessVelocity ?? 1.0, '0.01'),
      settingsInput('sensor.kalmanProcessAccel', 'Kalman accel process', sensor.kalmanProcessAccel ?? 4.0, '0.01')
    ].join('');
    const eventFields = events.map((event, index) => {
      const numericFields = Object.keys(event)
        .filter((key) => !['id', 'label', 'stage'].includes(key))
        .map((key) => settingsInput('events.' + index + '.' + key, fieldLabel(key), event[key], '0.01'))
        .join('');
      return [
        '<article class="settings-card">',
        settingsInput('events.' + index + '.label', 'Event', event.label || event.id || '', ''),
        settingsInput('events.' + index + '.stage', 'Stage', event.stage || 'All', ''),
        numericFields,
        '</article>'
      ].join('');
    }).join('');
    const chuteFields = chutes.map((chute, index) => [
      '<article class="settings-card chute-card">',
      settingsInput('chutes.' + index + '.stage', 'Stage', chute.stage || '', ''),
      settingsInput('chutes.' + index + '.name', 'Chute', chute.name || '', ''),
      settingsInput('chutes.' + index + '.deployAt', 'Deploy at', chute.deployAt || '', ''),
      settingsInput('chutes.' + index + '.altitudeM', 'Altitude (m)', chute.altitudeM ?? null, '1'),
      '</article>'
    ].join('')).join('');

    settingsContentEl.innerHTML = [
      '<section class="settings-section"><h3>Pad telemetry state</h3>' +
        '<div class="pad-state-controls">' +
          '<button type="button" data-pad-mode="idle">On Pad Idle</button>' +
          '<button type="button" data-pad-mode="launch_ready">On Pad Launch Ready</button>' +
        '</div>' +
        '<p id="pad-state-status" class="settings-status">Checking node pad state...</p>' +
      '</section>',
      '<section class="settings-section"><h3>Altitude zero</h3>' +
        '<div class="pad-state-controls">' +
          '<button type="button" data-alt-zero-action="set">Set pad zero</button>' +
          '<button type="button" data-alt-zero-action="clear">Clear zero</button>' +
        '</div>' +
        '<p id="altitude-zero-status" class="settings-status">Checking altitude zero...</p>' +
      '</section>',
      '<section class="settings-section"><h3>Clock sync</h3>' +
        '<div class="pad-state-controls">' +
          '<button type="button" data-time-sync="browser">Sync from browser</button>' +
        '</div>' +
        '<p id="time-sync-status" class="settings-status">GPS time is used automatically when available.</p>' +
      '</section>',
      '<section class="settings-section"><h3>Sensor processing</h3><div class="settings-grid">' + sensorFields + '</div></section>',
      '<section class="settings-section"><h3>Flight events</h3><div class="settings-card-grid">' + eventFields + '</div></section>',
      '<section class="settings-section"><h3>Recovery chutes</h3><div class="settings-card-grid">' + chuteFields + '</div></section>'
    ].join('');
    settingsContentEl.querySelectorAll('[data-pad-mode]').forEach((button) => {
      button.addEventListener('click', async () => {
        await setPadState(button.dataset.padMode);
      });
    });
    settingsContentEl.querySelectorAll('[data-alt-zero-action]').forEach((button) => {
      button.addEventListener('click', async () => {
        await setAltitudeZero(button.dataset.altZeroAction);
      });
    });
    settingsContentEl.querySelectorAll('[data-time-sync]').forEach((button) => {
      button.addEventListener('click', async () => {
        await syncTimeFromBrowser();
      });
    });
    refreshPadState();
    refreshAltitudeZero();
  }

  function setByPath(target, path, value) {
    const parts = path.split('.');
    let ref = target;
    for (let i = 0; i < parts.length - 1; i += 1) {
      const part = parts[i];
      ref = ref[Number.isNaN(Number(part)) ? part : Number(part)];
      if (ref == null) return;
    }
    const last = parts[parts.length - 1];
    ref[Number.isNaN(Number(last)) ? last : Number(last)] = value;
  }

  function readSettingsForm() {
    const next = cloneSettings(settingsModel);
    settingsContentEl.querySelectorAll('[data-settings-path]').forEach((input) => {
      let value = input.value;
      if (input.type === 'number') {
        value = input.value === '' ? null : Number(input.value);
      }
      setByPath(next, input.dataset.settingsPath, value);
    });
    settingsModel = next;
  }

  async function loadSettings() {
    if (!settingsContentEl) return;
    settingsStatusEl.textContent = 'Loading settings...';
    try {
      const res = await fetch(SETTINGS_API, { cache: 'no-cache' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      settingsModel = json.settings || cloneSettings(DEFAULT_SETTINGS);
      settingsStatusEl.textContent = 'Loaded from GC';
    } catch (err) {
      settingsModel = cloneSettings(DEFAULT_SETTINGS);
      settingsStatusEl.textContent = 'Using defaults; GC settings API unavailable';
    }
    renderSettings();
  }

  function padStateStatusEl() {
    return document.getElementById('pad-state-status');
  }

  function altitudeZeroStatusEl() {
    return document.getElementById('altitude-zero-status');
  }

  function timeSyncStatusEl() {
    return document.getElementById('time-sync-status');
  }

  function summarizePadState(result) {
    if (!result || !Array.isArray(result.nodes) || result.nodes.length === 0) return 'No nodes configured';
    const prefix = result.transport === 'lora' ? 'LoRa: ' : '';
    return result.nodes.map((node) => {
      const response = node.response || {};
      const state = response.padState || node.padState || {};
      const host = response.hostname || node.name || node.url || ('Node ' + node.deviceId);
      const mode = state.mode === 'launch_ready' ? 'Launch Ready' : state.mode === 'idle' ? 'Idle' : 'unknown';
      return host + ': ' + (node.ok ? mode : 'no telemetry');
    }).join(' | ').replace(/^/, prefix);
  }

  function summarizeAltitudeZero(result) {
    if (!result || !Array.isArray(result.stages)) return 'Unable to read altitude zero';
    const active = result.stages.filter((stage) => stage.lastSeenMs != null);
    if (!active.length) return 'No active nodes';
    return active.map((stage) => {
      const zeroSet = stage.altitudeZero && stage.altitudeZero.setUtc;
      const rel = stage.current ? stage.current.fusedRelAlt : null;
      return stage.name + ': ' + (zeroSet ? fmt(rel, 1, ' m') : 'not set');
    }).join(' | ');
  }

  async function refreshPadState() {
    const el = padStateStatusEl();
    if (!el) return;
    try {
      const res = await fetch(PAD_STATE_API, { cache: 'no-cache' });
      const json = await res.json();
      el.textContent = summarizePadState(json);
    } catch (err) {
      el.textContent = 'Unable to read pad state';
    }
  }

  async function refreshAltitudeZero() {
    const el = altitudeZeroStatusEl();
    if (!el) return;
    try {
      const res = await fetch(ALTITUDE_ZERO_API, { cache: 'no-cache' });
      const json = await res.json();
      el.textContent = summarizeAltitudeZero(json);
    } catch (err) {
      el.textContent = 'Unable to read altitude zero';
    }
  }

  async function setAltitudeZero(action) {
    const el = altitudeZeroStatusEl();
    if (el) el.textContent = action === 'clear' ? 'Clearing zero...' : 'Setting pad zero...';
    try {
      const res = await fetch(ALTITUDE_ZERO_API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action })
      });
      const json = await res.json();
      if (!res.ok || !json.ok) throw new Error(json.error || 'altitude zero request failed');
      if (el) el.textContent = summarizeAltitudeZero(json);
      await fetchTelemetry();
    } catch (err) {
      if (el) el.textContent = 'Altitude zero failed: ' + err.message;
    }
  }

  async function syncTimeFromBrowser() {
    const el = timeSyncStatusEl();
    if (el) el.textContent = 'Syncing GC and nodes from browser clock...';
    try {
      const res = await fetch(TIME_SYNC_API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          epoch: Date.now() / 1000,
          utc: new Date().toISOString(),
          source: 'browser'
        })
      });
      const json = await res.json();
      if (!res.ok || !json.ok) throw new Error(json.error || 'time sync failed');
      const nodeOk = Array.isArray(json.nodes) ? json.nodes.filter((node) => node.ok).length : 0;
      if (el) el.textContent = 'Synced ' + json.utc + ' | nodes: ' + nodeOk + '/' + (json.nodes || []).length;
    } catch (err) {
      if (el) el.textContent = 'Time sync failed: ' + err.message;
    }
  }

  async function setPadState(mode) {
    const el = padStateStatusEl();
    if (el) el.textContent = 'Sending pad state...';
    try {
      const res = await fetch(PAD_STATE_API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
      });
      const json = await res.json();
      if (!res.ok || !json.ok) throw new Error(json.error || 'pad state command failed');
      if (el) el.textContent = summarizePadState(json);
    } catch (err) {
      if (el) el.textContent = 'Pad state failed: ' + err.message;
    }
  }

  async function saveSettings() {
    readSettingsForm();
    settingsStatusEl.textContent = 'Saving...';
    const res = await fetch(SETTINGS_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: settingsModel })
    });
    const json = await res.json();
    if (!res.ok || !json.ok) throw new Error(json.error || 'settings save failed');
    settingsModel = json.settings;
    settingsStatusEl.textContent = 'Saved on GC';
    renderSettings();
  }

  function openSettings() {
    if (!settingsDrawerEl || !settingsBackdropEl) return;
    settingsDrawerEl.classList.add('open');
    settingsDrawerEl.setAttribute('aria-hidden', 'false');
    settingsBackdropEl.hidden = false;
    if (settingsToggleEl) settingsToggleEl.setAttribute('aria-expanded', 'true');
    loadSettings();
  }

  function closeSettings() {
    if (!settingsDrawerEl || !settingsBackdropEl) return;
    settingsDrawerEl.classList.remove('open');
    settingsDrawerEl.setAttribute('aria-hidden', 'true');
    settingsBackdropEl.hidden = true;
    if (settingsToggleEl) settingsToggleEl.setAttribute('aria-expanded', 'false');
  }

  function initSettingsDrawer() {
    if (!settingsToggleEl || !settingsDrawerEl) return;
    settingsToggleEl.addEventListener('click', openSettings);
    if (settingsCloseEl) settingsCloseEl.addEventListener('click', closeSettings);
    if (settingsBackdropEl) settingsBackdropEl.addEventListener('click', closeSettings);
    if (settingsResetEl) {
      settingsResetEl.addEventListener('click', () => {
        settingsModel = cloneSettings(DEFAULT_SETTINGS);
        settingsStatusEl.textContent = 'Defaults loaded; press Save to store';
        renderSettings();
      });
    }
    if (settingsSaveEl) {
      settingsSaveEl.addEventListener('click', async () => {
        try {
          await saveSettings();
        } catch (err) {
          settingsStatusEl.textContent = 'Save failed: ' + err.message;
        }
      });
    }
  }

  function initShutdownControls() {
    if (!shutdownArmEl || !shutdownConfirmEl || !shutdownTestEl || !shutdownHoldEl || !shutdownStatusEl) return;
    let holdTimer = null;
    let progressTimer = null;
    let holdStarted = 0;

    shutdownArmEl.addEventListener('change', () => updateShutdownControls());
    shutdownConfirmEl.addEventListener('input', () => updateShutdownControls());
    shutdownTestEl.addEventListener('click', async () => {
      try {
        updateShutdownControls('Testing LoRa shutdown path...');
        const result = await sendShutdownRequest(true, SHUTDOWN_HOLD_MS);
        const attempts = result.command && Array.isArray(result.command.attempts) ? result.command.attempts.length : 0;
        updateShutdownControls('Dry-run LoRa command sent. Attempts: ' + attempts + '. GC log: ' + result.gcLogPath);
      } catch (err) {
        updateShutdownControls('Test failed: ' + err.message);
      }
    });

    function clearHold(message) {
      if (holdTimer) clearTimeout(holdTimer);
      if (progressTimer) clearInterval(progressTimer);
      holdTimer = null;
      progressTimer = null;
      if (shutdownProgressEl) shutdownProgressEl.style.width = '0%';
      if (message) updateShutdownControls(message);
    }

    function beginHold(event) {
      event.preventDefault();
      if (!shutdownSafetyOk()) {
        updateShutdownControls('Arm the switch and type the exact phrase first.');
        return;
      }
      // The real shutdown path requires the arm switch, exact phrase, and a
      // continuous hold. Releasing before the timer finishes cancels locally.
      holdStarted = Date.now();
      shutdownStatusEl.textContent = 'Keep holding...';
      progressTimer = setInterval(() => {
        const pct = Math.min(100, ((Date.now() - holdStarted) / SHUTDOWN_HOLD_MS) * 100);
        if (shutdownProgressEl) shutdownProgressEl.style.width = pct + '%';
      }, 50);
      holdTimer = setTimeout(async () => {
        try {
          shutdownStatusEl.textContent = 'LoRa shutdown command sent. Saving logs and powering down.';
          await sendShutdownRequest(false, Date.now() - holdStarted);
        } catch (err) {
          clearHold('Shutdown failed: ' + err.message);
        }
      }, SHUTDOWN_HOLD_MS);
    }

    shutdownHoldEl.addEventListener('mousedown', beginHold);
    shutdownHoldEl.addEventListener('touchstart', beginHold, { passive: false });
    ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach((eventName) => {
      shutdownHoldEl.addEventListener(eventName, () => {
        if (holdTimer) clearHold('Shutdown cancelled before hold completed.');
      });
    });
    updateShutdownControls();
  }

  function initExportLinks() {
    if (exportLogEl) exportLogEl.href = CONTROL_ORIGIN + '/api/logs/current';
    if (exportCsvEl) exportCsvEl.href = CONTROL_ORIGIN + '/api/export/csv';
    if (exportKmlEl) exportKmlEl.href = CONTROL_ORIGIN + '/api/export/kml';
    if (exportHealthEl) exportHealthEl.href = HEALTH_API;
  }

  function init() {
    initTheme();
    initExportLinks();
    initCharts();
    tabs.forEach((btn) => {
      btn.addEventListener('click', () => {
        activeView = btn.dataset.view;
        updateUI();
      });
    });
    initSettingsDrawer();
    initShutdownControls();
    fetchTelemetry();
    setInterval(fetchTelemetry, POLL_INTERVAL);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
