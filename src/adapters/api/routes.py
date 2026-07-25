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
from src.adapters.session.config_provider import StaticSessionCookieProvider, COOKIE_NAME
from src.adapters.backend.hyperagent_client import HyperagentClient
from src.services.chat_service import ChatService
from src.services import accounts
from src.core.logging_config import get_logger

logger = get_logger("api")


def _build_cookie_provider():
    """Build the browserless session provider from config."""
    sessions = config.load_sessions()
    if not sessions:
        logger.warning("No sessions configured — add them to config.yaml or set HYPERAGENT_SESSION.")
    return StaticSessionCookieProvider(sessions)


# Initialize services
cookie_provider = _build_cookie_provider()
chat_backend = HyperagentClient()
chat_service = ChatService(cookie_provider, chat_backend)

app = FastAPI(title="Hyperagent Local API Proxy")


@app.on_event("startup")
async def _verify_configured_sessions():
    sessions = config.load_sessions()
    if not sessions:
        logger.warning("No sessions configured — add 'sessions:' to config.yaml or set HYPERAGENT_SESSION.")
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


@app.get("/api/live-status")
async def live_status():
    """Returns real-time runtime activity, active streams, sessions status, and recent request logs."""
    summary = STATS.summary()
    recent = STATS.recent()
    sessions_info = cookie_provider.list_status() if hasattr(cookie_provider, "list_status") else []
    
    # Enrich session info with email/name if available
    tokens = config.load_sessions()
    if tokens:
        verified = await accounts.verify_all(tokens)
        for s in sessions_info:
            tok_masked = s.get("token")
            match = next((v for v in verified if v.get("session") == tok_masked), None)
            if match and match.get("valid"):
                s["email"] = match.get("email")
                s["name"] = match.get("name")

    return {
        "status": "online",
        "uptime_seconds": summary["uptime_seconds"],
        "requests_total": summary["requests_total"],
        "requests_error": summary["requests_error"],
        "streams_active": summary["streams_active"],
        "tokens_prompt": summary["tokens_prompt"],
        "tokens_completion": summary["tokens_completion"],
        "sessions": sessions_info,
        "anti_detection": {
            "tls_impersonate": config.TLS_IMPERSONATE,
            "ua_rotation": config.ENABLE_UA_ROTATION,
            "human_jitter": config.ENABLE_HUMAN_JITTER,
            "session_cooldown": config.ENABLE_SESSION_COOLDOWN,
        },
        "recent_requests": recent[:15],
    }


@app.get("/", response_class=HTMLResponse)
async def root():
    """Renders real-time mini monitoring panel."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>hyperag2api — Live Monitor</title>
    <style>
        :root {
            --bg: #09090b;
            --card-bg: #121215;
            --border: #27272a;
            --text: #f4f4f5;
            --muted: #a1a1aa;
            --accent: #3b82f6;
            --success: #22c55e;
            --warning: #eab308;
            --danger: #ef4444;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg); color: var(--text); padding: 1.5rem;
            min-height: 100vh; display: flex; flex-direction: column; align-items: center;
        }
        .app { width: 100%; max-width: 900px; display: flex; flex-direction: column; gap: 1.5rem; }
        .header {
            display: flex; justify-content: space-between; align-items: center;
            background: var(--card-bg); border: 1px solid var(--border);
            padding: 1.25rem 1.5rem; border-radius: 12px;
        }
        .brand { display: flex; align-items: center; gap: 10px; }
        .brand h1 { font-size: 1.25rem; font-weight: 700; color: var(--text); }
        .badge-online {
            display: inline-flex; align-items: center; gap: 6px;
            background: rgba(34, 197, 94, 0.1); color: var(--success);
            padding: 4px 10px; border-radius: 9999px; font-size: 0.8rem; font-weight: 600;
            border: 1px solid rgba(34, 197, 94, 0.2);
        }
        .pulse { width: 8px; height: 8px; background: var(--success); border-radius: 50%; animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(1.2); } }
        
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }
        .card {
            background: var(--card-bg); border: 1px solid var(--border);
            padding: 1.25rem; border-radius: 12px; display: flex; flex-direction: column; gap: 6px;
        }
        .card-label { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
        .card-val { font-size: 1.6rem; font-weight: 700; color: var(--text); }
        .card-sub { font-size: 0.8rem; color: var(--muted); }

        .section-title { font-size: 1rem; font-weight: 600; color: var(--text); margin-bottom: 0.75rem; display: flex; align-items: center; justify-content: space-between; }
        
        .panel { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 1.25rem; }
        
        table { width: 100%; border-collapse: collapse; text-align: left; font-size: 0.875rem; }
        th { color: var(--muted); font-weight: 600; padding: 8px 12px; border-bottom: 1px solid var(--border); }
        td { padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.03); color: var(--text); }
        tr:last-child td { border-bottom: none; }
        
        .tag {
            display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; font-family: monospace;
        }
        .tag-active { background: rgba(34, 197, 94, 0.15); color: var(--success); }
        .tag-quarantine { background: rgba(239, 68, 68, 0.15); color: var(--danger); }
        .tag-running { background: rgba(59, 130, 246, 0.15); color: var(--accent); }
        .tag-ok { background: rgba(34, 197, 94, 0.15); color: var(--success); }
        .tag-err { background: rgba(239, 68, 68, 0.15); color: var(--danger); }
        
        .protection-pills { display: flex; gap: 8px; flex-wrap: wrap; }
        .pill { background: #1c1c21; border: 1px solid var(--border); padding: 6px 12px; border-radius: 8px; font-size: 0.8rem; color: var(--muted); }
        .pill span { color: var(--success); font-weight: 600; }
        
        .empty { color: var(--muted); font-style: italic; text-align: center; padding: 1.5rem; }
    </style>
</head>
<body>
    <div class="app">
        <div class="header">
            <div class="brand">
                <div class="badge-online"><span class="pulse"></span> LIVE</div>
                <h1>hyperag2api Proxy</h1>
            </div>
            <div class="protection-pills">
                <div class="pill">TLS: <span id="p-tls">Chrome 124</span></div>
                <div class="pill">UA Rotation: <span id="p-ua">Active</span></div>
                <div class="pill">Jitter: <span id="p-jit">Active</span></div>
                <div class="pill">Circuit Breaker: <span id="p-cb">Active</span></div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <div class="card-label">Active Streams</div>
                <div class="card-val" id="v-streams">0</div>
                <div class="card-sub">Real-time LLM stream sessions</div>
            </div>
            <div class="card">
                <div class="card-label">Total Requests</div>
                <div class="card-val" id="v-total">0</div>
                <div class="card-sub"><span id="v-err">0</span> errors</div>
            </div>
            <div class="card">
                <div class="card-label">Tokens Processed</div>
                <div class="card-val" id="v-tokens">0</div>
                <div class="card-sub"><span id="v-prompt">0</span> in / <span id="v-comp">0</span> out</div>
            </div>
            <div class="card">
                <div class="card-label">Uptime</div>
                <div class="card-val" id="v-uptime">0s</div>
                <div class="card-sub">Proxy server uptime</div>
            </div>
        </div>

        <div class="panel">
            <div class="section-title">
                <span>🔑 Sessions & Circuit Breaker Status</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Session Token</th>
                        <th>Account</th>
                        <th>Status</th>
                        <th>Current Active</th>
                    </tr>
                </thead>
                <tbody id="tb-sessions">
                    <tr><td colspan="5" class="empty">Loading session status...</td></tr>
                </tbody>
            </table>
        </div>

        <div class="panel">
            <div class="section-title">
                <span>⚡ Live Activity Log (Recent Requests)</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Time</th>
                        <th>Model</th>
                        <th>Mode</th>
                        <th>Thread ID</th>
                        <th>Latency</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="tb-requests">
                    <tr><td colspan="6" class="empty">No recent requests yet. Make a request to see live activity.</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        async function update() {
            try {
                const res = await fetch('/api/live-status');
                const data = await res.json();

                document.getElementById('v-streams').innerText = data.streams_active;
                document.getElementById('v-total').innerText = data.requests_total;
                document.getElementById('v-err').innerText = data.requests_error;
                document.getElementById('v-tokens').innerText = (data.tokens_prompt + data.tokens_completion).toLocaleString();
                document.getElementById('v-prompt').innerText = data.tokens_prompt.toLocaleString();
                document.getElementById('v-comp').innerText = data.tokens_completion.toLocaleString();

                const sec = Math.floor(data.uptime_seconds);
                const h = Math.floor(sec / 3600);
                const m = Math.floor((sec % 3600) / 60);
                const s = sec % 60;
                document.getElementById('v-uptime').innerText = h > 0 ? `${h}h ${m}m` : (m > 0 ? `${m}m ${s}s` : `${s}s`);

                document.getElementById('p-tls').innerText = data.anti_detection.tls_impersonate;
                document.getElementById('p-ua').innerText = data.anti_detection.ua_rotation ? "ON" : "OFF";
                document.getElementById('p-jit').innerText = data.anti_detection.human_jitter ? "ON" : "OFF";
                document.getElementById('p-cb').innerText = data.anti_detection.session_cooldown ? "ON" : "OFF";

                // Sessions table
                const sBody = document.getElementById('tb-sessions');
                if (data.sessions && data.sessions.length > 0) {
                    sBody.innerHTML = data.sessions.map(s => {
                        const acc = s.email ? `<b>${s.email}</b>${s.name ? ' (' + s.name + ')' : ''}` : '—';
                        return `
                            <tr>
                                <td>#${s.index}</td>
                                <td><code>${s.token}</code></td>
                                <td>${acc}</td>
                                <td>
                                    <span class="tag ${s.status === 'active' ? 'tag-active' : 'tag-quarantine'}">
                                        ${s.status.toUpperCase()} ${s.cooldown_remaining > 0 ? '(' + s.cooldown_remaining + 's cooldown)' : ''}
                                    </span>
                                </td>
                                <td>${s.is_current ? '🟢 Active Routing' : '—'}</td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    sBody.innerHTML = '<tr><td colspan="5" class="empty">No sessions configured</td></tr>';
                }

                // Requests table
                const rBody = document.getElementById('tb-requests');
                if (data.recent_requests && data.recent_requests.length > 0) {
                    rBody.innerHTML = data.recent_requests.map(r => {
                        const time = new Date(r.ts * 1000).toLocaleTimeString();
                        const mode = r.stream ? 'STREAM' : 'SYNC';
                        const statusClass = r.status === 'ok' ? 'tag-ok' : (r.status === 'running' ? 'tag-running' : 'tag-err');
                        const thread = r.thread_id ? `<code>${r.thread_id.substring(0, 12)}...</code>` : '—';
                        const lat = r.latency_ms !== null ? `${r.latency_ms} ms` : '...';
                        return `
                            <tr>
                                <td>${time}</td>
                                <td><b>${r.model}</b></td>
                                <td><span class="tag pill">${mode}</span></td>
                                <td>${thread}</td>
                                <td>${lat}</td>
                                <td><span class="tag ${statusClass}">${r.status.toUpperCase()}</span></td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    rBody.innerHTML = '<tr><td colspan="6" class="empty">No recent requests yet. Make a request to see live activity.</td></tr>';
                }
            } catch (err) {
                console.error("Live monitor update error:", err);
            }
        }

        setInterval(update, 1000);
        update();
    </script>
</body>
</html>
    """


@app.get("/health")
async def health():
    """Readiness probe: sessions configured + current account reachability."""
    sessions = config.load_sessions()
    session_ok = False
    detail = ""
    account = None
    try:
        cookies = await cookie_provider.get_cookies()
        token = cookies.get(COOKIE_NAME, "")
        if token:
            info = await accounts.verify_session(token)
            session_ok = bool(info.get("valid"))
            if session_ok:
                account = {"email": info.get("email"), "name": info.get("name")}
            else:
                detail = f"Session invalid ({info.get('status') or info.get('error')})."
    except Exception as exc:
        detail = str(exc)
    status = "ok" if session_ok else "degraded"
    return JSONResponse(
        status_code=200 if status == "ok" else 503,
        content={
            "status": status,
            "sessions_configured": len(sessions),
            "session_valid": session_ok,
            "account": account,
            "detail": detail,
            **STATS.summary(),
        },
    )



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


