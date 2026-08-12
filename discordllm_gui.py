# discordllm_gui.py (v2.0 — hide to tray + restore GUI, optional console hide on Windows)
#
# Requirements:
#   pip install discord.py aiohttp python-dotenv pystray pillow
# Python 3.10+
#
# Notes (Windows):
# - For no console at all, run with pythonw.exe or rename to .pyw.
# - This file also includes a best-effort console hide if you run it with python.exe.

import os, sys, json, asyncio, threading, time, re, subprocess, shutil, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter, defaultdict, deque
from typing import Any, Dict, List, Tuple, Optional

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ---------------- GUI -----------------
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

# ---------------- Stdlib HTTP helpers for Ollama tags ----------------
from urllib.parse import urlparse, unquote
import urllib.request as ureq

# ---------------- Tray (system tray icon) ----------------
TRAY_AVAILABLE = True
try:
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw
except Exception:
    TRAY_AVAILABLE = False

# --------------- Config & .env helpers ---------------
APP_DIR = Path(__file__).parent
SETTINGS_PATH = APP_DIR / "discordllm_gui.settings.json"
load_dotenv(APP_DIR / ".env")  # initial load

# --------------- agi-memory (optional) ---------------
_AGI_MEMORY_DIR = APP_DIR / "agi-memory"
if _AGI_MEMORY_DIR.is_dir() and str(_AGI_MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(_AGI_MEMORY_DIR))

AGI_MEMORY_AVAILABLE = False
AGI_MEMORY_IMPORT_ERROR = ""
try:
    from cognitive_memory_api import (
        CognitiveMemory, format_context_for_prompt, get_personhood_prompt,
        MemoryType, GoalPriority, GoalSource, HydratedContext,
    )
    AGI_MEMORY_AVAILABLE = True
except Exception as exc:
    AGI_MEMORY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _agi_memory_unavailable_reason() -> str:
    if AGI_MEMORY_AVAILABLE:
        return ""
    if not _AGI_MEMORY_DIR.is_dir():
        return "agi-memory/ is missing from the project directory."
    if AGI_MEMORY_IMPORT_ERROR:
        return AGI_MEMORY_IMPORT_ERROR
    return "Unknown import failure."


LOCAL_HEARTBEAT_WORKER_PATH = _AGI_MEMORY_DIR / "worker.py"
LOCAL_HEARTBEAT_WORKER_LOG_PATH = _AGI_MEMORY_DIR / "heartbeat_worker.local.log"


class HeartbeatOutboxDeferred(RuntimeError):
    """Transient delivery block; leave the proposal pending for retry."""

# --------------- Lightweight NLP helpers ---------------
_STOPWORDS = frozenset({
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it", "they", "them",
    "this", "that", "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "can", "may", "might", "shall", "must", "a", "an", "the", "and", "but", "or",
    "if", "then", "so", "no", "not", "of", "in", "on", "at", "to", "for", "with",
    "by", "from", "up", "about", "into", "through", "during", "before", "after",
    "above", "below", "between", "out", "off", "over", "under", "again", "further",
    "too", "very", "just", "don", "now", "here", "there", "when", "where", "why",
    "how", "what", "which", "who", "whom", "all", "each", "every", "both", "few",
    "more", "most", "other", "some", "such", "than", "also", "as", "like",
    "lol", "lmao", "haha", "yeah", "yep", "nah", "ok", "okay", "hey", "hi",
    "hello", "thanks", "thank", "please", "pls", "idk", "tbh", "imo", "gonna",
    "wanna", "gotta", "kinda", "really", "actually", "basically", "literally",
    "pretty", "quite", "um", "uh", "hmm", "oh", "ah", "well", "anyway",
    "got", "get", "go", "going", "went", "come", "came", "make", "made",
    "say", "said", "tell", "told", "know", "knew", "think", "thought",
    "want", "need", "let", "thing", "things", "something", "anything",
    "much", "many", "lot", "lots", "even", "still", "already", "yet",
})

_POSITIVE_WORDS = frozenset({
    "good", "great", "awesome", "amazing", "love", "happy", "glad",
    "excellent", "wonderful", "fantastic", "nice", "cool",
    "perfect", "beautiful", "fun", "enjoy", "best", "helpful", "appreciate",
    "brilliant", "impressive", "solid", "sweet", "neat", "superb", "excited",
})

_NEGATIVE_WORDS = frozenset({
    "bad", "terrible", "awful", "hate", "angry", "sad", "wrong", "broken",
    "error", "fail", "failed", "bug", "crash", "problem", "issue", "annoying",
    "frustrated", "worse", "worst", "ugly", "stupid", "sucks", "horrible",
    "confused", "stuck", "slow", "laggy", "painful", "disappointing",
})

_FACT_PATTERNS = re.compile(
    r"(?:my name is|i'?m called|i am from|i work at|i work as|i live in|i study"
    r"|the .{2,30} is |.{2,30} means |.{2,30} equals |defined as |refers to "
    r"|the answer is |it'?s called |known as |born in |created by )",
    re.IGNORECASE,
)


def _extract_concepts(text: str, max_concepts: int = 5) -> list[str]:
    """Pull meaningful keywords from text for concept tagging."""
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())
    meaningful = [w for w in words if w not in _STOPWORDS and len(w) > 2]
    freq: dict[str, int] = {}
    for w in meaningful:
        freq[w] = freq.get(w, 0) + 1
    return sorted(freq, key=lambda w: (-freq[w], w))[:max_concepts]


def _estimate_valence(text: str) -> float:
    """Cheap sentiment: returns float in [-1, 1]."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 2)


def _refine_query(text: str) -> str:
    """Strip filler words for better vector search."""
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text)
    meaningful = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2]
    if len(meaningful) < 2:
        return text
    return " ".join(meaningful)


def _memory_dsn() -> str:
    """Read the optional memory DSN from the process environment."""
    return os.getenv("MEMORY_DSN", "").strip()


DEFAULT_MEMORY_DSN = _memory_dsn()


def _hide_console_if_present():
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _env_token() -> str:
    t = os.getenv("DISCORD_TOKEN", "").strip().strip('""').strip("''")
    if t.lower().startswith("bot "):
        t = t[4:].strip()
    return t


def _sanitize_token(text: str) -> str:
    if not text:
        return ""
    t = text.strip().strip('""').strip("''")
    if t.lower().startswith("bot "):
        t = t[4:].strip()
    return t


DEFAULT_TOKEN = _env_token()
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "gpt-oss:20b")
DEFAULT_SYS_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Be concise. No chain of thought. Answer in 1-2 sentences unless code is requested.",
)
DEFAULT_MAX_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))
CHUNK_SIZE = 1900
VOICE_LOGGER_NAMES = (
    "discord.voice_state",
    "discord.voice_client",
    "discord.gateway",
)

# Greeting defaults
DEFAULT_GREET_MESSAGE = "Glad to have you back, {mention}!"
DEFAULT_GREET_COOLDOWN_SEC = 600  # 10 minutes

# Heartbeat defaults
DEFAULT_HEARTBEAT_ENABLED = False
DEFAULT_HEARTBEAT_SHADOW_MODE = True
DEFAULT_HEARTBEAT_MANUAL_APPROVAL = True
DEFAULT_HEARTBEAT_QUIET_HOURS_START = "23:00"
DEFAULT_HEARTBEAT_QUIET_HOURS_END = "08:00"
DEFAULT_HEARTBEAT_MAX_POSTS_PER_HOUR = 1
DEFAULT_HEARTBEAT_MAX_POSTS_PER_DAY = 4
DEFAULT_HEARTBEAT_MIN_RECENT_HUMAN_MESSAGES = 2
DEFAULT_HEARTBEAT_DEAD_CHANNEL_THRESHOLD_MINUTES = 90
DEFAULT_HEARTBEAT_REQUIRE_REPLY_CONTEXT = True
DEFAULT_HEARTBEAT_SERVER_TYPING = False
HEARTBEAT_ACTIVITY_FLUSH_INTERVAL_SECONDS = 30
HEARTBEAT_ACTIVITY_MAX_BUFFER_SECONDS = 90
HEARTBEAT_ACTIVITY_BATCH_SIZE = 4
HEARTBEAT_ACTIVITY_TTL_SECONDS = 6 * 3600
HEARTBEAT_ACTIVITY_PREVIEW_CHARS = 180

# Voice command defaults
DEFAULT_QWEN_PROJECT_DIR = Path(os.getenv("QWEN_TTS_PROJECT_DIR", "qwen-tts")).expanduser()
DEFAULT_QWEN_MODE = os.getenv("QWEN_TTS_MODE", "clone").strip().lower()
DEFAULT_QWEN_LANGUAGE = os.getenv("QWEN_TTS_LANGUAGE", "English").strip() or "English"
DEFAULT_QWEN_SPEAKER = os.getenv("QWEN_TTS_SPEAKER", "Ryan").strip() or "Ryan"
DEFAULT_QWEN_REF_INDEX = int(os.getenv("QWEN_TTS_REF_INDEX", "0"))
DEFAULT_QWEN_TIMEOUT_SEC = int(os.getenv("QWEN_TTS_TIMEOUT_SEC", "900"))
DEFAULT_QWEN_MAX_CHARS = int(os.getenv("QWEN_TTS_MAX_CHARS", "600"))
DEFAULT_FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg").strip() or "ffmpeg"
DEFAULT_YTDLP_PATH = (
    os.getenv("YT_DLP_PATH", "").strip()
    or os.getenv("YTDLP_PATH", "").strip()
)
DEFAULT_VOICE_PLAYLIST_LIMIT = max(1, int(os.getenv("VOICE_PLAYLIST_LIMIT", "100")))
DEFAULT_QWEN_CUSTOM_SPEAKERS = [
    "Ryan",
    "Vivian",
    "Aiden",
    "Dylan",
    "Eric",
    "Serena",
    "Sohee",
    "Uncle_Fu",
    "Ono_Anna",
]


def _tags_url_from_chat_url(chat_url: str) -> str:
    try:
        p = urlparse(chat_url)
        return f"{p.scheme}://{p.netloc}/api/tags"
    except Exception:
        return "http://127.0.0.1:11434/api/tags"


def _ollama_openai_base_url_from_chat_url(chat_url: str) -> str:
    try:
        p = urlparse(chat_url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/v1"
    except Exception:
        pass
    return "http://127.0.0.1:11434/v1"


def _format_duration(seconds: object) -> str:
    try:
        total = int(seconds)
    except Exception:
        return ""
    if total <= 0:
        return ""
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


BALLERJIT_PUBLIC_COMMANDS = "\n".join(
    [
        "**Ballerjit Commands**",
        "`!jith` - show ballerjit commands.",
        "`!help` - show ballerjit commands.",
        "`!jit <message>` - ask the local model.",
        "`!jitd <message>` - ask the model and return an MP3 voice reply.",
        "`!jitm` - show now playing and queue.",
        "`!jitm join` - join your current voice channel.",
        "`!jitm play <query>` - play now, or queue if something is already playing.",
        "`!jitm queue <query>` - add a track to the queue.",
        "`!jitm playlist <url>` - import a playlist into the queue.",
        "`!jitm pause|resume|skip|stop|clear|leave` - control playback.",
        "`!recall <query>` - search memories by relevance.",
        "`!greet status` - show your greeting status in this server.",
        "`!greet on` / `!greet off` - enable or disable greeting for you.",
        "`!greet prompt <text>` - set your personal greeting style.",
        "`!greet prompt reset` - clear your personal greeting style.",
        "`!greet test` - preview your greeting line now.",
    ]
)

BALLERJIT_ALL_COMMANDS = "\n".join(
    [
        "**Ballerjit Command Reference**",
        "This tab is local-only. Discord users do not see it.",
        "",
        "Public Discord commands:",
        "`!jith` - show ballerjit commands.",
        "`!help` - show ballerjit commands.",
        "`!jit <message>` - ask the local model.",
        "`!jitd <message>` - ask the model and return an MP3 voice reply.",
        "`!jitm` - show now playing and queue.",
        "`!jitm join` - join your current voice channel.",
        "`!jitm play <query>` - play now, or queue if something is already playing.",
        "`!jitm queue <query>` - add a track to the queue.",
        "`!jitm playlist <url>` - import a playlist into the queue.",
        "`!jitm pause` - pause playback.",
        "`!jitm resume` - resume playback.",
        "`!jitm skip` - skip the current track.",
        "`!jitm stop` - stop playback and clear the queue.",
        "`!jitm clear` - clear queued tracks.",
        "`!jitm leave` - disconnect from voice.",
        "`!recall <query>` - search memories by relevance.",
        "`!greet status` - show your greeting status in this server.",
        "`!greet on` / `!greet off` - enable or disable greeting for you.",
        "`!greet prompt <text>` - set your personal greeting style.",
        "`!greet prompt reset` - clear your personal greeting style.",
        "`!greet test` - preview your greeting line now.",
        "",
        "Hidden or owner-only reference commands:",
        "`!low [text|reset]` - show or set the lower-priority community prompt.",
        "`!clear` - clear your chat history for this channel.",
        "`!remember <text>` - store an explicit memory.",
        "`!goals [add <title>]` - list or create goals.",
        "`!memory [health|identity|emotions]` - memory system status.",
    ]
)


# --------------- Bot Runner ---------------
class BotRunner:
    def __init__(self, gui_callbacks):
        self.gui_callbacks = gui_callbacks
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.running = False

        # dynamic config controlled by GUI
        self.token = DEFAULT_TOKEN
        self.ollama_url = DEFAULT_OLLAMA_URL
        self.model = DEFAULT_MODEL
        self.system_prompt = DEFAULT_SYS_PROMPT
        self.community_prompt = ""
        self.max_history = DEFAULT_MAX_TURNS

        # Greeting settings (per guild)
        self.greet_settings: Dict[int, dict] = {}
        self._last_greet: Dict[Tuple[int, int], float] = {}
        self.global_cooldown_sec = DEFAULT_GREET_COOLDOWN_SEC

        # Cognitive memory (agi-memory)
        self.memory_enabled: bool = True
        self.memory_dsn: str = DEFAULT_MEMORY_DSN
        self.memory: object | None = None  # CognitiveMemory when connected
        self.memory_hydrate_limit: int = 5
        self.memory_formation_enabled: bool = True
        self.use_personhood_prompt: bool = False

        # Heartbeat policy (GUI-configured; delivery layer not wired yet)
        self.heartbeat_enabled: bool = DEFAULT_HEARTBEAT_ENABLED
        self.heartbeat_shadow_mode: bool = DEFAULT_HEARTBEAT_SHADOW_MODE
        self.heartbeat_manual_approval: bool = DEFAULT_HEARTBEAT_MANUAL_APPROVAL
        self.heartbeat_home_guild_id: int | None = None
        self.heartbeat_allowed_channel_ids: set[int] = set()
        self.heartbeat_server_configs: dict[int, dict[str, Any]] = {}
        self.heartbeat_quiet_hours_start: str = DEFAULT_HEARTBEAT_QUIET_HOURS_START
        self.heartbeat_quiet_hours_end: str = DEFAULT_HEARTBEAT_QUIET_HOURS_END
        self.heartbeat_max_posts_per_hour: int = DEFAULT_HEARTBEAT_MAX_POSTS_PER_HOUR
        self.heartbeat_max_posts_per_day: int = DEFAULT_HEARTBEAT_MAX_POSTS_PER_DAY
        self.heartbeat_min_recent_human_messages: int = DEFAULT_HEARTBEAT_MIN_RECENT_HUMAN_MESSAGES
        self.heartbeat_dead_channel_threshold_minutes: int = DEFAULT_HEARTBEAT_DEAD_CHANNEL_THRESHOLD_MINUTES
        self.heartbeat_require_reply_context: bool = DEFAULT_HEARTBEAT_REQUIRE_REPLY_CONTEXT

        # Voice command (!jitd) settings
        self.jitd_enabled: bool = False
        self.qwen_project_dir: Path = DEFAULT_QWEN_PROJECT_DIR
        self.qwen_python: Path = self.qwen_project_dir / "venv" / "Scripts" / "python.exe"
        self.qwen_script: Path = self.qwen_project_dir / "qwen_voice.py"
        self.qwen_mode: str = DEFAULT_QWEN_MODE if DEFAULT_QWEN_MODE in {"clone", "custom"} else "clone"
        self.qwen_language: str = DEFAULT_QWEN_LANGUAGE
        self.qwen_speaker: str = DEFAULT_QWEN_SPEAKER
        self.qwen_ref_index: int = DEFAULT_QWEN_REF_INDEX
        self.qwen_timeout_sec: int = max(30, DEFAULT_QWEN_TIMEOUT_SEC)
        self.qwen_max_chars: int = max(80, DEFAULT_QWEN_MAX_CHARS)
        self.ffmpeg_path: str = DEFAULT_FFMPEG_PATH
        self.ytdlp_path: str = DEFAULT_YTDLP_PATH
        self.jitd_output_dir: Path = APP_DIR / "jitd_audio"
        self.voice_now_playing: Dict[int, str] = {}
        self.voice_playback_ids: Dict[int, int] = {}
        self.voice_current_track: Dict[int, dict] = {}
        self.voice_queues: Dict[int, deque] = defaultdict(deque)
        self.voice_paused: Dict[int, bool] = {}
        self.voice_finish_actions: Dict[int, str] = {}
        self._voice_playback_serial = 0
        self._voice_log_handler: logging.Handler | None = None

        # chat memory per (channel_id, user_id)
        self.history: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=self.max_history))
        # topic keywords per (channel_id, user_id) — survives deque rotation
        self.topics: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))
        self.http_session: aiohttp.ClientSession | None = None
        self.bot: commands.Bot | None = None
        self._heartbeat_outbox_task: asyncio.Task | None = None
        self._heartbeat_seen_pending_outbox: set[str] = set()
        self._heartbeat_deferred_outbox_reasons: dict[str, str] = {}
        self._local_heartbeat_worker_proc: subprocess.Popen | None = None
        self._local_heartbeat_worker_log_handle = None
        self._local_heartbeat_worker_retry_after: float = 0.0
        self._discord_activity_task: asyncio.Task | None = None
        self._discord_activity_batches: dict[tuple[int, int], dict[str, Any]] = {}

    # ------- public controls from GUI -------
    def set_settings(self, *, token=None, url=None, model=None, sys_prompt=None, community_prompt=None, max_turns=None,
                       memory_enabled=None, memory_dsn=None, memory_hydrate_limit=None,
                       memory_formation_enabled=None, use_personhood_prompt=None,
                       heartbeat_enabled=None, heartbeat_shadow_mode=None, heartbeat_manual_approval=None,
                       heartbeat_server_configs=None,
                       heartbeat_home_guild_id=None, heartbeat_allowed_channel_ids=None,
                       heartbeat_quiet_hours_start=None, heartbeat_quiet_hours_end=None,
                       heartbeat_max_posts_per_hour=None, heartbeat_max_posts_per_day=None,
                       heartbeat_min_recent_human_messages=None, heartbeat_dead_channel_threshold_minutes=None,
                       heartbeat_require_reply_context=None, jitd_enabled=None,
                      jitd_mode=None, jitd_speaker=None, jitd_ref_index=None):
        if token is not None:
            self.token = _sanitize_token(token)
        if url is not None:
            self.ollama_url = url
        if model is not None:
            self.model = model
        if sys_prompt is not None:
            self.system_prompt = sys_prompt
        if community_prompt is not None:
            self.community_prompt = community_prompt
        if max_turns is not None:
            self.max_history = int(max_turns)
            new_hist: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=self.max_history))
            for k, dq in self.history.items():
                new_hist[k] = deque(dq, maxlen=self.max_history)
            self.history = new_hist
        if memory_enabled is not None:
            self.memory_enabled = bool(memory_enabled)
        if memory_dsn is not None:
            self.memory_dsn = str(memory_dsn)
        if memory_hydrate_limit is not None:
            self.memory_hydrate_limit = int(memory_hydrate_limit)
        if memory_formation_enabled is not None:
            self.memory_formation_enabled = bool(memory_formation_enabled)
        if use_personhood_prompt is not None:
            self.use_personhood_prompt = bool(use_personhood_prompt)
        if heartbeat_enabled is not None:
            self.heartbeat_enabled = bool(heartbeat_enabled)
        if heartbeat_shadow_mode is not None:
            self.heartbeat_shadow_mode = bool(heartbeat_shadow_mode)
        if heartbeat_manual_approval is not None:
            self.heartbeat_manual_approval = bool(heartbeat_manual_approval)
        if heartbeat_server_configs is not None:
            normalized: dict[int, dict[str, Any]] = {}
            src_items = heartbeat_server_configs.items() if isinstance(heartbeat_server_configs, dict) else []
            for gid_raw, cfg in src_items:
                try:
                    gid = int(gid_raw)
                except Exception:
                    continue
                if gid <= 0 or not isinstance(cfg, dict):
                    continue
                channel_id = self._coerce_outbox_int(cfg.get("channel_id"))
                normalized[gid] = {
                    "enabled": bool(cfg.get("enabled", False)) and channel_id is not None,
                    "channel_id": channel_id or 0,
                    "send_typing": bool(cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING)),
                }
            self.heartbeat_server_configs = normalized
        if heartbeat_home_guild_id is not None:
            try:
                gid = int(heartbeat_home_guild_id)
                self.heartbeat_home_guild_id = gid if gid > 0 else None
            except Exception:
                self.heartbeat_home_guild_id = None
        if heartbeat_allowed_channel_ids is not None:
            ids: set[int] = set()
            for item in heartbeat_allowed_channel_ids:
                try:
                    cid = int(item)
                except Exception:
                    continue
                if cid > 0:
                    ids.add(cid)
            self.heartbeat_allowed_channel_ids = ids
        if heartbeat_quiet_hours_start is not None:
            self.heartbeat_quiet_hours_start = str(heartbeat_quiet_hours_start or DEFAULT_HEARTBEAT_QUIET_HOURS_START).strip() or DEFAULT_HEARTBEAT_QUIET_HOURS_START
        if heartbeat_quiet_hours_end is not None:
            self.heartbeat_quiet_hours_end = str(heartbeat_quiet_hours_end or DEFAULT_HEARTBEAT_QUIET_HOURS_END).strip() or DEFAULT_HEARTBEAT_QUIET_HOURS_END
        if heartbeat_max_posts_per_hour is not None:
            self.heartbeat_max_posts_per_hour = max(0, int(heartbeat_max_posts_per_hour))
        if heartbeat_max_posts_per_day is not None:
            self.heartbeat_max_posts_per_day = max(0, int(heartbeat_max_posts_per_day))
        if heartbeat_min_recent_human_messages is not None:
            self.heartbeat_min_recent_human_messages = max(0, int(heartbeat_min_recent_human_messages))
        if heartbeat_dead_channel_threshold_minutes is not None:
            self.heartbeat_dead_channel_threshold_minutes = max(0, int(heartbeat_dead_channel_threshold_minutes))
        if heartbeat_require_reply_context is not None:
            self.heartbeat_require_reply_context = bool(heartbeat_require_reply_context)
        if jitd_enabled is not None:
            self.jitd_enabled = bool(jitd_enabled)
        if jitd_mode is not None:
            mode = str(jitd_mode).strip().lower()
            self.qwen_mode = mode if mode in {"clone", "custom"} else "clone"
        if jitd_speaker is not None:
            sp = str(jitd_speaker).strip()
            if sp:
                self.qwen_speaker = sp
        if jitd_ref_index is not None:
            try:
                self.qwen_ref_index = max(0, int(jitd_ref_index))
            except Exception:
                self.qwen_ref_index = 0
        self._reconcile_local_heartbeat_worker()
        if self.running and self.loop and self.memory and self.memory_enabled:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._sync_heartbeat_llm_config_from_gui_if_needed(),
                    self.loop,
                )
            except Exception:
                pass

    def _heartbeat_server_config(self, guild_id: int | None) -> dict[str, Any] | None:
        gid = self._coerce_outbox_int(guild_id)
        if not gid:
            return None
        cfg = self.heartbeat_server_configs.get(int(gid))
        if isinstance(cfg, dict):
            channel_id = self._coerce_outbox_int(cfg.get("channel_id"))
            return {
                "enabled": bool(cfg.get("enabled", False)) and channel_id is not None,
                "channel_id": channel_id or 0,
                "send_typing": bool(cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING)),
            }

        # Backward compatibility for older settings files.
        if not self.heartbeat_server_configs:
            legacy_channel_id = None
            if self.heartbeat_home_guild_id and int(self.heartbeat_home_guild_id) == int(gid):
                if len(self.heartbeat_allowed_channel_ids) == 1:
                    legacy_channel_id = next(iter(self.heartbeat_allowed_channel_ids))
            if legacy_channel_id:
                return {
                    "enabled": True,
                    "channel_id": int(legacy_channel_id),
                    "send_typing": DEFAULT_HEARTBEAT_SERVER_TYPING,
                }
        return None

    def _enabled_heartbeat_server_configs(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for gid, _cfg in self.heartbeat_server_configs.items():
            normalized = self._heartbeat_server_config(gid)
            if normalized and normalized.get("enabled") and int(normalized.get("channel_id", 0) or 0) > 0:
                out[int(gid)] = normalized
        if out:
            return out
        if not self.heartbeat_server_configs and self.heartbeat_home_guild_id and len(self.heartbeat_allowed_channel_ids) == 1:
            legacy = self._heartbeat_server_config(self.heartbeat_home_guild_id)
            if legacy and legacy.get("enabled"):
                out[int(self.heartbeat_home_guild_id)] = legacy
        return out

    def _heartbeat_channel_id_for_guild(self, guild_id: int | None) -> int | None:
        cfg = self._heartbeat_server_config(guild_id)
        if not cfg or not cfg.get("enabled"):
            return None
        return self._coerce_outbox_int(cfg.get("channel_id"))

    def _heartbeat_send_typing_for_guild(self, guild_id: int | None) -> bool:
        cfg = self._heartbeat_server_config(guild_id)
        return bool(cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING)) if cfg else False

    def set_greeting_for_guild(self, guild_id: int, *, enabled: bool, use_llm: bool, channel_id: int, user_ids: List[int], message: str, cooldown_sec: int):
        existing = self.greet_settings.get(guild_id, {})
        overrides = existing.get("overrides", {})
        self.greet_settings[guild_id] = {
            "enabled": bool(enabled),
            "use_llm": bool(use_llm),
            "channel_id": int(channel_id),
            "user_ids": set(int(x) for x in user_ids),
            "message": str(message or DEFAULT_GREET_MESSAGE),
            "cooldown": int(cooldown_sec),
            "overrides": overrides,  # preserve any per-user settings
        }
        cb = self.gui_callbacks.get("persist")
        if cb:
            cb()

    def clear_greeting_for_guild(self, guild_id: int):
        self.greet_settings.pop(guild_id, None)
        cb = self.gui_callbacks.get("persist")
        if cb:
            cb()

    def set_user_override(self, guild_id: int, user_id: int, *, enabled: Optional[bool] = None, prompt: Optional[Optional[str]] = None):
        cfg = self.greet_settings.setdefault(guild_id, {
            "enabled": True,
            "use_llm": True,
            "channel_id": 0,
            "user_ids": set(),
            "message": DEFAULT_GREET_MESSAGE,
            "cooldown": self.global_cooldown_sec,
            "overrides": {},
        })
        ov = cfg.setdefault("overrides", {}).setdefault(int(user_id), {"enabled": None, "prompt": None})
        if enabled is not None:
            ov["enabled"] = bool(enabled)
        if prompt is not None:
            ov["prompt"] = (prompt if (prompt and prompt.strip()) else None)
        cb = self.gui_callbacks.get("persist")
        if cb:
            cb()

    def request_guild_snapshot(self):
        if not self.running or not self.bot:
            self._log("Start the bot first to load servers/members.")
            return
        asyncio.run_coroutine_threadsafe(self._snapshot_guilds(), self.loop)

    def approve_outbox_message(
        self,
        outbox_id: str,
        *,
        message_override: str | None = None,
        channel_id_override: int | None = None,
        guild_id_override: int | None = None,
        reply_to_message_id_override: int | None = None,
    ):
        self._submit_async_action(
            lambda: self._approve_outbox_message(
                outbox_id,
                message_override=message_override,
                channel_id_override=channel_id_override,
                guild_id_override=guild_id_override,
                reply_to_message_id_override=reply_to_message_id_override,
            ),
            "Outbox approval failed",
        )

    def voice_join(self, guild_id: int, channel_id: int):
        self._submit_async_action(
            lambda: self._voice_join(guild_id, channel_id),
            "Voice join failed",
        )

    def voice_play(self, guild_id: int, channel_id: int, query: str):
        self._submit_async_action(
            lambda: self._voice_play(guild_id, channel_id, query),
            "Voice playback failed",
        )

    def voice_queue(self, guild_id: int, channel_id: int, query: str):
        self._submit_async_action(
            lambda: self._voice_queue(guild_id, channel_id, query),
            "Voice queue failed",
        )

    def voice_import_playlist(self, guild_id: int, channel_id: int, query: str):
        self._submit_async_action(
            lambda: self._voice_import_playlist(guild_id, channel_id, query),
            "Voice playlist import failed",
        )

    def voice_pause(self, guild_id: int):
        self._submit_async_action(
            lambda: self._voice_pause(guild_id),
            "Voice pause failed",
        )

    def voice_resume(self, guild_id: int):
        self._submit_async_action(
            lambda: self._voice_resume(guild_id),
            "Voice resume failed",
        )

    def voice_skip(self, guild_id: int):
        self._submit_async_action(
            lambda: self._voice_skip(guild_id),
            "Voice skip failed",
        )

    def voice_stop(self, guild_id: int):
        self._submit_async_action(
            lambda: self._voice_stop(guild_id),
            "Voice stop failed",
        )

    def voice_clear_queue(self, guild_id: int):
        self._submit_async_action(
            lambda: self._voice_clear_queue(guild_id),
            "Voice queue clear failed",
        )

    def voice_leave(self, guild_id: int):
        self._submit_async_action(
            lambda: self._voice_leave(guild_id),
            "Voice leave failed",
        )

    # ------- lifecycle -------
    def start(self):
        if self.running:
            self._log("Bot already running")
            return
        if not self.token:
            self.token = _env_token()
            if self.token:
                self._log("Using DISCORD_TOKEN from .env")
        if not self.token:
            self._log("No Discord token set. Enter token or set DISCORD_TOKEN in .env")
            return
        self._log("Starting Discord bot with configured token (value redacted)")
        self.thread = threading.Thread(target=self._thread_main, name="DiscordBotThread", daemon=True)
        self.thread.start()

    def stop(self):
        if not self.running or not self.loop:
            self._log("Bot not running")
            return
        asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)

    # ------- internal helpers -------
    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except Exception as e:
            self._log(f"Bot crashed: {e}")
            self._status("Stopped")

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.running = True
        self._status("Starting...")
        self._attach_voice_loggers()

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        intents.voice_states = True
        bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)  # custom !help below
        self.bot = bot
        runner = self

        async def ask_ollama(messages: list[dict]) -> str:
            assert runner.http_session is not None
            payload = {"model": runner.model, "messages": messages, "stream": True}
            reply_parts: list[str] = []
            async with runner.http_session.post(runner.ollama_url, json=payload) as r:
                r.raise_for_status()
                async for raw in r.content:
                    try:
                        line = raw.decode("utf-8").strip()
                    except Exception:
                        continue
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = j.get("message", {})
                    if isinstance(msg, dict):
                        part = msg.get("content")
                        if isinstance(part, str):
                            reply_parts.append(part)
                    if j.get("done"):
                        break
            return "".join(reply_parts).strip()

        # -------- Cognitive memory helpers --------
        _IMPORTANCE_PATTERNS = re.compile(
            r"\b(remember|important|my name is|i am|i\'m|don\'t forget|never forget|always)\b", re.IGNORECASE
        )

        async def hydrate_context(query: str) -> str | None:
            if not runner.memory or not runner.memory_enabled:
                return None
            try:
                refined = _refine_query(query)
                ctx = await runner.memory.hydrate(refined, memory_limit=runner.memory_hydrate_limit)
                # Touch recalled memories to update access counts
                if ctx.memories:
                    asyncio.create_task(runner.memory.touch_memories([m.id for m in ctx.memories]))
                parts: list[str] = []
                formatted = format_context_for_prompt(ctx, max_memories=runner.memory_hydrate_limit)
                if formatted.strip():
                    parts.append(formatted)
                # Pull from working memory for active conversation context
                try:
                    wm_results = await runner.memory.search_working(refined, limit=3)
                    if wm_results:
                        wm_lines = ["## Active Context"]
                        for wm_item in wm_results:
                            content = wm_item.get("content", "") if isinstance(wm_item, dict) else str(wm_item)
                            if content.strip():
                                wm_lines.append(f"- {content.strip()}")
                        if len(wm_lines) > 1:
                            parts.append("\n".join(wm_lines))
                except Exception:
                    pass  # working memory search is best-effort
                return "\n\n".join(parts) if parts else None
            except Exception as e:
                runner._log(f"Memory hydration error: {e}")
                return None

        async def form_memory(user_msg: str, assistant_resp: str, user_id: int, channel_id: int, guild_id: int | None):
            if not runner.memory or not runner.memory_enabled or not runner.memory_formation_enabled:
                return
            try:
                content = f"User: {user_msg}\n\nAssistant: {assistant_resp}"
                combined = f"{user_msg} {assistant_resp}"
                # --- Concept extraction ---
                concepts = _extract_concepts(combined)
                # --- Memory type classification ---
                mem_type = MemoryType.EPISODIC
                if _FACT_PATTERNS.search(user_msg) or _FACT_PATTERNS.search(assistant_resp):
                    mem_type = MemoryType.SEMANTIC
                # --- Importance scoring ---
                importance = 0.4
                if len(combined) > 500:
                    importance += 0.1  # longer exchanges tend to be meatier
                if _IMPORTANCE_PATTERNS.search(user_msg):
                    importance += 0.2  # explicit "remember" / "important"
                if "?" in user_msg and len(assistant_resp) > 100:
                    importance += 0.1  # substantive Q&A
                importance = min(importance, 0.9)
                # --- Emotional valence ---
                valence = _estimate_valence(combined)
                # --- Source attribution ---
                source_attr = {"kind": "discord", "user_id": user_id, "channel_id": channel_id}
                if guild_id is not None:
                    source_attr["guild_id"] = guild_id
                await runner.memory.remember(
                    content,
                    type=mem_type,
                    importance=importance,
                    emotional_valence=valence,
                    concepts=concepts if concepts else None,
                    source_attribution=source_attr,
                )
                # --- Working memory: hold active conversation context ---
                topic_summary = ", ".join(concepts[:3]) if concepts else user_msg[:80]
                try:
                    await runner.memory.hold(
                        f"[user {user_id} in ch {channel_id}] discussing: {topic_summary}",
                        ttl_seconds=1800,
                    )
                except Exception:
                    pass  # hold() is best-effort
            except Exception as e:
                runner._log(f"Memory formation error: {e}")

        @bot.event
        async def setup_hook():
            runner.http_session = aiohttp.ClientSession()
            runner._log("HTTP session ready")
            # Cognitive memory pool
            if AGI_MEMORY_AVAILABLE and runner.memory_enabled and runner.memory_dsn:
                try:
                    runner.memory = await CognitiveMemory.create(runner.memory_dsn, min_size=1, max_size=5)
                    runner._log("Cognitive memory connected")
                    sync_msg = await runner._sync_heartbeat_llm_config_from_gui_if_needed()
                    if sync_msg:
                        runner._log(sync_msg)
                    runner._reconcile_local_heartbeat_worker()
                except Exception as e:
                    runner._log(f"Cognitive memory unavailable (bot will use deque only): {e}")
                    runner.memory = None

        @bot.event
        async def on_ready():
            runner._status(f"Logged in as {bot.user} ({bot.user.id})")
            runner._push_voice_queue_state()
            asyncio.create_task(runner._snapshot_guilds())
            if runner._heartbeat_outbox_task is None or runner._heartbeat_outbox_task.done():
                runner._heartbeat_outbox_task = asyncio.create_task(runner._heartbeat_outbox_loop())
            if runner._discord_activity_task is None or runner._discord_activity_task.done():
                runner._discord_activity_task = asyncio.create_task(runner._discord_activity_loop())
            runner._reconcile_local_heartbeat_worker()

        @bot.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            if not bot.user or member.id != bot.user.id:
                return
            before_name = before.channel.name if before.channel else "(none)"
            after_name = after.channel.name if after.channel else "(none)"
            runner._log(f"Bot voice state changed: {before_name} -> {after_name}")
            if after.channel:
                title = runner.voice_now_playing.get(member.guild.id)
                if title and runner.voice_paused.get(member.guild.id):
                    runner._set_voice_state(
                        f"Paused in {member.guild.name} / {after.channel.name}: {title}"
                    )
                elif title:
                    runner._set_voice_state(
                        f"Playing in {member.guild.name} / {after.channel.name}: {title}"
                    )
                else:
                    runner._set_voice_state(
                        f"Connected to {member.guild.name} / {after.channel.name}"
                    )
            else:
                runner._clear_voice_runtime(member.guild.id, clear_queue=True)
                runner._set_voice_state("Idle")
            runner._push_voice_queue_state()

        # -------- channel context reader --------
        async def fetch_channel_context(channel, exclude_msg_id: int | None = None,
                                        limit: int = 20) -> str | None:
            """Read the last N messages in the channel for conversational awareness."""
            try:
                messages: list[discord.Message] = []
                async for msg in channel.history(limit=limit + 5):  # over-fetch to compensate for skips
                    if msg.id == exclude_msg_id:
                        continue
                    if not msg.content or not msg.content.strip():
                        continue
                    # skip bot command invocations (but keep bot responses)
                    if msg.content.startswith("!") and not msg.author.bot:
                        continue
                    messages.append(msg)
                    if len(messages) >= limit:
                        break
                if not messages:
                    return None
                messages.reverse()  # chronological: oldest first
                lines: list[str] = []
                for m in messages:
                    tag = "(bot)" if m.author.bot else ""
                    content = m.content[:300]
                    lines.append(f"{m.author.display_name}{tag}: {content}")
                return "\n".join(lines)
            except Exception as e:
                runner._log(f"Channel context fetch error: {e}")
                return None

        # -------- message construction for chat (single SYSTEM message) --------
        def build_msgs(chan_id: int, user_id: int, author_name: str, user_text: str,
                       memory_context: str | None = None,
                       channel_context: str | None = None) -> list[dict]:
            sys_parts: list[str] = []
            if runner.use_personhood_prompt and AGI_MEMORY_AVAILABLE:
                try:
                    sys_parts.append(get_personhood_prompt("conversation"))
                except Exception:
                    pass
            sys_parts.append(runner.system_prompt)
            if runner.community_prompt.strip():
                sys_parts.append(f"[community]\n{runner.community_prompt.strip()}")
            if channel_context:
                sys_parts.append(f"[channel context (recent messages in this channel)]\n{channel_context}")
            if memory_context:
                sys_parts.append(f"[memory]\n{memory_context}")
            # Non-agi-memory topic continuity (survives deque rotation)
            topic_kws = list(dict.fromkeys(runner.topics[(chan_id, user_id)]))  # dedupe, keep order
            if topic_kws:
                sys_parts.append(f"[recent topics]\n{', '.join(topic_kws)}")
            sys_parts.append(f"[speaker id={user_id} name={author_name}]")
            msgs: list[dict] = [{"role": "system", "content": "\n\n".join(sys_parts)}]
            msgs.extend(list(runner.history[(chan_id, user_id)]))
            msgs.append({"role": "user", "content": user_text})
            return msgs

        def _member_voice_channel(ctx: commands.Context) -> discord.VoiceChannel | None:
            if not ctx.guild or not isinstance(ctx.author, discord.Member):
                return None
            state = ctx.author.voice
            if state and isinstance(state.channel, discord.VoiceChannel):
                return state.channel
            return None

        def _command_voice_channel(ctx: commands.Context) -> discord.VoiceChannel | None:
            member_channel = _member_voice_channel(ctx)
            if member_channel:
                return member_channel
            active_voice = runner._active_voice_client()
            if (
                active_voice
                and active_voice.guild
                and ctx.guild
                and active_voice.guild.id == ctx.guild.id
                and isinstance(active_voice.channel, discord.VoiceChannel)
            ):
                return active_voice.channel
            return None

        async def _reply_voice_action(ctx: commands.Context, action_coro):
            try:
                message = await action_coro
            except Exception as e:
                await ctx.reply(str(e), mention_author=False)
                return
            if message:
                await ctx.reply(message, mention_author=False)

        async def _send_ballerjit_help(ctx: commands.Context):
            await ctx.reply(BALLERJIT_PUBLIC_COMMANDS)
            return
            text = (
                "**Ballerjit Commands**\n"
                "`!jith` - show all ballerjit commands.\n"
                "`!help` - show all ballerjit commands.\n"
                "`!jit <message>` - ask the local model.\n"
                "`!jitd <message>` - ask the model and return an MP3 voice reply.\n"
                "`!jitm` - show now playing and queue.\n"
                "`!jitm join` - join your current voice channel.\n"
                "`!jitm play <query>` - play now, or queue if something is already playing.\n"
                "`!jitm queue <query>` - add a track to the queue.\n"
                "`!jitm playlist <url>` - import a playlist into the queue.\n"
                "`!jitm pause|resume|skip|stop|clear|leave` - control playback.\n"
                "`!low [text|reset]` - show/set community prompt.\n"
                "`!clear` - clear your chat history for this channel.\n"
                "`!recall <query>` - search memories by relevance.\n"
                "`!remember <text>` - store an explicit memory.\n"
                "`!goals [add <title>]` - list or create goals.\n"
                "`!memory [health|identity|emotions]` - memory system status.\n"
                "`!greet status` - show your greeting status in this server.\n"
                "`!greet on` / `!greet off` - enable/disable greeting for you.\n"
                "`!greet prompt <text>` - set your personal greeting style.\n"
                "`!greet prompt reset` - clear your personal greeting style.\n"
                "`!greet test` - preview your greeting line now.\n"
            )
            await ctx.reply(text)
            return
            text = (
                "**Ballerjit Commands**\n"
                "`!jith` — show all ballerjit commands.\n"
                "`!help` — show all ballerjit commands.\n"
                "`!jit <message>` — ask the local model.\n"
                "`!jitd <message>` — ask the model and return an MP3 voice reply.\n"
                "`!jitm` — show now playing and queue.\n"
                "`!jitm join` — join your current voice channel.\n"
                "`!jitm play <query>` — play a track now.\n"
                "`!jitm queue <query>` — add a track to the queue.\n"
                "`!jitm playlist <url>` — import a playlist into the queue.\n"
                "`!jitm pause|resume|skip|stop|clear|leave` — control playback.\n"
                "`!low [text|reset]` — show/set community prompt.\n"
                "`!clear` — clear your chat history for this channel.\n"
                "`!recall <query>` — search memories by relevance.\n"
                "`!remember <text>` — store an explicit memory.\n"
                "`!goals [add <title>]` — list or create goals.\n"
                "`!memory [health|identity|emotions]` — memory system status.\n"
                "`!greet status` — show your greeting status in this server.\n"
                "`!greet on` / `!greet off` — enable/disable greeting for you.\n"
                "`!greet prompt <text>` — set your personal greeting style.\n"
                "`!greet prompt reset` — clear your personal greeting style.\n"
                "`!greet test` — preview your greeting line now.\n"
            )
            await ctx.reply(text)

        # -------------- Commands --------------
        @bot.command(name="help", help="Show available commands")
        async def help_cmd(ctx: commands.Context):
            await _send_ballerjit_help(ctx)
            return
            text = (
                "**Commands**\n"
                "`!jit <message>` — ask the local model.\n"
                "`!jitd <message>` — ask the model and return an MP3 voice reply.\n"
                "`!low [text|reset]` — show/set community prompt.\n"
                "`!clear` — clear your chat history for this channel.\n"
                "`!jitm` — show now playing and queue.\n"
                "`!jitm join` — join your current voice channel.\n"
                "`!jitm play <query>` — play a track now.\n"
                "`!jitm queue <query>` — add a track to the queue.\n"
                "`!jitm playlist <url>` — import a playlist into the queue.\n"
                "`!jitm pause|resume|skip|stop|clear|leave` — control playback.\n"
                "`!recall <query>` — search memories by relevance.\n"
                "`!remember <text>` — store an explicit memory.\n"
                "`!goals [add <title>]` — list or create goals.\n"
                "`!memory [health|identity|emotions]` — memory system status.\n"
                "`!greet status` — show your greeting status in this server.\n"
                "`!greet on` / `!greet off` — enable/disable greeting for you.\n"
                "`!greet prompt <text>` — set your personal greeting style.\n"
                "`!greet prompt reset` — clear your personal greeting style.\n"
                "`!greet test` — preview your greeting line now.\n"
            )
            await ctx.reply(text)

        @bot.command(name="jith", help="Show all ballerjit commands")
        async def jith_cmd(ctx: commands.Context):
            await _send_ballerjit_help(ctx)

        @bot.command(name="jit", help="Ask the local model. Usage: !jit your question")
        async def ask_cmd(ctx: commands.Context, *, prompt: str):
            if ctx.author.bot:
                return
            await ctx.channel.typing()
            chan_ctx = await fetch_channel_context(ctx.channel, exclude_msg_id=ctx.message.id)
            mem_ctx = await hydrate_context(prompt)
            msgs = build_msgs(ctx.channel.id, ctx.author.id, ctx.author.display_name, prompt,
                              memory_context=mem_ctx, channel_context=chan_ctx)
            try:
                answer = await ask_ollama(msgs)
            except Exception as e:
                await ctx.reply(f"Error talking to Ollama: {e}")
                return
            runner.history[(ctx.channel.id, ctx.author.id)].append({"role": "user", "content": prompt})
            runner.history[(ctx.channel.id, ctx.author.id)].append({"role": "assistant", "content": answer or ""})
            guild_id = ctx.guild.id if ctx.guild else None
            asyncio.create_task(form_memory(prompt, answer or "", ctx.author.id, ctx.channel.id, guild_id))
            # Track topic keywords for non-agi-memory context continuity
            for kw in _extract_concepts(prompt, max_concepts=3):
                runner.topics[(ctx.channel.id, ctx.author.id)].append(kw)
            text = answer or "(no response)"
            for i in range(0, len(text), CHUNK_SIZE):
                await ctx.reply(text[i:i+CHUNK_SIZE], mention_author=False)

        @bot.command(name="jitd", help="Ask the model and return an MP3 voice reply. Usage: !jitd your question")
        async def ask_voice_cmd(ctx: commands.Context, *, prompt: str):
            if ctx.author.bot:
                return
            if not runner.jitd_enabled:
                await ctx.reply("`!jitd` is disabled in the GUI (General tab).")
                return
            await ctx.channel.typing()
            chan_ctx = await fetch_channel_context(ctx.channel, exclude_msg_id=ctx.message.id)
            mem_ctx = await hydrate_context(prompt)
            msgs = build_msgs(
                ctx.channel.id,
                ctx.author.id,
                ctx.author.display_name,
                prompt,
                memory_context=mem_ctx,
                channel_context=chan_ctx,
            )
            try:
                answer = await ask_ollama(msgs)
            except Exception as e:
                await ctx.reply(f"Error talking to Ollama: {e}")
                return

            runner.history[(ctx.channel.id, ctx.author.id)].append({"role": "user", "content": prompt})
            runner.history[(ctx.channel.id, ctx.author.id)].append({"role": "assistant", "content": answer or ""})
            guild_id = ctx.guild.id if ctx.guild else None
            asyncio.create_task(form_memory(prompt, answer or "", ctx.author.id, ctx.channel.id, guild_id))
            for kw in _extract_concepts(prompt, max_concepts=3):
                runner.topics[(ctx.channel.id, ctx.author.id)].append(kw)

            voice_path, voice_error, _spoken_text = await runner.synthesize_jitd_mp3(answer or "(no response)")
            if voice_error or not voice_path:
                runner._log(f"!jitd failed: {voice_error}")
                await ctx.reply(f"Voice generation failed: {voice_error}")
                return

            try:
                await ctx.reply(file=discord.File(str(voice_path), filename=voice_path.name), mention_author=False)
            finally:
                try:
                    voice_path.unlink(missing_ok=True)
                except Exception:
                    pass

        @bot.command(name="low", help="Set or show the lower-priority community prompt. Usage: !low <text> or !low reset")
        async def low_cmd(ctx: commands.Context, *, text: str | None = None):
            if text is None:
                cur = runner.community_prompt or "(empty)"
                await ctx.reply("Community prompt:\n" + cur)
                return
            t = text.strip()
            if t.lower() in {"reset", "clear", "default"}:
                runner.community_prompt = ""
                cb = runner.gui_callbacks.get("set_low")
                if cb:
                    cb(runner.community_prompt)
                cb2 = runner.gui_callbacks.get("persist")
                if cb2:
                    cb2()
                await ctx.reply("Community prompt cleared.")
            else:
                runner.community_prompt = t
                cb = runner.gui_callbacks.get("set_low")
                if cb:
                    cb(runner.community_prompt)
                cb2 = runner.gui_callbacks.get("persist")
                if cb2:
                    cb2()
                await ctx.reply("Community prompt updated.")

        @bot.command(name="clear", help="Clear your chat history for this channel")
        async def clear_cmd(ctx: commands.Context):
            runner.history[(ctx.channel.id, ctx.author.id)].clear()
            runner.topics[(ctx.channel.id, ctx.author.id)].clear()
            await ctx.reply("Cleared your chat history and topic context for this channel.")

        @bot.group(name="jitm", invoke_without_command=True, help="Voice playback controls")
        async def voice_group(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await ctx.reply("\n".join(runner._voice_queue_lines(ctx.guild.id)), mention_author=False)

        @voice_group.command(name="now")
        async def voice_now_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await ctx.reply("\n".join(runner._voice_queue_lines(ctx.guild.id)), mention_author=False)

        @voice_group.command(name="join")
        async def voice_join_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            channel = _member_voice_channel(ctx)
            if not channel:
                await ctx.reply("Join a voice channel first.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_join(ctx.guild.id, channel.id))

        @voice_group.command(name="play")
        async def voice_play_cmd(ctx: commands.Context, *, query: str):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            channel = _command_voice_channel(ctx)
            if not channel:
                await ctx.reply("Join a voice channel first.", mention_author=False)
                return
            await _reply_voice_action(
                ctx,
                runner._voice_queue(ctx.guild.id, channel.id, query, requested_by=ctx.author.display_name),
            )

        @voice_group.command(name="queue")
        async def voice_queue_cmd(ctx: commands.Context, *, query: str):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            channel = _command_voice_channel(ctx)
            if not channel:
                await ctx.reply("Join a voice channel first.", mention_author=False)
                return
            await _reply_voice_action(
                ctx,
                runner._voice_queue(ctx.guild.id, channel.id, query, requested_by=ctx.author.display_name),
            )

        @voice_group.command(name="playlist")
        async def voice_playlist_cmd(ctx: commands.Context, *, query: str):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            channel = _command_voice_channel(ctx)
            if not channel:
                await ctx.reply("Join a voice channel first.", mention_author=False)
                return
            await _reply_voice_action(
                ctx,
                runner._voice_import_playlist(
                    ctx.guild.id,
                    channel.id,
                    query,
                    requested_by=ctx.author.display_name,
                ),
            )

        @voice_group.command(name="pause")
        async def voice_pause_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_pause(ctx.guild.id))

        @voice_group.command(name="resume")
        async def voice_resume_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_resume(ctx.guild.id))

        @voice_group.command(name="skip")
        async def voice_skip_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_skip(ctx.guild.id))

        @voice_group.command(name="stop")
        async def voice_stop_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_stop(ctx.guild.id))

        @voice_group.command(name="clear")
        async def voice_clear_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_clear_queue(ctx.guild.id))

        @voice_group.command(name="leave")
        async def voice_leave_cmd(ctx: commands.Context):
            if not ctx.guild:
                await ctx.reply("Run this in a server.", mention_author=False)
                return
            await _reply_voice_action(ctx, runner._voice_leave(ctx.guild.id))

        # ----- memory commands -----
        @bot.command(name="recall", help="Semantic search of memories. Usage: !recall <query>")
        async def recall_cmd(ctx: commands.Context, *, query: str):
            if not AGI_MEMORY_AVAILABLE or not runner.memory:
                if not AGI_MEMORY_AVAILABLE:
                    await ctx.reply(f"Memory system is not available: {_agi_memory_unavailable_reason()}")
                else:
                    await ctx.reply("Memory system is not connected.")
                return
            try:
                result = await runner.memory.recall(query, limit=5)
                if not result.memories:
                    await ctx.reply("No memories found.")
                    return
                lines = ["**Recalled Memories**"]
                for i, m in enumerate(result.memories, 1):
                    score = f"{m.similarity:.2f}" if m.similarity is not None else "?"
                    lines.append(f"{i}. [{score}] {m.content[:200]}")
                text = "\n".join(lines)
                for i in range(0, len(text), CHUNK_SIZE):
                    await ctx.reply(text[i:i+CHUNK_SIZE], mention_author=False)
            except Exception as e:
                await ctx.reply(f"Recall error: {e}")

        @bot.command(name="remember", help="Store an explicit memory. Usage: !remember <text>")
        async def remember_cmd(ctx: commands.Context, *, text: str):
            if not AGI_MEMORY_AVAILABLE or not runner.memory:
                if not AGI_MEMORY_AVAILABLE:
                    await ctx.reply(f"Memory system is not available: {_agi_memory_unavailable_reason()}")
                else:
                    await ctx.reply("Memory system is not connected.")
                return
            try:
                guild_id = ctx.guild.id if ctx.guild else None
                source_attr = {"kind": "discord_explicit", "user_id": ctx.author.id, "channel_id": ctx.channel.id}
                if guild_id is not None:
                    source_attr["guild_id"] = guild_id
                mid = await runner.memory.remember(
                    text,
                    type=MemoryType.EPISODIC,
                    importance=0.7,
                    source_attribution=source_attr,
                )
                await ctx.reply(f"Remembered (id: `{mid}`).")
            except Exception as e:
                await ctx.reply(f"Remember error: {e}")

        @bot.command(name="goals", help="List or create goals. Usage: !goals [add <title>]")
        async def goals_cmd(ctx: commands.Context, *, text: str | None = None):
            if not AGI_MEMORY_AVAILABLE or not runner.memory:
                if not AGI_MEMORY_AVAILABLE:
                    await ctx.reply(f"Memory system is not available: {_agi_memory_unavailable_reason()}")
                else:
                    await ctx.reply("Memory system is not connected.")
                return
            try:
                if text and text.strip().lower().startswith("add "):
                    title = text.strip()[4:].strip()
                    if not title:
                        await ctx.reply("Usage: `!goals add <title>`")
                        return
                    gid = await runner.memory.create_goal(title, source=GoalSource.USER_REQUEST, priority=GoalPriority.QUEUED)
                    await ctx.reply(f"Goal created: `{gid}`")
                    return
                goals = await runner.memory.get_goals()
                if not goals:
                    await ctx.reply("No active/queued goals.")
                    return
                lines = ["**Goals**"]
                for g in goals:
                    lines.append(f"- [{g.get('priority', '?')}] {g.get('title', '?')}")
                await ctx.reply("\n".join(lines))
            except Exception as e:
                await ctx.reply(f"Goals error: {e}")

        @bot.command(name="memory", help="Memory system status. Usage: !memory [health|identity|emotions]")
        async def memory_cmd(ctx: commands.Context, *, sub: str | None = None):
            if not AGI_MEMORY_AVAILABLE:
                await ctx.reply(f"agi-memory unavailable: {_agi_memory_unavailable_reason()}")
                return
            if not runner.memory:
                await ctx.reply("Memory system not connected (check DSN or DB status).")
                return
            try:
                sub = (sub or "").strip().lower()
                if sub == "health":
                    health = await runner.memory.get_health()
                    lines = ["**Cognitive Health**"]
                    for k, v in health.items():
                        lines.append(f"- {k}: {v}")
                    await ctx.reply("\n".join(lines) or "No health data.")
                elif sub == "identity":
                    aspects = await runner.memory.get_identity()
                    if not aspects:
                        await ctx.reply("No identity aspects found.")
                        return
                    lines = ["**Identity Aspects**"]
                    for a in aspects:
                        lines.append(f"- {a.get('aspect_type', '?')}: {a.get('content', '')} (stability: {a.get('stability', 0):.2f})")
                    await ctx.reply("\n".join(lines))
                elif sub == "emotions":
                    es = await runner.memory.get_emotional_state()
                    if not es:
                        await ctx.reply("No emotional state data.")
                        return
                    lines = ["**Emotional State**"]
                    for k, v in es.items():
                        lines.append(f"- {k}: {v}")
                    await ctx.reply("\n".join(lines))
                else:
                    health = await runner.memory.get_health()
                    total = health.get("total_memories", "?")
                    await ctx.reply(
                        f"**Memory System**\n"
                        f"Connected: Yes\n"
                        f"Total memories: {total}\n"
                        f"Formation enabled: {runner.memory_formation_enabled}\n"
                        f"Hydration limit: {runner.memory_hydrate_limit}\n"
                        f"Personhood prompt: {runner.use_personhood_prompt}\n"
                        f"Use `!memory health`, `!memory identity`, or `!memory emotions` for details."
                    )
            except Exception as e:
                await ctx.reply(f"Memory status error: {e}")

        # ----- greet group -----
        @bot.group(name="greet", invoke_without_command=True, help="Greeting controls: status|on|off|prompt|test")
        async def greet_group(ctx: commands.Context):
            await ctx.invoke(status_cmd)

        @greet_group.command(name="status")
        async def status_cmd(ctx: commands.Context):
            g = ctx.guild
            if not g:
                await ctx.reply("Run this in a server.")
                return
            cfg = runner.greet_settings.get(g.id)
            if not cfg:
                await ctx.reply("No greeting settings configured for this server.")
                return
            ov = (cfg.get("overrides") or {}).get(ctx.author.id, {})
            sel = (ctx.author.id in cfg.get("user_ids", set()))
            enabled_guild = bool(cfg.get("enabled", True))
            enabled_user = ov.get("enabled")
            prompt_user = ov.get("prompt") or "(none)"
            eff_enabled = enabled_guild and sel and (enabled_user is not False)
            ch = cfg.get("channel_id")
            await ctx.reply(
                f"Server enabled: {enabled_guild}\n"
                f"You selected by admin: {sel}\n"
                f"Your override enabled: {enabled_user if enabled_user is not None else '(default)'}\n"
                f"Effective greeting: {eff_enabled}\n"
                f"Channel id: {ch}\n"
                f"Your personal prompt: {prompt_user}"
            )

        @greet_group.command(name="on")
        async def on_cmd(ctx: commands.Context):
            g = ctx.guild
            if not g:
                await ctx.reply("Run this in a server.")
                return
            runner.set_user_override(g.id, ctx.author.id, enabled=True)
            await ctx.reply("Your greeting is ON (if the server admin selected you).")

        @greet_group.command(name="off")
        async def off_cmd(ctx: commands.Context):
            g = ctx.guild
            if not g:
                await ctx.reply("Run this in a server.")
                return
            runner.set_user_override(g.id, ctx.author.id, enabled=False)
            await ctx.reply("Your greeting is OFF.")

        @greet_group.command(name="prompt")
        async def prompt_cmd(ctx: commands.Context, *, text: str | None = None):
            g = ctx.guild
            if not g:
                await ctx.reply("Run this in a server.")
                return
            if text is None:
                await ctx.reply("Usage: `!greet prompt <text>` or `!greet prompt reset`")
                return
            t = text.strip()
            if t.lower() in {"reset", "clear", "default"}:
                runner.set_user_override(g.id, ctx.author.id, prompt=None)
                await ctx.reply("Your personal greeting prompt was cleared.")
            else:
                runner.set_user_override(g.id, ctx.author.id, prompt=t)
                await ctx.reply("Your personal greeting prompt was updated.")

        @greet_group.command(name="test")
        async def test_cmd(ctx: commands.Context):
            g = ctx.guild
            if not g:
                await ctx.reply("Run this in a server.")
                return
            text = await generate_greeting_line(runner, g.id, ctx.author)
            text = text or "(no greeting produced)"
            await ctx.reply("[preview] " + text)

        # ---------- Mention handler ----------
        @bot.event
        async def on_message(message: discord.Message):
            await bot.process_commands(message)
            if message.author.bot or not bot.user:
                return
            if runner.memory and runner.memory_enabled:
                asyncio.create_task(runner.memory.touch_last_user_contact())
                if message.guild is not None:
                    asyncio.create_task(runner._track_discord_activity(message))
            if bot.user in message.mentions:
                prompt = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
                if not prompt:
                    return
                await message.channel.typing()
                chan_ctx = await fetch_channel_context(message.channel, exclude_msg_id=message.id)
                mem_ctx = await hydrate_context(prompt)
                msgs = build_msgs(message.channel.id, message.author.id, message.author.display_name, prompt,
                                  memory_context=mem_ctx, channel_context=chan_ctx)
                try:
                    answer = await ask_ollama(msgs)
                except Exception as e:
                    await message.reply(f"Error talking to Ollama: {e}")
                    return
                runner.history[(message.channel.id, message.author.id)].append({"role": "user", "content": prompt})
                runner.history[(message.channel.id, message.author.id)].append({"role": "assistant", "content": answer or ""})
                guild_id = message.guild.id if message.guild else None
                asyncio.create_task(form_memory(prompt, answer or "", message.author.id, message.channel.id, guild_id))
                # Track topic keywords for non-agi-memory context continuity
                for kw in _extract_concepts(prompt, max_concepts=3):
                    runner.topics[(message.channel.id, message.author.id)].append(kw)
                text = answer or "(no response)"
                for i in range(0, len(text), CHUNK_SIZE):
                    await message.reply(text[i:i+CHUNK_SIZE], mention_author=False)

        # ---------- Presence watcher (LLM greeting) ----------
        @bot.event
        async def on_presence_update(before: discord.Member, after: discord.Member):
            try:
                # Only trigger on OFFLINE -> ONLINE transition
                if not (before.status == discord.Status.offline and after.status == discord.Status.online):
                    return
                guild = after.guild
                if not guild:
                    return
                cfg = runner.greet_settings.get(guild.id)
                if not cfg or not cfg.get("enabled", True):
                    return
                if after.id not in cfg.get("user_ids", set()):
                    return  # admin must have selected the user
                ov = (cfg.get("overrides") or {}).get(after.id, {})
                if ov.get("enabled") is False:
                    return  # user opted out
                now = time.monotonic()
                last = runner._last_greet.get((guild.id, after.id), 0)
                if now - last < cfg.get("cooldown", runner.global_cooldown_sec):
                    return
                channel_id = cfg.get("channel_id")
                if not channel_id:
                    return
                channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                # Compose greeting (single SYSTEM message)
                greet_text = await generate_greeting_line(runner, guild.id, after, user_override_prompt=ov.get("prompt"))
                greet_text = greet_text or (cfg.get("message") or DEFAULT_GREET_MESSAGE).replace("{mention}", after.mention).replace("{name}", after.display_name).replace("{user}", after.name)
                await channel.send(greet_text)
                runner._last_greet[(guild.id, after.id)] = now
            except Exception as e:
                runner._log(f"on_presence_update error: {e}")

        async def generate_greeting_line(runner: "BotRunner", guild_id: int, member: discord.Member, user_override_prompt: Optional[str] = None) -> Optional[str]:
            cfg = runner.greet_settings.get(guild_id) or {}
            if not cfg.get("use_llm", True):
                return None
            sys_chunks = [
                runner.system_prompt,
                "You are greeting a user who just came online in a Discord server. "
                "Reply with exactly one short, friendly sentence. Casual tone. "
                "No markdown, no prefixes, avoid more than one emoji, and do not include @-mentions."
            ]
            if user_override_prompt and user_override_prompt.strip():
                sys_chunks.append(f"[personal_greeting_style]\n{user_override_prompt.strip()}")
            if runner.community_prompt.strip():
                sys_chunks.append(f"[community]\n{runner.community_prompt.strip()}")
            # Optionally hydrate memory about the user being greeted
            greet_mem = await hydrate_context(f"greeting {member.display_name}")
            if greet_mem:
                sys_chunks.append(f"[memory]\n{greet_mem}")
            system_text = "\n\n".join(sys_chunks)
            user_msg = {"role": "user", "content": f"Write a warm one-line greeting for user named {member.display_name}."}
            try:
                resp = await ask_ollama([{"role": "system", "content": system_text}, user_msg])
            except Exception as e:
                runner._log(f"Greeter LLM error: {e}")
                resp = None
            if not resp:
                return None
            resp = resp.replace("\n", " ").strip()
            if len(resp) > 220:
                resp = resp[:220].rstrip() + "…"
            return f"{member.mention} {resp}"

        try:
            await bot.start(self.token)
        finally:
            self.running = False
            self._stop_local_heartbeat_worker(quiet=True)
            if self._heartbeat_outbox_task:
                self._heartbeat_outbox_task.cancel()
                self._heartbeat_outbox_task = None
            if self._discord_activity_task:
                self._discord_activity_task.cancel()
                self._discord_activity_task = None
            if self.memory:
                try:
                    await self._flush_pending_discord_activity(force=True)
                except Exception:
                    pass
            self.voice_now_playing.clear()
            self.voice_playback_ids.clear()
            self.voice_current_track.clear()
            self.voice_queues.clear()
            self.voice_paused.clear()
            self.voice_finish_actions.clear()
            self._set_voice_state("Idle")
            self._push_voice_queue_state()
            self._detach_voice_loggers()
            if self.memory:
                try:
                    await self.memory.close()
                except Exception:
                    pass
                self.memory = None
            if self.http_session and not self.http_session.closed:
                await self.http_session.close()
            self.http_session = None
            self._discord_activity_batches.clear()
            self._heartbeat_seen_pending_outbox.clear()
            self._heartbeat_deferred_outbox_reasons.clear()
            self._status("Stopped")

    async def _shutdown(self):
        self._status("Stopping...")
        self._stop_local_heartbeat_worker(quiet=True)
        if self._heartbeat_outbox_task:
            self._heartbeat_outbox_task.cancel()
            self._heartbeat_outbox_task = None
        if self._discord_activity_task:
            self._discord_activity_task.cancel()
            self._discord_activity_task = None
        if self.memory:
            try:
                await self._flush_pending_discord_activity(force=True)
            except Exception:
                pass
        if self.memory:
            try:
                await self.memory.close()
            except Exception:
                pass
            self.memory = None
        self._discord_activity_batches.clear()
        self._heartbeat_seen_pending_outbox.clear()
        self._heartbeat_deferred_outbox_reasons.clear()
        try:
            if self.bot:
                await self.bot.close()
        except Exception as e:
            self._log(f"Error while stopping: {e}")

    async def _snapshot_guilds(self):
        bot = self.bot
        if not bot:
            return
        await bot.wait_until_ready()
        payload = []
        try:
            for g in bot.guilds:
                entry = {
                    "id": g.id,
                    "name": g.name,
                    "channels": [{"id": c.id, "name": f"#{c.name}"} for c in g.text_channels],
                    "voice_channels": [{"id": c.id, "name": c.name} for c in g.voice_channels],
                    "members": [{"id": m.id, "name": m.display_name} for m in g.members],
                }
                payload.append(entry)
        except Exception as e:
            self._log(f"Snapshot error: {e}")
            return
        cb = self.gui_callbacks.get("guild_snapshot")
        if cb:
            cb(payload)

    # GUI hooks
    def _log(self, text: str):
        cb = self.gui_callbacks.get("log")
        if cb:
            cb(text)

    def _status(self, text: str):
        cb = self.gui_callbacks.get("status")
        if cb:
            cb(text)

    def _set_voice_state(self, text: str):
        cb = self.gui_callbacks.get("set_voice_state")
        if cb:
            cb(text)

    def _set_voice_queue_state(self, payload: dict):
        cb = self.gui_callbacks.get("set_voice_queue")
        if cb:
            cb(payload)

    def _heartbeat_mode_summary(self) -> str:
        if not self.heartbeat_enabled:
            return "disabled"
        if self.heartbeat_shadow_mode:
            return "shadow"
        if self.heartbeat_manual_approval:
            return "manual"
        return "auto"

    def _parse_heartbeat_clock(self, text: str) -> tuple[int, int] | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    def _heartbeat_quiet_hours_active(self) -> bool:
        start = self._parse_heartbeat_clock(self.heartbeat_quiet_hours_start)
        end = self._parse_heartbeat_clock(self.heartbeat_quiet_hours_end)
        if not start or not end:
            return False
        now = time.localtime()
        cur_minutes = now.tm_hour * 60 + now.tm_min
        start_minutes = start[0] * 60 + start[1]
        end_minutes = end[0] * 60 + end[1]
        if start_minutes == end_minutes:
            return False
        if start_minutes < end_minutes:
            return start_minutes <= cur_minutes < end_minutes
        return cur_minutes >= start_minutes or cur_minutes < end_minutes

    def _coerce_outbox_int(self, *values: Any) -> int | None:
        for value in values:
            if value in (None, "", 0, "0"):
                continue
            try:
                parsed = int(value)
            except Exception:
                continue
            if parsed > 0:
                return parsed
        return None

    def _clone_outbox_with_overrides(
        self,
        outbox: dict,
        *,
        message_override: str | None = None,
        channel_id_override: int | None = None,
        guild_id_override: int | None = None,
        reply_to_message_id_override: int | None = None,
    ) -> dict:
        cloned = dict(outbox or {})
        payload = cloned.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        payload = dict(payload)
        context = payload.get("context")
        if not isinstance(context, dict):
            context = {}
        else:
            context = dict(context)

        if message_override is not None:
            text = str(message_override).strip()
            payload["message"] = text
            if "content" in payload:
                payload["content"] = text
        if channel_id_override:
            payload["channel_id"] = int(channel_id_override)
            context["channel_id"] = int(channel_id_override)
        if guild_id_override:
            payload["guild_id"] = int(guild_id_override)
            context["guild_id"] = int(guild_id_override)
        if reply_to_message_id_override:
            payload["reply_to_message_id"] = int(reply_to_message_id_override)
            context["reply_to_message_id"] = int(reply_to_message_id_override)
        if context:
            payload["context"] = context

        cloned["payload"] = payload
        return cloned

    def _normalize_discord_activity_item(self, item: dict | None) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        source = item.get("source_attribution") or {}
        if not isinstance(source, dict):
            return None
        channel_id = self._coerce_outbox_int(source.get("channel_id"))
        guild_id = self._coerce_outbox_int(source.get("guild_id"))
        if not channel_id or not guild_id:
            return None
        return {
            "guild_id": guild_id,
            "guild_name": str(source.get("guild_name") or guild_id),
            "channel_id": channel_id,
            "channel_name": str(source.get("channel_name") or channel_id),
            "reply_to_message_id": self._coerce_outbox_int(
                source.get("reply_to_message_id"),
                source.get("last_message_id"),
            ),
            "last_message_id": self._coerce_outbox_int(source.get("last_message_id")),
            "message_count": max(0, int(source.get("message_count", 0) or 0)),
            "question_count": max(0, int(source.get("question_count", 0) or 0)),
            "bot_mention_count": max(0, int(source.get("bot_mention_count", source.get("mention_count", 0)) or 0)),
            "reply_count": max(0, int(source.get("reply_count", 0) or 0)),
            "revived_after_seconds": max(0, int(source.get("revived_after_seconds", 0) or 0)),
            "last_preview": str(source.get("last_preview") or source.get("preview") or "").strip(),
            "reason": str(source.get("reason") or "").strip(),
            "created_at": item.get("created_at"),
            "raw": item,
        }

    def _score_activity_for_outbox(self, activity: dict[str, Any], *, intent: str) -> int:
        score = 0
        enabled_cfgs = self._enabled_heartbeat_server_configs()
        guild_cfg = enabled_cfgs.get(int(activity["guild_id"]))
        if enabled_cfgs:
            if not guild_cfg:
                return -1000
            if int(guild_cfg.get("channel_id", 0) or 0) != int(activity["channel_id"]):
                return -1000
            score += 55
        elif self.heartbeat_home_guild_id and int(activity["guild_id"]) == int(self.heartbeat_home_guild_id):
            score += 30
        elif self.heartbeat_allowed_channel_ids:
            if int(activity["channel_id"]) in self.heartbeat_allowed_channel_ids:
                score += 25
            else:
                score -= 200
        score += min(12, int(activity.get("message_count", 0)))
        score += min(40, int(activity.get("question_count", 0)) * 18)
        score += min(36, int(activity.get("bot_mention_count", 0)) * 18)
        score += min(20, int(activity.get("reply_count", 0)) * 8)
        if activity.get("reply_to_message_id"):
            score += 8
        if int(activity.get("revived_after_seconds", 0) or 0) > 0:
            score += 10
        lower_intent = intent.lower()
        if "question" in lower_intent or "answer" in lower_intent or "reply" in lower_intent:
            score += int(activity.get("question_count", 0)) * 10
        if "follow" in lower_intent or "check" in lower_intent:
            if int(activity.get("revived_after_seconds", 0) or 0) > 0:
                score += 12
            if activity.get("reply_to_message_id"):
                score += 8
        return score

    def _infer_outbox_target_from_recent_activity(
        self,
        *,
        intent: str,
        recent_activity: list[dict] | None,
    ) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        best_score: int | None = None
        for item in recent_activity or []:
            activity = self._normalize_discord_activity_item(item)
            if not activity:
                continue
            score = self._score_activity_for_outbox(activity, intent=intent)
            if best is None or best_score is None or score > best_score:
                best = activity
                best_score = score
        if not best or best_score is None or best_score < 0:
            return None
        inferred = dict(best)
        inferred["score"] = best_score
        preview = inferred.get("last_preview") or "(no recent preview)"
        inferred["inference_reason"] = (
            f"Inferred from recent Discord activity in {inferred['guild_name']} / "
            f"#{inferred['channel_name']} (score {best_score}; preview: {preview})"
        )
        return inferred

    def preview_heartbeat_outbox_candidate(
        self,
        outbox: dict,
        *,
        recent_activity: list[dict] | None = None,
        message_override: str | None = None,
        channel_id_override: int | None = None,
        guild_id_override: int | None = None,
        reply_to_message_id_override: int | None = None,
    ) -> dict[str, Any]:
        preview_outbox = self._clone_outbox_with_overrides(
            outbox,
            message_override=message_override,
            channel_id_override=channel_id_override,
            guild_id_override=guild_id_override,
            reply_to_message_id_override=reply_to_message_id_override,
        )
        candidate, error = self._make_heartbeat_candidate(preview_outbox, recent_activity=recent_activity)
        warnings: list[str] = []
        enabled_cfgs = self._enabled_heartbeat_server_configs()
        if not enabled_cfgs:
            warnings.append("No heartbeat servers are enabled.")
        if candidate:
            channel_id = candidate.get("channel_id")
            guild_id = candidate.get("guild_id")
            if guild_id:
                cfg = self._heartbeat_server_config(int(guild_id))
                if not cfg or not cfg.get("enabled"):
                    warnings.append(f"Server {guild_id} is not enabled for heartbeat responses.")
                elif channel_id and int(channel_id) != int(cfg.get("channel_id", 0) or 0):
                    warnings.append(
                        f"Target channel {channel_id} does not match the configured heartbeat channel "
                        f"{cfg.get('channel_id')} for server {guild_id}."
                    )
            if self.heartbeat_require_reply_context and not candidate.get("reply_to_message_id"):
                warnings.append("Policy requires reply context, but no reply target is resolved.")

        lines: list[str] = []
        if error:
            lines.append(f"Preview error: {error}")
        elif candidate:
            target_label = "unresolved"
            if candidate.get("channel_name"):
                target_label = f"{candidate.get('guild_name') or candidate.get('guild_id')} / #{candidate.get('channel_name')}"
            elif candidate.get("channel_id"):
                target_label = f"guild {candidate.get('guild_id') or '?'} / channel {candidate.get('channel_id')}"
            lines.append(f"Target preview: {target_label}")
            if candidate.get("reply_to_message_id"):
                lines.append(f"Reply target: {candidate.get('reply_to_message_id')}")
            if candidate.get("inference_reason"):
                lines.append(str(candidate["inference_reason"]))
        if warnings:
            lines.extend(f"Warning: {warning}" for warning in warnings)

        return {
            "ok": error is None,
            "error": error,
            "warnings": warnings,
            "candidate": candidate,
            "summary": "\n".join(lines) if lines else "No candidate preview available.",
            "outbox": preview_outbox,
        }

    def _heartbeat_activity_preview(self, message: discord.Message) -> str:
        raw = getattr(message, "clean_content", None) or message.content or ""
        raw = re.sub(r"\s+", " ", str(raw)).strip()
        attachment_count = len(getattr(message, "attachments", []) or [])
        if not raw and attachment_count:
            raw = f"[{attachment_count} attachment(s)]"
        elif raw and attachment_count:
            raw = f"{raw} [{attachment_count} attachment(s)]"
        if len(raw) > HEARTBEAT_ACTIVITY_PREVIEW_CHARS:
            raw = raw[:HEARTBEAT_ACTIVITY_PREVIEW_CHARS - 3].rstrip() + "..."
        return raw

    def _new_discord_activity_bucket(
        self,
        message: discord.Message,
        *,
        revived_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        created_at = message.created_at or discord.utils.utcnow()
        return {
            "guild_id": int(message.guild.id),
            "guild_name": str(getattr(message.guild, "name", message.guild.id)),
            "channel_id": int(message.channel.id),
            "channel_name": str(getattr(message.channel, "name", message.channel.id)),
            "started_at": created_at,
            "last_seen_at": created_at,
            "message_count": 0,
            "question_count": 0,
            "bot_mention_count": 0,
            "reply_count": 0,
            "attachment_count": 0,
            "author_ids": set(),
            "author_names": [],
            "concept_counts": Counter(),
            "revived_after_seconds": revived_after_seconds,
            "last_author_id": None,
            "last_author_name": "",
            "last_message_id": None,
            "last_preview": "",
            "last_reply_to_message_id": None,
            "last_jump_url": "",
        }

    async def _track_discord_activity(self, message: discord.Message) -> None:
        if not self.memory or not self.memory_enabled or not message.guild or message.author.bot:
            return
        key = (int(message.guild.id), int(message.channel.id))
        created_at = message.created_at or discord.utils.utcnow()
        bucket = self._discord_activity_batches.get(key)
        revived_after_seconds: int | None = None
        dead_threshold_seconds = max(0, int(self.heartbeat_dead_channel_threshold_minutes)) * 60

        if bucket:
            last_seen_at = bucket.get("last_seen_at")
            if isinstance(last_seen_at, datetime):
                gap_seconds = max(0, int((created_at - last_seen_at).total_seconds()))
                if gap_seconds >= HEARTBEAT_ACTIVITY_MAX_BUFFER_SECONDS:
                    await self._flush_discord_activity_bucket(key, reason="buffer_elapsed")
                    bucket = None
                if dead_threshold_seconds > 0 and gap_seconds >= dead_threshold_seconds:
                    revived_after_seconds = gap_seconds

        if bucket is None:
            bucket = self._new_discord_activity_bucket(message, revived_after_seconds=revived_after_seconds)
            self._discord_activity_batches[key] = bucket
        elif revived_after_seconds and not bucket.get("revived_after_seconds"):
            bucket["revived_after_seconds"] = revived_after_seconds

        preview = self._heartbeat_activity_preview(message)
        reply_to_message_id = None
        if message.reference is not None:
            reply_to_message_id = self._coerce_outbox_int(getattr(message.reference, "message_id", None))
        mentioned_bot = bool(self.bot and self.bot.user and self.bot.user in message.mentions)
        is_question = "?" in preview

        bucket["last_seen_at"] = created_at
        bucket["message_count"] = int(bucket.get("message_count", 0)) + 1
        bucket["question_count"] = int(bucket.get("question_count", 0)) + (1 if is_question else 0)
        bucket["bot_mention_count"] = int(bucket.get("bot_mention_count", 0)) + (1 if mentioned_bot else 0)
        bucket["reply_count"] = int(bucket.get("reply_count", 0)) + (1 if reply_to_message_id else 0)
        bucket["attachment_count"] = int(bucket.get("attachment_count", 0)) + len(getattr(message, "attachments", []) or [])

        author_ids = bucket.get("author_ids")
        if isinstance(author_ids, set):
            author_ids.add(int(message.author.id))
        author_names = bucket.get("author_names")
        if isinstance(author_names, list) and message.author.display_name not in author_names:
            author_names.append(str(message.author.display_name))
        concept_counts = bucket.get("concept_counts")
        if isinstance(concept_counts, Counter):
            for concept in _extract_concepts(preview, max_concepts=6):
                concept_counts[concept] += 1

        bucket["last_author_id"] = int(message.author.id)
        bucket["last_author_name"] = str(message.author.display_name)
        bucket["last_message_id"] = int(message.id)
        bucket["last_preview"] = preview
        bucket["last_reply_to_message_id"] = reply_to_message_id
        bucket["last_jump_url"] = str(getattr(message, "jump_url", "") or "")

        should_flush = False
        reason = "batched"
        if bucket.get("revived_after_seconds"):
            should_flush = True
            reason = "channel_revival"
        elif mentioned_bot or is_question or reply_to_message_id:
            should_flush = True
            reason = "notable_message"
        elif int(bucket.get("message_count", 0)) >= HEARTBEAT_ACTIVITY_BATCH_SIZE:
            should_flush = True
            reason = "batch_size"
        else:
            started_at = bucket.get("started_at")
            if isinstance(started_at, datetime):
                age_seconds = max(0, int((created_at - started_at).total_seconds()))
                if age_seconds >= HEARTBEAT_ACTIVITY_MAX_BUFFER_SECONDS:
                    should_flush = True
                    reason = "buffer_elapsed"

        if should_flush:
            await self._flush_discord_activity_bucket(key, reason=reason)

    async def _flush_discord_activity_bucket(self, key: tuple[int, int], *, reason: str) -> None:
        bucket = self._discord_activity_batches.pop(key, None)
        if not bucket:
            return
        if not self.memory or not self.memory_enabled:
            return

        message_count = max(0, int(bucket.get("message_count", 0) or 0))
        if message_count <= 0:
            return

        author_ids = bucket.get("author_ids")
        if isinstance(author_ids, set):
            author_id_list = sorted(int(x) for x in author_ids)
        else:
            author_id_list = []
        author_names = list(bucket.get("author_names") or [])
        concept_counts = bucket.get("concept_counts")
        topics = concept_counts.most_common(5) if isinstance(concept_counts, Counter) else []
        topic_names = [name for name, _count in topics]
        started_at = bucket.get("started_at")
        last_seen_at = bucket.get("last_seen_at")
        duration_seconds = 0
        if isinstance(started_at, datetime) and isinstance(last_seen_at, datetime):
            duration_seconds = max(0, int((last_seen_at - started_at).total_seconds()))

        lead = (
            f"Discord activity in {bucket.get('guild_name')} / #{bucket.get('channel_name')}: "
            f"{message_count} human message(s) from {len(author_id_list) or 1} user(s)"
        )
        detail_bits: list[str] = []
        if int(bucket.get("question_count", 0) or 0) > 0:
            detail_bits.append(f"{int(bucket['question_count'])} question(s)")
        if int(bucket.get("bot_mention_count", 0) or 0) > 0:
            detail_bits.append(f"{int(bucket['bot_mention_count'])} bot mention(s)")
        if int(bucket.get("reply_count", 0) or 0) > 0:
            detail_bits.append(f"{int(bucket['reply_count'])} reply/replies")
        if int(bucket.get("attachment_count", 0) or 0) > 0:
            detail_bits.append(f"{int(bucket['attachment_count'])} attachment(s)")
        if bucket.get("revived_after_seconds"):
            quiet_minutes = max(1, int(int(bucket["revived_after_seconds"]) / 60))
            detail_bits.append(f"channel revived after {quiet_minutes} minute(s) quiet")
        if detail_bits:
            lead += " (" + ", ".join(detail_bits) + ")"

        tail_parts: list[str] = []
        if author_names:
            shown_authors = ", ".join(author_names[:3])
            if len(author_names) > 3:
                shown_authors += f", +{len(author_names) - 3} more"
            tail_parts.append(f"Participants: {shown_authors}.")
        preview = str(bucket.get("last_preview") or "").strip()
        if preview:
            tail_parts.append(f"Latest from {bucket.get('last_author_name') or 'unknown'}: {preview}")
        if topic_names:
            tail_parts.append("Topics: " + ", ".join(topic_names) + ".")
        content = " ".join(part for part in [lead] + tail_parts if part).strip()

        source_attr: dict[str, Any] = {
            "kind": "discord_activity",
            "guild_id": int(bucket.get("guild_id")),
            "guild_name": str(bucket.get("guild_name") or ""),
            "channel_id": int(bucket.get("channel_id")),
            "channel_name": str(bucket.get("channel_name") or ""),
            "message_count": message_count,
            "question_count": int(bucket.get("question_count", 0) or 0),
            "bot_mention_count": int(bucket.get("bot_mention_count", 0) or 0),
            "reply_count": int(bucket.get("reply_count", 0) or 0),
            "attachment_count": int(bucket.get("attachment_count", 0) or 0),
            "author_ids": author_id_list,
            "author_names": author_names[:5],
            "last_author_id": self._coerce_outbox_int(bucket.get("last_author_id")),
            "last_author_name": str(bucket.get("last_author_name") or ""),
            "last_message_id": self._coerce_outbox_int(bucket.get("last_message_id")),
            "last_preview": preview,
            "reply_to_message_id": self._coerce_outbox_int(bucket.get("last_reply_to_message_id")),
            "jump_url": str(bucket.get("last_jump_url") or ""),
            "topics": topic_names,
            "window_started_at": started_at.isoformat() if isinstance(started_at, datetime) else None,
            "window_ended_at": last_seen_at.isoformat() if isinstance(last_seen_at, datetime) else None,
            "duration_seconds": duration_seconds,
            "reason": reason,
        }
        if bucket.get("revived_after_seconds"):
            source_attr["revived_after_seconds"] = int(bucket["revived_after_seconds"])

        importance = 0.18
        importance += min(0.18, 0.03 * message_count)
        if source_attr["question_count"] > 0:
            importance += 0.10
        if source_attr["bot_mention_count"] > 0:
            importance += 0.12
        if source_attr["reply_count"] > 0:
            importance += 0.05
        if bucket.get("revived_after_seconds"):
            importance += 0.08
        importance = max(0.18, min(0.78, importance))

        ttl_seconds = HEARTBEAT_ACTIVITY_TTL_SECONDS
        if source_attr["question_count"] > 0 or source_attr["bot_mention_count"] > 0:
            ttl_seconds += 2 * 3600

        try:
            await self.memory.hold(
                content,
                ttl_seconds=ttl_seconds,
                importance=importance,
                source_attribution=source_attr,
                trust_level=0.85,
                promote_to_long_term=False,
            )
        except Exception as exc:
            self._log(
                f"Discord activity memory flush failed for "
                f"{bucket.get('guild_name')} / #{bucket.get('channel_name')}: {exc}"
            )
            if key not in self._discord_activity_batches:
                self._discord_activity_batches[key] = bucket
            return

    async def _flush_pending_discord_activity(self, *, force: bool = False) -> None:
        if not self._discord_activity_batches:
            return
        now = discord.utils.utcnow()
        for key, bucket in list(self._discord_activity_batches.items()):
            if force:
                await self._flush_discord_activity_bucket(key, reason="shutdown_flush")
                continue
            started_at = bucket.get("started_at")
            last_seen_at = bucket.get("last_seen_at")
            age_seconds = None
            idle_seconds = None
            if isinstance(started_at, datetime):
                age_seconds = max(0, int((now - started_at).total_seconds()))
            if isinstance(last_seen_at, datetime):
                idle_seconds = max(0, int((now - last_seen_at).total_seconds()))
            if (idle_seconds is not None and idle_seconds >= HEARTBEAT_ACTIVITY_FLUSH_INTERVAL_SECONDS) or (
                age_seconds is not None and age_seconds >= HEARTBEAT_ACTIVITY_MAX_BUFFER_SECONDS
            ):
                await self._flush_discord_activity_bucket(key, reason="idle_flush")

    async def _discord_activity_loop(self) -> None:
        try:
            while self.running:
                try:
                    await self._flush_pending_discord_activity(force=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log(f"Discord activity loop error: {exc}")
                await asyncio.sleep(HEARTBEAT_ACTIVITY_FLUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def _resolve_heartbeat_channel(self, channel_id: int | None):
        if not channel_id or not self.bot:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(int(channel_id))
        except Exception:
            return None

    async def _count_recent_human_messages(self, channel) -> tuple[int, datetime | None]:
        threshold_minutes = max(0, int(self.heartbeat_dead_channel_threshold_minutes))
        cutoff = None
        if threshold_minutes > 0:
            cutoff = discord.utils.utcnow() - timedelta(minutes=threshold_minutes)
        count = 0
        last_human_at: datetime | None = None
        history_kwargs: dict[str, Any] = {"limit": 50}
        if cutoff is not None:
            history_kwargs["after"] = cutoff
        async for msg in channel.history(**history_kwargs):
            if msg.author.bot:
                continue
            count += 1
            if last_human_at is None:
                last_human_at = msg.created_at
        return count, last_human_at

    async def _count_recent_sent_outbox(self, *, within: timedelta) -> int:
        if not self.memory:
            return 0
        sent_items = await self.memory.get_outbox_messages(status="sent", limit=500)
        now = datetime.now(timezone.utc)
        total = 0
        for item in sent_items:
            sent_at = item.get("sent_at")
            if not isinstance(sent_at, datetime):
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            if now - sent_at <= within:
                total += 1
        return total

    def _make_heartbeat_candidate(
        self,
        outbox: dict,
        *,
        recent_activity: list[dict] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        payload = outbox.get("payload") or {}
        if not isinstance(payload, dict):
            return None, "Outbox payload is not a JSON object."
        context = payload.get("context")
        if not isinstance(context, dict):
            context = {}

        if outbox.get("kind") != "user":
            return None, f"Unsupported outbox kind for Discord delivery: {outbox.get('kind')!r}"

        content = payload.get("message") or payload.get("content")
        content = str(content or "").strip()
        if not content:
            return None, "Outbox payload has no message content."

        intent = str(payload.get("intent") or context.get("intent") or "").strip()

        channel_id = self._coerce_outbox_int(
            payload.get("channel_id"),
            payload.get("target_channel_id"),
            context.get("channel_id"),
        )
        guild_id = self._coerce_outbox_int(
            payload.get("guild_id"),
            payload.get("target_guild_id"),
            context.get("guild_id"),
        )
        reply_to_message_id = self._coerce_outbox_int(
            payload.get("reply_to_message_id"),
            payload.get("target_message_id"),
            context.get("reply_to_message_id"),
            context.get("message_id"),
        )

        inferred_from_activity = False
        inference_reason = ""
        activity_choice = None
        if channel_id is None or guild_id is None or (
            self.heartbeat_require_reply_context and reply_to_message_id is None
        ):
            activity_choice = self._infer_outbox_target_from_recent_activity(
                intent=intent,
                recent_activity=recent_activity,
            )
            if activity_choice:
                inferred_from_activity = True
                inference_reason = str(activity_choice.get("inference_reason") or "")
                if guild_id is None:
                    guild_id = self._coerce_outbox_int(activity_choice.get("guild_id"))
                if channel_id is None:
                    channel_id = self._coerce_outbox_int(activity_choice.get("channel_id"))
                if reply_to_message_id is None:
                    reply_to_message_id = self._coerce_outbox_int(
                        activity_choice.get("reply_to_message_id"),
                        activity_choice.get("last_message_id"),
                    )

        enabled_cfgs = self._enabled_heartbeat_server_configs()
        if channel_id is None and guild_id is not None:
            channel_id = self._heartbeat_channel_id_for_guild(int(guild_id))
            if channel_id and not inference_reason:
                inference_reason = f"Used configured heartbeat channel {channel_id} for server {guild_id}."
        if guild_id is None and channel_id is not None:
            for cfg_gid, cfg in enabled_cfgs.items():
                if int(cfg.get("channel_id", 0) or 0) == int(channel_id):
                    guild_id = int(cfg_gid)
                    if not inference_reason:
                        inference_reason = f"Matched configured heartbeat server {guild_id} from channel {channel_id}."
                    break
        if channel_id is None and len(enabled_cfgs) == 1:
            only_gid, only_cfg = next(iter(enabled_cfgs.items()))
            channel_id = self._coerce_outbox_int(only_cfg.get("channel_id"))
            if guild_id is None:
                guild_id = int(only_gid)
            if channel_id and not inference_reason:
                inference_reason = f"Fell back to the only enabled heartbeat route: server {only_gid}, channel {channel_id}."

        return {
            "outbox_id": str(outbox.get("id")),
            "content": content,
            "intent": intent,
            "channel_id": channel_id,
            "guild_id": guild_id,
            "reply_to_message_id": reply_to_message_id,
            "inferred_from_recent_activity": inferred_from_activity,
            "inference_reason": inference_reason,
            "activity_choice": activity_choice,
            "guild_name": activity_choice.get("guild_name") if isinstance(activity_choice, dict) else None,
            "channel_name": activity_choice.get("channel_name") if isinstance(activity_choice, dict) else None,
            "raw": outbox,
        }, None

    async def _validate_heartbeat_candidate(self, candidate: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if not self.bot:
            return None, "Discord bot is not running."
        enabled_cfgs = self._enabled_heartbeat_server_configs()
        if not enabled_cfgs:
            return None, "No heartbeat servers are enabled."
        if self._heartbeat_quiet_hours_active():
            return None, "Heartbeat quiet hours are active."

        channel_id = candidate.get("channel_id")
        if not channel_id:
            return None, "No target channel could be resolved for this outbox message."

        channel = await self._resolve_heartbeat_channel(int(channel_id))
        if channel is None:
            return None, f"Could not resolve Discord channel {channel_id}."
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return None, "Target channel is not a text channel or thread."

        guild = getattr(channel, "guild", None)
        resolved_guild_id = int(guild.id) if guild else None
        if resolved_guild_id is None:
            return None, "Target channel is not attached to a guild."
        guild_cfg = enabled_cfgs.get(int(resolved_guild_id))
        if not guild_cfg or not guild_cfg.get("enabled"):
            return None, f"Heartbeat responses are disabled for guild {resolved_guild_id}."
        configured_channel_id = self._coerce_outbox_int(guild_cfg.get("channel_id"))
        if configured_channel_id and int(configured_channel_id) != int(channel_id):
            return None, (
                f"Channel {channel_id} does not match the configured heartbeat channel "
                f"{configured_channel_id} for guild {resolved_guild_id}."
            )
        if candidate.get("guild_id") and int(candidate["guild_id"]) != resolved_guild_id:
            return None, "Outbox payload guild does not match the resolved channel guild."

        if self.heartbeat_require_reply_context and not candidate.get("reply_to_message_id"):
            return None, "Heartbeat policy requires reply context, but this proposal has no parent message."

        hour_count = await self._count_recent_sent_outbox(within=timedelta(hours=1))
        if self.heartbeat_max_posts_per_hour > 0 and hour_count >= self.heartbeat_max_posts_per_hour:
            return None, "Heartbeat hourly send quota reached."
        day_count = await self._count_recent_sent_outbox(within=timedelta(days=1))
        if self.heartbeat_max_posts_per_day > 0 and day_count >= self.heartbeat_max_posts_per_day:
            return None, "Heartbeat daily send quota reached."

        human_count, _last_human_at = await self._count_recent_human_messages(channel)
        if self.heartbeat_min_recent_human_messages > 0 and human_count < self.heartbeat_min_recent_human_messages:
            return None, (
                f"Recent human activity below threshold: {human_count} < {self.heartbeat_min_recent_human_messages}."
            )

        validated = dict(candidate)
        validated["channel"] = channel
        validated["resolved_guild_id"] = resolved_guild_id
        validated["send_typing"] = bool(guild_cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING))
        validated["recent_human_messages"] = human_count
        return validated, None

    async def _mark_outbox_failed(self, outbox_id: str, reason: str) -> None:
        if self.memory:
            await self.memory.update_outbox_message_status(outbox_id, "failed", error_message=reason)

    def _heartbeat_outbox_should_defer(self, reason: str) -> bool:
        normalized = str(reason or "").strip()
        if not normalized:
            return False
        retryable_prefixes = (
            "Discord bot is not running.",
            "No heartbeat servers are enabled.",
            "Heartbeat quiet hours are active.",
            "No target channel could be resolved for this outbox message.",
            "Could not resolve Discord channel ",
            "Target channel is not a text channel or thread.",
            "Target channel is not attached to a guild.",
            "Heartbeat responses are disabled for guild ",
            "Outbox payload guild does not match the resolved channel guild.",
            "Heartbeat policy requires reply context, but this proposal has no parent message.",
            "Heartbeat hourly send quota reached.",
            "Heartbeat daily send quota reached.",
            "Recent human activity below threshold:",
        )
        if normalized.startswith(retryable_prefixes):
            return True
        return (
            normalized.startswith("Channel ")
            and "does not match the configured heartbeat channel" in normalized
        )

    async def _fail_or_defer_heartbeat_outbox(self, outbox_id: str, reason: str) -> None:
        if self._heartbeat_outbox_should_defer(reason):
            raise HeartbeatOutboxDeferred(reason)
        await self._mark_outbox_failed(outbox_id, reason)
        raise RuntimeError(reason)

    def _synthetic_test_outbox_reason(self, outbox: dict) -> str | None:
        payload = outbox.get("payload") or {}
        if not isinstance(payload, dict):
            return None

        intent = str(payload.get("intent") or "").strip().lower()
        message = str(payload.get("message") or payload.get("content") or "").strip().lower()
        reason = str(payload.get("reason") or "").strip().lower()
        community_signal = str(payload.get("community_signal") or "").strip().lower()

        reserved_test_intents = {"test", "smoke_test", "worker_smoke"}
        if intent in reserved_test_intents:
            return f"Blocked synthetic test outbox payload (intent={intent})."

        combined = " ".join(part for part in (message, reason, community_signal) if part)
        if "worker_e2e_" in combined or message.startswith("hello worker_"):
            return "Blocked synthetic worker_e2e outbox payload."

        return None

    def _non_heartbeat_outbox_reason(self, outbox: dict) -> str | None:
        payload = outbox.get("payload") or {}
        if not isinstance(payload, dict):
            return "Blocked malformed outbox payload."

        context = payload.get("context") or {}
        if not isinstance(context, dict):
            context = {}

        heartbeat_id = payload.get("heartbeat_id") or context.get("heartbeat_id")
        if heartbeat_id:
            return None
        return "Blocked non-heartbeat outbox payload (missing heartbeat_id)."

    async def _send_heartbeat_outbox(
        self,
        outbox: dict,
        *,
        approved_by_operator: bool = False,
        message_override: str | None = None,
        channel_id_override: int | None = None,
        guild_id_override: int | None = None,
        reply_to_message_id_override: int | None = None,
    ) -> str:
        if not self.memory:
            raise RuntimeError("Memory system is not connected.")
        outbox_id = str(outbox.get("id"))

        effective_outbox = self._clone_outbox_with_overrides(
            outbox,
            message_override=message_override,
            channel_id_override=channel_id_override,
            guild_id_override=guild_id_override,
            reply_to_message_id_override=reply_to_message_id_override,
        )
        synthetic_reason = self._synthetic_test_outbox_reason(effective_outbox)
        if synthetic_reason:
            await self._mark_outbox_failed(outbox_id, synthetic_reason)
            raise RuntimeError(synthetic_reason)
        non_heartbeat_reason = self._non_heartbeat_outbox_reason(effective_outbox)
        if non_heartbeat_reason:
            await self._mark_outbox_failed(outbox_id, non_heartbeat_reason)
            raise RuntimeError(non_heartbeat_reason)

        recent_activity = await self.memory.get_recent_discord_activity(
            guild_id=None,
            limit=12,
            within_hours=24,
        )
        candidate, err = self._make_heartbeat_candidate(effective_outbox, recent_activity=recent_activity)
        if err:
            await self._fail_or_defer_heartbeat_outbox(outbox_id, err)

        effective_outbox = self._clone_outbox_with_overrides(
            effective_outbox,
            channel_id_override=self._coerce_outbox_int(candidate.get("channel_id")),
            guild_id_override=self._coerce_outbox_int(candidate.get("guild_id")),
            reply_to_message_id_override=self._coerce_outbox_int(candidate.get("reply_to_message_id")),
        )
        if effective_outbox.get("payload") != outbox.get("payload"):
            await self.memory.update_outbox_message_payload(outbox_id, effective_outbox.get("payload") or {})

        validated, err = await self._validate_heartbeat_candidate(candidate)
        if err:
            await self._fail_or_defer_heartbeat_outbox(outbox_id, err)

        channel = validated["channel"]
        content = validated["content"]
        reply_to_message_id = validated.get("reply_to_message_id")
        send_kwargs: dict[str, Any] = {}
        if reply_to_message_id:
            try:
                send_kwargs["reference"] = channel.get_partial_message(int(reply_to_message_id))
                send_kwargs["mention_author"] = False
            except Exception:
                pass

        try:
            if validated.get("send_typing"):
                async with channel.typing():
                    await asyncio.sleep(1.0)
                    sent_message = await channel.send(content, **send_kwargs)
            else:
                sent_message = await channel.send(content, **send_kwargs)
        except Exception as exc:
            reason = f"Discord send failed: {exc}"
            await self._mark_outbox_failed(outbox_id, reason)
            raise RuntimeError(reason) from exc
        await self.memory.update_outbox_message_status(outbox_id, "sent")
        mode = "manual" if approved_by_operator else "auto"
        inference_note = ""
        if validated.get("inference_reason"):
            inference_note = f" {validated['inference_reason']}"
        return (
            f"Heartbeat outbox {outbox_id} sent via {mode} mode to "
            f"{getattr(channel, 'guild', None).name if getattr(channel, 'guild', None) else validated['resolved_guild_id']} / "
            f"{getattr(channel, 'name', validated['channel_id'])} "
            f"(message {sent_message.id}).{inference_note}"
        )

    async def _approve_outbox_message(
        self,
        outbox_id: str,
        *,
        message_override: str | None = None,
        channel_id_override: int | None = None,
        guild_id_override: int | None = None,
        reply_to_message_id_override: int | None = None,
    ) -> str:
        if not self.memory:
            raise RuntimeError("Memory system is not connected.")
        outbox = await self.memory.get_outbox_message(outbox_id)
        if not outbox:
            raise RuntimeError(f"Outbox message not found: {outbox_id}")
        if str(outbox.get("status")) != "pending":
            raise RuntimeError(f"Outbox message {outbox_id} is not pending.")
        return await self._send_heartbeat_outbox(
            outbox,
            approved_by_operator=True,
            message_override=message_override,
            channel_id_override=channel_id_override,
            guild_id_override=guild_id_override,
            reply_to_message_id_override=reply_to_message_id_override,
        )

    async def _heartbeat_outbox_loop(self) -> None:
        try:
            while self.running:
                try:
                    await self._process_heartbeat_outbox_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log(f"Heartbeat outbox loop error: {exc}")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            return

    async def _process_heartbeat_outbox_tick(self) -> None:
        self._reconcile_local_heartbeat_worker()
        if not self.memory or not self.memory_enabled or not self.heartbeat_enabled:
            return
        pending = await self.memory.get_outbox_messages(status="pending", limit=25)
        pending_ids = {str(item.get("id")) for item in pending}
        self._heartbeat_seen_pending_outbox.intersection_update(pending_ids)
        self._heartbeat_deferred_outbox_reasons = {
            outbox_id: reason
            for outbox_id, reason in self._heartbeat_deferred_outbox_reasons.items()
            if outbox_id in pending_ids
        }
        for item in pending:
            outbox_id = str(item.get("id"))
            if outbox_id not in self._heartbeat_seen_pending_outbox:
                self._heartbeat_seen_pending_outbox.add(outbox_id)
                self._log(
                    f"Heartbeat proposal queued: {outbox_id} "
                    f"({self._heartbeat_mode_summary()} mode)."
                )
            if self.heartbeat_shadow_mode or self.heartbeat_manual_approval:
                continue
            try:
                msg = await self._send_heartbeat_outbox(item, approved_by_operator=False)
            except HeartbeatOutboxDeferred as exc:
                reason = str(exc)
                if self._heartbeat_deferred_outbox_reasons.get(outbox_id) != reason:
                    self._heartbeat_deferred_outbox_reasons[outbox_id] = reason
                    self._log(f"Heartbeat auto-send deferred for {outbox_id}: {reason}")
                continue
            except Exception as exc:
                self._heartbeat_deferred_outbox_reasons.pop(outbox_id, None)
                self._log(f"Heartbeat auto-send blocked for {outbox_id}: {exc}")
                continue
            self._heartbeat_deferred_outbox_reasons.pop(outbox_id, None)
            self._log(msg)

    def _active_voice_client(self) -> discord.VoiceClient | None:
        bot = self.bot
        if not bot:
            return None
        for voice_client in getattr(bot, "voice_clients", []):
            try:
                if voice_client and voice_client.is_connected():
                    return voice_client
            except Exception:
                continue
        return None

    def _voice_runtime_guild_id(self) -> int | None:
        active = self._active_voice_client()
        if active and active.guild:
            return int(active.guild.id)
        for store in (self.voice_current_track, self.voice_queues, self.voice_paused, self.voice_playback_ids):
            for guild_id, value in store.items():
                if value:
                    return int(guild_id)
        return None

    def _format_track_label(self, track: dict) -> str:
        title = str(track.get("title") or "Untitled track").strip()
        duration = _format_duration(track.get("duration"))
        uploader = str(track.get("uploader") or "").strip()
        requested_by = str(track.get("requested_by") or "").strip()
        parts = [title]
        if duration:
            parts.append(f"[{duration}]")
        if uploader:
            parts.append(f"- {uploader}")
        if requested_by:
            parts.append(f"(by {requested_by})")
        return " ".join(parts)

    def _voice_queue_lines(self, guild_id: int, *, limit: int = 10) -> list[str]:
        lines: list[str] = []
        track = self.voice_current_track.get(int(guild_id))
        paused = bool(self.voice_paused.get(int(guild_id), False))
        if track:
            prefix = "Paused" if paused else "Now playing"
            lines.append(f"**{prefix}:** {self._format_track_label(track)}")
        else:
            lines.append("**Now playing:** nothing")
        queue_items = list(self.voice_queues.get(int(guild_id)) or [])
        if not queue_items:
            lines.append("**Queue:** empty")
            return lines
        lines.append("**Queue**")
        for idx, item in enumerate(queue_items[:limit], 1):
            lines.append(f"{idx}. {self._format_track_label(item)}")
        if len(queue_items) > limit:
            lines.append(f"... and {len(queue_items) - limit} more")
        return lines

    def _push_voice_queue_state(self):
        guild_id = self._voice_runtime_guild_id()
        active = self._active_voice_client()
        guild_name = ""
        channel_name = ""
        connected = False
        if active and active.guild:
            guild_name = active.guild.name
            connected = bool(active.is_connected())
            if active.channel:
                channel_name = active.channel.name
            guild_id = int(active.guild.id)
        elif guild_id is not None and self.bot:
            guild = self.bot.get_guild(int(guild_id))
            if guild:
                guild_name = guild.name
        queue_labels: list[str] = []
        now_playing = ""
        paused = False
        if guild_id is not None:
            track = self.voice_current_track.get(int(guild_id))
            if track:
                now_playing = self._format_track_label(track)
            paused = bool(self.voice_paused.get(int(guild_id), False))
            queue_labels = [
                self._format_track_label(item)
                for item in list(self.voice_queues.get(int(guild_id)) or [])
            ]
        self._set_voice_queue_state(
            {
                "active_guild_id": guild_id,
                "active_guild_name": guild_name,
                "channel_name": channel_name,
                "connected": connected,
                "paused": paused,
                "now_playing": now_playing,
                "queue": queue_labels,
            }
        )

    def _clear_voice_runtime(self, guild_id: int, *, clear_queue: bool):
        guild_id = int(guild_id)
        self.voice_current_track.pop(guild_id, None)
        self.voice_now_playing.pop(guild_id, None)
        self.voice_playback_ids.pop(guild_id, None)
        self.voice_paused.pop(guild_id, None)
        self.voice_finish_actions.pop(guild_id, None)
        if clear_queue:
            self.voice_queues.pop(guild_id, None)

    def _voice_conflict_message(self, active_voice_client: discord.VoiceClient, target_guild_id: int) -> str:
        active_guild = active_voice_client.guild.name if active_voice_client.guild else f"guild {target_guild_id}"
        active_channel = active_voice_client.channel.name if active_voice_client.channel else "voice"
        target_name = f"guild {target_guild_id}"
        if self.bot:
            guild = self.bot.get_guild(int(target_guild_id))
            if guild:
                target_name = guild.name
        return (
            f"Bot is already connected to {active_guild} / {active_channel}. "
            f"Leave that server first before joining {target_name}."
        )

    def _assert_voice_guild_access(self, guild_id: int):
        active_voice_client = self._active_voice_client()
        if not active_voice_client or not active_voice_client.guild:
            return
        if int(active_voice_client.guild.id) == int(guild_id):
            return
        message = self._voice_conflict_message(active_voice_client, guild_id)
        self._set_voice_state(message)
        raise RuntimeError(message)

    def _submit_async_action(self, coro_factory, error_prefix: str):
        if not self.running or not self.bot or not self.loop:
            self._log("Start the bot first.")
            return
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)

        def _done(done_fut):
            try:
                message = done_fut.result()
            except Exception as exc:
                self._log(f"{error_prefix}: {exc}")
                return
            if message:
                self._log(message)

        fut.add_done_callback(_done)

    def _heartbeat_worker_python_executable(self) -> str:
        exe = Path(sys.executable)
        if exe.name.lower() == "pythonw.exe":
            candidate = exe.with_name("python.exe")
            if candidate.exists():
                exe = candidate
        return str(exe)

    def _heartbeat_worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        dsn = str(self.memory_dsn or "").strip()
        if dsn:
            try:
                parsed = urlparse(dsn)
                if parsed.scheme.startswith("postgres"):
                    if parsed.hostname:
                        env["POSTGRES_HOST"] = parsed.hostname
                    if parsed.port:
                        env["POSTGRES_PORT"] = str(parsed.port)
                    db_name = parsed.path.lstrip("/")
                    if db_name:
                        env["POSTGRES_DB"] = unquote(db_name)
                    if parsed.username:
                        env["POSTGRES_USER"] = unquote(parsed.username)
                    if parsed.password:
                        env["POSTGRES_PASSWORD"] = unquote(parsed.password)
            except Exception:
                pass
        env["LLM_PROVIDER"] = "ollama"
        env["LLM_MODEL"] = str(self.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        env["OPENAI_BASE_URL"] = _ollama_openai_base_url_from_chat_url(self.ollama_url)
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _local_heartbeat_worker_should_run(self) -> bool:
        return bool(
            self.running
            and self.memory_enabled
            and self.heartbeat_enabled
            and self.memory is not None
            and LOCAL_HEARTBEAT_WORKER_PATH.is_file()
        )

    def _close_local_heartbeat_worker_log(self) -> None:
        handle = self._local_heartbeat_worker_log_handle
        self._local_heartbeat_worker_log_handle = None
        if handle:
            try:
                handle.close()
            except Exception:
                pass

    def _start_local_heartbeat_worker(self) -> None:
        if not self._local_heartbeat_worker_should_run():
            return
        proc = self._local_heartbeat_worker_proc
        if proc and proc.poll() is None:
            return
        if time.monotonic() < self._local_heartbeat_worker_retry_after:
            return
        self._close_local_heartbeat_worker_log()
        try:
            LOCAL_HEARTBEAT_WORKER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._local_heartbeat_worker_log_handle = open(LOCAL_HEARTBEAT_WORKER_LOG_PATH, "a", encoding="utf-8")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._local_heartbeat_worker_proc = subprocess.Popen(
                [self._heartbeat_worker_python_executable(), str(LOCAL_HEARTBEAT_WORKER_PATH), "--mode", "heartbeat"],
                cwd=str(_AGI_MEMORY_DIR),
                env=self._heartbeat_worker_env(),
                stdout=self._local_heartbeat_worker_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            self._local_heartbeat_worker_retry_after = 0.0
            self._log(
                f"Local heartbeat worker started (pid {self._local_heartbeat_worker_proc.pid}). "
                f"Logs: {LOCAL_HEARTBEAT_WORKER_LOG_PATH.name}"
            )
        except Exception as exc:
            self._local_heartbeat_worker_proc = None
            self._local_heartbeat_worker_retry_after = time.monotonic() + 30.0
            self._close_local_heartbeat_worker_log()
            self._log(f"Failed to start local heartbeat worker: {exc}")

    def _stop_local_heartbeat_worker(self, *, quiet: bool = False) -> None:
        proc = self._local_heartbeat_worker_proc
        self._local_heartbeat_worker_proc = None
        self._local_heartbeat_worker_retry_after = 0.0
        if proc:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                        proc.wait(timeout=5)
                    if not quiet:
                        self._log("Local heartbeat worker stopped.")
            except Exception as exc:
                if not quiet:
                    self._log(f"Failed to stop local heartbeat worker cleanly: {exc}")
        self._close_local_heartbeat_worker_log()

    def _reconcile_local_heartbeat_worker(self) -> None:
        proc = self._local_heartbeat_worker_proc
        if proc and proc.poll() is not None:
            exit_code = proc.returncode
            self._local_heartbeat_worker_proc = None
            self._local_heartbeat_worker_retry_after = time.monotonic() + 30.0
            self._close_local_heartbeat_worker_log()
            self._log(
                f"Local heartbeat worker exited with code {exit_code}. "
                f"Will retry in 30 seconds."
            )
        if self._local_heartbeat_worker_should_run():
            self._start_local_heartbeat_worker()
        else:
            self._stop_local_heartbeat_worker(quiet=True)

    async def _sync_heartbeat_llm_config_from_gui_if_needed(self) -> str | None:
        memory = self.memory
        if not memory or not AGI_MEMORY_AVAILABLE:
            return None
        try:
            current = await memory.get_config_value("llm.heartbeat")
        except Exception as exc:
            return f"Could not read AGI-memory heartbeat model config: {exc}"

        if not isinstance(current, dict):
            current = {}
        provider = str(current.get("provider") or "").strip().lower()
        api_key_env = str(current.get("api_key_env") or "").strip()
        has_configured_key = bool(api_key_env and os.getenv(api_key_env))
        has_default_key = bool(os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"))
        needs_gui_fallback = (
            not current
            or (provider in {"openai", "openai_compatible", "anthropic"} and not has_configured_key and not has_default_key)
        )
        if not needs_gui_fallback:
            return None
        desired = {
            "provider": "ollama",
            "model": str(self.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            "endpoint": _ollama_openai_base_url_from_chat_url(self.ollama_url),
            "api_key_env": "",
        }
        try:
            await memory.set_config_value("llm.heartbeat", desired)
        except Exception as exc:
            return (
                "AGI-memory heartbeat model config was using a provider with no usable API key, "
                f"but sync to GUI Ollama settings failed: {exc}"
            )
        return (
            "AGI-memory heartbeat model config was updated to the GUI Ollama settings: "
            f"{desired['model']} @ {desired['endpoint']}."
        )

    def _attach_voice_loggers(self):
        if self._voice_log_handler is not None:
            return
        runner = self

        class _GuiVoiceLogHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    msg = self.format(record)
                except Exception:
                    msg = record.getMessage()
                runner._log(msg)

        handler = _GuiVoiceLogHandler(level=logging.INFO)
        handler.setFormatter(logging.Formatter("[voice] %(name)s: %(message)s"))
        for name in VOICE_LOGGER_NAMES:
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
        self._voice_log_handler = handler

    def _detach_voice_loggers(self):
        handler = self._voice_log_handler
        if handler is None:
            return
        for name in VOICE_LOGGER_NAMES:
            logger = logging.getLogger(name)
            try:
                logger.removeHandler(handler)
            except Exception:
                pass
        self._voice_log_handler = None

    def _trim_jitd_text(self, text: str) -> str:
        clean = re.sub(r"\s+", " ", (text or "")).strip()
        if not clean:
            clean = "I do not have a response right now."
        if len(clean) > self.qwen_max_chars:
            clean = clean[: self.qwen_max_chars].rstrip() + "..."
        return clean

    def _proc_last_line(self, proc: subprocess.CompletedProcess) -> str:
        blob = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        if not blob:
            return ""
        return blob.splitlines()[-1].strip()

    def _silent_proc_kwargs(self) -> dict:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                kwargs["startupinfo"] = si
            except Exception:
                pass
        return kwargs

    def _resolve_ffmpeg_exe(self) -> str | None:
        raw = (self.ffmpeg_path or "").strip() or "ffmpeg"
        if os.path.isabs(raw):
            return raw if Path(raw).exists() else None
        return shutil.which(raw)

    def _resolve_ytdlp_cmd(self) -> list[str]:
        raw = (self.ytdlp_path or "").strip()
        if raw:
            if os.path.isabs(raw):
                if Path(raw).exists():
                    return [raw]
            else:
                found = shutil.which(raw)
                if found:
                    return [found]

        found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        if found:
            return [found]
        return [sys.executable, "-m", "yt_dlp"]

    def _extract_youtube_audio_sync(self, query: str) -> dict:
        clean_query = (query or "").strip()
        if not clean_query:
            raise RuntimeError("Enter a YouTube URL or search query.")

        parsed = urlparse(clean_query)
        target = clean_query if (parsed.scheme and parsed.netloc) else f"ytsearch1:{clean_query}"
        cmd = self._resolve_ytdlp_cmd() + [
            "--dump-single-json",
            "--no-warnings",
            "--no-playlist",
            "-f",
            "bestaudio/best",
            target,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                **self._silent_proc_kwargs(),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("yt-dlp timed out while resolving the track.")
        except Exception as e:
            raise RuntimeError(f"Failed to run yt-dlp: {e}") from e

        if proc.returncode != 0:
            last = self._proc_last_line(proc)
            extra = f" {last}" if last else ""
            raise RuntimeError(
                "yt-dlp could not resolve the YouTube audio."
                f"{extra} Install yt-dlp or set YT_DLP_PATH in .env."
            )

        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError("yt-dlp returned invalid JSON output.") from e

        if isinstance(info, dict) and info.get("entries"):
            entries = [entry for entry in (info.get("entries") or []) if entry]
            if not entries:
                raise RuntimeError("yt-dlp did not find a playable result.")
            info = entries[0]

        if not isinstance(info, dict):
            raise RuntimeError("yt-dlp returned an unexpected result.")

        stream_url = info.get("url")
        title = info.get("title") or info.get("fulltitle") or clean_query
        page_url = info.get("webpage_url") or info.get("original_url") or clean_query
        duration = info.get("duration")
        uploader = info.get("uploader") or info.get("channel") or info.get("playlist_uploader") or ""
        if not stream_url:
            raise RuntimeError("yt-dlp did not return a direct audio stream URL.")

        return {
            "title": str(title),
            "stream_url": str(stream_url),
            "page_url": str(page_url),
            "duration": int(duration) if isinstance(duration, (int, float)) else duration,
            "uploader": str(uploader).strip(),
        }

    def _extract_youtube_playlist_sync(self, query: str) -> dict:
        clean_query = (query or "").strip()
        if not clean_query:
            raise RuntimeError("Enter a YouTube playlist URL.")
        parsed = urlparse(clean_query)
        if not (parsed.scheme and parsed.netloc):
            raise RuntimeError("Playlist import requires a full YouTube playlist URL.")

        cmd = self._resolve_ytdlp_cmd() + [
            "--dump-single-json",
            "--no-warnings",
            "--flat-playlist",
            "--playlist-end",
            str(DEFAULT_VOICE_PLAYLIST_LIMIT),
            clean_query,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                **self._silent_proc_kwargs(),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("yt-dlp timed out while importing the playlist.")
        except Exception as e:
            raise RuntimeError(f"Failed to run yt-dlp: {e}") from e

        if proc.returncode != 0:
            last = self._proc_last_line(proc)
            extra = f" {last}" if last else ""
            raise RuntimeError(f"yt-dlp could not import the playlist.{extra}")

        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError("yt-dlp returned invalid playlist JSON output.") from e

        entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
        if not entries:
            raise RuntimeError("yt-dlp did not find any playlist entries.")

        host = parsed.netloc.lower()
        tracks: list[dict] = []
        for entry in entries:
            page_url = str(entry.get("webpage_url") or entry.get("original_url") or "").strip()
            raw_url = str(entry.get("url") or "").strip()
            if not page_url:
                if raw_url and urlparse(raw_url).scheme:
                    page_url = raw_url
                else:
                    entry_id = str(entry.get("id") or raw_url).strip()
                    if entry_id and ("youtube.com" in host or "youtu.be" in host):
                        page_url = f"https://www.youtube.com/watch?v={entry_id}"
            if not page_url:
                continue
            duration = entry.get("duration")
            tracks.append(
                {
                    "title": str(entry.get("title") or entry.get("fulltitle") or page_url),
                    "page_url": page_url,
                    "duration": int(duration) if isinstance(duration, (int, float)) else duration,
                    "uploader": str(entry.get("uploader") or entry.get("channel") or "").strip(),
                }
            )

        if not tracks:
            raise RuntimeError("yt-dlp returned playlist entries without playable URLs.")

        total = info.get("playlist_count")
        try:
            total_count = int(total)
        except Exception:
            total_count = len(tracks)
        return {
            "title": str(info.get("title") or "Playlist"),
            "entries": tracks,
            "truncated": total_count > len(tracks),
            "total_count": total_count,
        }

    def _track_from_info(self, info: dict, *, requested_by: str | None = None) -> dict:
        duration = info.get("duration")
        return {
            "title": str(info.get("title") or "Untitled track"),
            "page_url": str(info.get("page_url") or info.get("webpage_url") or ""),
            "duration": int(duration) if isinstance(duration, (int, float)) else duration,
            "uploader": str(info.get("uploader") or "").strip(),
            "requested_by": str(requested_by or "").strip(),
        }

    async def _get_voice_channel(self, guild_id: int, channel_id: int) -> discord.VoiceChannel:
        bot = self.bot
        if not bot:
            raise RuntimeError("Bot is not running.")
        guild = bot.get_guild(int(guild_id))
        if not guild:
            raise RuntimeError("Server not found.")
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            raise RuntimeError("Selected channel is not a voice channel.")
        return channel

    async def _log_voice_snapshot(self, guild_id: int, delay_sec: float):
        await asyncio.sleep(delay_sec)
        bot = self.bot
        if not bot:
            return
        guild = bot.get_guild(int(guild_id))
        if not guild:
            self._log(f"Voice snapshot after {delay_sec:.1f}s: guild missing.")
            return
        voice_client = guild.voice_client
        if not voice_client:
            self._log(f"Voice snapshot after {delay_sec:.1f}s: no active voice client.")
            return
        conn = getattr(voice_client, "_connection", None)
        state = getattr(conn, "state", None)
        state_name = getattr(state, "name", str(state)) if state is not None else "unknown"
        channel_name = voice_client.channel.name if voice_client.channel else "(none)"
        endpoint = getattr(conn, "endpoint", None) if conn is not None else None
        try:
            latency = f"{voice_client.latency:.3f}s"
        except Exception:
            latency = "n/a"
        self._log(
            "Voice snapshot after "
            f"{delay_sec:.1f}s: connected={voice_client.is_connected()} "
            f"channel={channel_name} state={state_name} latency={latency} endpoint={endpoint or 'n/a'}"
        )

    async def _ensure_voice_client(self, guild_id: int, channel_id: int) -> tuple[discord.VoiceChannel, discord.VoiceClient]:
        self._assert_voice_guild_access(guild_id)
        channel = await self._get_voice_channel(guild_id, channel_id)
        voice_client = channel.guild.voice_client
        if voice_client and voice_client.channel and voice_client.channel.id != channel.id:
            if voice_client.is_playing() or voice_client.is_paused():
                self.voice_finish_actions[channel.guild.id] = "move"
                voice_client.stop()
            await voice_client.move_to(channel)
        elif not voice_client:
            try:
                voice_client = await channel.connect(timeout=30.0, reconnect=True, self_deaf=True)
            except discord.ClientException:
                voice_client = channel.guild.voice_client
        if not voice_client:
            raise RuntimeError("Could not connect to the selected voice channel.")
        return channel, voice_client

    async def _voice_join(self, guild_id: int, channel_id: int) -> str:
        channel, _voice_client = await self._ensure_voice_client(guild_id, channel_id)
        self._set_voice_state(f"Connected to {channel.guild.name} / {channel.name}")
        self._push_voice_queue_state()
        asyncio.create_task(self._log_voice_snapshot(channel.guild.id, 1.0))
        asyncio.create_task(self._log_voice_snapshot(channel.guild.id, 3.0))
        return f"Connected to voice channel {channel.name}."

    async def _start_track(self, guild_id: int, channel_id: int, track: dict, *, stream_info: dict | None = None) -> str:
        ffmpeg_exe = self._resolve_ffmpeg_exe()
        if not ffmpeg_exe:
            raise RuntimeError(
                "Voice playback requires ffmpeg. Install ffmpeg and add it to PATH,"
                " or set FFMPEG_PATH in .env."
            )

        channel, voice_client = await self._ensure_voice_client(guild_id, channel_id)
        info = dict(stream_info or {})
        if not info:
            self._set_voice_state(f"Resolving track for {channel.guild.name} / {channel.name}...")
            source_query = str(track.get("page_url") or track.get("title") or "").strip()
            info = await asyncio.to_thread(self._extract_youtube_audio_sync, source_query)
        if not info.get("stream_url"):
            raise RuntimeError("yt-dlp did not return a playable audio stream.")

        resolved_track = self._track_from_info(
            {
                **track,
                **info,
                "page_url": info.get("page_url") or track.get("page_url") or "",
            },
            requested_by=str(track.get("requested_by") or "").strip() or None,
        )

        if voice_client.is_playing() or voice_client.is_paused():
            self.voice_finish_actions[channel.guild.id] = "replace"
            voice_client.stop()

        before_options = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        source = discord.FFmpegOpusAudio(
            str(info["stream_url"]),
            executable=ffmpeg_exe,
            before_options=before_options,
            options="-vn",
        )
        self._voice_playback_serial += 1
        playback_id = self._voice_playback_serial
        self.voice_current_track[channel.guild.id] = resolved_track
        self.voice_now_playing[channel.guild.id] = resolved_track["title"]
        self.voice_playback_ids[channel.guild.id] = playback_id
        self.voice_paused[channel.guild.id] = False

        def _after_playback(error):
            loop = self.loop
            if not loop or not self.running:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self._on_voice_playback_finished(channel.guild.id, playback_id, error),
                    loop,
                )
            except Exception:
                pass

        try:
            voice_client.play(source, after=_after_playback)
        except Exception:
            source.cleanup()
            self._clear_voice_runtime(channel.guild.id, clear_queue=False)
            self._push_voice_queue_state()
            raise

        self._set_voice_state(
            f"Playing in {channel.guild.name} / {channel.name}: {resolved_track['title']}"
        )
        self._push_voice_queue_state()
        return f"Now playing: {resolved_track['title']}"

    async def _on_voice_playback_finished(self, guild_id: int, playback_id: int, error) -> None:
        guild_id = int(guild_id)
        if self.voice_playback_ids.get(guild_id) != playback_id:
            return
        action = self.voice_finish_actions.pop(guild_id, None)
        self.voice_current_track.pop(guild_id, None)
        self.voice_now_playing.pop(guild_id, None)
        self.voice_playback_ids.pop(guild_id, None)
        self.voice_paused.pop(guild_id, None)

        if error:
            self._log(f"Voice playback error: {error}")

        if action == "skip" or (action is None and self.voice_queues.get(guild_id)):
            started = await self._start_next_queued_track(guild_id)
            if started:
                return

        bot = self.bot
        guild = bot.get_guild(guild_id) if bot else None
        voice_client = guild.voice_client if guild else None
        if voice_client and voice_client.channel:
            self._set_voice_state(f"Connected to {guild.name} / {voice_client.channel.name}")
        else:
            self._set_voice_state("Idle")
        self._push_voice_queue_state()

    async def _start_next_queued_track(self, guild_id: int) -> str | None:
        guild_id = int(guild_id)
        bot = self.bot
        if not bot:
            return None
        guild = bot.get_guild(guild_id)
        if not guild or not guild.voice_client or not guild.voice_client.channel:
            self._clear_voice_runtime(guild_id, clear_queue=True)
            self._set_voice_state("Idle")
            self._push_voice_queue_state()
            return None

        queue = self.voice_queues.get(guild_id)
        last_error: str | None = None
        while queue:
            track = queue.popleft()
            if not queue:
                self.voice_queues.pop(guild_id, None)
            try:
                return await self._start_track(guild_id, guild.voice_client.channel.id, track)
            except Exception as exc:
                last_error = str(exc)
                self._log(f"Skipping queued track in {guild.name}: {exc}")
                queue = self.voice_queues.get(guild_id)

        if last_error:
            self._log(f"Queue exhausted after playback failures in {guild.name}.")
        self._push_voice_queue_state()
        return None

    async def _voice_queue(self, guild_id: int, channel_id: int, query: str, *, requested_by: str | None = None) -> str:
        channel, voice_client = await self._ensure_voice_client(guild_id, channel_id)
        self._set_voice_state(f"Resolving track for {channel.guild.name} / {channel.name}...")
        info = await asyncio.to_thread(self._extract_youtube_audio_sync, query)
        track = self._track_from_info(info, requested_by=requested_by)

        if (
            not voice_client.is_playing()
            and not voice_client.is_paused()
            and not self.voice_current_track.get(channel.guild.id)
        ):
            return await self._start_track(channel.guild.id, channel.id, track, stream_info=info)

        self.voice_queues[channel.guild.id].append(track)
        position = len(self.voice_queues[channel.guild.id])
        self._push_voice_queue_state()
        return f"Queued #{position}: {track['title']}"

    async def _voice_import_playlist(
        self,
        guild_id: int,
        channel_id: int,
        query: str,
        *,
        requested_by: str | None = None,
    ) -> str:
        channel, voice_client = await self._ensure_voice_client(guild_id, channel_id)
        self._set_voice_state(f"Importing playlist for {channel.guild.name} / {channel.name}...")
        payload = await asyncio.to_thread(self._extract_youtube_playlist_sync, query)
        entries = [
            {**entry, "requested_by": str(requested_by or "").strip()}
            for entry in payload["entries"]
        ]
        if not entries:
            raise RuntimeError("Playlist import did not return any playable tracks.")

        started_now = False
        first_title = entries[0]["title"]
        if (
            not voice_client.is_playing()
            and not voice_client.is_paused()
            and not self.voice_current_track.get(channel.guild.id)
        ):
            first = entries.pop(0)
            await self._start_track(channel.guild.id, channel.id, first)
            started_now = True
        if entries:
            self.voice_queues[channel.guild.id].extend(entries)
        self._push_voice_queue_state()

        imported_count = len(payload["entries"])
        suffix = " (truncated to limit)" if payload.get("truncated") else ""
        if started_now:
            return (
                f"Imported {imported_count} tracks from {payload['title']}{suffix}. "
                f"Started with {first_title}."
            )
        return f"Imported {imported_count} tracks from {payload['title']}{suffix}."

    async def _voice_pause(self, guild_id: int) -> str:
        self._assert_voice_guild_access(guild_id)
        bot = self.bot
        if not bot:
            raise RuntimeError("Bot is not running.")
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            raise RuntimeError("Bot is not connected to a voice channel in that server.")
        voice_client = guild.voice_client
        if voice_client.is_paused():
            return "Playback is already paused."
        if not voice_client.is_playing():
            raise RuntimeError("Nothing is currently playing.")
        voice_client.pause()
        self.voice_paused[guild.id] = True
        title = self.voice_now_playing.get(guild.id) or "current track"
        channel_name = voice_client.channel.name if voice_client.channel else "voice"
        self._set_voice_state(f"Paused in {guild.name} / {channel_name}: {title}")
        self._push_voice_queue_state()
        return f"Paused: {title}"

    async def _voice_resume(self, guild_id: int) -> str:
        self._assert_voice_guild_access(guild_id)
        bot = self.bot
        if not bot:
            raise RuntimeError("Bot is not running.")
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            raise RuntimeError("Bot is not connected to a voice channel in that server.")
        voice_client = guild.voice_client
        if not voice_client.is_paused():
            raise RuntimeError("Playback is not paused.")
        voice_client.resume()
        self.voice_paused[guild.id] = False
        title = self.voice_now_playing.get(guild.id) or "current track"
        channel_name = voice_client.channel.name if voice_client.channel else "voice"
        self._set_voice_state(f"Playing in {guild.name} / {channel_name}: {title}")
        self._push_voice_queue_state()
        return f"Resumed: {title}"

    async def _voice_skip(self, guild_id: int) -> str:
        self._assert_voice_guild_access(guild_id)
        bot = self.bot
        if not bot:
            raise RuntimeError("Bot is not running.")
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            raise RuntimeError("Bot is not connected to a voice channel in that server.")
        voice_client = guild.voice_client
        if voice_client.is_playing() or voice_client.is_paused():
            self.voice_finish_actions[guild.id] = "skip"
            title = self.voice_now_playing.get(guild.id) or "current track"
            voice_client.stop()
            return f"Skipped: {title}"
        if self.voice_queues.get(guild.id):
            started = await self._start_next_queued_track(guild.id)
            if started:
                return started
        raise RuntimeError("Nothing is currently playing or queued.")

    async def _voice_clear_queue(self, guild_id: int) -> str:
        self._assert_voice_guild_access(guild_id)
        queue = self.voice_queues.get(int(guild_id))
        count = len(queue or [])
        if not count:
            return "Queue is already empty."
        self.voice_queues.pop(int(guild_id), None)
        self._push_voice_queue_state()
        return f"Cleared {count} queued track(s)."

    async def _voice_stop(self, guild_id: int) -> str:
        bot = self.bot
        if not bot:
            raise RuntimeError("Bot is not running.")
        self._assert_voice_guild_access(guild_id)
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            raise RuntimeError("Bot is not connected to a voice channel in that server.")
        voice_client = guild.voice_client
        had_current = bool(self.voice_current_track.get(guild.id)) or voice_client.is_playing() or voice_client.is_paused()
        queued = len(self.voice_queues.get(guild.id) or [])
        self._clear_voice_runtime(guild.id, clear_queue=True)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        channel_name = voice_client.channel.name if voice_client.channel else "voice"
        self._set_voice_state(f"Connected to {guild.name} / {channel_name}")
        self._push_voice_queue_state()
        if not had_current and not queued:
            return "Nothing was playing."
        if queued:
            return f"Stopped playback and cleared {queued} queued track(s)."
        return "Stopped playback."

    async def _voice_leave(self, guild_id: int) -> str:
        bot = self.bot
        if not bot:
            raise RuntimeError("Bot is not running.")
        self._assert_voice_guild_access(guild_id)
        guild = bot.get_guild(int(guild_id))
        if not guild or not guild.voice_client:
            raise RuntimeError("Bot is not connected to a voice channel in that server.")
        voice_client = guild.voice_client
        self._clear_voice_runtime(guild.id, clear_queue=True)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        await voice_client.disconnect(force=True)
        self._set_voice_state("Idle")
        self._push_voice_queue_state()
        return "Left the voice channel."

    async def _voice_play(self, guild_id: int, channel_id: int, query: str, *, requested_by: str | None = None) -> str:
        self._set_voice_state("Resolving track...")
        info = await asyncio.to_thread(self._extract_youtube_audio_sync, query)
        track = self._track_from_info(info, requested_by=requested_by)
        return await self._start_track(guild_id, channel_id, track, stream_info=info)

    def _convert_wav_to_mp3(self, wav_path: Path, mp3_path: Path) -> str | None:
        ffmpeg_exe = self._resolve_ffmpeg_exe()
        if not ffmpeg_exe:
            return (
                "MP3 conversion requires ffmpeg. Install ffmpeg and add it to PATH, "
                "or set FFMPEG_PATH in .env."
            )
        cmd = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(wav_path),
            "-vn",
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(mp3_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                **self._silent_proc_kwargs(),
            )
        except Exception as e:
            return f"Failed to run ffmpeg: {e}"
        if proc.returncode != 0 or not mp3_path.exists():
            last = self._proc_last_line(proc)
            extra = f" {last}" if last else ""
            return f"ffmpeg conversion failed.{extra}"
        return None

    def _synthesize_jitd_mp3_sync(self, text: str) -> tuple[Path | None, str | None, str]:
        spoken_text = self._trim_jitd_text(text)
        if not self.qwen_project_dir.exists():
            return None, f"Qwen project folder not found: {self.qwen_project_dir}", spoken_text
        if not self.qwen_python.exists():
            return None, f"Qwen venv Python not found: {self.qwen_python}", spoken_text
        if not self.qwen_script.exists():
            return None, f"Qwen script not found: {self.qwen_script}", spoken_text

        self.jitd_output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.time_ns()
        wav_path = self.jitd_output_dir / f"jitd_{stamp}.wav"
        mp3_path = self.jitd_output_dir / f"jitd_{stamp}.mp3"

        mode = self.qwen_mode if self.qwen_mode in {"clone", "custom"} else "clone"
        cmd = [
            str(self.qwen_python),
            str(self.qwen_script),
            "--mode",
            mode,
            "--language",
            self.qwen_language,
            "--output",
            str(wav_path),
        ]
        if mode == "custom":
            cmd.extend(["--speaker", self.qwen_speaker])
        else:
            cmd.extend(["--ref-index", str(self.qwen_ref_index)])
        cmd.append(spoken_text)

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.qwen_project_dir),
                capture_output=True,
                text=True,
                timeout=self.qwen_timeout_sec,
                **self._silent_proc_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return None, f"Qwen synthesis timed out after {self.qwen_timeout_sec} seconds.", spoken_text
        except Exception as e:
            return None, f"Failed to run qwen_voice.py: {e}", spoken_text

        if proc.returncode != 0:
            last = self._proc_last_line(proc)
            extra = f" {last}" if last else ""
            return None, f"qwen_voice.py failed with exit code {proc.returncode}.{extra}", spoken_text
        if not wav_path.exists():
            return None, f"Qwen did not create expected WAV file: {wav_path}", spoken_text

        conv_error = self._convert_wav_to_mp3(wav_path, mp3_path)
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
        if conv_error:
            return None, conv_error, spoken_text
        return mp3_path, None, spoken_text

    async def synthesize_jitd_mp3(self, text: str) -> tuple[Path | None, str | None, str]:
        return await asyncio.to_thread(self._synthesize_jitd_mp3_sync, text)


# --------------- GUI App ---------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Discord Ollama Controller")
        self.geometry("1000x820")
        self.minsize(920, 740)

        callbacks = {
            "log": self.log,
            "status": self.set_status,
            "set_voice_state": self.set_voice_state_from_bot,
            "set_voice_queue": self.set_voice_queue_from_bot,
            "set_low": self.set_low_from_bot,
            "persist": self.persist_settings_from_bot,
            "guild_snapshot": self.on_guild_snapshot,
        }
        self.runner = BotRunner(callbacks)

        # UI state
        self.var_token = tk.StringVar(value=DEFAULT_TOKEN)
        self.var_url = tk.StringVar(value=DEFAULT_OLLAMA_URL)
        self.var_model = tk.StringVar(value=DEFAULT_MODEL)
        self.var_sys = tk.StringVar(value=DEFAULT_SYS_PROMPT)
        self.var_low = tk.StringVar(value="")
        self.var_max = tk.IntVar(value=DEFAULT_MAX_TURNS)
        self.var_jitd_enabled = tk.BooleanVar(value=False)
        self.var_jitd_mode = tk.StringVar(value=self.runner.qwen_mode)
        self.var_jitd_speaker = tk.StringVar(value=self.runner.qwen_speaker)
        self.var_jitd_ref_index = tk.IntVar(value=self.runner.qwen_ref_index)
        self.status_var = tk.StringVar(value="Stopped")
        self.var_voice_status = tk.StringVar(value="Idle")
        self.var_voice_now_playing = tk.StringVar(value="Nothing playing.")
        self.var_voice_queue_summary = tk.StringVar(value="No active voice server.")
        self._jitd_ref_label_to_idx: Dict[str, int] = {}

        # Greeting UI state
        self._guild_snapshot: List[dict] = []
        self._guild_id_by_name: Dict[str, int] = {}
        self._channels_by_guild: Dict[int, List[dict]] = {}
        self._voice_channels_by_guild: Dict[int, List[dict]] = {}
        self._members_by_guild: Dict[int, List[dict]] = {}
        self._member_id_name: Dict[int, str] = {}

        self.var_greet_guild = tk.StringVar(value="")
        self.var_greet_channel = tk.StringVar(value="")
        self.var_greet_template = tk.StringVar(value=DEFAULT_GREET_MESSAGE)
        self.var_greet_cooldown = tk.IntVar(value=DEFAULT_GREET_COOLDOWN_SEC)
        self.var_greet_enabled = tk.BooleanVar(value=True)
        self.var_greet_use_llm = tk.BooleanVar(value=True)
        self.var_voice_guild = tk.StringVar(value="")
        self.var_voice_channel = tk.StringVar(value="")
        self.var_voice_query = tk.StringVar(value="")

        # Memory UI state
        self.var_mem_enabled = tk.BooleanVar(value=True)
        self.var_mem_formation = tk.BooleanVar(value=True)
        self.var_mem_personhood = tk.BooleanVar(value=False)
        self.var_mem_dsn = tk.StringVar(value=DEFAULT_MEMORY_DSN)
        self.var_mem_hydrate_limit = tk.IntVar(value=5)

        # Heartbeat UI state
        self.var_hb_enabled = tk.BooleanVar(value=DEFAULT_HEARTBEAT_ENABLED)
        self.var_hb_shadow_mode = tk.BooleanVar(value=DEFAULT_HEARTBEAT_SHADOW_MODE)
        self.var_hb_manual_approval = tk.BooleanVar(value=DEFAULT_HEARTBEAT_MANUAL_APPROVAL)
        self.var_hb_home_guild = tk.StringVar(value="")
        self.var_hb_server_enabled = tk.BooleanVar(value=False)
        self.var_hb_server_channel = tk.StringVar(value="")
        self.var_hb_server_typing = tk.BooleanVar(value=DEFAULT_HEARTBEAT_SERVER_TYPING)
        self.var_hb_quiet_start = tk.StringVar(value=DEFAULT_HEARTBEAT_QUIET_HOURS_START)
        self.var_hb_quiet_end = tk.StringVar(value=DEFAULT_HEARTBEAT_QUIET_HOURS_END)
        self.var_hb_max_posts_per_hour = tk.IntVar(value=DEFAULT_HEARTBEAT_MAX_POSTS_PER_HOUR)
        self.var_hb_max_posts_per_day = tk.IntVar(value=DEFAULT_HEARTBEAT_MAX_POSTS_PER_DAY)
        self.var_hb_min_recent_human_messages = tk.IntVar(value=DEFAULT_HEARTBEAT_MIN_RECENT_HUMAN_MESSAGES)
        self.var_hb_dead_channel_threshold_minutes = tk.IntVar(value=DEFAULT_HEARTBEAT_DEAD_CHANNEL_THRESHOLD_MINUTES)
        self.var_hb_require_reply_context = tk.BooleanVar(value=DEFAULT_HEARTBEAT_REQUIRE_REPLY_CONTEXT)
        self.var_hb_db_summary = tk.StringVar(value="Heartbeat status not loaded.")
        self.var_hb_db_tasks = tk.StringVar(value="No worker task data yet.")
        self.var_hb_candidate_summary = tk.StringVar(value="No outbox proposal selected.")
        self.var_hb_override_channel_id = tk.StringVar(value="")
        self.var_hb_override_reply_to = tk.StringVar(value="")
        self._heartbeat_outbox_items: List[dict] = []
        self._heartbeat_activity_items: List[dict] = []

        # autosave debounce + file-watch
        self._autosave_job = None
        self._settings_last_mtime = 0.0
        self._write_guard_until = 0.0

        # tray state
        self._tray_icon = None
        self._tray_thread = None

        # Build UI
        self._build_ui_tabs()
        self._load_settings_from_disk(silent=True)

        # Prime mtime (after initial load)
        try:
            self._settings_last_mtime = SETTINGS_PATH.stat().st_mtime
        except Exception:
            self._settings_last_mtime = 0.0

        # --- Autosave wiring (after loading) ---
        self._setup_autosave()

        # Start file watcher (polling)
        self.after(1200, self._watch_settings_file)

        # Close button hides to tray instead of exiting
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        # Make sure tray is ready early so you can always restore the GUI
        self._ensure_tray()

    # -------- Tray helpers --------
    def _tray_image(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((10, 10, 54, 54), fill=(90, 140, 255, 255))
        d.ellipse((24, 24, 40, 40), fill=(255, 255, 255, 255))
        return img

    def _ensure_tray(self):
        if not TRAY_AVAILABLE:
            self.log("Tray not available. Install: pip install pystray pillow")
            return
        if self._tray_icon is not None:
            return

        def on_show(icon, _):
            self.after(0, self.show_from_tray)

        def on_hide(icon, _):
            self.after(0, self.hide_to_tray)

        def on_start(icon, _):
            self.after(0, self.start_bot)

        def on_stop(icon, _):
            self.after(0, self.stop_bot)

        def on_quit(icon, _):
            self.after(0, self.quit_from_tray)

        menu = pystray.Menu(
            item("Show", on_show, default=True),
            item("Hide", on_hide),
            pystray.Menu.SEPARATOR,
            item("Start Bot", on_start),
            item("Stop Bot", on_stop),
            pystray.Menu.SEPARATOR,
            item("Quit", on_quit),
        )

        self._tray_icon = pystray.Icon("discordllm", self._tray_image(), "DiscordLLM", menu)

        def tray_run():
            try:
                self._tray_icon.run()
            except Exception:
                pass

        self._tray_thread = threading.Thread(target=tray_run, daemon=True)
        self._tray_thread.start()

    def hide_to_tray(self):
        if TRAY_AVAILABLE and self._tray_icon is not None:
            self.withdraw()
            return
        # Fallback if tray is unavailable
        self.iconify()

    def show_from_tray(self):
        self.deiconify()
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

    def quit_from_tray(self):
        try:
            self.runner.stop()
        except Exception:
            pass
        try:
            if self._tray_icon is not None:
                self._tray_icon.stop()
        except Exception:
            pass
        self.destroy()

    # -------- Tabs ---------
    def _build_ui_tabs(self):
        pad = dict(padx=6, pady=6)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # Tabs
        self.tab_general = ttk.Frame(nb)
        self.tab_commands = ttk.Frame(nb)
        self.tab_greet = ttk.Frame(nb)
        self.tab_voice = ttk.Frame(nb)
        self.tab_heartbeat = ttk.Frame(nb)
        self.tab_memory = ttk.Frame(nb)
        nb.add(self.tab_general, text="General")
        nb.add(self.tab_commands, text="Commands")
        nb.add(self.tab_greet, text="Greetings")
        nb.add(self.tab_voice, text="Music")
        nb.add(self.tab_heartbeat, text="Heartbeat")
        nb.add(self.tab_memory, text="Memory")

        # -------- General tab --------
        top = ttk.Frame(self.tab_general)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Token:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.var_token, show="*", width=44).grid(row=0, column=1, sticky="we")
        ttk.Button(top, text="Reload .env", command=self.reload_env).grid(row=0, column=2, sticky="e")
        ttk.Label(top, text="Ollama URL:").grid(row=0, column=3, sticky="e")
        ttk.Entry(top, textvariable=self.var_url, width=28).grid(row=0, column=4, sticky="we")

        ttk.Label(top, text="Model:").grid(row=1, column=0, sticky="w")
        self.model_box = ttk.Combobox(top, textvariable=self.var_model, width=30, state="readonly")
        self.model_box.grid(row=1, column=1, sticky="we")
        self.model_box.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Button(top, text="Refresh Models", command=self.refresh_models).grid(row=1, column=2, sticky="w")

        ttk.Label(top, text="Max turns:").grid(row=1, column=3, sticky="e")
        ttk.Spinbox(top, from_=2, to=200, textvariable=self.var_max, width=6).grid(row=1, column=4, sticky="w")

        ttk.Checkbutton(
            top,
            text="Enable !jitd voice replies (Qwen)",
            variable=self.var_jitd_enabled,
            command=self._debounced_autosave,
        ).grid(row=2, column=0, columnspan=3, sticky="w")

        btns = ttk.Frame(top)
        btns.grid(row=2, column=3, columnspan=2, sticky="e")
        ttk.Button(btns, text="Start Bot", command=self.start_bot).pack(side="left", padx=4)
        ttk.Button(btns, text="Stop Bot", command=self.stop_bot).pack(side="left")

        ttk.Label(top, text="!jitd mode:").grid(row=3, column=0, sticky="w")
        self.jitd_mode_box = ttk.Combobox(top, textvariable=self.var_jitd_mode, state="readonly", width=12)
        self.jitd_mode_box["values"] = ("clone", "custom")
        self.jitd_mode_box.grid(row=3, column=1, sticky="w")
        self.jitd_mode_box.bind("<<ComboboxSelected>>", lambda e: self._debounced_autosave())

        ttk.Label(top, text="!jitd speaker:").grid(row=3, column=2, sticky="e")
        self.jitd_speaker_box = ttk.Combobox(top, textvariable=self.var_jitd_speaker, width=18)
        self.jitd_speaker_box["values"] = DEFAULT_QWEN_CUSTOM_SPEAKERS
        self.jitd_speaker_box.grid(row=3, column=3, sticky="we")
        self.jitd_speaker_box.bind("<<ComboboxSelected>>", lambda e: self._debounced_autosave())

        ttk.Label(top, text="Clone ref:").grid(row=4, column=0, sticky="w")
        self.jitd_ref_box = ttk.Combobox(top, state="readonly", width=50)
        self.jitd_ref_box.grid(row=4, column=1, columnspan=3, sticky="we")
        self.jitd_ref_box.bind("<<ComboboxSelected>>", self._on_jitd_ref_selected)
        ttk.Button(top, text="Refresh Refs", command=self._refresh_jitd_ref_choices).grid(row=4, column=4, sticky="e")

        # Prompts
        sysf = ttk.LabelFrame(self.tab_general, text="System Prompt (Primary, GUI-only)")
        sysf.pack(fill="x", **pad)
        ttk.Entry(sysf, textvariable=self.var_sys).pack(fill="x", padx=6, pady=6)

        lowf = ttk.LabelFrame(self.tab_general, text="Community Prompt (Lower Priority, chat-editable via !low)")
        lowf.pack(fill="x", **pad)
        ttk.Entry(lowf, textvariable=self.var_low).pack(fill="x", padx=6, pady=6)

        # Logs
        logf = ttk.LabelFrame(self.tab_general, text="Logs")
        logf.pack(fill="both", expand=True, **pad)
        self.log_text = ScrolledText(logf, height=20, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

        for i in range(5):
            top.columnconfigure(i, weight=1)
        self._refresh_jitd_ref_choices()

        # -------- Commands tab --------
        cmdf = ttk.LabelFrame(self.tab_commands, text="Local Command Reference")
        cmdf.pack(fill="both", expand=True, **pad)
        ttk.Label(
            cmdf,
            text="This tab is only visible in your local GUI. It includes commands hidden from !jith.",
            wraplength=860,
            justify="left",
        ).pack(anchor="w", padx=6, pady=(6, 0))
        self.commands_text = ScrolledText(cmdf, height=28, wrap="word", state="normal")
        self.commands_text.pack(fill="both", expand=True, padx=6, pady=6)
        self.commands_text.insert("1.0", BALLERJIT_ALL_COMMANDS)
        self.commands_text.configure(state="disabled")

        # -------- Greetings tab --------
        gf = ttk.LabelFrame(self.tab_greet, text="Greeting Pings (OFFLINE → ONLINE)")
        gf.pack(fill="both", expand=True, **pad)

        row = ttk.Frame(gf)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Button(row, text="Load Servers/Members", command=self.load_guilds).pack(side="left")
        ttk.Label(row, text="Server:").pack(side="left", padx=(12,4))
        self.guild_box = ttk.Combobox(row, textvariable=self.var_greet_guild, state="readonly", width=28)
        self.guild_box.pack(side="left")
        self.guild_box.bind("<<ComboboxSelected>>", self.on_select_guild)
        ttk.Label(row, text="Channel:").pack(side="left", padx=(12,4))
        self.channel_box = ttk.Combobox(row, textvariable=self.var_greet_channel, state="readonly", width=28)
        self.channel_box.pack(side="left")

        row2 = ttk.Frame(gf)
        row2.pack(fill="both", expand=True, padx=6, pady=6)

        left = ttk.Frame(row2)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="All members:").pack(anchor="w")
        self.member_list = tk.Listbox(left, selectmode="extended", height=14, exportselection=False)
        self.member_list.pack(fill="both", expand=True)
        self.member_list.bind("<Double-1>", lambda e: self._add_selected())

        mid = ttk.Frame(row2)
        mid.pack(side="left", fill="y", padx=8)
        ttk.Button(mid, text="→ Add", command=self._add_selected, width=10).pack(pady=2)
        ttk.Button(mid, text="← Remove", command=self._remove_selected, width=10).pack(pady=2)
        ttk.Button(mid, text="All →", command=self._add_all, width=10).pack(pady=8)
        ttk.Button(mid, text="← Clear", command=self._clear_selected, width=10).pack(pady=2)

        right = ttk.Frame(row2)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="Selected users to greet:").pack(anchor="w")
        self.sel_list = tk.Listbox(right, selectmode="extended", height=14, exportselection=False)
        self.sel_list.pack(fill="both", expand=True)
        self.sel_list.bind("<Double-1>", lambda e: self._remove_selected())

        row3 = ttk.Frame(gf)
        row3.pack(fill="x", padx=6, pady=6)
        ttk.Checkbutton(row3, text="Enable greetings for selected users", variable=self.var_greet_enabled, command=self._debounced_autosave).pack(side="left")
        ttk.Checkbutton(row3, text="Use LLM for greeting text", variable=self.var_greet_use_llm, command=self._debounced_autosave).pack(side="left", padx=(16,0))

        right2 = ttk.Frame(gf)
        right2.pack(fill="x", padx=6, pady=(0,6))
        ttk.Label(right2, text="Fallback template (used if LLM disabled or errors):").pack(anchor="w")
        ttk.Entry(right2, textvariable=self.var_greet_template).pack(fill="x", pady=4)
        ttk.Label(right2, text="Cooldown seconds:").pack(anchor="w", pady=(6,0))
        ttk.Spinbox(right2, from_=0, to=86400, textvariable=self.var_greet_cooldown, width=10).pack(anchor="w")
        btnrow = ttk.Frame(right2)
        btnrow.pack(fill="x", pady=(8,0))
        ttk.Button(btnrow, text="Apply to Server", command=self.apply_greeting).pack(side="left")
        ttk.Button(btnrow, text="Remove for Server", command=self.remove_greeting).pack(side="left", padx=6)

        # -------- Voice tab --------
        vf = ttk.LabelFrame(self.tab_voice, text="Voice Channel Playback")
        vf.pack(fill="both", expand=True, **pad)

        vtop = ttk.Frame(vf)
        vtop.pack(fill="x", padx=6, pady=6)
        ttk.Button(vtop, text="Load Servers/Channels", command=self.load_guilds).pack(side="left")
        ttk.Label(vtop, text="Server:").pack(side="left", padx=(12, 4))
        self.voice_guild_box = ttk.Combobox(vtop, textvariable=self.var_voice_guild, state="readonly", width=28)
        self.voice_guild_box.pack(side="left")
        self.voice_guild_box.bind("<<ComboboxSelected>>", self.on_select_voice_guild)
        ttk.Label(vtop, text="Voice Channel:").pack(side="left", padx=(12, 4))
        self.voice_channel_box = ttk.Combobox(vtop, textvariable=self.var_voice_channel, state="readonly", width=28)
        self.voice_channel_box.pack(side="left")

        vquery = ttk.Frame(vf)
        vquery.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(vquery, text="YouTube URL, search, or playlist URL:").pack(anchor="w")
        ttk.Entry(vquery, textvariable=self.var_voice_query).pack(fill="x", pady=(4, 0))

        vbuttons1 = ttk.Frame(vf)
        vbuttons1.pack(fill="x", padx=6, pady=(6, 3))
        ttk.Button(vbuttons1, text="Join", command=self.join_voice_from_gui).pack(side="left")
        ttk.Button(vbuttons1, text="Play Now", command=self.play_voice_from_gui).pack(side="left", padx=6)
        ttk.Button(vbuttons1, text="Add Queue", command=self.queue_voice_from_gui).pack(side="left")
        ttk.Button(vbuttons1, text="Import Playlist", command=self.import_playlist_from_gui).pack(side="left", padx=6)

        vbuttons2 = ttk.Frame(vf)
        vbuttons2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(vbuttons2, text="Pause", command=self.pause_voice_from_gui).pack(side="left")
        ttk.Button(vbuttons2, text="Resume", command=self.resume_voice_from_gui).pack(side="left", padx=6)
        ttk.Button(vbuttons2, text="Skip", command=self.skip_voice_from_gui).pack(side="left")
        ttk.Button(vbuttons2, text="Stop", command=self.stop_voice_from_gui).pack(side="left", padx=6)
        ttk.Button(vbuttons2, text="Clear Queue", command=self.clear_voice_queue_from_gui).pack(side="left")
        ttk.Button(vbuttons2, text="Leave", command=self.leave_voice_from_gui).pack(side="left", padx=6)

        vnow = ttk.LabelFrame(vf, text="Now Playing")
        vnow.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(vnow, textvariable=self.var_voice_now_playing, wraplength=860, justify="left").pack(
            fill="x", padx=6, pady=6
        )

        vqueue = ttk.LabelFrame(vf, text="Queue")
        vqueue.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        ttk.Label(vqueue, textvariable=self.var_voice_queue_summary).pack(anchor="w", padx=6, pady=(6, 0))
        self.voice_queue_list = tk.Listbox(vqueue, height=10, exportselection=False)
        self.voice_queue_list.pack(fill="both", expand=True, padx=6, pady=6)
        self.voice_queue_list.insert("end", "(queue empty)")

        vstatus = ttk.LabelFrame(vf, text="Voice Status")
        vstatus.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(vstatus, textvariable=self.var_voice_status, wraplength=860, justify="left").pack(
            fill="x", padx=6, pady=6
        )
        ttk.Label(
            vf,
            text="Requires discord.py voice support, PyNaCl, ffmpeg, and yt-dlp.",
        ).pack(anchor="w", padx=6, pady=(0, 6))

        # -------- Heartbeat tab --------
        hf = ttk.LabelFrame(self.tab_heartbeat, text="Community Heartbeat")
        hf.pack(fill="both", expand=True, **pad)

        hb_top = ttk.Frame(hf)
        hb_top.pack(fill="x", padx=6, pady=(6, 4))
        ttk.Button(hb_top, text="Load Servers/Channels", command=self.load_guilds).pack(side="left")
        ttk.Label(hb_top, text="Server config:").pack(side="left", padx=(12, 4))
        self.heartbeat_guild_box = ttk.Combobox(hb_top, textvariable=self.var_hb_home_guild, state="readonly", width=30)
        self.heartbeat_guild_box.pack(side="left")
        self.heartbeat_guild_box.bind("<<ComboboxSelected>>", self.on_select_heartbeat_guild)
        ttk.Button(hb_top, text="Refresh Status", command=self.refresh_heartbeat_status).pack(side="right")
        ttk.Button(hb_top, text="Run If Due", command=self.run_heartbeat_from_gui).pack(side="right", padx=(0, 6))
        ttk.Button(hb_top, text="Resume", command=self.resume_heartbeat_from_gui).pack(side="right", padx=(0, 6))
        ttk.Button(hb_top, text="Pause", command=self.pause_heartbeat_from_gui).pack(side="right", padx=(0, 6))

        hb_settings = ttk.LabelFrame(hf, text="Policy")
        hb_settings.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Checkbutton(
            hb_settings,
            text="Enable app-side heartbeat controls",
            variable=self.var_hb_enabled,
            command=self._debounced_autosave,
        ).grid(row=0, column=0, sticky="w", padx=6, pady=(6, 2))
        ttk.Checkbutton(
            hb_settings,
            text="Shadow mode (never auto-send)",
            variable=self.var_hb_shadow_mode,
            command=self._debounced_autosave,
        ).grid(row=0, column=1, sticky="w", padx=6, pady=(6, 2))
        ttk.Checkbutton(
            hb_settings,
            text="Require manual approval",
            variable=self.var_hb_manual_approval,
            command=self._debounced_autosave,
        ).grid(row=0, column=2, sticky="w", padx=6, pady=(6, 2))
        ttk.Checkbutton(
            hb_settings,
            text="Require reply context",
            variable=self.var_hb_require_reply_context,
            command=self._debounced_autosave,
        ).grid(row=0, column=3, sticky="w", padx=6, pady=(6, 2))

        ttk.Label(hb_settings, text="Quiet hours start:").grid(row=1, column=0, sticky="w", padx=6, pady=(4, 6))
        ttk.Entry(hb_settings, textvariable=self.var_hb_quiet_start, width=10).grid(row=1, column=1, sticky="w", padx=6, pady=(4, 6))
        ttk.Label(hb_settings, text="Quiet hours end:").grid(row=1, column=2, sticky="w", padx=6, pady=(4, 6))
        ttk.Entry(hb_settings, textvariable=self.var_hb_quiet_end, width=10).grid(row=1, column=3, sticky="w", padx=6, pady=(4, 6))

        ttk.Label(hb_settings, text="Max posts/hour:").grid(row=2, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Spinbox(hb_settings, from_=0, to=20, textvariable=self.var_hb_max_posts_per_hour, width=6).grid(row=2, column=1, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(hb_settings, text="Max posts/day:").grid(row=2, column=2, sticky="w", padx=6, pady=(0, 6))
        ttk.Spinbox(hb_settings, from_=0, to=200, textvariable=self.var_hb_max_posts_per_day, width=6).grid(row=2, column=3, sticky="w", padx=6, pady=(0, 6))

        ttk.Label(hb_settings, text="Min recent human messages:").grid(row=3, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Spinbox(hb_settings, from_=0, to=50, textvariable=self.var_hb_min_recent_human_messages, width=6).grid(row=3, column=1, sticky="w", padx=6, pady=(0, 6))
        ttk.Label(hb_settings, text="Dead channel threshold (min):").grid(row=3, column=2, sticky="w", padx=6, pady=(0, 6))
        ttk.Spinbox(hb_settings, from_=0, to=1440, textvariable=self.var_hb_dead_channel_threshold_minutes, width=8).grid(row=3, column=3, sticky="w", padx=6, pady=(0, 6))

        hb_channels = ttk.LabelFrame(hf, text="Per-Server Route")
        hb_channels.pack(fill="x", padx=6, pady=(0, 6))
        hb_route = ttk.Frame(hb_channels)
        hb_route.pack(fill="x", padx=6, pady=6)
        ttk.Checkbutton(
            hb_route,
            text="Enable heartbeat responses in this server",
            variable=self.var_hb_server_enabled,
            command=self.apply_current_heartbeat_server_config,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Checkbutton(
            hb_route,
            text="Show typing before heartbeat send",
            variable=self.var_hb_server_typing,
            command=self.apply_current_heartbeat_server_config,
        ).grid(row=0, column=2, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(hb_route, text="Heartbeat channel:").grid(row=1, column=0, sticky="w", pady=(0, 4))
        self.heartbeat_channel_box = ttk.Combobox(
            hb_route,
            textvariable=self.var_hb_server_channel,
            state="readonly",
            width=34,
        )
        self.heartbeat_channel_box.grid(row=1, column=1, sticky="w", pady=(0, 4))
        self.heartbeat_channel_box.bind("<<ComboboxSelected>>", self.on_select_heartbeat_channel)
        ttk.Button(
            hb_route,
            text="Apply Server Route",
            command=self.apply_current_heartbeat_server_config,
        ).grid(row=1, column=2, sticky="w", padx=(10, 0), pady=(0, 4))
        ttk.Button(
            hb_route,
            text="Disable Server",
            command=self.disable_current_heartbeat_server_config,
        ).grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(0, 4))
        ttk.Label(
            hb_channels,
            text="Each server can have at most one heartbeat channel. If a server is disabled here, heartbeat will never talk there.",
            justify="left",
            wraplength=860,
        ).pack(anchor="w", padx=6, pady=(0, 6))

        hb_status = ttk.LabelFrame(hf, text="Live Status")
        hb_status.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(hb_status, textvariable=self.var_hb_db_summary, justify="left", wraplength=860).pack(anchor="w", padx=6, pady=(6, 2))
        ttk.Label(hb_status, textvariable=self.var_hb_db_tasks, justify="left", wraplength=860).pack(anchor="w", padx=6, pady=(0, 6))

        hb_logs = ttk.Frame(hf)
        hb_logs.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        hb_recent = ttk.LabelFrame(hb_logs, text="Recent Heartbeats")
        hb_recent.pack(side="left", fill="both", expand=True, padx=(0, 3))
        self.heartbeat_recent_text = ScrolledText(hb_recent, height=10, wrap="word", state="disabled")
        self.heartbeat_recent_text.pack(fill="both", expand=True, padx=6, pady=6)

        hb_activity = ttk.LabelFrame(hb_logs, text="Recent Community Activity")
        hb_activity.pack(side="left", fill="both", expand=True, padx=3)
        self.heartbeat_activity_text = ScrolledText(hb_activity, height=10, wrap="word", state="disabled")
        self.heartbeat_activity_text.pack(fill="both", expand=True, padx=6, pady=6)

        hb_outbox = ttk.LabelFrame(hb_logs, text="Recent Outbox")
        hb_outbox.pack(side="left", fill="both", expand=True, padx=(3, 0))
        hb_outbox_btns = ttk.Frame(hb_outbox)
        hb_outbox_btns.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(hb_outbox_btns, text="Approve Selected", command=self.approve_selected_heartbeat_outbox).pack(side="left")
        ttk.Button(hb_outbox_btns, text="Reject Selected", command=self.reject_selected_heartbeat_outbox).pack(side="left", padx=6)
        ttk.Button(hb_outbox_btns, text="Refresh Preview", command=self.refresh_selected_heartbeat_outbox_preview).pack(side="left")
        self.heartbeat_outbox_list = tk.Listbox(hb_outbox, height=7, exportselection=False)
        self.heartbeat_outbox_list.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.heartbeat_outbox_list.bind("<<ListboxSelect>>", self.on_select_heartbeat_outbox)
        self.heartbeat_outbox_detail = ScrolledText(hb_outbox, height=7, wrap="word", state="disabled")
        self.heartbeat_outbox_detail.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        hb_outbox_target = ttk.Frame(hb_outbox)
        hb_outbox_target.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(hb_outbox_target, text="Override channel ID:").grid(row=0, column=0, sticky="w")
        ttk.Entry(hb_outbox_target, textvariable=self.var_hb_override_channel_id, width=16).grid(row=0, column=1, sticky="w", padx=(4, 10))
        ttk.Label(hb_outbox_target, text="Override reply-to ID:").grid(row=0, column=2, sticky="w")
        ttk.Entry(hb_outbox_target, textvariable=self.var_hb_override_reply_to, width=18).grid(row=0, column=3, sticky="w", padx=(4, 0))
        ttk.Label(hb_outbox, textvariable=self.var_hb_candidate_summary, justify="left", wraplength=420).pack(
            anchor="w", padx=6, pady=(0, 4)
        )
        ttk.Label(hb_outbox, text="Editable Draft:").pack(anchor="w", padx=6)
        self.heartbeat_outbox_edit = ScrolledText(hb_outbox, height=6, wrap="word")
        self.heartbeat_outbox_edit.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        if not AGI_MEMORY_AVAILABLE:
            hb_warn_text = (
                "Heartbeat controls require agi-memory.\n"
                f"Reason: {_agi_memory_unavailable_reason()}"
            )
            ttk.Label(hf, text=hb_warn_text, foreground="red", justify="left", wraplength=860).pack(
                anchor="w", padx=6, pady=(0, 6)
            )

        # -------- Memory tab --------
        mf = ttk.LabelFrame(self.tab_memory, text="Cognitive Memory (agi-memory)")
        mf.pack(fill="both", expand=True, **pad)

        ttk.Checkbutton(mf, text="Enable memory system", variable=self.var_mem_enabled,
                        command=self._debounced_autosave).pack(anchor="w", padx=6, pady=(6,2))
        ttk.Checkbutton(mf, text="Auto-form memories from conversations", variable=self.var_mem_formation,
                        command=self._debounced_autosave).pack(anchor="w", padx=6, pady=2)
        ttk.Checkbutton(mf, text="Use personhood prompt", variable=self.var_mem_personhood,
                        command=self._debounced_autosave).pack(anchor="w", padx=6, pady=2)

        dsn_frame = ttk.Frame(mf)
        dsn_frame.pack(fill="x", padx=6, pady=(8,2))
        ttk.Label(dsn_frame, text="Database DSN:").pack(side="left")
        ttk.Entry(dsn_frame, textvariable=self.var_mem_dsn, width=60).pack(side="left", fill="x", expand=True, padx=(6,0))

        hlimit_frame = ttk.Frame(mf)
        hlimit_frame.pack(fill="x", padx=6, pady=(4,6))
        ttk.Label(hlimit_frame, text="Hydration limit (memories per query):").pack(side="left")
        ttk.Spinbox(hlimit_frame, from_=1, to=20, textvariable=self.var_mem_hydrate_limit, width=6).pack(side="left", padx=(6,0))

        if not AGI_MEMORY_AVAILABLE:
            warn_text = (
                "WARNING: agi-memory is unavailable.\n"
                f"Reason: {_agi_memory_unavailable_reason()}\n"
                "If the module is present, install missing deps in the project venv, for example:\n"
                r".venv\Scripts\python.exe -m pip install asyncpg"
            )
            warn = ttk.Label(mf, text=warn_text, foreground="red", justify="left", wraplength=860)
            warn.pack(anchor="w", padx=6, pady=(8,6))

        # Status bar (global)
        sb = ttk.Frame(self)
        sb.pack(fill="x")
        ttk.Label(sb, textvariable=self.status_var).pack(side="left", padx=6, pady=4)

        # Initial models refresh slight delay
        self.after(200, self.refresh_models)

        # Autosave geometry on move/resize (debounced)
        self.bind("<Configure>", self._on_configure)

    # ---- Member picker helpers ----
    def _current_gid(self) -> int | None:
        name = self.guild_box.get().strip()
        return self._guild_id_by_name.get(name)

    def _current_voice_gid(self) -> int | None:
        name = self.voice_guild_box.get().strip()
        return self._guild_id_by_name.get(name)

    def _current_heartbeat_gid(self) -> int | None:
        name = self.heartbeat_guild_box.get().strip()
        return self._guild_id_by_name.get(name)

    def _rebuild_member_lists(self, gid: int):
        self._member_id_name.clear()
        self.member_list.delete(0, "end")
        self.sel_list.delete(0, "end")
        for m in self._members_by_guild.get(gid, []):
            mid = int(m["id"])
            nm = str(m["name"])
            self._member_id_name[mid] = nm
            self.member_list.insert("end", f"{nm} · {mid}")
        cfg = self.runner.greet_settings.get(gid) or {}
        for mid in sorted(cfg.get("user_ids", set())):
            nm = self._member_id_name.get(int(mid)) or str(mid)
            self.sel_list.insert("end", f"{nm} · {int(mid)}")

    def _channel_label(self, channel: dict) -> str:
        return f"{channel.get('name', '?')} | {int(channel.get('id', 0))}"

    def _parse_channel_id_label(self, text: str) -> int | None:
        if "|" not in text:
            return None
        try:
            return int(text.rsplit("|", 1)[1].strip())
        except Exception:
            return None

    def _rebuild_heartbeat_channel_lists(self, gid: int):
        labels = [self._channel_label(channel) for channel in self._channels_by_guild.get(gid, [])]
        self.heartbeat_channel_box["values"] = labels
        cfg = self.runner._heartbeat_server_config(gid) or {}
        channel_id = int(cfg.get("channel_id", 0) or 0)
        enabled = bool(cfg.get("enabled", False))
        send_typing = bool(cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING))
        selected_label = ""
        if channel_id > 0:
            for channel in self._channels_by_guild.get(gid, []):
                if int(channel.get("id", 0) or 0) == channel_id:
                    selected_label = self._channel_label(channel)
                    break
        self.var_hb_server_enabled.set(enabled)
        self.var_hb_server_typing.set(send_typing)
        self.var_hb_server_channel.set(selected_label)

    def _ids_from_listbox(self, lb: tk.Listbox) -> List[int]:
        out: List[int] = []
        for i in lb.curselection():
            txt = lb.get(i)
            if "·" in txt:
                try:
                    mid = int(txt.split("·", 1)[1].strip())
                    out.append(mid)
                except Exception:
                    pass
        return out

    def _add_selected(self):
        ids = self._ids_from_listbox(self.member_list)
        existing = set()
        for i in range(self.sel_list.size()):
            txt = self.sel_list.get(i)
            if "·" in txt:
                try:
                    existing.add(int(txt.split("·", 1)[1].strip()))
                except Exception:
                    pass
        for mid in ids:
            if mid in existing:
                continue
            nm = self._member_id_name.get(mid, str(mid))
            self.sel_list.insert("end", f"{nm} · {mid}")
        self._debounced_autosave()

    def _remove_selected(self):
        for i in reversed(self.sel_list.curselection()):
            self.sel_list.delete(i)
        self._debounced_autosave()

    def apply_current_heartbeat_server_config(self):
        gid = self._current_heartbeat_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        channel_id = self._parse_channel_id_label(self.var_hb_server_channel.get())
        enabled = bool(self.var_hb_server_enabled.get()) and channel_id is not None
        self.runner.heartbeat_server_configs[int(gid)] = {
            "enabled": enabled,
            "channel_id": int(channel_id or 0),
            "send_typing": bool(self.var_hb_server_typing.get()),
        }
        self.runner.heartbeat_home_guild_id = int(gid)
        self._debounced_autosave()

    def disable_current_heartbeat_server_config(self):
        gid = self._current_heartbeat_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.heartbeat_server_configs[int(gid)] = {
            "enabled": False,
            "channel_id": 0,
            "send_typing": bool(self.var_hb_server_typing.get()),
        }
        self.var_hb_server_enabled.set(False)
        self.var_hb_server_channel.set("")
        self._rebuild_heartbeat_channel_lists(gid)
        self._debounced_autosave()

    def on_select_heartbeat_channel(self, event=None):
        if self.var_hb_server_channel.get().strip():
            self.var_hb_server_enabled.set(True)
        self.apply_current_heartbeat_server_config()

    def _enabled_heartbeat_server_summary(self) -> str:
        enabled_cfgs = self.runner._enabled_heartbeat_server_configs()
        if not enabled_cfgs:
            return "No heartbeat servers enabled."
        parts: list[str] = []
        for gid, cfg in sorted(enabled_cfgs.items()):
            guild_label = next((name for name, value in self._guild_id_by_name.items() if int(value) == int(gid)), str(gid))
            parts.append(
                f"{guild_label}: channel {int(cfg.get('channel_id', 0) or 0)}"
                + (" (typing)" if cfg.get("send_typing") else "")
            )
        return "Enabled heartbeat routes: " + " | ".join(parts)

    def _clear_heartbeat_channels(self):
        # Legacy no-op kept only to avoid breaking old callbacks.
        self._debounced_autosave()

    def _add_all(self):
        self.sel_list.delete(0, "end")
        for i in range(self.member_list.size()):
            txt = self.member_list.get(i)
            self.sel_list.insert("end", txt)
        self._debounced_autosave()

    def _clear_selected(self):
        self.sel_list.delete(0, "end")
        self._debounced_autosave()

    def _selected_voice_channel_id(self, gid: int) -> int | None:
        cname = self.voice_channel_box.get().strip()
        if not cname:
            return None
        for c in self._voice_channels_by_guild.get(gid, []):
            if c.get("name") == cname:
                try:
                    return int(c.get("id"))
                except Exception:
                    return None
        return None

    def _with_memory_client(self, task_coro_factory, on_success, error_prefix: str):
        if not AGI_MEMORY_AVAILABLE:
            self.log(f"{error_prefix}: {_agi_memory_unavailable_reason()}")
            return
        dsn = self.var_mem_dsn.get().strip()
        if not dsn:
            self.log(f"{error_prefix}: no memory DSN configured.")
            return

        def _worker():
            async def _run():
                mem = await CognitiveMemory.create(dsn, min_size=1, max_size=2)
                try:
                    return await task_coro_factory(mem)
                finally:
                    await mem.close()

            try:
                result = asyncio.run(_run())
            except Exception as exc:
                self.after(0, lambda: self.log(f"{error_prefix}: {exc}"))
                return
            if on_success:
                self.after(0, lambda: on_success(result))

        threading.Thread(target=_worker, name="HeartbeatGuiWorker", daemon=True).start()

    def _set_text_widget(self, widget: ScrolledText, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _set_editable_text_widget(self, widget: ScrolledText, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)

    def _fmt_dt(self, value: Any) -> str:
        if isinstance(value, datetime):
            return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if value is None:
            return "-"
        return str(value)

    def _summarize_worker_tasks(self, items: List[dict]) -> str:
        if not items:
            return "No worker task data."
        parts = []
        for item in items:
            parts.append(f"{item.get('task_type', '?')}: {item.get('pending_count', 0)}")
        return "Worker tasks: " + " | ".join(parts)

    def _format_discord_activity_item(self, item: dict) -> str:
        source = item.get("source_attribution") or {}
        if not isinstance(source, dict):
            source = {}
        stamp = self._fmt_dt(item.get("created_at"))
        guild_name = source.get("guild_name") or source.get("guild_id") or "?"
        channel_name = source.get("channel_name") or source.get("channel_id") or "?"
        message_count = int(source.get("message_count", 0) or 0)
        question_count = int(source.get("question_count", 0) or 0)
        mention_count = int(source.get("bot_mention_count", source.get("mention_count", 0)) or 0)
        body = str(item.get("content") or "").strip()
        headline = (
            f"{stamp} | {guild_name} / #{channel_name} | "
            f"msgs {message_count} | questions {question_count} | bot mentions {mention_count}"
        )
        return f"{headline}\n{body}" if body else headline

    def _format_outbox_summary(self, item: dict) -> str:
        payload = item.get("payload") or {}
        body = payload.get("message") or payload.get("content") or json.dumps(payload)
        body = str(body).strip().replace("\n", " ")
        if len(body) > 72:
            body = body[:69] + "..."
        return f"{self._fmt_dt(item.get('created_at'))} | {item.get('status', '?')} | {body}"

    def _parse_optional_int(self, raw: str) -> int | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            value = int(text)
        except Exception:
            return None
        return value if value > 0 else None

    def _current_heartbeat_draft_text(self) -> str:
        return self.heartbeat_outbox_edit.get("1.0", "end").strip()

    def _build_heartbeat_outbox_preview(
        self,
        item: dict | None,
        *,
        message_override: str | None = None,
    ) -> dict[str, Any] | None:
        if not item:
            return None
        if message_override is None:
            message_override = self._current_heartbeat_draft_text()
        if not message_override:
            payload = item.get("payload") or {}
            message_override = str(payload.get("message") or payload.get("content") or "").strip()
        return self.runner.preview_heartbeat_outbox_candidate(
            item,
            recent_activity=self._heartbeat_activity_items,
            message_override=message_override or None,
            channel_id_override=self._parse_optional_int(self.var_hb_override_channel_id.get()),
            guild_id_override=None,
            reply_to_message_id_override=self._parse_optional_int(self.var_hb_override_reply_to.get()),
        )

    def _render_heartbeat_outbox_detail(self, item: dict | None, *, reset_editor: bool = True):
        if not item:
            self._set_text_widget(self.heartbeat_outbox_detail, "No outbox message selected.")
            self._set_editable_text_widget(self.heartbeat_outbox_edit, "")
            self.var_hb_candidate_summary.set("No outbox proposal selected.")
            self.var_hb_override_channel_id.set("")
            self.var_hb_override_reply_to.set("")
            return
        payload = item.get("payload") or {}
        body = str(payload.get("message") or payload.get("content") or "").strip()
        preview = self._build_heartbeat_outbox_preview(item, message_override=body if reset_editor else None)
        candidate = preview.get("candidate") if isinstance(preview, dict) else None
        detail_preview = preview.get("summary") if isinstance(preview, dict) else ""
        lines = [
            f"ID: {item.get('id')}",
            f"Created: {self._fmt_dt(item.get('created_at'))}",
            f"Kind: {item.get('kind')}",
            f"Status: {item.get('status')}",
            f"Sent: {self._fmt_dt(item.get('sent_at'))}",
            f"Error: {item.get('error_message') or '-'}",
            f"Target preview: {detail_preview or '-'}",
            "",
            "Payload:",
            json.dumps(payload, indent=2, default=str),
        ]
        self._set_text_widget(self.heartbeat_outbox_detail, "\n".join(lines))
        if reset_editor:
            self._set_editable_text_widget(self.heartbeat_outbox_edit, body)
        if candidate:
            self.var_hb_candidate_summary.set(detail_preview or "Preview available.")
            self.var_hb_override_channel_id.set(str(candidate.get("channel_id") or ""))
            self.var_hb_override_reply_to.set(str(candidate.get("reply_to_message_id") or ""))
        else:
            self.var_hb_candidate_summary.set(detail_preview or "No target preview available.")
            self.var_hb_override_channel_id.set("")
            self.var_hb_override_reply_to.set("")

    def on_select_heartbeat_outbox(self, event=None):
        selection = self.heartbeat_outbox_list.curselection()
        if not selection:
            self._render_heartbeat_outbox_detail(None)
            return
        idx = int(selection[0])
        if idx < 0 or idx >= len(self._heartbeat_outbox_items):
            self._render_heartbeat_outbox_detail(None)
            return
        self._render_heartbeat_outbox_detail(self._heartbeat_outbox_items[idx])

    def _selected_heartbeat_outbox_item(self) -> dict | None:
        selection = self.heartbeat_outbox_list.curselection()
        if not selection:
            return None
        idx = int(selection[0])
        if idx < 0 or idx >= len(self._heartbeat_outbox_items):
            return None
        return self._heartbeat_outbox_items[idx]

    def refresh_selected_heartbeat_outbox_preview(self):
        item = self._selected_heartbeat_outbox_item()
        if not item:
            self.log("Select an outbox proposal first.")
            return
        preview = self._build_heartbeat_outbox_preview(item)
        if not preview:
            self.log("Could not build an outbox preview.")
            return
        candidate = preview.get("candidate") or {}
        self.var_hb_candidate_summary.set(str(preview.get("summary") or "Preview updated."))
        if candidate.get("channel_id"):
            self.var_hb_override_channel_id.set(str(candidate.get("channel_id")))
        if candidate.get("reply_to_message_id"):
            self.var_hb_override_reply_to.set(str(candidate.get("reply_to_message_id")))
        self._render_heartbeat_outbox_detail(item, reset_editor=False)

    def _render_heartbeat_snapshot(self, snapshot: dict):
        state = snapshot.get("state") or {}
        health = snapshot.get("health") or {}
        activity = snapshot.get("discord_activity") or {}
        affective_state = state.get("affective_state") or {}
        emotion = affective_state.get("primary_emotion") or health.get("current_emotion") or "-"
        energy = health.get("current_energy", state.get("current_energy", 0))
        total = health.get("total_heartbeats", state.get("heartbeat_count", 0))
        max_energy = health.get("max_energy", "?")
        summary_lines = [
            f"Agent configured: {bool(snapshot.get('agent_configured'))}",
            f"Heartbeat due: {bool(snapshot.get('heartbeat_due'))}",
            f"Paused: {bool(health.get('is_paused', state.get('is_paused')))}",
            f"Energy: {energy} / {max_energy}",
            f"Total heartbeats: {total}",
            f"Last heartbeat: {self._fmt_dt(health.get('last_heartbeat', state.get('last_heartbeat_at')))}",
            f"Next heartbeat: {self._fmt_dt(health.get('next_heartbeat', state.get('next_heartbeat_at')))}",
            f"Last user contact: {self._fmt_dt(state.get('last_user_contact'))}",
            f"Current emotion: {emotion}",
            (
                "Community activity: "
                f"{int(activity.get('total_items', 0) or 0)} summaries / "
                f"{int(activity.get('message_count', 0) or 0)} messages / "
                f"{int(activity.get('active_channels', 0) or 0)} channels / "
                f"{int(activity.get('question_count', 0) or 0)} questions"
            ),
            self._enabled_heartbeat_server_summary(),
        ]
        self.var_hb_db_summary.set("\n".join(summary_lines))
        self.var_hb_db_tasks.set(self._summarize_worker_tasks(snapshot.get("worker_tasks") or []))

        recent_lines: List[str] = []
        for hb in snapshot.get("recent_heartbeats") or []:
            started = self._fmt_dt(hb.get("started_at"))
            ended = self._fmt_dt(hb.get("ended_at"))
            narrative = str(hb.get("narrative") or "").strip().replace("\n", " ")
            if len(narrative) > 140:
                narrative = narrative[:137] + "..."
            recent_lines.append(
                f"#{hb.get('heartbeat_number', '?')} | started {started} | ended {ended} | "
                f"actions {hb.get('action_count', 0)} | energy {hb.get('energy_start', '?')} -> {hb.get('energy_end', '?')}\n"
                f"{narrative or '(no narrative yet)'}"
            )
        self._set_text_widget(
            self.heartbeat_recent_text,
            "\n\n".join(recent_lines) if recent_lines else "No recent heartbeats.",
        )

        activity_lines: List[str] = []
        activity_summary = str(activity.get("summary") or "").strip()
        self._heartbeat_activity_items = list(activity.get("items") or [])
        if activity_summary:
            activity_lines.append(activity_summary)
        for item in self._heartbeat_activity_items:
            activity_lines.append(self._format_discord_activity_item(item))
        self._set_text_widget(
            self.heartbeat_activity_text,
            "\n\n".join(activity_lines) if activity_lines else "No recent community activity.",
        )

        previous_id = None
        selection = self.heartbeat_outbox_list.curselection()
        if selection:
            current = self._selected_heartbeat_outbox_item()
            if current:
                previous_id = str(current.get("id"))

        self._heartbeat_outbox_items = list(snapshot.get("outbox_messages") or [])
        self.heartbeat_outbox_list.delete(0, "end")
        selected_index = None
        for idx, item in enumerate(self._heartbeat_outbox_items):
            self.heartbeat_outbox_list.insert("end", self._format_outbox_summary(item))
            if previous_id and str(item.get("id")) == previous_id:
                selected_index = idx

        if self._heartbeat_outbox_items:
            if selected_index is None:
                selected_index = 0
            self.heartbeat_outbox_list.selection_clear(0, "end")
            self.heartbeat_outbox_list.selection_set(selected_index)
            self.heartbeat_outbox_list.activate(selected_index)
            self._render_heartbeat_outbox_detail(self._heartbeat_outbox_items[selected_index])
        else:
            self._render_heartbeat_outbox_detail(None)

    def refresh_heartbeat_status(self):
        self.var_hb_db_summary.set("Loading heartbeat status...")
        self.var_hb_db_tasks.set("Fetching worker tasks...")
        self._with_memory_client(
            lambda mem: mem.get_heartbeat_snapshot(heartbeat_limit=10, outbox_limit=12),
            self._render_heartbeat_snapshot,
            "Heartbeat refresh failed",
        )

    def pause_heartbeat_from_gui(self):
        self._with_memory_client(
            lambda mem: mem.set_heartbeat_paused(True),
            lambda _result: (self.log("Heartbeat paused in AGI-memory."), self.refresh_heartbeat_status()),
            "Heartbeat pause failed",
        )

    def resume_heartbeat_from_gui(self):
        self._with_memory_client(
            lambda mem: mem.set_heartbeat_paused(False),
            lambda _result: (self.log("Heartbeat resumed in AGI-memory."), self.refresh_heartbeat_status()),
            "Heartbeat resume failed",
        )

    def run_heartbeat_from_gui(self):
        def _done(hb_id):
            if hb_id:
                self.log(f"Heartbeat queued: {hb_id}")
            else:
                self.log("Heartbeat did not run. It may be paused, not due yet, or not configured.")
            self.refresh_heartbeat_status()

        self._with_memory_client(
            lambda mem: mem.run_heartbeat(),
            _done,
            "Heartbeat run failed",
        )

    def approve_selected_heartbeat_outbox(self):
        item = self._selected_heartbeat_outbox_item()
        if not item:
            self.log("Select an outbox proposal first.")
            return
        if str(item.get("status")) != "pending":
            self.log("Only pending outbox proposals can be approved.")
            return
        if not self.runner.running:
            self.log("Start the bot before approving outbox proposals.")
            return
        message_override = self._current_heartbeat_draft_text()
        if not message_override:
            self.log("Draft message is empty.")
            return
        preview = self._build_heartbeat_outbox_preview(item)
        if preview and preview.get("error"):
            self.log(str(preview.get("error")))
            return
        self.runner.approve_outbox_message(
            str(item.get("id")),
            message_override=message_override,
            channel_id_override=self._parse_optional_int(self.var_hb_override_channel_id.get()),
            guild_id_override=None,
            reply_to_message_id_override=self._parse_optional_int(self.var_hb_override_reply_to.get()),
        )
        self.after(1200, self.refresh_heartbeat_status)

    def reject_selected_heartbeat_outbox(self):
        item = self._selected_heartbeat_outbox_item()
        if not item:
            self.log("Select an outbox proposal first.")
            return
        if str(item.get("status")) != "pending":
            self.log("Only pending outbox proposals can be rejected.")
            return

        outbox_id = str(item.get("id"))

        def _done(_result):
            self.log(f"Outbox proposal rejected: {outbox_id}")
            self.refresh_heartbeat_status()

        self._with_memory_client(
            lambda mem: mem.update_outbox_message_status(
                outbox_id,
                "failed",
                error_message="Rejected by operator from DiscordLLM GUI.",
            ),
            _done,
            "Outbox reject failed",
        )

    # -------- GUI event handlers ---------
    def start_bot(self):
        self._apply_settings()
        self.runner.start()
        self.hide_to_tray()  # hide window after starting

    def stop_bot(self):
        self.runner.stop()

    def reload_env(self):
        load_dotenv(APP_DIR / ".env", override=True)
        tok = _env_token()
        self.var_token.set(tok)
        self.log(f".env reloaded (token length {len(tok)})")

    def _apply_settings(self):
        raw_tok = self.var_token.get().strip()
        tok = _sanitize_token(raw_tok) if raw_tok else _env_token()
        if not tok:
            self.log("No token provided. Set DISCORD_TOKEN in .env or paste it into the Token field.")
        enabled_cfgs = self.runner._enabled_heartbeat_server_configs()
        legacy_allowed_channel_ids = [
            int(cfg.get("channel_id", 0) or 0)
            for _gid, cfg in sorted(enabled_cfgs.items())
            if int(cfg.get("channel_id", 0) or 0) > 0
        ]
        self.runner.set_settings(
            token=tok,
            url=self.var_url.get().strip(),
            model=self.var_model.get().strip(),
            sys_prompt=self.var_sys.get(),
            community_prompt=self.var_low.get(),
            max_turns=self.var_max.get(),
            memory_enabled=self.var_mem_enabled.get(),
            memory_dsn=self.var_mem_dsn.get().strip(),
            memory_hydrate_limit=self.var_mem_hydrate_limit.get(),
            memory_formation_enabled=self.var_mem_formation.get(),
            use_personhood_prompt=self.var_mem_personhood.get(),
            heartbeat_enabled=self.var_hb_enabled.get(),
            heartbeat_shadow_mode=self.var_hb_shadow_mode.get(),
            heartbeat_manual_approval=self.var_hb_manual_approval.get(),
            heartbeat_server_configs=self.runner.heartbeat_server_configs,
            heartbeat_home_guild_id=self.runner.heartbeat_home_guild_id,
            heartbeat_allowed_channel_ids=legacy_allowed_channel_ids,
            heartbeat_quiet_hours_start=self.var_hb_quiet_start.get().strip(),
            heartbeat_quiet_hours_end=self.var_hb_quiet_end.get().strip(),
            heartbeat_max_posts_per_hour=self.var_hb_max_posts_per_hour.get(),
            heartbeat_max_posts_per_day=self.var_hb_max_posts_per_day.get(),
            heartbeat_min_recent_human_messages=self.var_hb_min_recent_human_messages.get(),
            heartbeat_dead_channel_threshold_minutes=self.var_hb_dead_channel_threshold_minutes.get(),
            heartbeat_require_reply_context=self.var_hb_require_reply_context.get(),
            jitd_enabled=self.var_jitd_enabled.get(),
            jitd_mode=self.var_jitd_mode.get().strip(),
            jitd_speaker=self.var_jitd_speaker.get().strip(),
            jitd_ref_index=self.var_jitd_ref_index.get(),
        )

    def _on_model_selected(self, event=None):
        name = self.model_box.get().strip()
        if name:
            self.var_model.set(name)
            self.runner.set_settings(model=name)
            self.log(f"Model set to {name}")
            self.save_settings(quiet=True)

    def refresh_models(self):
        tags_url = _tags_url_from_chat_url(self.var_url.get())
        try:
            with ureq.urlopen(tags_url, timeout=5) as resp:
                data = json.load(resp)
        except Exception as e:
            self.log(f"Could not fetch models from {tags_url}: {e}")
            return
        items = (data.get("models") or []) if isinstance(data, dict) else []
        names = []
        for m in items:
            n = m.get("name") or m.get("model")
            if isinstance(n, str):
                names.append(n)
        names = sorted(set(names))
        if not names:
            self.log("No models returned by Ollama. Try `ollama pull ...`")
            return
        self.model_box["values"] = names
        cur = self.var_model.get().strip()
        if cur in names:
            self.model_box.set(cur)
        else:
            self.model_box.set(names[0])
            self._on_model_selected()
        self.log(f"Loaded {len(names)} models from Ollama")

    def _load_jitd_ref_choices(self) -> List[Tuple[int, str]]:
        metadata_path = self.runner.qwen_project_dir / "metadata.csv"
        wavs_dir = self.runner.qwen_project_dir / "wavs"
        out: List[Tuple[int, str]] = []
        if not metadata_path.exists():
            return out
        try:
            lines = metadata_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return out

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            wav_name = parts[0]
            wav_path = Path(wav_name)
            if not wav_path.is_absolute():
                wav_path = wavs_dir / wav_name
            if not wav_path.exists():
                continue
            speaker = parts[2] if len(parts) > 2 else ""
            preview = parts[1]
            if len(preview) > 56:
                preview = preview[:53] + "..."
            idx = len(out)
            label = f"[{idx}] {wav_path.name}"
            if speaker:
                label += f" ({speaker})"
            label += f" - {preview}"
            out.append((idx, label))
        return out

    def _refresh_jitd_ref_choices(self):
        choices = self._load_jitd_ref_choices()
        self._jitd_ref_label_to_idx = {label: idx for idx, label in choices}
        labels = [label for _, label in choices]
        self.jitd_ref_box["values"] = labels
        if not labels:
            self.jitd_ref_box.set("")
            return
        wanted = int(self.var_jitd_ref_index.get())
        selected = next((label for idx, label in choices if idx == wanted), labels[0])
        self.jitd_ref_box.set(selected)
        self.var_jitd_ref_index.set(self._jitd_ref_label_to_idx.get(selected, 0))

    def _on_jitd_ref_selected(self, event=None):
        label = self.jitd_ref_box.get().strip()
        if not label:
            return
        idx = self._jitd_ref_label_to_idx.get(label)
        if idx is None:
            return
        self.var_jitd_ref_index.set(int(idx))
        self._debounced_autosave()

    # ----- Greeting UI flow -----
    def load_guilds(self):
        self.runner.request_guild_snapshot()

    def on_guild_snapshot(self, payload: List[dict]):
        def _do():
            greet_selected = self.guild_box.get().strip()
            voice_selected = self.voice_guild_box.get().strip()
            heartbeat_selected = self.heartbeat_guild_box.get().strip()
            self._guild_snapshot = payload
            self._guild_id_by_name.clear()
            self._channels_by_guild.clear()
            self._voice_channels_by_guild.clear()
            self._members_by_guild.clear()
            names = []
            for g in payload:
                gid = g.get("id")
                name = g.get("name")
                self._guild_id_by_name[name] = gid
                self._channels_by_guild[gid] = g.get("channels", [])
                self._voice_channels_by_guild[gid] = g.get("voice_channels", [])
                self._members_by_guild[gid] = g.get("members", [])
                names.append(name)
            names.sort()
            self.guild_box["values"] = names
            self.voice_guild_box["values"] = names
            self.heartbeat_guild_box["values"] = names
            if greet_selected in names:
                self.guild_box.set(greet_selected)
            elif names:
                self.guild_box.set(names[0])
            if names:
                self.on_select_guild()
            if voice_selected in names:
                self.voice_guild_box.set(voice_selected)
            elif names:
                self.voice_guild_box.set(names[0])
            if names:
                self.on_select_voice_guild()
            heartbeat_name = heartbeat_selected if heartbeat_selected in names else ""
            if not heartbeat_name:
                enabled_cfgs = self.runner._enabled_heartbeat_server_configs()
                if enabled_cfgs:
                    first_enabled_gid = next(iter(sorted(enabled_cfgs.keys())))
                    for name, gid in self._guild_id_by_name.items():
                        if int(gid) == int(first_enabled_gid):
                            heartbeat_name = name
                            break
            if not heartbeat_name and self.runner.heartbeat_home_guild_id is not None:
                for name, gid in self._guild_id_by_name.items():
                    if int(gid) == int(self.runner.heartbeat_home_guild_id):
                        heartbeat_name = name
                        break
            if heartbeat_name:
                self.heartbeat_guild_box.set(heartbeat_name)
                self.on_select_heartbeat_guild()
            else:
                self.heartbeat_guild_box.set("")
                self.heartbeat_channel_box["values"] = []
                self.var_hb_server_enabled.set(False)
                self.var_hb_server_channel.set("")
                self.var_hb_server_typing.set(DEFAULT_HEARTBEAT_SERVER_TYPING)
            self.log(f"Loaded {len(names)} servers, member lists, and voice channels.")
        self.after(0, _do)

    def on_select_guild(self, event=None):
        gid = self._current_gid()
        if not gid:
            return
        chans = self._channels_by_guild.get(gid, [])
        chan_names = [c["name"] for c in chans]
        self.channel_box["values"] = chan_names
        if chan_names:
            self.channel_box.set(chan_names[0])
        self._rebuild_member_lists(gid)
        cfg = self.runner.greet_settings.get(gid) or {}
        self.var_greet_enabled.set(bool(cfg.get("enabled", True)))
        self.var_greet_use_llm.set(bool(cfg.get("use_llm", True)))
        if cfg.get("message") is not None:
            self.var_greet_template.set(str(cfg.get("message")))
        self.var_greet_cooldown.set(int(cfg.get("cooldown", DEFAULT_GREET_COOLDOWN_SEC)))
        cid = cfg.get("channel_id")
        if cid:
            for c in chans:
                if int(c.get("id")) == int(cid):
                    self.channel_box.set(c.get("name"))
                    break
        self._debounced_autosave()

    def on_select_voice_guild(self, event=None):
        gid = self._current_voice_gid()
        if not gid:
            return
        chans = self._voice_channels_by_guild.get(gid, [])
        chan_names = [c["name"] for c in chans]
        current = self.voice_channel_box.get().strip()
        self.voice_channel_box["values"] = chan_names
        if current in chan_names:
            self.voice_channel_box.set(current)
        elif chan_names:
            self.voice_channel_box.set(chan_names[0])
        else:
            self.voice_channel_box.set("")

    def on_select_heartbeat_guild(self, event=None):
        gid = self._current_heartbeat_gid()
        if not gid:
            return
        self.runner.heartbeat_home_guild_id = int(gid)
        self._rebuild_heartbeat_channel_lists(gid)
        self._debounced_autosave()

    def _selected_member_ids_from_sel_list(self) -> List[int]:
        out: List[int] = []
        for i in range(self.sel_list.size()):
            txt = self.sel_list.get(i)
            if "·" in txt:
                try:
                    out.append(int(txt.split("·", 1)[1].strip()))
                except Exception:
                    pass
        return out

    def apply_greeting(self):
        name = self.guild_box.get().strip()
        gid = self._guild_id_by_name.get(name)
        if not gid:
            self.log("Choose a server first.")
            return
        cname = self.channel_box.get().strip()
        chan_id = None
        for c in self._channels_by_guild.get(gid, []):
            if c.get("name") == cname:
                chan_id = c.get("id")
                break
        if not chan_id:
            self.log("Choose a channel.")
            return
        uids = self._selected_member_ids_from_sel_list()
        if not uids:
            self.log("Select at least one user (move to Selected list).")
            return
        tmpl = self.var_greet_template.get().strip()
        cooldown = int(self.var_greet_cooldown.get())
        enabled = bool(self.var_greet_enabled.get())
        use_llm = bool(self.var_greet_use_llm.get())
        self.runner.set_greeting_for_guild(int(gid), enabled=enabled, use_llm=use_llm, channel_id=int(chan_id), user_ids=uids, message=tmpl, cooldown_sec=cooldown)
        self.log(f"Greeting set for guild {gid} in {cname} for {len(uids)} users. Enabled={enabled}, LLM={use_llm}.")
        self.save_settings(quiet=True)

    def remove_greeting(self):
        name = self.guild_box.get().strip()
        gid = self._guild_id_by_name.get(name)
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.clear_greeting_for_guild(int(gid))
        self.log(f"Greeting removed for guild {gid}.")
        self.save_settings(quiet=True)
        self._rebuild_member_lists(gid)

    def join_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        chan_id = self._selected_voice_channel_id(gid)
        if chan_id is None:
            self.log("Choose a voice channel.")
            return
        self.var_voice_status.set("Joining voice channel...")
        self.runner.voice_join(int(gid), int(chan_id))

    def play_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        chan_id = self._selected_voice_channel_id(gid)
        if chan_id is None:
            self.log("Choose a voice channel.")
            return
        query = self.var_voice_query.get().strip()
        if not query:
            self.log("Enter a YouTube URL or search query.")
            return
        self.var_voice_status.set("Resolving track...")
        self.runner.voice_play(int(gid), int(chan_id), query)

    def queue_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        chan_id = self._selected_voice_channel_id(gid)
        if chan_id is None:
            self.log("Choose a voice channel.")
            return
        query = self.var_voice_query.get().strip()
        if not query:
            self.log("Enter a YouTube URL or search query.")
            return
        self.var_voice_status.set("Queueing track...")
        self.runner.voice_queue(int(gid), int(chan_id), query)

    def import_playlist_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        chan_id = self._selected_voice_channel_id(gid)
        if chan_id is None:
            self.log("Choose a voice channel.")
            return
        query = self.var_voice_query.get().strip()
        if not query:
            self.log("Enter a playlist URL.")
            return
        self.var_voice_status.set("Importing playlist...")
        self.runner.voice_import_playlist(int(gid), int(chan_id), query)

    def pause_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.voice_pause(int(gid))

    def resume_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.voice_resume(int(gid))

    def skip_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.voice_skip(int(gid))

    def stop_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.voice_stop(int(gid))

    def clear_voice_queue_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.voice_clear_queue(int(gid))

    def leave_voice_from_gui(self):
        gid = self._current_voice_gid()
        if not gid:
            self.log("Choose a server first.")
            return
        self.runner.voice_leave(int(gid))

    # -------- Settings persistence --------
    def _current_settings(self) -> dict:
        return {
            "url": self.var_url.get(),
            "model": self.var_model.get(),
            "system_prompt": self.var_sys.get(),
            "community_prompt": self.var_low.get(),
            "max_turns": int(self.var_max.get()),
            "geometry": self.geometry(),
            "greeting_settings": self._serialize_greeting(),
            "heartbeat_settings": self._serialize_heartbeat(),
            "memory_enabled": bool(self.var_mem_enabled.get()),
            "memory_hydrate_limit": int(self.var_mem_hydrate_limit.get()),
            "memory_formation_enabled": bool(self.var_mem_formation.get()),
            "use_personhood_prompt": bool(self.var_mem_personhood.get()),
            "jitd_enabled": bool(self.var_jitd_enabled.get()),
            "jitd_mode": self.var_jitd_mode.get().strip(),
            "jitd_speaker": self.var_jitd_speaker.get().strip(),
            "jitd_ref_index": int(self.var_jitd_ref_index.get()),
        }

    def _serialize_greeting(self) -> dict:
        per = {}
        for gid, cfg in self.runner.greet_settings.items():
            ovj = {}
            for uid, ov in (cfg.get("overrides") or {}).items():
                ovj[str(int(uid))] = {"enabled": ov.get("enabled", None), "prompt": ov.get("prompt", None)}
            per[str(gid)] = {
                "enabled": bool(cfg.get("enabled", True)),
                "use_llm": bool(cfg.get("use_llm", True)),
                "channel_id": int(cfg.get("channel_id", 0)),
                "user_ids": list(int(x) for x in cfg.get("user_ids", set())),
                "message": cfg.get("message", DEFAULT_GREET_MESSAGE),
                "cooldown": int(cfg.get("cooldown", DEFAULT_GREET_COOLDOWN_SEC)),
                "overrides": ovj,
            }
        return {"global_cooldown_sec": int(self.runner.global_cooldown_sec), "per_guild": per}

    def _serialize_heartbeat(self) -> dict:
        server_configs = {}
        for gid, cfg in sorted(self.runner.heartbeat_server_configs.items()):
            server_configs[str(int(gid))] = {
                "enabled": bool(cfg.get("enabled", False)),
                "channel_id": int(cfg.get("channel_id", 0) or 0),
                "send_typing": bool(cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING)),
            }
        enabled_cfgs = self.runner._enabled_heartbeat_server_configs()
        legacy_home_guild_id = int(self.runner.heartbeat_home_guild_id) if self.runner.heartbeat_home_guild_id else 0
        legacy_allowed_channel_ids = [
            int(cfg.get("channel_id", 0) or 0)
            for _gid, cfg in sorted(enabled_cfgs.items())
            if int(cfg.get("channel_id", 0) or 0) > 0
        ]
        return {
            "enabled": bool(self.var_hb_enabled.get()),
            "shadow_mode": bool(self.var_hb_shadow_mode.get()),
            "manual_approval": bool(self.var_hb_manual_approval.get()),
            "server_configs": server_configs,
            "home_guild_id": legacy_home_guild_id,
            "allowed_channel_ids": legacy_allowed_channel_ids,
            "quiet_hours_start": self.var_hb_quiet_start.get().strip(),
            "quiet_hours_end": self.var_hb_quiet_end.get().strip(),
            "max_posts_per_hour": int(self.var_hb_max_posts_per_hour.get()),
            "max_posts_per_day": int(self.var_hb_max_posts_per_day.get()),
            "min_recent_human_messages": int(self.var_hb_min_recent_human_messages.get()),
            "dead_channel_threshold_minutes": int(self.var_hb_dead_channel_threshold_minutes.get()),
            "require_reply_context": bool(self.var_hb_require_reply_context.get()),
        }

    def _apply_greeting_from_settings(self, data: dict):
        g = data.get("greeting_settings") or {}
        self.runner.global_cooldown_sec = int(g.get("global_cooldown_sec", DEFAULT_GREET_COOLDOWN_SEC))
        per = g.get("per_guild", {})
        for gid_str, cfg in per.items():
            try:
                gid = int(gid_str)
                ovsrc = cfg.get("overrides") or {}
                ov: Dict[int, dict] = {}
                for uid_str, v in ovsrc.items():
                    try:
                        ov[int(uid_str)] = {"enabled": v.get("enabled", None), "prompt": v.get("prompt", None)}
                    except Exception:
                        pass
                self.runner.greet_settings[gid] = {
                    "enabled": bool(cfg.get("enabled", True)),
                    "use_llm": bool(cfg.get("use_llm", True)),
                    "channel_id": int(cfg.get("channel_id", 0)),
                    "user_ids": set(int(x) for x in cfg.get("user_ids", [])),
                    "message": cfg.get("message", DEFAULT_GREET_MESSAGE),
                    "cooldown": int(cfg.get("cooldown", DEFAULT_GREET_COOLDOWN_SEC)),
                    "overrides": ov,
                }
            except Exception:
                continue

    def _apply_heartbeat_from_settings(self, data: dict):
        hb = data.get("heartbeat_settings") or {}
        self.var_hb_enabled.set(bool(hb.get("enabled", DEFAULT_HEARTBEAT_ENABLED)))
        self.var_hb_shadow_mode.set(bool(hb.get("shadow_mode", DEFAULT_HEARTBEAT_SHADOW_MODE)))
        self.var_hb_manual_approval.set(bool(hb.get("manual_approval", DEFAULT_HEARTBEAT_MANUAL_APPROVAL)))
        self.var_hb_quiet_start.set(str(hb.get("quiet_hours_start", DEFAULT_HEARTBEAT_QUIET_HOURS_START)))
        self.var_hb_quiet_end.set(str(hb.get("quiet_hours_end", DEFAULT_HEARTBEAT_QUIET_HOURS_END)))
        self.var_hb_max_posts_per_hour.set(int(hb.get("max_posts_per_hour", DEFAULT_HEARTBEAT_MAX_POSTS_PER_HOUR)))
        self.var_hb_max_posts_per_day.set(int(hb.get("max_posts_per_day", DEFAULT_HEARTBEAT_MAX_POSTS_PER_DAY)))
        self.var_hb_min_recent_human_messages.set(
            int(hb.get("min_recent_human_messages", DEFAULT_HEARTBEAT_MIN_RECENT_HUMAN_MESSAGES))
        )
        self.var_hb_dead_channel_threshold_minutes.set(
            int(hb.get("dead_channel_threshold_minutes", DEFAULT_HEARTBEAT_DEAD_CHANNEL_THRESHOLD_MINUTES))
        )
        self.var_hb_require_reply_context.set(
            bool(hb.get("require_reply_context", DEFAULT_HEARTBEAT_REQUIRE_REPLY_CONTEXT))
        )
        server_configs_src = hb.get("server_configs") or {}
        parsed_server_configs: dict[int, dict[str, Any]] = {}
        if isinstance(server_configs_src, dict):
            for gid_str, cfg in server_configs_src.items():
                try:
                    gid = int(gid_str)
                except Exception:
                    continue
                if gid <= 0 or not isinstance(cfg, dict):
                    continue
                channel_id = self.runner._coerce_outbox_int(cfg.get("channel_id"))
                parsed_server_configs[gid] = {
                    "enabled": bool(cfg.get("enabled", False)) and channel_id is not None,
                    "channel_id": int(channel_id or 0),
                    "send_typing": bool(cfg.get("send_typing", DEFAULT_HEARTBEAT_SERVER_TYPING)),
                }
        try:
            home_guild_id = int(hb.get("home_guild_id", 0) or 0)
        except Exception:
            home_guild_id = 0
        self.runner.heartbeat_home_guild_id = home_guild_id or None
        self.runner.heartbeat_allowed_channel_ids = {
            int(x) for x in hb.get("allowed_channel_ids", []) if str(x).strip()
        }
        self.runner.heartbeat_server_configs = parsed_server_configs
        if not self.runner.heartbeat_server_configs and self.runner.heartbeat_home_guild_id and self.runner.heartbeat_allowed_channel_ids:
            channel_id = sorted(self.runner.heartbeat_allowed_channel_ids)[0]
            self.runner.heartbeat_server_configs[int(self.runner.heartbeat_home_guild_id)] = {
                "enabled": True,
                "channel_id": int(channel_id),
                "send_typing": DEFAULT_HEARTBEAT_SERVER_TYPING,
            }
        self.var_hb_home_guild.set("")
        selected_gid = self.runner.heartbeat_home_guild_id
        if selected_gid is None and self.runner.heartbeat_server_configs:
            selected_gid = next(iter(sorted(self.runner.heartbeat_server_configs.keys())))
        if selected_gid is not None:
            for name, gid in self._guild_id_by_name.items():
                if int(gid) == int(selected_gid):
                    self.var_hb_home_guild.set(name)
                    self.heartbeat_guild_box.set(name)
                    self._rebuild_heartbeat_channel_lists(int(gid))
                    break

    def save_settings(self, quiet: bool = False):
        data = self._current_settings()
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            try:
                self._settings_last_mtime = SETTINGS_PATH.stat().st_mtime
            except Exception:
                pass
            self._write_guard_until = time.time() + 1.0
            if not quiet:
                self.log(f"Settings saved to {SETTINGS_PATH.name}. Tokens and memory credentials were not written.")
        except Exception as e:
            self.log(f"Failed to save settings: {e}")

    def _load_settings_from_disk(self, silent: bool = False):
        if not SETTINGS_PATH.exists():
            if not silent:
                self.log("No saved settings found.")
            return False
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            if not silent:
                self.log(f"Failed to read settings: {e}")
            return False
        self.var_url.set(data.get("url", self.var_url.get()))
        self.var_model.set(data.get("model", self.var_model.get()))
        self.var_sys.set(data.get("system_prompt", self.var_sys.get()))
        self.var_low.set(data.get("community_prompt", self.var_low.get()))
        self.var_max.set(int(data.get("max_turns", self.var_max.get())))
        self.var_mem_enabled.set(bool(data.get("memory_enabled", self.var_mem_enabled.get())))
        self.var_mem_hydrate_limit.set(int(data.get("memory_hydrate_limit", self.var_mem_hydrate_limit.get())))
        self.var_mem_formation.set(bool(data.get("memory_formation_enabled", self.var_mem_formation.get())))
        self.var_mem_personhood.set(bool(data.get("use_personhood_prompt", self.var_mem_personhood.get())))
        self.var_jitd_enabled.set(bool(data.get("jitd_enabled", self.var_jitd_enabled.get())))
        self.var_jitd_mode.set(str(data.get("jitd_mode", self.var_jitd_mode.get()) or "clone").strip().lower())
        self.var_jitd_speaker.set(str(data.get("jitd_speaker", self.var_jitd_speaker.get()) or "Ryan").strip())
        self.var_jitd_ref_index.set(int(data.get("jitd_ref_index", self.var_jitd_ref_index.get())))
        self._refresh_jitd_ref_choices()
        self._apply_greeting_from_settings(data)
        self._apply_heartbeat_from_settings(data)
        geom = data.get("geometry")
        if isinstance(geom, str) and "x" in geom:
            try:
                self.geometry(geom)
            except Exception:
                pass
        self._apply_settings()
        if not silent:
            self.log(f"Settings loaded from {SETTINGS_PATH.name}.")
        return True

    # --------- callbacks from runner --------
    def log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str):
        self.status_var.set(text)
        self.log(text)

    def set_voice_state_from_bot(self, value: str):
        self.after(0, lambda: self.var_voice_status.set(value))

    def set_voice_queue_from_bot(self, payload: dict):
        def _do():
            guild_name = str(payload.get("active_guild_name") or "").strip()
            channel_name = str(payload.get("channel_name") or "").strip()
            connected = bool(payload.get("connected"))
            paused = bool(payload.get("paused"))
            now_playing = str(payload.get("now_playing") or "").strip() or "Nothing playing."
            if paused and now_playing != "Nothing playing.":
                now_playing = f"Paused: {now_playing}"

            if guild_name:
                summary = guild_name
                if channel_name:
                    summary = f"{summary} / {channel_name}"
                if connected:
                    summary += " (paused)" if paused else " (connected)"
            else:
                summary = "No active voice server."

            self.var_voice_now_playing.set(now_playing)
            self.var_voice_queue_summary.set(summary)
            self.voice_queue_list.delete(0, "end")
            queue_items = list(payload.get("queue") or [])
            if queue_items:
                for idx, item in enumerate(queue_items, 1):
                    self.voice_queue_list.insert("end", f"{idx}. {item}")
            else:
                self.voice_queue_list.insert("end", "(queue empty)")

        self.after(0, _do)

    def set_low_from_bot(self, value: str):
        def _do():
            self.var_low.set(value)
            self.log("Community prompt synced from chat.")
            self.save_settings(quiet=True)
        self.after(0, _do)

    def persist_settings_from_bot(self):
        self.after(0, lambda: self.save_settings(quiet=True))

    # --------- AUTOSAVE + FILE WATCH --------
    def _setup_autosave(self):
        for v in (self.var_token, self.var_url, self.var_model, self.var_sys, self.var_low):
            v.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_max.trace_add("write", lambda *args: self._debounced_autosave())
        for v in (self.var_greet_guild, self.var_greet_channel, self.var_greet_template, self.var_greet_cooldown):
            v.trace_add("write", lambda *args: self._debounced_autosave())
        for v in (self.var_mem_dsn, self.var_mem_hydrate_limit):
            v.trace_add("write", lambda *args: self._debounced_autosave())
        for v in (
            self.var_hb_home_guild,
            self.var_hb_server_channel,
            self.var_hb_quiet_start,
            self.var_hb_quiet_end,
            self.var_hb_max_posts_per_hour,
            self.var_hb_max_posts_per_day,
            self.var_hb_min_recent_human_messages,
            self.var_hb_dead_channel_threshold_minutes,
        ):
            v.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_hb_enabled.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_hb_shadow_mode.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_hb_manual_approval.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_hb_server_enabled.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_hb_server_typing.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_hb_require_reply_context.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_jitd_enabled.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_jitd_mode.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_jitd_speaker.trace_add("write", lambda *args: self._debounced_autosave())
        self.var_jitd_ref_index.trace_add("write", lambda *args: self._debounced_autosave())

    def _debounced_autosave(self, delay_ms: int = 600):
        if self._autosave_job is not None:
            try:
                self.after_cancel(self._autosave_job)
            except Exception:
                pass
        self._autosave_job = self.after(delay_ms, self._do_autosave)

    def _do_autosave(self):
        self._autosave_job = None
        self._apply_settings()
        self.save_settings(quiet=True)

    def _on_configure(self, event):
        if event.widget is self:
            self._debounced_autosave(900)

    def _watch_settings_file(self):
        try:
            if SETTINGS_PATH.exists():
                mtime = SETTINGS_PATH.stat().st_mtime
                if mtime != self._settings_last_mtime and time.time() > self._write_guard_until:
                    self._settings_last_mtime = mtime
                    self._load_settings_from_disk(silent=False)
        except Exception as e:
            self.log(f"Watcher error: {e}")
        finally:
            self.after(1200, self._watch_settings_file)


if __name__ == "__main__":
    _hide_console_if_present()
    app = App()
    app.mainloop()
