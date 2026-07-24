import time
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse

from src.core import config
from src.core.config import PROXY_API_KEY, SESSION_HEADER
from src.core.models import estimate_tokens
from src.core.stats import STATS
from src.adapters.api.schemas import ChatCompletionRequest
from src.adapters.api.dashboard import DASHBOARD_HTML
from src.adapters.session.config_provider import StaticSessionCookieProvider, COOKIE_NAME
from src.adapters.backend.hyperagent_client import HyperagentClient
from src.services.chat_service import ChatService
from src.services import accounts
from src.core.logging_config import get_logger

logger = get_logger("api")


def _build_cookie_provider():
    """Pick the auth source: config-provided sessions (browserless) or the
    browser over CDP. Playwright is imported lazily so a browserless install
    (no Playwright) works fine in config mode."""
    sessions = config.load_sessions()
    mode = config.SESSION_MODE
    if mode != "browser" and (mode == "config" or sessions):
        if not sessions:
            logger.error("SESSION_MODE=config but no sessions configured.")
        return StaticSessionCookieProvider(sessions)
    try:
        from src.adapters.browser.playwright_cdp import PlaywrightCDPCookieProvider
        return PlaywrightCDPCookieProvider()
    except Exception as exc:  # pragma: no cover - only when playwright missing
        logger.error(
            "Browser (Playwright) provider unavailable: %s. Configure "
            "HYPERAGENT_SESSION to run browserless.", exc,
        )
        return StaticSessionCookieProvider([])


# Initialize services
cookie_provider = _build_cookie_provider()
chat_backend = HyperagentClient()
chat_service = ChatService(cookie_provider, chat_backend)

app = FastAPI(title="Hyperagent Local API Proxy")


@app.on_event("startup")
async def _verify_configured_sessions():
    sessions = config.load_sessions()
    if not sessions:
        return
    for info in await accounts.verify_all(sessions):
        if info.get("valid"):
            logger.info("Session %s OK — %s <%s>", info["session"], info.get("name"), info.get("email"))
        else:
            logger.warning("Session %s INVALID (%s)", info.get("session"),
                           info.get("status") or info.get("error"))


async def verify_api_key(authorization: Optional[str] = Header(None)):
    """Verifies proxy API key if one is configured in the environment."""
    if PROXY_API_KEY:
        if not authorization:
            raise HTTPException(status_code=401, detail="Unauthorized: Missing API Key")
        token = authorization.replace("Bearer ", "").strip()
        if token != PROXY_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid API Key")


@app.get("/", response_class=HTMLResponse)
async def root():
    """Renders basic proxy documentation home page."""
    return """
    <html>
        <head>
            <title>Hyperagent Local Proxy</title>
            <style>
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    background: #0f0f11; color: #e4e4e7;
                    display: flex; align-items: center; justify-content: center;
                    min-height: 100vh; margin: 0;
                }
                .container {
                    text-align: center; padding: 2rem; border: 1px solid #27272a;
                    border-radius: 12px; background: #18181b; max-width: 480px;
                    box-shadow: 0 4px 20px rgba(0,0,0,0.5);
                }
                h1 { color: #3b82f6; margin-bottom: 0.5rem; }
                p { color: #a1a1aa; line-height: 1.5; }
                a { color: #60a5fa; text-decoration: none; font-weight: 500; }
                a:hover { text-decoration: underline; }
                .status {
                    display: inline-flex; align-items: center; gap: 6px;
                    background: #14532d; color: #4ade80; padding: 4px 10px;
                    border-radius: 9999px; font-size: 0.85rem; font-weight: 600;
                    margin-bottom: 1rem;
                }
                .dot { width: 8px; height: 8px; background: #4ade80; border-radius: 50%; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="status"><span class="dot"></span>Proxy Online</div>
                <h1>Hyperagent Local API Proxy</h1>
                <p>OpenAI-compatible local server forwarding requests to Hyperagent.</p>
                <p>Live Dashboard: <a href="/dashboard">/dashboard</a></p>
                <p>Interactive API Docs: <a href="/docs">/docs</a></p>
                <p>Models endpoint: <a href="/v1/models">/v1/models</a></p>
                <p>Chat Completions: <code>/v1/chat/completions</code></p>
            </div>
        </body>
    </html>
    """


@app.get("/health")
async def health():
    """Readiness probe: reports uptime and browser/cookie reachability."""
    browser_ok = False
    cookies_ok = False
    detail = ""
    try:
        cookies = await cookie_provider.get_cookies()
        browser_ok = True
        cookies_ok = bool(cookies)
        if not cookies_ok:
            detail = "Connected to browser but no Hyperagent cookies (not logged in?)."
    except Exception as exc:
        detail = str(exc)
    account = None
    if cookies_ok:
        try:
            token = cookies.get(COOKIE_NAME, "")
            if token:
                info = await accounts.verify_session(token)
                if info.get("valid"):
                    account = {"email": info.get("email"), "name": info.get("name")}
        except Exception:
            pass
    status = "ok" if (browser_ok and cookies_ok) else "degraded"
    code = 200 if status == "ok" else 503
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "auth_mode": config.SESSION_MODE,
            "browser_connected": browser_ok,
            "cookies_present": cookies_ok,
            "account": account,
            "detail": detail,
            **STATS.summary(),
        },
    )


@app.get("/accounts")
async def accounts_endpoint(dependencies=Depends(verify_api_key)):
    """Verify configured session(s) and show which account each belongs to."""
    sessions = config.load_sessions()
    if sessions:
        infos = await accounts.verify_all(sessions)
        return {"mode": config.SESSION_MODE, "count": len(infos),
                "accounts": infos, "balance_note": accounts.BALANCE_NOTE}
    # Browser mode: verify whatever the live session yields.
    try:
        cookies = await cookie_provider.get_cookies()
        token = cookies.get(COOKIE_NAME, "")
        info = await accounts.verify_session(token) if token else {"valid": False, "error": "no session cookie"}
    except Exception as exc:
        info = {"valid": False, "error": str(exc)[:120]}
    return {"mode": "browser", "count": 1, "accounts": [info], "balance_note": accounts.BALANCE_NOTE}


@app.get("/v1/models")
async def list_models(dependencies=Depends(verify_api_key)):
    """List available models in OpenAI format."""
    models_list = chat_service.get_available_models()
    return {"object": "list", "data": models_list}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    dependencies=Depends(verify_api_key),
    x_session_id: Optional[str] = Header(None, alias=SESSION_HEADER),
):
    """Handles chat completions requests (both streaming and non-streaming)."""
    request_id = f"req-{uuid.uuid4().hex[:12]}"
    logger.info(
        "[%s] completions model=%s stream=%s session_hdr=%s",
        request_id, req.model, req.stream, bool(x_session_id),
    )

    if req.stream:
        async def _wrapped():
            entry = STATS.request_started(request_id, req.model, True)
            meta: dict = {}
            status = "ok"
            err = None
            try:
                async for chunk in chat_service.execute_chat_stream(req, session_id=x_session_id, meta=meta):
                    yield chunk
            except Exception as e:  # noqa: BLE001
                status, err = "error", str(e)
                raise
            finally:
                completion = estimate_tokens(meta.get("completion_text", "")) or None
                STATS.request_finished(
                    entry,
                    status="error" if (err or meta.get("error")) else status,
                    prompt_tokens=meta.get("prompt_tokens"),
                    completion_tokens=meta.get("completion_tokens") or completion,
                    session_key=meta.get("session_key"),
                    thread_id=meta.get("thread_id"),
                    reused_thread=meta.get("reused_thread"),
                    error=err or meta.get("error"),
                )

        return StreamingResponse(_wrapped(), media_type="text/event-stream")

    entry = STATS.request_started(request_id, req.model, False)
    meta: dict = {}
    try:
        response = await chat_service.execute_chat_non_stream(req, session_id=x_session_id, meta=meta)
    except Exception as e:  # noqa: BLE001
        STATS.request_finished(entry, status="error", error=str(e))
        raise
    STATS.request_finished(
        entry,
        status="error" if meta.get("error") else "ok",
        prompt_tokens=meta.get("prompt_tokens"),
        completion_tokens=meta.get("completion_tokens"),
        session_key=meta.get("session_key"),
        thread_id=meta.get("thread_id"),
        reused_thread=meta.get("reused_thread"),
        error=meta.get("error"),
    )
    return response


@app.get("/api/stats")
async def api_stats():
    """JSON stats consumed by the dashboard (and any external monitor)."""
    return {
        "summary": STATS.summary(),
        "sessions": await chat_service.session_store.snapshot(),
        "recent": STATS.recent(),
        "server_time": time.time(),
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Live monitoring dashboard (polls /api/stats)."""
    return DASHBOARD_HTML
