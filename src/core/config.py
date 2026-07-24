"""Central configuration for the Hyperagent Local Proxy.

Everything here is driven by environment variables so the same code can run
under the interactive launcher, standalone, in Docker, or in tests. Import-time
constants keep backward compatibility with the original module (``PROXY_API_KEY``,
``HYPERAGENT_*_API``, ``CDP_*``, ``DEFAULT_HEADERS``, ``MODEL_MAPPING``); the new
settings are grouped into a small :class:`Settings` snapshot plus a few helpers.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- #
# Small env helpers                                                            #
# --------------------------------------------------------------------------- #
def env_flag(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (ValueError, AttributeError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except (ValueError, AttributeError):
        return default


def env_str(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val is not None and val != "" else default


# --------------------------------------------------------------------------- #
# API server                                                                   #
# --------------------------------------------------------------------------- #
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")

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
# Attachment upload (verified live): POST JSON {filename, mimeType, size, content(base64)}
# -> {success, fileId, url, ...}. The thread-scoped attachments route takes
# {files:[{name, size, mimeType, base64}]} and binds the file to the thread.
HYPERAGENT_UPLOAD_API = f"{HYPERAGENT_BASE_URL}/api/files/upload"
HYPERAGENT_ATTACHMENTS_API_TEMPLATE = f"{HYPERAGENT_BASE_URL}/api/threads/{{thread_id}}/attachments"


# --------------------------------------------------------------------------- #
# CDP / browser                                                                #
# --------------------------------------------------------------------------- #
CDP_PORT = env_int("CDP_PORT", 9222)
CDP_HOST = env_str("CDP_HOST", "localhost")
CDP_URL = f"http://{CDP_HOST}:{CDP_PORT}"

# How long a fetched cookie jar is reused before we re-read it over CDP.
COOKIE_TTL_SECONDS = env_float("COOKIE_TTL_SECONDS", 30.0)


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

# Approximate usage token accounting in responses.
ENABLE_USAGE = env_flag("ENABLE_USAGE", True)

SEARCH_MODE = env_str("SEARCH_MODE", "exa")


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
# Every capability defaults OFF (matching the original proxy) but can be flipped
# on per-deployment through the env, e.g. ENABLE_WEB_SEARCH=1.
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


def get_chat_feature_flags() -> Dict[str, Any]:
    """Build the capability-flag portion of the /chat payload from the env."""
    flags: Dict[str, Any] = {key: env_flag(env_name, False) for key, env_name in _CHAT_FLAG_ENV.items()}
    flags["searchMode"] = SEARCH_MODE
    flags["integrationMode"] = env_str("INTEGRATION_MODE", "open")
    flags["injectPlanMode"] = INJECT_PLAN_MODE
    return flags


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


def resolve_model(model: str) -> "tuple[str, Optional[str]]":
    """Split an incoming model id into (hyperagent_model_id, explicit_session_id).

    Supports named sessions via a suffix: ``opus-4.8@my-project`` pins the
    conversation to a stable session key regardless of message history.
    """
    raw = (model or "").strip()
    session_id: Optional[str] = None
    if SESSION_SUFFIX_SEP in raw:
        base, _, suffix = raw.partition(SESSION_SUFFIX_SEP)
        raw = base.strip()
        suffix = suffix.strip()
        if suffix:
            session_id = suffix
    mapped = MODEL_MAPPING.get(raw.lower(), DEFAULT_MODEL)
    return mapped, session_id
