# 🚀 hyperag2api

**English** · [Русский](README.ru.md)

<p align="center">
  <a href="https://hyperagent.com">
    <img src="https://img.shields.io/badge/Powered%20by-Hyperagent-0052FF?style=for-the-badge" alt="Powered by Hyperagent" />
  </a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue?style=for-the-badge" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License" />
</p>

---

**`hyperag2api`** is a high-performance, **browserless**, **OpenAI-compatible** proxy server for **[Hyperagent.com](https://hyperagent.com)**. It exposes a local `/v1/chat/completions` endpoint and forwards requests to Hyperagent's backend using a session token from your account (no browser, no Playwright) — so you can drive advanced models (*Claude Opus 4.8*, *GPT 5.6*, *Gemini 3.5*, *Grok 4.5*, and more) from **OpenCode**, **Continue**, **Cursor**, or any custom OpenAI client.

---

## ✨ Features

- 🧵 **Session-aware threads** — one Hyperagent thread per conversation, reused across turns. Instead of creating a brand-new thread (and re-sending the entire history) on every request, the proxy recognises follow-up turns and sends only the newest message. Optional **SQLite persistence** keeps the mapping across restarts, and **named sessions** let you pin a thread explicitly.
- 🧠 **Reasoning / thinking display** — model reasoning is streamed as OpenAI `reasoning_content` and/or inline `<think>…</think>` tags (`REASONING_STYLE`).
- 🛡️ **Stability** — automatic account rotation on 401/403, split connect/stream timeouts, exponential-backoff retries, resilient SSE parsing, and SSE keepalive heartbeats so long generations don't time out.
- 🔧 **Tool-call passthrough** — backend tool activity can surface as OpenAI `tool_calls` deltas (`TOOLCALL_MODE`).
- 🔌 **Client-side tool calling (MCP / functions)** — bridges the OpenAI function-calling loop over Hyperagent's text agent, so clients like **OpenCode** can use their **own** MCP servers/functions through the proxy. Accepts `tools`, makes the model emit `tool_calls` with `finish_reason: "tool_calls"`, and feeds your `role:"tool"` results back into the same thread. Verified live.
- 🖼️ **Multimodal input** — `image_url` parts are uploaded via Hyperagent's real presigned flow (`POST /api/uploads` → S3 `PUT` with `If-None-Match: *`) and referenced as `attachmentIds` (verified end-to-end: the model correctly describes uploaded images).
- 🔁 **Account rotation** — supply multiple sessions; the proxy rotates to the next account on a 401/403.
- 💳 **Account & balance** — `/accounts` verifies each session and shows the account + pay-as-you-go credit (initial / used / remaining).
- 📊 **Live dashboard** — a read-only monitor at `/dashboard` showing accounts + remaining balance, active sessions, recent requests, latency, and tokens.
- ❤️ **Health probe** — `/health` reports session validity and the current account.

> Plan mode is **off by default** (`INJECT_PLAN_MODE=0`): the model does the work instead of only proposing a plan. Steering/`AskQuestion` prompts are still detected and rendered.

---

## 📦 Setup — no browser required

Requires **Python 3.9+**. No Playwright, no Chromium — authentication uses a session token you paste into config.

```bash
pip install -r requirements.txt        # fastapi, uvicorn, httpx, pydantic, PyYAML
cp config.example.yaml config.yaml     # then edit it (see below)
python3 start.py                        # runs on http://127.0.0.1:8000
```

### 🔑 Get your session token
Grab the value of the **`__Host-hyperagent_session`** cookie from a browser where you're logged in to Hyperagent (DevTools → Application → Cookies → `hyperagent.com`). Paste it under `sessions:` in `config.yaml`. Add several tokens to rotate across accounts.

> Treat the token like a password. `config.yaml` and `sessions.txt` are git-ignored; tokens are only ever logged as a `…last4` fingerprint.

---

## ⚙️ Configuration

Settings resolve as **defaults < `config.yaml` < environment variables**. The file is read from `$CONFIG_FILE` or `./config.yaml` (`.yml`). See [`config.example.yaml`](config.example.yaml) for the full annotated template. Every key also has an env-var equivalent (upper-case), e.g. `port:` ↔ `PORT`, `low_latency_mode:` ↔ `LOW_LATENCY_MODE`.

```yaml
# config.yaml
sessions:
  - "your __Host-hyperagent_session value"
  # - "second account token"
port: 8000
proxy_api_key: ""            # if set, clients must send Authorization: Bearer <key>
rotate_by_balance: true      # prefer the account with the most remaining credit
low_latency_mode: true       # server-side tools/plan-mode off (fast + cheap)
```

### Environment variables (override the file)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PROXY_API_KEY` | *(empty)* | If set, clients must send `Authorization: Bearer <key>`. |
| `HOST` | `127.0.0.1` | Bind address for the proxy server. |
| `PORT` | `8000` | Port for the proxy server. |
| `HYPERAGENT_SESSION` / `HYPERAGENT_SESSIONS` | *(unset)* | Session token(s) — single, or comma-separated for multiple accounts. |
| `SESSIONS_FILE` | *(unset)* | Path to a token file (defaults to `sessions.txt` if present). |
| `ROTATE_BY_BALANCE` | `1` | Prefer the account with the most remaining credit. |
| `BALANCE_REFRESH_SECONDS` | `300` | How often balances are re-checked for rotation. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `LOG_TO_FILE` | `off` | Set to `1` to also write rotating logs to `logs/hyperagent-proxy.log`. |
| `LOG_FILE` | *(unset)* | Explicit log-file path (implies file logging). |
| `LOG_DIR` | `logs` | Directory for the log file when `LOG_FILE` is not set. |

### 🧵 Sessions & threads

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SESSION_PERSIST` | `1` | Persist the session→thread map to SQLite (survives restarts). |
| `SESSION_DB_PATH` | `sessions.db` | SQLite file path (set to `:memory:` for memory-only). |
| `SESSION_TTL_SECONDS` | `21600` | Idle lifetime of a session mapping (6h). |
| `SESSION_MAX` | `1000` | Max mappings kept in memory (LRU eviction). |
| `SESSION_HEADER` | `X-Session-Id` | Request header a client can send to pin a conversation. |

You can also pin a thread inline via the model id using an `@suffix`, e.g. `hyperag2api/opus-latest@my-project`. Any request with the same suffix reuses the same thread. Precedence: `X-Session-Id` header → model `@suffix` → OpenAI `user` field → automatic history matching.

### 🧠 Behaviour & features

| Variable | Default | Description |
| :--- | :--- | :--- |
| `REASONING_STYLE` | `reasoning_content` | `reasoning_content` (native field), `think_tags` (`<think>…</think>` inline), or `both`. |
| `INJECT_PLAN_MODE` | `0` | Force Hyperagent plan mode on every request. |
| `TOOLCALL_MODE` | `content` | Surface backend tool activity: `content`, `openai` (real `tool_calls`), or `off`. |
| `ENABLE_MULTIMODAL` | `1` | Upload & attach `image_url` content parts. |
| `DISABLE_SERVER_MCP` | `1` | During client tool-calling turns, force Hyperagent's own tools/MCP off so the server agent delegates to your client. |
| `LOW_LATENCY_MODE` | `1` | Force every server capability + plan mode off for **all** requests — leanest, fastest, cheapest response. Set `0` to allow server-side tools in plain chat. |
| `ENABLE_USAGE` | `1` | Include approximate token `usage` in responses. |
| `KEEPALIVE_INTERVAL` | `15` | Seconds of silence before an SSE keepalive comment (`0` disables). |
| `SEARCH_MODE` | `exa` | Search mode sent in the chat payload. |
| `ENABLE_WEB_SEARCH`, `ENABLE_BROWSER`, `ENABLE_IMAGE_GENERATION`, … | `0` | Per-capability toggles forwarded to the Hyperagent chat payload. |

### 🛡️ Stability / networking

| Variable | Default | Description |
| :--- | :--- | :--- |
| `HTTP_CONNECT_TIMEOUT` | `10` | Connection timeout (seconds). |
| `REQUEST_READ_TIMEOUT` | `60` | Read timeout for one-shot create/warm calls. |
| `STREAM_READ_TIMEOUT` | `300` | Read timeout for streaming responses. |
| `MAX_RETRIES` | `3` | Retries for transient (429/5xx/network) failures on create/warm. |
| `RETRY_BASE_DELAY` | `0.5` | Base delay for exponential backoff (seconds). |

### 📊 Endpoints

| Path | Description |
| :--- | :--- |
| `/v1/chat/completions` | OpenAI-compatible chat (streaming & non-streaming). |
| `/v1/models` | Model list. |
| `/health` | Readiness: sessions configured, session validity, current account. |
| `/accounts` | Verify session(s); show account + remaining balance for each. |
| `/dashboard` | Live read-only monitoring UI (accounts, balance, requests). |
| `/api/stats` | JSON stats (sessions, recent requests, counters). |

Run the server directly:

```bash
PORT=8080 LOG_LEVEL=DEBUG python3 proxy_server.py
```

---

## 🔌 Client-side tool calling (MCP / functions)

The proxy implements the OpenAI tool-calling loop **on top of** Hyperagent's server-side agent, so clients like **OpenCode** can use their **own** MCP servers / functions through it:

1. Your client sends `tools` (function definitions) with the request.
2. The proxy injects a tool-use contract into the conversation; the model writes a `<tool_call>{…}</tool_call>` request, which the proxy converts into OpenAI `tool_calls` with `finish_reason: "tool_calls"`.
3. Your client executes the tool (its MCP server) and posts the `role:"tool"` result back.
4. The proxy forwards the result into the **same** Hyperagent thread and the model continues — more tool calls or a final answer.

It engages automatically whenever a request carries `tools` (no config needed). `tool_choice` supports `auto` (default), `required`, `none`, and a specific `{function}`.

**Token-lean by design:**
- The full tool schema block is injected **once per thread**; later turns of the same conversation get only a one-line reminder. If your client's tool set **changes** (e.g. you add a new MCP server), the proxy detects the new signature and re-sends the full schemas automatically.
- Tool JSON schemas are **minified**, and only the newest turn's delta is ever sent (thanks to thread reuse) — not the whole history.

**Hybrid streaming:** ordinary answers stream **token-by-token** even when `tools` are present; the proxy only buffers once the `<tool_call>` sentinel appears (a partial sentinel at a token boundary is held back so it never leaks into your output).

> **Adding new client MCP servers?** Nothing to do on the proxy. OpenCode advertises every configured MCP's functions in `tools` on each request, and the proxy is tool-agnostic — new tools just appear and become callable. Add them mid-conversation and the proxy re-primes the thread with the updated schemas.

**Server-side tools/MCP are kept out of the way:**
- During a tool-calling turn the proxy forces Hyperagent's own capabilities **off** (web search, browser, code, integrations, plan mode) so the **server agent delegates everything to your client** instead of doing its own thing. Controlled by `DISABLE_SERVER_MCP` (default on). `enabledIntegrations` is always empty, so Hyperagent's own MCP never fires.

**Notes & limits:**
- This is a **prompt-level bridge**, not native function calling — reliable for the common single/parallel-call loop, but not guaranteed for every edge case. The primary call format is `<tool_call>{…}</tool_call>`; fenced ```tool_call``` / ```json``` blocks are also parsed as a fallback.
- The functions run **on your client side** (your MCP), not on Hyperagent — so **any** client MCP (Figma, filesystem, GitHub, …) works; the proxy is tool-agnostic.
- Many tools = a larger one-time preamble; for big tool sets and long loops, pin the conversation with `X-Session-Id` (or `model@session`) for rock-solid reuse.

---

## 🔑 Sessions, rotation & balance

Sessions can be supplied three ways (merged, de-duplicated):

- **`config.yaml`** — a `sessions:` YAML list (recommended).
- **`sessions.txt`** — one token per line (`#` comments ok), or a JSON array. Point `SESSIONS_FILE` at any path, or just drop `sessions.txt` in the working dir.
- **Env** — `HYPERAGENT_SESSION` (single) or `HYPERAGENT_SESSIONS` (comma-separated).

**Rotation.** With multiple accounts the proxy rotates automatically:
- on a **401/403** while creating a thread → advances to the next account;
- with **`rotate_by_balance: true`** (default) → orders accounts by **remaining credit** and uses the richest, so an exhausted account naturally falls to the back (balances refresh every `balance_refresh_seconds`).

**Verify + balance.** Each session is checked on startup (via `/api/auth/me`) and logged. Anytime:

```bash
curl -s localhost:<PORT>/accounts | jq
# → {"count":1,"total_remaining_usd":298.20,"rotate_by_balance":true,"accounts":[{
#      "valid":true,"email":"you@example.com","session":"…6fb8",
#      "balance":{"plan":"Pay As You Go","credit_initial_usd":500,
#                 "credit_used_usd":201.80,"credit_remaining_usd":298.20}}]}
```

Balance is pulled live from `/api/settings/billing/*` (Hyperagent's own pay-as-you-go credit; the proxy adds nothing). It's also shown on the **`/dashboard`**.

---

## ⚡ Latency — what to expect

Response time is dominated by **Hyperagent's backend**, not the proxy. Each thread runs in a cloud sandbox that is provisioned on demand. Measured against the live API:

| State | Time to first token |
| :--- | :--- |
| **Cold** (new thread / new conversation) | ~10 s (sandbox provisions in ~3.7 s, then the agent runs) |
| **Hot** (sandbox already up, follow-up message) | **~0.1 s** |
| After **~40 s idle** | cold again — the sandbox is torn down |

Two honest takeaways:

- **During active back-and-forth it's fast.** The proxy reuses one thread per conversation, so while messages are less than ~40 s apart the sandbox stays hot and replies are near-instant. The big latencies you see are **cold starts** — the first message of a conversation, or a message after an idle gap (occasionally 40–80 s under backend load).
- **There is no free keep-alive.** We tested pinging the `warm` endpoint on an interval — it does **not** keep the sandbox hot (only real activity does). So the proxy doesn't ship a fake keep-alive; a true one would cost a model run per ping and pollute your thread, which isn't worth it.

What the proxy does do to help:
- **Thread reuse** keeps the sandbox hot throughout an active session.
- **`LOW_LATENCY_MODE`** (default on) forces server-side tools + plan mode off so the agent returns the fastest, cheapest possible answer.
- **SSE heartbeats** during long waits keep the connection alive so a 40–80 s cold start doesn't trip your client's timeout.

---

## 🧪 Development & Tests

The project ships a full unit-test suite (mocked — no network or real browser required):

```bash
python3 -m unittest discover -p "test_*.py" -v
```

---

## 🛠️ OpenCode Configuration

### 1️⃣ Open the config file
- **Linux/macOS:** `~/.config/opencode/opencode.jsonc`
- **Windows:** `%APPDATA%\opencode\opencode.jsonc`

### 2️⃣ Add the provider

Each model needs `"attachment": true` (so OpenCode will send images/files), `"tool_call": true` (MCP / function calling), and `"reasoning": true`. A model **without `attachment: true`** produces *"this model does not support image input"* when you paste a screenshot.

```json
"provider": {
  "hyperag2api": {
    "name": "hyperag2api",
    "npm": "@ai-sdk/openai-compatible",
    "options": {
      "baseURL": "http://localhost:<PORT>/v1",
      "apiKey": "<YOUR_API_KEY>"
    },
    "models": {
      "opus-latest":      { "name": "Claude Opus 4.8", "attachment": true, "tool_call": true, "reasoning": true },
      "sonnet-5":         { "name": "Claude Sonnet 5", "attachment": true, "tool_call": true, "reasoning": true },
      "gpt-5.6-sol":      { "name": "GPT 5.6 Sol",     "attachment": true, "tool_call": true, "reasoning": true },
      "gemini-3.5-flash": { "name": "Gemini 3.5 Flash","attachment": true, "tool_call": true },
      "deepseek-v4-pro":  { "name": "DeepSeek V4 Pro",  "attachment": true, "tool_call": true },
      "grok-4.5":         { "name": "Grok 4.5",         "attachment": true, "tool_call": true }
    }
  }
}
```

> [!IMPORTANT]
> - Replace `<PORT>` with your configured port (default `8000`).
> - Replace `<YOUR_API_KEY>` with your enforced key (or any string if key enforcement is disabled).
> - **Images not working?** Make sure the model has `"attachment": true` — that's OpenCode's per-model gate for image/file input; the proxy handles the upload once OpenCode actually sends it.

### 3️⃣ Use it
Select the models from the OpenCode dropdown, or reference them as `hyperag2api/opus-latest`, `hyperag2api/gpt-5.6-sol`, etc.

---

## 🤖 Supported Models

| Provider | Model Identifier | Hyperagent Engine |
| :--- | :--- | :--- |
| **Anthropic** | `hyperag2api/opus-latest` | Claude Opus 4.8 |
| **Anthropic** | `hyperag2api/sonnet-5` | Claude Sonnet 5 |
| **Anthropic** | `hyperag2api/haiku-4` | Claude Haiku 4.5 |
| **Anthropic** | `hyperag2api/fable` | Fable 5 |
| **OpenAI** | `hyperag2api/gpt-5.6-sol` | GPT 5.6 Sol (Reasoning) |
| **OpenAI** | `hyperag2api/gpt-5.6-terra` | GPT 5.6 Terra |
| **OpenAI** | `hyperag2api/gpt-5.6-luna` | GPT 5.6 Luna |
| **Google** | `hyperag2api/gemini-3.5-flash` | Gemini 3.5 Flash |
| **DeepSeek** | `hyperag2api/deepseek-v4-pro` | DeepSeek V4 Pro |
| **xAI** | `hyperag2api/grok-4.5` | Grok 4.5 |
| **Alibaba** | `hyperag2api/qwen3.7-plus` | Qwen 3.7 Plus |
| **Moonshot** | `hyperag2api/kimi-k2.6` | Kimi K2.6 |
| **Zhipu** | `hyperag2api/glm-5.2-fast` | GLM 5.2 Fast |
| **Other** | `hyperag2api/fugu-ultra` | Fugu Ultra |

---

## ⚖️ Disclaimer

`hyperag2api` drives your **own** authenticated Hyperagent session through its web endpoints. It is an unofficial, community project and is not affiliated with or endorsed by Hyperagent. Use it in accordance with Hyperagent's Terms of Service and only with an account you own. Keep your session cookies private — treat them like a password.

## 📄 License

[MIT](LICENSE)
