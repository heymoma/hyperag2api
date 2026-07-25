"""Central configuration for the Hyperagent Local Proxy.

Settings resolve with the precedence **defaults < config file (YAML) < env vars**,
so you can keep everything in a ``config.yaml`` and still override any value with
an environment variable (handy for Docker / CI). The config file path is
``$CONFIG_FILE`` or ``config.yaml`` / ``config.yml`` in the working directory.

Values are deliberately plain module-level constants rather than a settings
object: the rest of the codebase reads them as ``config.NAME`` at call time, so a
deployment can override one at startup and tests can patch one in place.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Config file (YAML) loading                                                   #
# --------------------------------------------------------------------------- #
def _load_config_file() -> Dict[str, Any]:
    path = os.environ.get("CONFIG_FILE") or next(
        (p for p in ("config.yaml", "config.yml") if os.path.exists(p)), ""
    )
    if not path or not os.path.exists(path):
        return {}
    try:
        import yaml  # PyYAML

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_FILE: Dict[str, Any] = _load_config_file()


def _lookup(name: str) -> Any:
    """Value for ``name`` from env (highest priority) then the config file.

    File keys may be given as the exact env name or its lowercase form, so both
    ``PORT`` and ``port`` work in YAML.
    """
    ev = os.environ.get(name)
    if ev is not None and ev != "":
        return ev
    for key in (name, name.lower()):
        if key in _FILE and _FILE[key] not in (None, ""):
            return _FILE[key]
    return None


# --------------------------------------------------------------------------- #
# Small typed helpers (env + config file aware)                                #
# --------------------------------------------------------------------------- #
def env_flag(name: str, default: bool = False) -> bool:
    val = _lookup(name)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    val = _lookup(name)
    try:
        return int(str(val).strip())
    except (ValueError, AttributeError, TypeError):
        return default


def env_float(name: str, default: float) -> float:
    val = _lookup(name)
    try:
        return float(str(val).strip())
    except (ValueError, AttributeError, TypeError):
        return default


def env_str(name: str, default: str) -> str:
    val = _lookup(name)
    return str(val) if val is not None and val != "" else default


# --------------------------------------------------------------------------- #
# API server                                                                   #
# --------------------------------------------------------------------------- #
PROXY_API_KEY = env_str("PROXY_API_KEY", "")
HOST = env_str("HOST", "127.0.0.1")
PORT = env_int("PORT", 8000)

# Header a client can send to explicitly pin a conversation to one thread.
SESSION_HEADER = env_str("SESSION_HEADER", "X-Session-Id")


# --------------------------------------------------------------------------- #
# Target URLs                                                                  #
# --------------------------------------------------------------------------- #
HYPERAGENT_BASE_URL = env_str("HYPERAGENT_BASE_URL", "https://hyperagent.com")
HYPERAGENT_THREADS_API = f"{HYPERAGENT_BASE_URL}/api/threads"
HYPERAGENT_CHAT_API_TEMPLATE = f"{HYPERAGENT_BASE_URL}/api/threads/{{thread_id}}/chat"
HYPERAGENT_WARM_API_TEMPLATE = f"{HYPERAGENT_BASE_URL}/api/threads/{{thread_id}}/warm"
HYPERAGENT_INTERRUPT_API_TEMPLATE = f"{HYPERAGENT_BASE_URL}/api/threads/{{thread_id}}/interrupt"
# Verified live: returns the account (userId, email, name, ...) for a valid session.
HYPERAGENT_AUTH_ME_API = f"{HYPERAGENT_BASE_URL}/api/auth/me"
# Verified via captured web traffic — presigned upload flow:
#   POST /api/uploads {filename, mimeType, sizeBytes} -> {fileId, uploadUrl}
#   PUT <uploadUrl> (raw bytes to S3); then reference fileId in chat "attachmentIds".
HYPERAGENT_UPLOADS_API = f"{HYPERAGENT_BASE_URL}/api/uploads"
# Attachment upload (verified live): POST JSON {filename, mimeType, size, content(base64)}
# -> {success, fileId, url, ...}. The thread-scoped attachments route takes
# {files:[{name, size, mimeType, base64}]} and binds the file to the thread.
HYPERAGENT_UPLOAD_API = f"{HYPERAGENT_BASE_URL}/api/files/upload"
HYPERAGENT_ATTACHMENTS_API_TEMPLATE = f"{HYPERAGENT_BASE_URL}/api/threads/{{thread_id}}/attachments"

# The cookie Hyperagent authenticates browser sessions with.
SESSION_COOKIE_NAME = "__Host-hyperagent_session"


# --------------------------------------------------------------------------- #
# HTTP client tuning                                                           #
# --------------------------------------------------------------------------- #
HTTP_CONNECT_TIMEOUT = env_float("HTTP_CONNECT_TIMEOUT", 10.0)
HTTP_WRITE_TIMEOUT = env_float("HTTP_WRITE_TIMEOUT", 30.0)
HTTP_POOL_TIMEOUT = env_float("HTTP_POOL_TIMEOUT", 10.0)
# Short read timeout for one-shot requests (create/warm), long one for streams.
REQUEST_READ_TIMEOUT = env_float("REQUEST_READ_TIMEOUT", 60.0)
STREAM_READ_TIMEOUT = env_float("STREAM_READ_TIMEOUT", 300.0)

MAX_RETRIES = env_int("MAX_RETRIES", 3)
RETRY_BASE_DELAY = env_float("RETRY_BASE_DELAY", 0.5)
RETRY_MAX_DELAY = env_float("RETRY_MAX_DELAY", 8.0)

# Emit an SSE keepalive comment every N seconds of silence (0 disables).
KEEPALIVE_INTERVAL = env_float("KEEPALIVE_INTERVAL", 15.0)


# --------------------------------------------------------------------------- #
# Session → thread mapping                                                     #
# --------------------------------------------------------------------------- #
SESSION_PERSIST = env_flag("SESSION_PERSIST", True)
SESSION_DB_PATH = env_str("SESSION_DB_PATH", "sessions.db")
SESSION_TTL_SECONDS = env_int("SESSION_TTL_SECONDS", 60 * 60 * 6)  # 6h
SESSION_MAX = env_int("SESSION_MAX", 1000)

# How long an /api/auth/me account lookup stays fresh. The dashboard polls every
# second; without this it would hammer the upstream auth endpoint.
ACCOUNT_CACHE_TTL = env_float("ACCOUNT_CACHE_TTL", 300.0)


# --------------------------------------------------------------------------- #
# Behaviour / feature switches                                                 #
# --------------------------------------------------------------------------- #
# reasoning_content (native OpenAI field) | think_tags (<think>..</think> inline) | both
REASONING_STYLE = env_str("REASONING_STYLE", "reasoning_content").strip().lower()

# Force Hyperagent "plan mode" on every request. Default OFF: plan mode makes the
# model stop and propose a plan instead of doing the work, which is wrong for
# coding clients. AskQuestion/steering is still detected regardless.
INJECT_PLAN_MODE = env_flag("INJECT_PLAN_MODE", False)

# How backend tool activity is surfaced: content (readable text, default) |
# openai (real delta.tool_calls) | off.
TOOLCALL_MODE = env_str("TOOLCALL_MODE", "content").strip().lower()

# Whether to accept image_url content parts and upload them to Hyperagent.
ENABLE_MULTIMODAL = env_flag("ENABLE_MULTIMODAL", True)

# Keep Hyperagent's OWN server-side MCP/integrations and tools out of the loop so
# the server agent stays a "pure brain" and delegates to the client's tools.
# enabledIntegrations is always empty; this additionally forces every server tool
# flag off while a client-side tool-calling turn is in flight.
DISABLE_SERVER_MCP = env_flag("DISABLE_SERVER_MCP", True)

# Approximate usage token accounting in responses.
ENABLE_USAGE = env_flag("ENABLE_USAGE", True)

SEARCH_MODE = env_str("SEARCH_MODE", "exa")

# Low-latency / lean mode: keep the server agent from doing slow (and costly)
# work — force every server-side capability + plan mode OFF for ALL requests
# unless explicitly turned off. Measured: heavy server tools add seconds per
# request. To use Hyperagent's own server-side tools, set LOW_LATENCY_MODE=0
# and enable the specific ENABLE_* flags you want.
LOW_LATENCY_MODE = env_flag("LOW_LATENCY_MODE", True)


# --------------------------------------------------------------------------- #
# Anti-detection and browser impersonation                                     #
# --------------------------------------------------------------------------- #
TLS_IMPERSONATE = env_str("TLS_IMPERSONATE", "chrome124")
ENABLE_TLS_FINGERPRINT = env_flag("ENABLE_TLS_FINGERPRINT", True)
ENABLE_HUMAN_JITTER = env_flag("ENABLE_HUMAN_JITTER", True)
JITTER_MIN_MS = env_float("JITTER_MIN_MS", 150.0)
JITTER_MAX_MS = env_float("JITTER_MAX_MS", 600.0)
ENABLE_UA_ROTATION = env_flag("ENABLE_UA_ROTATION", True)
ENABLE_SESSION_COOLDOWN = env_flag("ENABLE_SESSION_COOLDOWN", True)
COOLDOWN_SECONDS = env_float("COOLDOWN_SECONDS", 600.0)


# --------------------------------------------------------------------------- #
# Default network headers                                                      #
# --------------------------------------------------------------------------- #
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    ),
    "Content-Type": "application/json",
    "Referer": f"{HYPERAGENT_BASE_URL}/threads/new",
    "Origin": HYPERAGENT_BASE_URL,
}


# --------------------------------------------------------------------------- #
# Chat feature flags (payload sent to /chat)                                   #
# --------------------------------------------------------------------------- #
# Every capability defaults OFF but can be flipped on per-deployment through the
# env, e.g. ENABLE_WEB_SEARCH=1 (with LOW_LATENCY_MODE=0).
_CHAT_FLAG_ENV = {
    "enableExecuteScript": "ENABLE_EXECUTE_SCRIPT",
    "enablePersistentSandbox": "ENABLE_PERSISTENT_SANDBOX",
    "enableWebpage": "ENABLE_WEBPAGE",
    "enableSlides": "ENABLE_SLIDES",
    "tablesEnabled": "ENABLE_TABLES",
    "enableWebSearch": "ENABLE_WEB_SEARCH",
    "enableBrowser": "ENABLE_BROWSER",
    "enableImageGeneration": "ENABLE_IMAGE_GENERATION",
    "enableVideoGeneration": "ENABLE_VIDEO_GENERATION",
    "enableAudioGeneration": "ENABLE_AUDIO_GENERATION",
    "enableTranscription": "ENABLE_TRANSCRIPTION",
    "enableAvatarVideo": "ENABLE_AVATAR_VIDEO",
    "enableExaFindSimilar": "ENABLE_EXA_FIND_SIMILAR",
    "enableExaAnswer": "ENABLE_EXA_ANSWER",
    "enableExaResearch": "ENABLE_EXA_RESEARCH",
    "enableExaWebsets": "ENABLE_EXA_WEBSETS",
    "enableGeoTools": "ENABLE_GEO_TOOLS",
    "hyperAppsEnabled": "ENABLE_HYPERAPPS",
    "documentsEnabled": "ENABLE_DOCUMENTS",
    "enableThreadSearch": "ENABLE_THREAD_SEARCH",
    "residentialProxyEnabled": "ENABLE_RESIDENTIAL_PROXY",
    "solveCaptchasEnabled": "ENABLE_SOLVE_CAPTCHAS",
    "globalTablesEnabled": "ENABLE_GLOBAL_TABLES",
}


def _flag_envelope(flags: Dict[str, Any], inject_plan_mode: bool) -> Dict[str, Any]:
    flags["searchMode"] = SEARCH_MODE
    flags["integrationMode"] = env_str("INTEGRATION_MODE", "open")
    flags["injectPlanMode"] = inject_plan_mode
    return flags


def get_chat_feature_flags() -> Dict[str, Any]:
    """Build the capability-flag portion of the /chat payload from the env.

    In LOW_LATENCY_MODE (default) every server-side capability and plan mode are
    forced off for the leanest, fastest (and cheapest) agent response.
    """
    if LOW_LATENCY_MODE:
        return server_tools_off_flags()
    flags = {key: env_flag(env_name, False) for key, env_name in _CHAT_FLAG_ENV.items()}
    return _flag_envelope(flags, INJECT_PLAN_MODE)


def server_tools_off_flags() -> Dict[str, Any]:
    """All server-side capabilities OFF — used during client tool-calling turns
    so the server agent never runs its own tools/MCP and just delegates to the
    client. (enabledIntegrations is emptied separately in the payload builder.)"""
    return _flag_envelope({key: False for key in _CHAT_FLAG_ENV}, inject_plan_mode=False)


# --------------------------------------------------------------------------- #
# OpenAI → Hyperagent model mapping                                            #
# --------------------------------------------------------------------------- #
MODEL_MAPPING = {
    # OpenAI general fallbacks
    "gpt-4o": "gpt-5.6-sol",
    "gpt-4": "gpt-5.6-sol",
    "claude-3-opus": "opus-latest",
    "claude-3-5-sonnet": "sonnet-5",
    "gemini-1.5-flash": "gemini-3.5-flash",
    "deepseek-chat": "deepseek-v4-pro",

    # Mapped directly to verified internal modelIds
    "fable": "fable",
    "fable-5": "fable",
    "opus-latest": "opus-latest",
    "opus-4.8": "opus-latest",
    "opus-4": "opus-4",
    "sonnet-latest": "sonnet-latest",
    "sonnet-5": "sonnet-5",
    "sonnet-4": "sonnet-4",
    "haiku-4": "haiku-4",

    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4": "gpt-5.4",

    "kimi-k2.6": "kimi-k2.6",
    "glm-5.2-fast": "glm-5.2-fast",
    "glm-5.2": "glm-5.2",
    "qwen3.7-plus": "qwen3.7-plus",
    "qwen-3.7-plus": "qwen3.7-plus",
    "gemini-3.5-flash": "gemini-3.5-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "fugu-ultra": "fugu-ultra",
    "grok-4.5": "grok-4.5",
}

DEFAULT_MODEL = env_str("DEFAULT_MODEL", "opus-latest")

# Character used to pin a named session inside the model id, e.g. "opus-4.8@proj".
SESSION_SUFFIX_SEP = "@"


def resolve_model(model: str) -> Tuple[str, Optional[str]]:
    """Split an incoming model id into (hyperagent_model_id, explicit_session_id).

    Supports named sessions via a suffix: ``opus-4.8@my-project`` pins the
    conversation to a stable session key regardless of message history.
    """
    raw = (model or "").strip()
    session_id: Optional[str] = None
    if SESSION_SUFFIX_SEP in raw:
        base, _, suffix = raw.partition(SESSION_SUFFIX_SEP)
        raw = base.strip()
        session_id = suffix.strip() or None
    return MODEL_MAPPING.get(raw.lower(), DEFAULT_MODEL), session_id


# --------------------------------------------------------------------------- #
# Sessions — config-provided token(s)                                          #
# --------------------------------------------------------------------------- #
SESSIONS_FILE = env_str("SESSIONS_FILE", "")


def _tokens_from_env() -> List[str]:
    tokens: List[str] = []
    single = os.environ.get("HYPERAGENT_SESSION", "").strip()
    if single:
        tokens.append(single)
    for token in os.environ.get("HYPERAGENT_SESSIONS", "").split(","):
        token = token.strip()
        if token:
            tokens.append(token)
    return tokens


def _tokens_from_config_file() -> List[str]:
    entries = _FILE.get("sessions")
    if not isinstance(entries, list):
        return []
    return [t.strip() for t in entries if isinstance(t, str) and t.strip()]


def _tokens_from_file(path: str) -> List[str]:
    """Read tokens from a JSON array or a one-per-line file (``#`` comments)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:  # pragma: no cover - defensive
        return []

    if raw.startswith("["):
        try:
            return [t.strip() for t in json.loads(raw) if isinstance(t, str) and t.strip()]
        except ValueError:  # pragma: no cover - defensive
            return []

    return [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load_sessions() -> List[str]:
    """Collect Hyperagent session token(s), merged and de-duplicated (order kept).

    Sources, in order:
    - ``HYPERAGENT_SESSION``  — a single ``__Host-hyperagent_session`` value
    - ``HYPERAGENT_SESSIONS`` — comma-separated values (multiple accounts)
    - config file ``sessions:`` — a YAML list of tokens
    - ``SESSIONS_FILE`` / ``sessions.txt`` — one token per line, or a JSON array
    """
    tokens = _tokens_from_env() + _tokens_from_config_file()

    session_file = SESSIONS_FILE or ("sessions.txt" if os.path.exists("sessions.txt") else "")
    if session_file and os.path.exists(session_file):
        tokens += _tokens_from_file(session_file)

    seen, unique = set(), []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique
