const state = {
    roundId: null,
    settings: null,
    toneSrc: null,
    activeAudio: null,
    awaitingNext: false,
};

const elements = {
    startBtn: document.getElementById("start-btn"),
    replayBtn: document.getElementById("replay-btn"),
    endBtn: document.getElementById("end-btn"),
    resetBtn: document.getElementById("reset-btn"),
    optionCount: document.getElementById("option-count"),
    status: document.getElementById("status-message"),
    options: document.getElementById("options-container"),
    graphAccuracy: document.getElementById("graph-accuracy"),
    graphCumulative: document.getElementById("graph-cumulative"),
    statRounds: document.getElementById("stat-rounds"),
    statGuesses: document.getElementById("stat-guesses"),
    statAccuracy: document.getElementById("stat-accuracy"),
    statFirstTry: document.getElementById("stat-first-try"),
    statAverage: document.getElementById("stat-average"),
    toneBest: document.getElementById("tone-best"),
    toneWorst: document.getElementById("tone-worst"),
};

function getSelectedDifficulties() {
    return Array.from(document.querySelectorAll(".difficulty-option:checked")).map(
        (checkbox) => checkbox.value,
    );
}

function setStatus(message, tone = "info") {
    elements.status.textContent = message;
    elements.status.dataset.tone = tone;
}

function resetBoard() {
    state.roundId = null;
    state.toneSrc = null;
    state.awaitingNext = false;
    stopAudio();
    elements.options.innerHTML = "";
    elements.replayBtn.disabled = true;
    elements.endBtn.disabled = true;
}

function stopAudio() {
    if (state.activeAudio) {
        try {
            state.activeAudio.pause();
        } catch (err) {
            // ignore
        }
    }
    state.activeAudio = null;
}

function playAudio(src) {
    if (!src) {
        return Promise.resolve();
    }

    stopAudio();

    const audio = new Audio(src);
    audio.preload = "auto";
    state.activeAudio = audio;

    return new Promise((resolve) => {
        const cleanup = () => {
            if (state.activeAudio === audio) {
                state.activeAudio = null;
            }
            audio.removeEventListener("ended", cleanup);
            audio.removeEventListener("error", cleanup);
            resolve();
        };

        audio.addEventListener("ended", cleanup);
        audio.addEventListener("error", cleanup);

        const playPromise = audio.play();
        if (playPromise && typeof playPromise.catch === "function") {
            playPromise.catch(() => {
                cleanup();
            });
        }
    });
}

async function postJSON(url, payload) {
    const response = await fetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
        const error = data && data.error ? data.error : "Request failed.";
        throw new Error(error);
    }
    return data;
}

async function fetchStats() {
    try {
        const response = await fetch("/api/stats");
        if (!response.ok) {
            return;
        }
        const data = await response.json();
        const summary = data.summary || {};
        elements.statRounds.textContent = summary.rounds_completed || 0;
        elements.statGuesses.textContent = summary.total_guesses || 0;

        const accuracy = summary.accuracy ? summary.accuracy * 100 : 0;
        elements.statAccuracy.textContent = `${accuracy.toFixed(1)}%`;
        elements.statFirstTry.textContent = summary.first_try_success || 0;

        const avg = summary.average_attempts_per_round || 0;
        elements.statAverage.textContent = avg ? avg.toFixed(2) : "0";

        if (data.graphs) {
            if (data.graphs.accuracy_by_difficulty) {
                elements.graphAccuracy.src = data.graphs.accuracy_by_difficulty;
            }
            if (data.graphs.cumulative_accuracy) {
                elements.graphCumulative.src = data.graphs.cumulative_accuracy;
            }
        }

        if (data.tones) {
            renderToneLists(data.tones);
        } else {
            renderToneLists({ best: [], worst: [] });
        }
    } catch (error) {
        console.error("Unable to fetch stats:", error);
    }
}

function renderToneLists(tones) {
    renderToneList(elements.toneWorst, tones?.worst, "Keep playing to gather data.");
    renderToneList(elements.toneBest, tones?.best, "Keep playing to gather data.");
}

function renderToneList(container, items, emptyText) {
    if (!container) {
        return;
    }
    container.innerHTML = "";
    if (!items || !items.length) {
        const li = document.createElement("li");
        li.className = "tone-empty";
        li.textContent = emptyText;
        container.appendChild(li);
        return;
    }

    items.forEach((item) => {
        const li = document.createElement("li");
        const label = document.createElement("span");
        label.className = "tone-label";
        label.textContent = item.label;

        const meta = document.createElement("span");
        meta.className = "tone-meta";
        const accuracy = typeof item.accuracy === "number" ? item.accuracy * 100 : 0;
        meta.textContent = `${accuracy.toFixed(1)}% • ${item.correct}/${item.total}`;

        li.appendChild(label);
        li.appendChild(meta);
        container.appendChild(li);
    });
}

function renderOptions(options) {
    elements.options.innerHTML = "";

    options.forEach((label) => {
        const button = document.createElement("button");
        button.className = "option-button";
        button.textContent = label;
        button.dataset.value = label;
        button.addEventListener("click", () => handleGuess(button));
        elements.options.appendChild(button);
    });
}

async function handleGuess(button) {
    if (!state.roundId || state.awaitingNext) {
        return;
    }

    const choice = button.dataset.value;
    if (!choice) {
        return;
    }

    button.disabled = true;

    try {
        const result = await postJSON("/api/guess", {
            round_id: state.roundId,
            choice,
        });

        button.classList.add(result.correct ? "correct" : "wrong");

        await playAudio(result.feedback_audio);
        await playAudio(state.toneSrc);
        

        await fetchStats();

        if (result.correct) {
            setStatus(`Nice! "${result.correct_label}" was the right tone.`, "success");
            state.awaitingNext = true;
            disableAllOptions();
            setTimeout(() => {
                fetchNextRound();
            }, 1200);
        } else {
            setStatus("Not quite — try a different tone.", "warn");
        }
    } catch (error) {
        console.error(error);
        button.disabled = false;
        button.classList.remove("correct", "wrong");
        setStatus(error.message || "Error submitting guess.", "error");
    }
}

function disableAllOptions() {
    Array.from(elements.options.querySelectorAll(".option-button")).forEach((btn) => {
        btn.disabled = true;
    });
}

async function startGame() {
    const selected = getSelectedDifficulties();
    if (!selected.length) {
        setStatus("Pick at least one difficulty to begin.", "warn");
        return;
    }

    const optionCount = Number(elements.optionCount.value) || 4;
    state.settings = {
        difficulties: selected,
        option_count: optionCount,
    };

    if (state.roundId) {
        try {
            await postJSON("/api/end", { round_id: state.roundId });
        } catch (error) {
            console.debug("Unable to end previous round:", error);
        }
        resetBoard();
    }

    try {
        const data = await postJSON("/api/start", state.settings);
        prepareRound(data.round);
        await fetchStats();
    } catch (error) {
        setStatus(error.message || "Unable to start the game.", "error");
    }
}

function prepareRound(round) {
    if (!round) {
        setStatus("No round data received.", "error");
        return;
    }

    state.roundId = round.id;
    state.toneSrc = round.audio_url;
    state.awaitingNext = false;

    elements.replayBtn.disabled = false;
    elements.endBtn.disabled = false;

    renderOptions(round.options);
    setStatus("Listen closely and pick the tone.", "info");
    playAudio(state.toneSrc);
}

async function fetchNextRound() {
    if (!state.settings) {
        return;
    }

    try {
        const data = await postJSON("/api/next", state.settings);
        prepareRound(data.round);
    } catch (error) {
        setStatus(error.message || "Unable to fetch the next tone.", "error");
        resetBoard();
    }
}

async function endGame() {
    if (state.roundId) {
        try {
            await postJSON("/api/end", { round_id: state.roundId });
        } catch (error) {
            console.debug("Unable to end round:", error);
        }
    }
    resetBoard();
    setStatus("Session ended. Configure settings and start again when ready.", "info");
}

function replayTone() {
    if (!state.toneSrc) {
        return;
    }
    playAudio(state.toneSrc);
}

async function resetProgress() {
    if (!elements.resetBtn) {
        return;
    }
    elements.resetBtn.disabled = true;
    try {
        await postJSON("/api/reset", {});
        await fetchStats();
        setStatus("Progress reset. Ready for a fresh start.", "info");
    } catch (error) {
        console.error(error);
        setStatus(error.message || "Unable to reset progress.", "error");
    } finally {
        elements.resetBtn.disabled = false;
    }
}

elements.startBtn.addEventListener("click", startGame);
elements.replayBtn.addEventListener("click", replayTone);
elements.endBtn.addEventListener("click", endGame);
if (elements.resetBtn) {
    elements.resetBtn.addEventListener("click", resetProgress);
}

fetchStats();
