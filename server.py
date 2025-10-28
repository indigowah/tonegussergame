import base64
import datetime as dt
import hashlib
import hmac
import os
import random
import secrets
import sqlite3
import threading
import time
import uuid
from io import BytesIO
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    try:
        from watchdog.observers.polling import PollingObserver
    except Exception:  # pragma: no cover - optional dependency path
        PollingObserver = Observer  # type: ignore
except Exception:  # pragma: no cover - watchdog optional
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore
    PollingObserver = None  # type: ignore

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - pillow should be installed
    raise RuntimeError("Pillow is required to render reports") from exc

BASE_DIR = Path(__file__).resolve().parent
CHINESE_AUDIO_DIR = BASE_DIR / "chinese_audio"
STATIC_DIR = BASE_DIR / "static"
FEEDBACK_DIR = BASE_DIR / "sounds" / "feedback"
TEMPLATES_DIR = BASE_DIR / "templates"
DATABASE_PATH = BASE_DIR / "tonegusser.sqlite3"
SECRET_KEY = os.getenv("TONEGUSSER_SECRET", "dev-secret-key-change-me").encode()
SESSION_COOKIE = "tonegusser_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # one week
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac"}
RECENT_EXCLUSION = 10

for directory in (STATIC_DIR, CHINESE_AUDIO_DIR, FEEDBACK_DIR, TEMPLATES_DIR):
    os.makedirs(directory, exist_ok=True)

def _ensure_directories() -> None:
    for path in (STATIC_DIR, CHINESE_AUDIO_DIR, FEEDBACK_DIR, TEMPLATES_DIR):
        os.makedirs(path, exist_ok=True)


class CatalogEventHandler(FileSystemEventHandler):
    def __init__(self, manager: "CatalogManager") -> None:
        self.manager = manager

    def on_any_event(self, event):  # type: ignore[override]
        if event.is_directory:
            return
        self.manager.request_refresh()


class CatalogManager:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = threading.Lock()
        self.catalog: Dict[str, Dict[str, object]] = {}
        self.catalog_version = 0
        self._pending_refresh = False
        self._observer = None
        self._stop_event = threading.Event()
        self._polling_thread: Optional[threading.Thread] = None
        self._start_watcher()
        self.refresh(force=True)

    def _start_watcher(self) -> None:
        if Observer is None:
            self._start_polling_fallback()
            return
        observer_cls = Observer
        if PollingObserver is not None:
            # Use polling to be more portable when file notifications are unavailable
            observer_cls = PollingObserver
        self.directory.mkdir(parents=True, exist_ok=True)
        self._observer = observer_cls()
        handler = CatalogEventHandler(self)
        self._observer.schedule(handler, str(self.directory), recursive=True)
        self._observer.daemon = True
        self._observer.start()

    def _start_polling_fallback(self) -> None:
        if self._polling_thread and self._polling_thread.is_alive():
            return

        def _poll() -> None:
            while not self._stop_event.wait(2.0):
                self.refresh()

        self._polling_thread = threading.Thread(target=_poll, daemon=True)
        self._polling_thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        if self._polling_thread:
            self._polling_thread.join(timeout=2)
            self._polling_thread = None

    def request_refresh(self) -> None:
        with self.lock:
            if not self._pending_refresh:
                self._pending_refresh = True
                threading.Thread(target=self.refresh, daemon=True).start()

    def refresh(self, force: bool = False) -> None:
        try:
            time.sleep(0.2)  # Debounce bursts of file change events
            files: Dict[str, Dict[str, object]] = {}
            if not self.directory.exists():
                self.directory.mkdir(parents=True, exist_ok=True)
            for path in sorted(self.directory.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    continue
                rel_path = path.relative_to(self.directory)
                sound_id = hashlib.sha1(str(rel_path).encode("utf-8")).hexdigest()[:16]
                difficulty = self._infer_difficulty(rel_path)
                files[sound_id] = {
                    "soundId": sound_id,
                    "path": str(rel_path).replace(os.sep, "/"),
                    "difficulty": difficulty,
                    "updatedAt": path.stat().st_mtime,
                }
            with self.lock:
                if not force and files == self.catalog:
                    self._pending_refresh = False
                    return
                self.catalog = files
                self.catalog_version += 1
                self._pending_refresh = False
        finally:
            with self.lock:
                self._pending_refresh = False

    def _infer_difficulty(self, relative_path: Path) -> int:
        try:
            # assume folder name contains difficulty (e.g., "2 - medium")
            first_part = str(relative_path.parent).split(os.sep)[0]
            digits = ''.join(ch for ch in first_part if ch.isdigit())
            if digits:
                return max(1, min(10, int(digits)))
        except Exception:
            pass
        return 1

    def get_catalog(self) -> Dict[str, Dict[str, object]]:
        with self.lock:
            return dict(self.catalog)

    def get_entry(self, sound_id: str) -> Optional[Dict[str, object]]:
        with self.lock:
            return self.catalog.get(sound_id)

    def choose_random(self, difficulty: Optional[int], excluded: List[str]) -> Dict[str, object]:
        with self.lock:
            items = list(self.catalog.values())
            if not items:
                raise HTTPException(status_code=404, detail="Catalog is empty")
            if difficulty is not None:
                items = [item for item in items if item["difficulty"] == difficulty]
            if difficulty is not None and not items:
                raise HTTPException(status_code=404, detail="No entries for requested difficulty")
            filtered = [item for item in items if item["soundId"] not in excluded]
            if filtered:
                items = filtered
            selection = random.choice(items)
            return selection

    def get_version(self) -> int:
        with self.lock:
            return self.catalog_version


_ensure_directories()
catalog_manager = CatalogManager(CHINESE_AUDIO_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_directories()
    catalog_manager.refresh(force=True)
    try:
        yield
    finally:
        catalog_manager.shutdown()


app = FastAPI(title="Tone Gusser Game", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/chinese_audio", StaticFiles(directory=CHINESE_AUDIO_DIR), name="chinese_audio")
app.mount("/sounds/feedback", StaticFiles(directory=FEEDBACK_DIR), name="feedback")

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _build_catalog_payload() -> List[Dict[str, Any]]:
    catalog = catalog_manager.get_catalog()
    grouped: Dict[str, Dict[str, Any]] = {}

    for entry in catalog.values():
        relative_path = Path(str(entry.get("path", "")))
        if not relative_path.parts:
            mode_key = "default"
        else:
            mode_key = relative_path.parts[0]

        mode = grouped.setdefault(
            mode_key,
            {
                "id": mode_key,
                "name": mode_key.replace("_", " ").title(),
                "items": [],
            },
        )

        item_name = relative_path.stem or entry.get("soundId") or "item"
        mode["items"].append(
            {
                "id": entry.get("soundId"),
                "name": item_name,
                "answer": item_name,
                "difficulty": entry.get("difficulty", 1),
                "url": f"/chinese_audio/{entry.get('path', '')}",
                "updatedAt": entry.get("updatedAt"),
            }
        )

    payload: List[Dict[str, Any]] = []
    for key in sorted(grouped):
        mode = grouped[key]
        mode["items"].sort(key=lambda item: (item.get("name") or "").lower())
        payload.append(mode)
    return payload


@app.get("/catalog.json")
def get_catalog() -> JSONResponse:
    payload = _build_catalog_payload()
    return JSONResponse(payload)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self._local = threading.local()
        self._initialize()

    def _initialize(self) -> None:
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    sound_id TEXT NOT NULL,
                    difficulty INTEGER NOT NULL,
                    correct INTEGER NOT NULL,
                    playback_ms INTEGER,
                    playback_count INTEGER,
                    catalog_version INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_attempts_user_created ON attempts(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_attempts_sound ON attempts(sound_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            setattr(self._local, "connection", conn)
        return conn

    @contextmanager
    def connection(self):
        conn = self._get_connection()
        with self.lock:
            yield conn
            conn.commit()


db = Database(DATABASE_PATH)


class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=255)


class AttemptRequest(BaseModel):
    soundId: str
    correct: bool
    difficulty: int = Field(ge=1, le=10)
    playbackMs: Optional[int] = Field(default=None, ge=0)
    playbackCount: Optional[int] = Field(default=None, ge=0)
    catalogVersion: Optional[int] = None


class AttemptResponse(BaseModel):
    success: bool
    catalogVersion: int


class GameNextResponse(BaseModel):
    soundId: str
    difficulty: int
    url: str
    catalogVersion: int


class StatsSummaryResponse(BaseModel):
    totalAttempts: int
    correctAttempts: int
    accuracy: float
    recentAttempts: List[Dict[str, object]]
    bestSounds: List[Dict[str, object]]
    worstSounds: List[Dict[str, object]]


class User(BaseModel):
    id: int
    username: str


class AuthenticatedUser(User):
    pass


class UnauthorizedError(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def _hash_password(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return {"salt": salt, "hash": hashed}


def _verify_password(password: str, salt: str, stored_hash: str) -> bool:
    hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(hashed, stored_hash)


def _sign_session(session_id: str) -> str:
    signature = hmac.new(SECRET_KEY, session_id.encode("utf-8"), hashlib.sha256).digest()
    token = f"{session_id}.{base64.urlsafe_b64encode(signature).decode('utf-8').rstrip('=')}"
    return token


def _unsign_session(token: str) -> Optional[str]:
    if "." not in token:
        return None
    session_id, signature_b64 = token.rsplit(".", 1)
    try:
        signature = base64.urlsafe_b64decode(signature_b64 + "==")
    except Exception:
        return None
    expected = hmac.new(SECRET_KEY, session_id.encode("utf-8"), hashlib.sha256).digest()
    if hmac.compare_digest(signature, expected):
        return session_id
    return None


def _now() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)


def _get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with db.connection() as conn:
        cur = conn.execute("SELECT * FROM users WHERE username = ?", (username.lower(),))
        return cur.fetchone()


def _get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with db.connection() as conn:
        cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cur.fetchone()


def _create_user(username: str, password: str) -> sqlite3.Row:
    username_key = username.lower()
    if _get_user_by_username(username_key):
        raise HTTPException(status_code=400, detail="Username already taken")
    creds = _hash_password(password)
    created_at = _now().isoformat()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username_key, creds["hash"], creds["salt"], created_at),
        )
        user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _create_session(user_id: int) -> str:
    session_id = uuid.uuid4().hex
    created_at = _now()
    expires_at = created_at + dt.timedelta(seconds=SESSION_TTL_SECONDS)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, created_at.isoformat(), expires_at.isoformat()),
        )
    return session_id


def _delete_session(session_id: str) -> None:
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def _get_session(session_id: str) -> Optional[sqlite3.Row]:
    with db.connection() as conn:
        cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
    if not row:
        return None
    expires_at = dt.datetime.fromisoformat(row["expires_at"])
    if expires_at < _now():
        _delete_session(session_id)
        return None
    return row


def _set_session_cookie(response: Response, session_id: str) -> None:
    token = _sign_session(session_id)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=False,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


async def get_current_user(request: Request) -> AuthenticatedUser:
    user = request.state.user
    if not user:
        raise UnauthorizedError()
    return user


@app.middleware("http")
async def load_user(request: Request, call_next):
    token = request.cookies.get(SESSION_COOKIE)
    request.state.user = None
    request.state.session_id = None
    if token:
        session_id = _unsign_session(token)
        if session_id:
            session = _get_session(session_id)
            if session:
                user_row = _get_user_by_id(session["user_id"])
                if user_row:
                    request.state.user = AuthenticatedUser(id=user_row["id"], username=user_row["username"])
                    request.state.session_id = session_id
    response = await call_next(request)
    return response


@app.post("/api/signup", response_model=User)
async def signup(payload: AuthRequest, response: Response) -> User:
    user_row = _create_user(payload.username, payload.password)
    session_id = _create_session(user_row["id"])
    _set_session_cookie(response, session_id)
    return User(id=user_row["id"], username=user_row["username"])


@app.post("/api/login", response_model=User)
async def login(payload: AuthRequest, response: Response) -> User:
    user_row = _get_user_by_username(payload.username.lower())
    if not user_row:
        raise HTTPException(status_code=400, detail="Invalid username or password")
    if not _verify_password(payload.password, user_row["salt"], user_row["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid username or password")
    session_id = _create_session(user_row["id"])
    _set_session_cookie(response, session_id)
    return User(id=user_row["id"], username=user_row["username"])


@app.post("/api/logout")
async def logout(request: Request, response: Response) -> JSONResponse:
    session_id = request.state.session_id
    if session_id:
        _delete_session(session_id)
    _clear_session_cookie(response)
    return JSONResponse({"success": True})


@app.get("/api/me", response_model=User)
async def me(user: AuthenticatedUser = Depends(get_current_user)) -> User:
    return user


def _recent_exclusions(user_id: int) -> List[str]:
    with db.connection() as conn:
        cur = conn.execute(
            "SELECT sound_id FROM attempts WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, RECENT_EXCLUSION),
        )
        return [row["sound_id"] for row in cur.fetchall()]


def _log_attempt(user_id: int, attempt: AttemptRequest) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO attempts (user_id, sound_id, difficulty, correct, playback_ms, playback_count, catalog_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                attempt.soundId,
                attempt.difficulty,
                1 if attempt.correct else 0,
                attempt.playbackMs,
                attempt.playbackCount,
                attempt.catalogVersion,
                _now().isoformat(),
            ),
        )


@app.get("/api/game/next", response_model=GameNextResponse)
async def game_next(
    difficulty: Optional[int] = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> GameNextResponse:
    if difficulty is not None and not (1 <= difficulty <= 10):
        raise HTTPException(status_code=400, detail="Difficulty out of bounds")
    excluded = _recent_exclusions(user.id)
    entry = catalog_manager.choose_random(difficulty, excluded)
    url = f"/chinese_audio/{entry['path']}"
    return GameNextResponse(
        soundId=entry["soundId"],
        difficulty=entry["difficulty"],
        url=url,
        catalogVersion=catalog_manager.get_version(),
    )


@app.post("/api/game/attempt", response_model=AttemptResponse)
async def game_attempt(
    payload: AttemptRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AttemptResponse:
    entry = catalog_manager.get_entry(payload.soundId)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown sound identifier")
    if payload.difficulty != entry["difficulty"]:
        raise HTTPException(status_code=400, detail="Difficulty mismatch")
    _log_attempt(user.id, payload)
    return AttemptResponse(success=True, catalogVersion=catalog_manager.get_version())


def _summary_stats(user_id: int) -> StatsSummaryResponse:
    window_start = (_now() - dt.timedelta(days=7)).isoformat()
    with db.connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) as total, SUM(correct) as correct FROM attempts WHERE user_id = ? AND created_at >= ?",
            (user_id, window_start),
        )
        row = cur.fetchone()
        total = row["total"] or 0
        correct = row["correct"] or 0
        cur = conn.execute(
            """
            SELECT sound_id, SUM(correct) AS correct, COUNT(*) AS total
            FROM attempts
            WHERE user_id = ? AND created_at >= ?
            GROUP BY sound_id
            """,
            (user_id, window_start),
        )
        performance = []
        for sound_row in cur.fetchall():
            entry = catalog_manager.get_entry(sound_row["sound_id"])
            difficulty = entry["difficulty"] if entry else None
            performance.append(
                {
                    "soundId": sound_row["sound_id"],
                    "correct": sound_row["correct"],
                    "total": sound_row["total"],
                    "accuracy": (sound_row["correct"] or 0) / (sound_row["total"] or 1),
                    "difficulty": difficulty,
                }
            )
        performance.sort(key=lambda item: item["accuracy"], reverse=True)
        best = performance[:5]
        worst = sorted(performance, key=lambda item: item["accuracy"])[:5]
        cur = conn.execute(
            """
            SELECT sound_id, difficulty, correct, created_at
            FROM attempts
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 25
            """,
            (user_id,),
        )
        recent = [dict(row) for row in cur.fetchall()]
    accuracy = (correct / total) if total else 0.0
    return StatsSummaryResponse(
        totalAttempts=total,
        correctAttempts=correct,
        accuracy=round(accuracy, 4),
        recentAttempts=recent,
        bestSounds=best,
        worstSounds=worst,
    )


@app.get("/api/stats/summary", response_model=StatsSummaryResponse)
async def stats_summary(user: AuthenticatedUser = Depends(get_current_user)) -> StatsSummaryResponse:
    return _summary_stats(user.id)


CATPPUCCIN_COLORS = {
    "base": "#1e1e2e",
    "surface": "#302d41",
    "text": "#cdd6f4",
    "accent": "#b4befe",
    "green": "#a6e3a1",
    "red": "#f38ba8",
    "yellow": "#f9e2af",
}


def _draw_donut(draw: ImageDraw.ImageDraw, center, radius, percentage: float) -> None:
    start_angle = -90
    end_angle = start_angle + int(360 * percentage)
    bbox = [
        center[0] - radius,
        center[1] - radius,
        center[0] + radius,
        center[1] + radius,
    ]
    draw.pieslice(bbox, start=start_angle, end=end_angle, fill=CATPPUCCIN_COLORS["green"])
    draw.pieslice(bbox, start=end_angle, end=start_angle + 360, fill=CATPPUCCIN_COLORS["surface"])
    inner_radius = radius * 0.55
    inner_bbox = [
        center[0] - inner_radius,
        center[1] - inner_radius,
        center[0] + inner_radius,
        center[1] + inner_radius,
    ]
    draw.ellipse(inner_bbox, fill=CATPPUCCIN_COLORS["base"])


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


@app.get("/api/report.png")
async def report_png(user: AuthenticatedUser = Depends(get_current_user)) -> Response:
    stats = _summary_stats(user.id)
    img = Image.new("RGB", (1200, 628), CATPPUCCIN_COLORS["base"])
    draw = ImageDraw.Draw(img)
    title_font = _load_font(48)
    subtitle_font = _load_font(28)
    body_font = _load_font(24)

    draw.text((60, 40), "Tone Gusser Report", fill=CATPPUCCIN_COLORS["text"], font=title_font)
    draw.text((60, 110), f"User: {user.username}", fill=CATPPUCCIN_COLORS["accent"], font=subtitle_font)

    center = (320, 320)
    _draw_donut(draw, center, 200, stats.accuracy)
    accuracy_text = f"{int(stats.accuracy * 100)}% accuracy"
    w, h = draw.textsize(accuracy_text, font=subtitle_font)
    draw.text((center[0] - w / 2, center[1] - h / 2), accuracy_text, fill=CATPPUCCIN_COLORS["text"], font=subtitle_font)

    draw.text((560, 180), "Recent Attempts", fill=CATPPUCCIN_COLORS["text"], font=subtitle_font)
    for idx, attempt in enumerate(stats.recentAttempts[:10]):
        line = f"{attempt['created_at'][:16]} — {attempt['sound_id']} ({'✓' if attempt['correct'] else '✗'})"
        draw.text((560, 220 + idx * 30), line, fill=CATPPUCCIN_COLORS["text"], font=body_font)

    draw.text((560, 520), "Best", fill=CATPPUCCIN_COLORS["green"], font=subtitle_font)
    for idx, entry in enumerate(stats.bestSounds):
        line = f"{entry['soundId']} — {int(entry['accuracy'] * 100)}%"
        draw.text((560, 560 + idx * 30), line, fill=CATPPUCCIN_COLORS["text"], font=body_font)

    draw.text((860, 520), "Needs Work", fill=CATPPUCCIN_COLORS["red"], font=subtitle_font)
    for idx, entry in enumerate(stats.worstSounds):
        line = f"{entry['soundId']} — {int(entry['accuracy'] * 100)}%"
        draw.text((860, 560 + idx * 30), line, fill=CATPPUCCIN_COLORS["text"], font=body_font)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return Response(content=buffer.getvalue(), media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


__all__ = ["app"]
