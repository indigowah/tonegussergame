(function () {
  const prefersDark = window.matchMedia(
    '(prefers-color-scheme: dark)'
  ).matches;

  const defaultPreferences = {
    theme: prefersDark ? 'mocha' : 'latte',
    volume: 0.8,
    rate: 1,
    listeningMode: false,
    modes: [],
  };

  const state = {
    catalog: [],
    currentMode: null,
    currentItem: null,
    audio: new Audio(),
    stats: {
      correct: 0,
      wrong: 0,
      streak: 0,
      history: [],
    },
    preferences: loadPreferences(),
  };

  const root = document.documentElement;
  const toastEl = document.getElementById('toast');
  const hud = {
    correct: document.getElementById('hud-correct'),
    wrong: document.getElementById('hud-wrong'),
    streak: document.getElementById('hud-streak'),
    mode: document.getElementById('hud-mode'),
  };
  const promptText = document.getElementById('prompt-text');
  const answerButtons = Array.from(document.querySelectorAll('.answer'));
  const modeChecklist = document.getElementById('mode-checklist');
  const modeForm = document.getElementById('mode-form');
  const listeningToggle = document.getElementById('listening-mode');
  const settingsToggle = document.getElementById('settings-toggle');
  const settingsDrawer = document.getElementById('settings-drawer');
  const closeSettingsBtn = document.getElementById('close-settings');
  const volumeControl = document.getElementById('volume-control');
  const rateControl = document.getElementById('rate-control');
  const themeToggle = document.getElementById('theme-toggle');
  const playButton = document.getElementById('play-button');
  const replayButton = document.getElementById('replay-button');
  const skipButton = document.getElementById('skip-button');
  const downloadReport = document.getElementById('download-report');

  function loadPreferences() {
    try {
      const saved = localStorage.getItem('tone-gusser-preferences');
      if (!saved) {
        return { ...defaultPreferences };
      }
      const parsed = JSON.parse(saved);
      return { ...defaultPreferences, ...parsed };
    } catch (error) {
      console.warn('Failed to load preferences:', error);
      return { ...defaultPreferences };
    }
  }

  function savePreferences() {
    try {
      localStorage.setItem(
        'tone-gusser-preferences',
        JSON.stringify(state.preferences)
      );
    } catch (error) {
      console.warn('Failed to save preferences:', error);
    }
  }

  function applyTheme() {
    if (!root) return;
    const theme = state.preferences.theme;
    root.setAttribute('data-theme', theme);
    if (themeToggle) {
      themeToggle.setAttribute('aria-pressed', theme === 'mocha');
    }
  }

  function toggleTheme() {
    const nextTheme = state.preferences.theme === 'latte' ? 'mocha' : 'latte';
    state.preferences.theme = nextTheme;
    applyTheme();
    savePreferences();
  }

  function hydrateForm() {
    if (volumeControl) {
      volumeControl.value = state.preferences.volume;
    }
    if (rateControl) {
      rateControl.value = state.preferences.rate;
    }
    if (listeningToggle) {
      listeningToggle.checked = Boolean(state.preferences.listeningMode);
    }
  }

  function updateStatsDisplay() {
    if (!hud.correct) return;
    hud.correct.textContent = state.stats.correct;
    hud.wrong.textContent = state.stats.wrong;
    hud.streak.textContent = state.stats.streak;
    hud.mode.textContent = state.currentMode || '—';
  }

  function showToast(message, type = 'info') {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.className = type;
    toastEl.dataset.active = 'true';
    window.setTimeout(() => {
      toastEl.dataset.active = 'false';
    }, 2400);
  }

  function openSettings() {
    if (!settingsDrawer) return;
    settingsDrawer.dataset.open = 'true';
    settingsDrawer.setAttribute('aria-hidden', 'false');
    if (settingsToggle) settingsToggle.setAttribute('aria-expanded', 'true');
  }

  function closeSettings() {
    if (!settingsDrawer) return;
    settingsDrawer.dataset.open = 'false';
    settingsDrawer.setAttribute('aria-hidden', 'true');
    if (settingsToggle) settingsToggle.setAttribute('aria-expanded', 'false');
  }

  function toggleSettings() {
    if (!settingsDrawer) return;
    const isOpen = settingsDrawer.dataset.open === 'true';
    if (isOpen) {
      closeSettings();
    } else {
      openSettings();
    }
  }

  function fetchCatalog() {
    return fetch('/catalog.json')
      .then((response) => {
        if (!response.ok) {
          throw new Error('Failed to load catalog');
        }
        return response.json();
      })
      .then((catalog) => {
        state.catalog = Array.isArray(catalog) ? catalog : [];
        renderModes();
      })
      .catch((error) => {
        console.error(error);
        showToast('Unable to load catalog data.', 'error');
      });
  }

  function renderModes() {
    if (!modeChecklist) return;
    modeChecklist.innerHTML = '';
    const modes = state.catalog.map((entry) => entry.id || entry.name);
    if (!modes.length) {
      const empty = document.createElement('li');
      empty.textContent = 'No modes available yet.';
      modeChecklist.appendChild(empty);
      return;
    }
    const selected = new Set(state.preferences.modes || []);
    modes.forEach((modeId) => {
      const li = document.createElement('li');
      const label = document.createElement('label');
      label.className = 'mode-item';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = modeId;
      checkbox.checked = selected.has(modeId);
      checkbox.addEventListener('change', onModeToggle);
      const span = document.createElement('span');
      span.textContent = modeId;
      label.append(checkbox, span);
      li.appendChild(label);
      modeChecklist.appendChild(li);
    });
  }

  function onModeToggle(event) {
    const { value, checked } = event.target;
    const modes = new Set(state.preferences.modes || []);
    if (checked) {
      modes.add(value);
    } else {
      modes.delete(value);
    }
    state.preferences.modes = Array.from(modes);
    savePreferences();
  }

  function nextPrompt() {
    if (!state.catalog.length) {
      showToast('Load a catalog to start playing.', 'error');
      return;
    }
    const availableModes = state.preferences.modes.length
      ? state.catalog.filter((entry) =>
          state.preferences.modes.includes(entry.id || entry.name)
        )
      : state.catalog;
    if (!availableModes.length) {
      showToast('Select at least one mode to continue.', 'error');
      return;
    }
    const mode = sample(availableModes);
    state.currentMode = mode.id || mode.name;
    state.currentItem = sample(mode.items || []);
    if (!state.currentItem) {
      showToast('Mode has no items.', 'error');
      return;
    }
    updateStatsDisplay();
    playCurrent();
    if (promptText) {
      promptText.textContent = 'Identify the tone you hear.';
      promptText.className = '';
    }
  }

  function playCurrent() {
    if (!state.currentItem || !state.currentItem.url) return;
    state.audio.pause();
    state.audio.src = state.currentItem.url;
    state.audio.volume = Number(state.preferences.volume);
    state.audio.playbackRate = Number(state.preferences.rate);
    state.audio.play().catch((error) => {
      console.warn('Playback failed:', error);
      showToast('Unable to play audio.', 'error');
    });
  }

  function handleAnswer(event) {
    if (state.preferences.listeningMode) {
      showToast('Listening mode enabled: answers disabled.', 'info');
      return;
    }
    const button = event.currentTarget;
    const answer = button.dataset.answer;
    if (!state.currentItem) {
      showToast('Press play to start.', 'info');
      return;
    }
    const isCorrect = String(state.currentItem.answer) === String(answer);
    registerResult(isCorrect, answer);
    giveFeedback(isCorrect, button);
    nextPrompt();
  }

  function registerResult(isCorrect, guess) {
    const entry = {
      timestamp: Date.now(),
      mode: state.currentMode,
      correctAnswer: state.currentItem.answer,
      guess,
      correct: isCorrect,
    };
    state.stats.history.push(entry);
    if (isCorrect) {
      state.stats.correct += 1;
      state.stats.streak += 1;
    } else {
      state.stats.wrong += 1;
      state.stats.streak = 0;
    }
    updateStatsDisplay();
    updateReportLink();
  }

  function giveFeedback(isCorrect, button) {
    if (!promptText) return;
    promptText.textContent = isCorrect ? 'Correct!' : 'Try again!';
    promptText.className = isCorrect ? 'correct' : 'wrong';
    if (button) {
      button.classList.add(isCorrect ? 'correct' : 'wrong');
      window.setTimeout(() => {
        button.classList.remove('correct', 'wrong');
      }, 600);
    }
    playFeedbackSound(isCorrect);
  }

  function playFeedbackSound(isCorrect) {
    const url = isCorrect
      ? '/sounds/feedback/correct.mp3'
      : '/sounds/feedback/wrong.mp3';
    const sfx = new Audio(url);
    sfx.volume = Number(state.preferences.volume);
    sfx.play().catch(() => {
      /* ignore */
    });
  }

  function sample(list) {
    if (!list || !list.length) return null;
    const index = Math.floor(Math.random() * list.length);
    return list[index];
  }

  function onListeningModeChange(event) {
    state.preferences.listeningMode = event.target.checked;
    savePreferences();
    showToast(
      state.preferences.listeningMode
        ? 'Listening mode enabled.'
        : 'Listening mode disabled.',
      'info'
    );
  }

  function onVolumeChange(event) {
    state.preferences.volume = Number(event.target.value);
    state.audio.volume = state.preferences.volume;
    savePreferences();
  }

  function onRateChange(event) {
    state.preferences.rate = Number(event.target.value);
    state.audio.playbackRate = state.preferences.rate;
    savePreferences();
  }

  function updateReportLink() {
    if (!downloadReport) return;
    const { history } = state.stats;
    if (!history.length) {
      downloadReport.setAttribute('disabled', 'true');
      downloadReport.removeAttribute('href');
      downloadReport.removeAttribute('download');
      return;
    }
    downloadReport.removeAttribute('disabled');
    const blob = new Blob([JSON.stringify(history, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    downloadReport.href = url;
    downloadReport.download = `tone-gusser-report-${new Date()
      .toISOString()
      .slice(0, 10)}.json`;
  }

  function onPlay() {
    if (!state.currentItem) {
      nextPrompt();
      return;
    }
    playCurrent();
  }

  function onReplay() {
    if (!state.currentItem) {
      showToast('Nothing to replay yet.', 'info');
      return;
    }
    playCurrent();
  }

  function onSkip() {
    if (!state.catalog.length) {
      showToast('No catalog loaded.', 'error');
      return;
    }
    state.stats.streak = 0;
    updateStatsDisplay();
    nextPrompt();
  }

  function bindEvents() {
    if (modeForm) {
      modeForm.addEventListener('submit', (event) => event.preventDefault());
    }
    answerButtons.forEach((button) =>
      button.addEventListener('click', handleAnswer)
    );
    if (settingsToggle) settingsToggle.addEventListener('click', toggleSettings);
    if (closeSettingsBtn) closeSettingsBtn.addEventListener('click', closeSettings);
    if (listeningToggle)
      listeningToggle.addEventListener('change', onListeningModeChange);
    if (volumeControl) volumeControl.addEventListener('input', onVolumeChange);
    if (rateControl) rateControl.addEventListener('input', onRateChange);
    if (playButton) playButton.addEventListener('click', onPlay);
    if (replayButton) replayButton.addEventListener('click', onReplay);
    if (skipButton) skipButton.addEventListener('click', onSkip);
    if (themeToggle) themeToggle.addEventListener('click', toggleTheme);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeSettings();
      }
    });
  }

  function initThemeFromDocument() {
    const current = root?.getAttribute('data-theme');
    if (current && current !== state.preferences.theme) {
      state.preferences.theme = current;
    }
    applyTheme();
  }

  function init() {
    if (!document.body) return;
    hydrateForm();
    initThemeFromDocument();
    bindEvents();
    fetchCatalog();
    updateStatsDisplay();
    updateReportLink();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
