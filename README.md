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

**`hyperag2api`** is a lightweight, **browserless**, **OpenAI-compatible** proxy for **[Hyperagent.com](https://hyperagent.com)**. It exposes a local `/v1/chat/completions` endpoint and forwards requests to Hyperagent using session tokens — making top-tier models (*Claude Opus 4.8*, *GPT 5.6*, *Gemini 3.5*, *Grok 4.5*) instantly accessible from **OpenCode**, **Continue**, **Cursor**, or custom OpenAI clients.

---

## ✨ Key Features

- 🥷 **Anti-Detection Suite** — TLS fingerprint spoofing (`curl_cffi` impersonating Chrome/Safari JA3/JA4 signatures), modern browser User-Agent & Client Hints rotation, dynamic endpoint `Referer` navigation headers, and human timing jitters (150–600 ms).
- ⚡ **Circuit Breaker & Cooldown** — Automatic session quarantine (10m default) upon encountering `401`, `403`, or `429` rate limits, smoothly failing over to healthy active accounts.
- 🧵 **Session-Aware Thread Reuse** — Maps conversations 1-to-1 with Hyperagent threads, sending only delta messages to keep turns fast (~0.1s hot state) and token-lean.
- 🔌 **Client-Side MCP & Tool-Calling Bridge** — Prompts the model to emit function calls (`finish_reason: "tool_calls"`) and passes `role: "tool"` responses back into the thread, letting tools (Figma, GitHub, Local FS) run client-side.
- 🧠 **Reasoning Content** — Streams reasoning process via OpenAI `reasoning_content` or inline `<think>...</think>` tags.
- 🖼️ **Multimodal Support** — Uploads `image_url` parts via Hyperagent's presigned S3 flow (`attachmentIds`).

---

## 📦 Quick Start

Requires **Python 3.9+**.

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python3 start.py
```

### 🔑 Get Your Session Token
Copy the `__Host-hyperagent_session` cookie from your browser logged in to `hyperagent.com` (DevTools → Application → Cookies). Add one or more tokens under `sessions:` in `config.yaml`.

---

## ⚙️ Configuration

Settings resolve with precedence: **defaults < `config.yaml` < environment variables**.

```yaml
sessions:
  - "PASTE_YOUR___Host-hyperagent_session_VALUE_HERE"
host: 127.0.0.1
port: 8000
proxy_api_key: ""

# Anti-Detection
tls_impersonate: chrome124
enable_tls_fingerprint: true
enable_human_jitter: true
enable_ua_rotation: true
enable_session_cooldown: true
cooldown_seconds: 600
```

### Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PROXY_API_KEY` | *(empty)* | Optional Bearer authorization key for clients. |
| `PORT` | `8000` | Local proxy server port. |
| `TLS_IMPERSONATE` | `chrome124` | Browser TLS fingerprint target (`chrome124`, `safari15_5`). |
| `ENABLE_TLS_FINGERPRINT` | `1` | Impersonate browser TLS ClientHello signatures. |
| `ENABLE_HUMAN_JITTER` | `1` | Apply randomized human-like timing delays before requests. |
| `ENABLE_SESSION_COOLDOWN` | `1` | Auto-quarantine rate-limited or auth-failed sessions. |
| `COOLDOWN_SECONDS` | `600` | Quarantined session cooldown duration in seconds. |

---

## 📊 API Endpoints

| Endpoint | Description |
| :--- | :--- |
| `/v1/chat/completions` | OpenAI-compatible chat completion route (Streaming & Non-streaming). |
| `/v1/models` | List supported models. |
| `/health` | Health check endpoint returning session & system status. |
| `/` | Live monitoring dashboard (sessions, streams, recent requests). |
| `/api/live-status` | JSON feed behind the dashboard. |

---

## 🛠️ OpenCode Integration

In your `opencode.jsonc` (`~/.config/opencode/opencode.jsonc` or `%APPDATA%\opencode\opencode.jsonc`):

```json
"provider": {
  "hyperag2api": {
    "name": "hyperag2api",
    "npm": "@ai-sdk/openai-compatible",
    "options": {
      "baseURL": "http://localhost:8000/v1",
      "apiKey": "optional-key"
    },
    "models": {
      "opus-latest":      { "name": "Claude Opus 4.8", "attachment": true, "tool_call": true, "reasoning": true },
      "sonnet-5":         { "name": "Claude Sonnet 5", "attachment": true, "tool_call": true, "reasoning": true },
      "gpt-5.6-sol":      { "name": "GPT 5.6 Sol",     "attachment": true, "tool_call": true, "reasoning": true },
      "gemini-3.5-flash": { "name": "Gemini 3.5 Flash", "attachment": true, "tool_call": true }
    }
  }
}
```

---

## 🤖 Supported Models

| Provider | Model ID | Hyperagent Engine |
| :--- | :--- | :--- |
| **Anthropic** | `hyperag2api/opus-latest` | Claude Opus 4.8 |
| **Anthropic** | `hyperag2api/sonnet-5` | Claude Sonnet 5 |
| **OpenAI** | `hyperag2api/gpt-5.6-sol` | GPT 5.6 Sol (Reasoning) |
| **Google** | `hyperag2api/gemini-3.5-flash` | Gemini 3.5 Flash |
| **DeepSeek** | `hyperag2api/deepseek-v4-pro` | DeepSeek V4 Pro |
| **xAI** | `hyperag2api/grok-4.5` | Grok 4.5 |

## 🗂️ Project Structure

The codebase is layered, and dependencies only ever point inwards:
`adapters` → `services` → `core`, with `infra` supplying the transport.

```
src/
├── core/                    # Framework-free foundation
│   ├── config.py            #   Settings: defaults < config.yaml < env
│   ├── schemas.py           #   OpenAI request/response models
│   ├── interfaces.py        #   CookieProvider / ChatBackend ports
│   ├── sse.py               #   SSE chunk formatting + token estimates
│   ├── session_store.py     #   Conversation key → thread id (LRU + SQLite)
│   └── stats.py             #   In-memory runtime counters
├── infra/                   # How bytes reach hyperagent.com
│   ├── fingerprint.py       #   Browser headers, Client Hints, timing jitter
│   └── http_client.py       #   curl_cffi (TLS impersonation) / httpx transport
├── services/                # Business logic
│   ├── chat_service.py      #   Orchestrates one completion end to end
│   ├── conversation.py      #   Prompts and session keys
│   ├── threads.py           #   Thread creation with account rotation
│   ├── attachments.py       #   Multimodal image uploads
│   ├── streaming.py         #   Backend iteration with keepalives
│   ├── stream_events.py     #   Classification of backend SSE frames
│   ├── render.py            #   Frames → OpenAI delta chunks
│   ├── tool_bridge.py       #   The tool-calling prompt contract
│   ├── tool_mode.py         #   One client tool-calling turn
│   └── accounts.py          #   Session verification (cached)
├── adapters/                # The outside world
│   ├── api/                 #   FastAPI app, routers, DI, dashboard
│   ├── backend/             #   Hyperagent HTTP client
│   └── session/             #   Config-provided session tokens
└── server.py                # Entry point (start.py delegates here)
```

---

## 🧪 Testing

Run unit tests:
```bash
python3 -m pytest tests
```

---

## 📄 License

[MIT](LICENSE)
