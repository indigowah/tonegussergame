from __future__ import annotations

import base64
import dataclasses
import io
import os
import random
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence
from uuid import uuid4
import unicodedata

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)


BASE_DIR = Path(__file__).parent
AUDIO_ROOT = BASE_DIR / "chinese_audio"
FEEDBACK_ROOT = BASE_DIR / "sounds" / "feedback"
DATABASE_PATH = BASE_DIR / "tone_stats.db"

CATPPUCCIN = {
    "crust": "#11111b",
    "mantle": "#181825",
    "base": "#1e1e2e",
    "surface0": "#313244",
    "surface1": "#45475a",
    "text": "#cdd6f4",
    "green": "#a6e3a1",
    "red": "#f38ba8",
    "blue": "#89b4fa",
    "yellow": "#f9e2af",
    "peach": "#fab387",
    "teal": "#94e2d5",
    "lavender": "#b4befe",
}


def normalize_label(label: str) -> str:
    return unicodedata.normalize("NFC", label)


@dataclasses.dataclass(slots=True)
class ToneClip:
    difficulty: str
    file_name: str
    label: str


@dataclasses.dataclass(slots=True)
class RoundState:
    id: str
    difficulty: str
    file_name: str
    correct_label: str
    correct_label_norm: str
    options: List[str]
    option_count: int
    selected_difficulties: List[str]
    attempts: int = 0


def discover_tones() -> Dict[str, List[ToneClip]]:
    tones: Dict[str, List[ToneClip]] = {}
    if not AUDIO_ROOT.exists():
        return tones
    for difficulty_dir in sorted(AUDIO_ROOT.iterdir()):
        if not difficulty_dir.is_dir():
            continue
        difficulty = difficulty_dir.name
        entries: List[ToneClip] = []
        for file_path in difficulty_dir.glob("*.mp3"):
            entries.append(
                ToneClip(
                    difficulty=difficulty,
                    file_name=file_path.name,
                    label=normalize_label(file_path.stem),
                )
            )
        if entries:
            tones[difficulty] = entries
    return tones


AVAILABLE_TONES = discover_tones()
ROUND_LOCK = threading.Lock()
ACTIVE_ROUNDS: Dict[str, RoundState] = {}


def ensure_database() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                round_id TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                tone_label TEXT NOT NULL,
                chosen_label TEXT NOT NULL,
                is_correct INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL,
                option_count INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def record_guess(round_state: RoundState, choice_label: str, is_correct: bool) -> int:
    timestamp = datetime.utcnow().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO guesses (
                timestamp,
                round_id,
                difficulty,
                tone_label,
                chosen_label,
                is_correct,
                attempt_number,
                option_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                round_state.id,
                round_state.difficulty,
                round_state.correct_label,
                choice_label,
                1 if is_correct else 0,
                round_state.attempts,
                round_state.option_count,
            ),
        )
        conn.commit()
    return round_state.attempts


def pick_round(
    difficulties: Sequence[str],
    option_count: int,
) -> RoundState:
    pools: List[ToneClip] = []
    for diff in difficulties:
        if diff not in AVAILABLE_TONES:
            continue
        pools.extend(AVAILABLE_TONES[diff])
    if not pools:
        raise ValueError("No audio clips available for the chosen difficulties.")

    target = random.choice(pools)
    other_choices = [
        clip for clip in pools if normalize_label(clip.label) != normalize_label(target.label)
    ]
    random.shuffle(other_choices)
    selected = other_choices[: max(0, option_count - 1)]
    options_labels = [normalize_label(target.label)] + [
        normalize_label(clip.label) for clip in selected
    ]
    random.shuffle(options_labels)

    round_id = str(uuid4())
    return RoundState(
        id=round_id,
        difficulty=target.difficulty,
        file_name=target.file_name,
        correct_label=target.label,
        correct_label_norm=normalize_label(target.label),
        options=options_labels,
        option_count=option_count,
        selected_difficulties=list(difficulties),
    )


def round_payload(round_state: RoundState) -> dict:
    audio_url = url_for(
        "serve_audio",
        difficulty=round_state.difficulty,
        filename=round_state.file_name,
    )
    return {
        "id": round_state.id,
        "difficulty": round_state.difficulty,
        "audio_url": audio_url,
        "options": round_state.options,
        "option_count": round_state.option_count,
    }


def get_accuracy_by_difficulty(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        """
        SELECT difficulty, SUM(is_correct) AS correct, COUNT(*) AS total
        FROM guesses
        GROUP BY difficulty
        ORDER BY difficulty
        """
    ).fetchall()
    return [
        {
            "difficulty": row["difficulty"],
            "correct": int(row["correct"]),
            "total": int(row["total"]),
            "accuracy": (row["correct"] / row["total"]) if row["total"] else 0.0,
        }
        for row in rows
    ]


def get_rolling_accuracy(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        """
        SELECT timestamp, is_correct
        FROM guesses
        ORDER BY datetime(timestamp)
        """
    ).fetchall()
    total = 0
    correct = 0
    points = []
    for row in rows:
        total += 1
        correct += int(row["is_correct"])
        accuracy = correct / total if total else 0.0
        points.append(
            {
                "timestamp": row["timestamp"],
                "accuracy": accuracy,
            }
        )
    return points


def make_bar_chart(data: List[dict]) -> str:
    fig, ax = plt.subplots(figsize=(5.5, 3.0))
    ax.set_facecolor(CATPPUCCIN["base"])
    fig.patch.set_facecolor(CATPPUCCIN["base"])

    if not data:
        ax.text(
            0.5,
            0.5,
            "No data yet",
            color=CATPPUCCIN["text"],
            ha="center",
            va="center",
            fontsize=14,
        )
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        labels = [entry["difficulty"] for entry in data]
        values = [entry["accuracy"] * 100 for entry in data]
        colors = [CATPPUCCIN["teal"], CATPPUCCIN["blue"], CATPPUCCIN["lavender"]]
        chosen_colors = (colors * ((len(values) // len(colors)) + 1))[: len(values)]
        bars = ax.bar(labels, values, color=chosen_colors)
        ax.set_ylim(0, max(100, max(values) + 5 if values else 100))
        ax.set_ylabel("Accuracy (%)", color=CATPPUCCIN["text"])
        ax.tick_params(colors=CATPPUCCIN["text"])
        ax.spines[:].set_color(CATPPUCCIN["surface1"])
        ax.set_title("Accuracy by Difficulty", color=CATPPUCCIN["text"])
        for bar, value in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 1,
                f"{value:.1f}%",
                ha="center",
                va="bottom",
                color=CATPPUCCIN["text"],
                fontsize=10,
            )

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def make_line_chart(points: List[dict]) -> str:
    fig, ax = plt.subplots(figsize=(5.5, 3.0))
    ax.set_facecolor(CATPPUCCIN["base"])
    fig.patch.set_facecolor(CATPPUCCIN["base"])

    if len(points) < 2:
        ax.text(
            0.5,
            0.5,
            "Play some rounds to see progress!",
            color=CATPPUCCIN["text"],
            ha="center",
            va="center",
            fontsize=14,
        )
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        x = list(range(1, len(points) + 1))
        y = [p["accuracy"] * 100 for p in points]
        ax.plot(x, y, color=CATPPUCCIN["peach"], linewidth=2)
        ax.fill_between(x, y, color=CATPPUCCIN["peach"], alpha=0.2)
        ax.set_ylim(0, 100)
        ax.set_xlim(1, len(points))
        ax.set_xlabel("Guess #", color=CATPPUCCIN["text"])
        ax.set_ylabel("Cumulative Accuracy (%)", color=CATPPUCCIN["text"])
        ax.tick_params(colors=CATPPUCCIN["text"])
        ax.spines[:].set_color(CATPPUCCIN["surface1"])
        ax.set_title("Cumulative Accuracy", color=CATPPUCCIN["text"])

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def gather_summary(conn: sqlite3.Connection) -> dict:
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total_guesses,
            SUM(is_correct) AS total_correct,
            SUM(CASE WHEN attempt_number = 1 AND is_correct = 1 THEN 1 ELSE 0 END) AS first_try
        FROM guesses
        """
    ).fetchone()

    total_guesses = int(totals["total_guesses"] or 0)
    total_correct = int(totals["total_correct"] or 0)
    first_try = int(totals["first_try"] or 0)

    rounds_completed = conn.execute(
        """
        SELECT COUNT(DISTINCT round_id) AS rounds_won
        FROM guesses
        WHERE is_correct = 1
        """
    ).fetchone()["rounds_won"]

    attempts_per_round = conn.execute(
        """
        SELECT AVG(attempts) AS avg_attempts FROM (
            SELECT MAX(attempt_number) AS attempts
            FROM guesses
            WHERE is_correct = 1
            GROUP BY round_id
        )
        """
    ).fetchone()["avg_attempts"]

    return {
        "total_guesses": total_guesses,
        "total_correct": total_correct,
        "accuracy": (total_correct / total_guesses) if total_guesses else 0.0,
        "rounds_completed": int(rounds_completed or 0),
        "first_try_success": first_try,
        "average_attempts_per_round": float(attempts_per_round or 0.0),
    }


def get_tone_extremes(conn: sqlite3.Connection, limit: int = 5) -> dict:
    rows = conn.execute(
        """
        SELECT tone_label, SUM(is_correct) AS correct, COUNT(*) AS total
        FROM guesses
        GROUP BY tone_label
        HAVING total > 0
        """
    ).fetchall()

    tones = []
    for row in rows:
        total = int(row["total"] or 0)
        correct = int(row["correct"] or 0)
        accuracy = (correct / total) if total else 0.0
        tones.append(
            {
                "label": row["tone_label"],
                "total": total,
                "correct": correct,
                "accuracy": accuracy,
            }
        )

    if not tones:
        return {"best": [], "worst": []}

    sorted_best = sorted(
        tones,
        key=lambda entry: (-entry["accuracy"], -entry["total"], entry["label"]),
    )
    sorted_worst = sorted(
        tones,
        key=lambda entry: (entry["accuracy"], -entry["total"], entry["label"]),
    )

    return {
        "best": sorted_best[:limit],
        "worst": sorted_worst[:limit],
    }


def build_stats_payload() -> dict:
    with get_connection() as conn:
        summary = gather_summary(conn)
        accuracy = get_accuracy_by_difficulty(conn)
        rolling = get_rolling_accuracy(conn)
        tone_extremes = get_tone_extremes(conn)

    return {
        "summary": summary,
        "graphs": {
            "accuracy_by_difficulty": make_bar_chart(accuracy),
            "cumulative_accuracy": make_line_chart(rolling),
        },
        "tones": tone_extremes,
    }


app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["JSON_SORT_KEYS"] = False


@app.before_request
def startup() -> None:
    ensure_database()


@app.route("/")
def index() -> str:
    return render_template(
        "index.html",
        difficulties=sorted(AVAILABLE_TONES.keys()),
    )


def parse_difficulties(data: dict) -> List[str]:
    requested = data.get("difficulties") or []
    if not isinstance(requested, list):
        raise ValueError("difficulties must be a list.")
    normalized = []
    for item in requested:
        if not isinstance(item, str):
            continue
        label = item.strip()
        if label in AVAILABLE_TONES and label not in normalized:
            normalized.append(label)
    if not normalized:
        raise ValueError("Choose at least one difficulty.")
    return normalized


def parse_option_count(data: dict) -> int:
    raw = data.get("option_count") or 4
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("option_count must be an integer.")
    return max(2, min(8, value))


def create_round_response(difficulties: Sequence[str], option_count: int) -> dict:
    round_state = pick_round(difficulties, option_count)
    with ROUND_LOCK:
        ACTIVE_ROUNDS[round_state.id] = round_state
    return {
        "round": round_payload(round_state),
    }


@app.route("/api/start", methods=["POST"])
def api_start() -> tuple:
    data = request.get_json(silent=True) or {}
    try:
        difficulties = parse_difficulties(data)
        option_count = parse_option_count(data)
        response = create_round_response(difficulties, option_count)
        return jsonify(response)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/next", methods=["POST"])
def api_next() -> tuple:
    data = request.get_json(silent=True) or {}
    try:
        difficulties = parse_difficulties(data)
        option_count = parse_option_count(data)
        response = create_round_response(difficulties, option_count)
        return jsonify(response)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/guess", methods=["POST"])
def api_guess() -> tuple:
    data = request.get_json(silent=True) or {}
    round_id = data.get("round_id")
    choice = data.get("choice")
    if not round_id or not isinstance(round_id, str):
        return jsonify({"error": "Missing round_id."}), 400
    if not choice or not isinstance(choice, str):
        return jsonify({"error": "Missing choice."}), 400

    normalized_choice = normalize_label(choice)

    with ROUND_LOCK:
        round_state = ACTIVE_ROUNDS.get(round_id)
        if not round_state:
            return jsonify({"error": "Round expired. Start a new game."}), 400
        round_state.attempts += 1
        attempt_number = round_state.attempts
        is_correct = normalized_choice == round_state.correct_label_norm
        if is_correct:
            ACTIVE_ROUNDS.pop(round_id, None)

    record_guess(round_state, normalized_choice, is_correct)

    feedback = url_for(
        "serve_feedback_audio",
        filename="correct.mp3" if is_correct else "wrong.mp3",
    )

    return jsonify(
        {
            "correct": is_correct,
            "attempt_number": attempt_number,
            "feedback_audio": feedback,
            "correct_label": round_state.correct_label if is_correct else None,
        }
    )


@app.route("/api/stats", methods=["GET"])
def api_stats() -> tuple:
    payload = build_stats_payload()
    return jsonify(payload)


@app.route("/api/end", methods=["POST"])
def api_end() -> tuple:
    data = request.get_json(silent=True) or {}
    round_id = data.get("round_id")
    if round_id:
        with ROUND_LOCK:
            ACTIVE_ROUNDS.pop(round_id, None)
    return jsonify({"status": "ended"})


@app.route("/api/reset", methods=["POST"])
def api_reset() -> tuple:
    with get_connection() as conn:
        conn.execute("DELETE FROM guesses")
        conn.commit()
    return jsonify({"status": "reset"})


@app.route("/audio/<difficulty>/<path:filename>")
def serve_audio(difficulty: str, filename: str):
    directory = AUDIO_ROOT / difficulty
    if not directory.exists():
        return jsonify({"error": "Audio not found"}), 404
    return send_from_directory(directory, filename)


@app.route("/feedback/<path:filename>")
def serve_feedback_audio(filename: str):
    directory = FEEDBACK_ROOT
    if not directory.exists():
        return jsonify({"error": "Feedback not found"}), 404
    return send_from_directory(directory, filename)


if __name__ == "__main__":
    ensure_database()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
