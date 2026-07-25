# 🚀 hyperag2api

[English](README.md) · **Русский**

<p align="center">
  <a href="https://hyperagent.com">
    <img src="https://img.shields.io/badge/Powered%20by-Hyperagent-0052FF?style=for-the-badge" alt="Powered by Hyperagent" />
  </a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue?style=for-the-badge" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License" />
</p>

---

**`hyperag2api`** — производительный, **без-браузерный** **OpenAI-совместимый** прокси для **[Hyperagent.com](https://hyperagent.com)**. Он поднимает локальный эндпоинт `/v1/chat/completions` и транслирует запросы в Hyperagent — благодаря чему топовые модели (*Claude Opus 4.8*, *GPT 5.6*, *Gemini 3.5*, *Grok 4.5*) мгновенно доступны из **OpenCode**, **Continue**, **Cursor** или любого OpenAI-клиента.

---

## ✨ Ключевые возможности

- 🥷 **Комплекс Anti-Detection** — подмена TLS-отпечатков (`curl_cffi` подделывает JA3/JA4 отпечатки Chrome/Safari), ротация браузерных User-Agent & Client Hints, контекстные навигационные заголовки `Referer` под каждый эндпоинт и человеческие джиттер-паузы (150–600 мс).
- ⚡ **Circuit Breaker & Cooldown** — автоматический карантин сессий (10 мин по умолчанию) при ошибках `401`, `403` или `429` с переключением на здоровые аккаунты.
- 🧵 **Переиспользование тредов** — привязка диалога 1-к-1 к треду Hyperagent: отправляются только новые сообщения, что ускоряет ответ (~0.1s в горячем состоянии) и экономит токены.
- 🔌 **Клиентский MCP & Tool-Calling** — модель генерирует вызовы функций (`finish_reason: "tool_calls"`), а результаты `role: "tool"` возвращаются в тред. Инструменты (Figma, GitHub, файловая система) исполняются на стороне вашего клиента.
- 🧠 **Размышления (Reasoning)** — трансляция хода мыслей модели в OpenAI `reasoning_content` или тегах `<think>...</think>`.
- 🖼️ **Мультимодальность** — загрузка изображений `image_url` через S3-флоу Hyperagent (`attachmentIds`).

---

## 📦 Быстрый старт

Требуется **Python 3.9+**.

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
python3 start.py
```

### 🔑 Получение токена сессии
Скопируйте значение cookie `__Host-hyperagent_session` из браузера, где вы авторизованы в `hyperagent.com` (DevTools → Application → Cookies). Вставьте один или несколько токенов под `sessions:` в `config.yaml`.

---

## ⚙️ Конфигурация

Приоритет параметров: **дефолты < `config.yaml` < переменные окружения**.

```yaml
sessions:
  - "ВСТАВЬТЕ_ВАШ___Host-hyperagent_session_ТОКЕН"
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

### Переменные окружения

| Переменная | По умолчанию | Описание |
| :--- | :--- | :--- |
| `PROXY_API_KEY` | *(пусто)* | Ключ авторизации Bearer для клиентов. |
| `PORT` | `8000` | Порт локального прокси-сервера. |
| `TLS_IMPERSONATE` | `chrome124` | Целевой TLS-отпечаток браузера (`chrome124`, `safari15_5`). |
| `ENABLE_TLS_FINGERPRINT` | `1` | Имитация браузерного TLS ClientHello. |
| `ENABLE_HUMAN_JITTER` | `1` | Использование человеческих тайминг-пауз перед запросами. |
| `ENABLE_SESSION_COOLDOWN` | `1` | Карантин сессий при ошибках лимитов или авторизации. |
| `COOLDOWN_SECONDS` | `600` | Длительность карантина сессии в секундах. |

---

## 📊 Эндпоинты API

| Путь | Описание |
| :--- | :--- |
| `/v1/chat/completions` | Чат-эндпоинт OpenAI (Стриминг и обычные ответы). |
| `/v1/models` | Список поддерживаемых моделей. |
| `/health` | Проверка состояния сессий и сервера. |
| `/` | Живая панель мониторинга (сессии, стримы, последние запросы). |
| `/api/live-status` | JSON-фид, на котором работает панель. |

---

## 🛠️ Настройка OpenCode

В вашем `opencode.jsonc` (`~/.config/opencode/opencode.jsonc` или `%APPDATA%\opencode\opencode.jsonc`):

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

## 🤖 Поддерживаемые модели

| Провайдер | Идентификатор модели | Движок Hyperagent |
| :--- | :--- | :--- |
| **Anthropic** | `hyperag2api/opus-latest` | Claude Opus 4.8 |
| **Anthropic** | `hyperag2api/sonnet-5` | Claude Sonnet 5 |
| **OpenAI** | `hyperag2api/gpt-5.6-sol` | GPT 5.6 Sol (Reasoning) |
| **Google** | `hyperag2api/gemini-3.5-flash` | Gemini 3.5 Flash |
| **DeepSeek** | `hyperag2api/deepseek-v4-pro` | DeepSeek V4 Pro |
| **xAI** | `hyperag2api/grok-4.5` | Grok 4.5 |

## 🗂️ Структура проекта

Код разделён на слои, зависимости направлены только внутрь:
`adapters` → `services` → `core`, транспорт предоставляет `infra`.

```
src/
├── core/                    # Фундамент без зависимостей от фреймворка
│   ├── config.py            #   Настройки: defaults < config.yaml < env
│   ├── schemas.py           #   Модели запроса/ответа OpenAI
│   ├── interfaces.py        #   Порты CookieProvider / ChatBackend
│   ├── sse.py               #   Формирование SSE-чанков и оценка токенов
│   ├── session_store.py     #   Ключ диалога → id треда (LRU + SQLite)
│   └── stats.py             #   Счётчики рантайма в памяти
├── infra/                   # Как байты доходят до hyperagent.com
│   ├── fingerprint.py       #   Заголовки браузера, Client Hints, джиттер
│   └── http_client.py       #   Транспорт curl_cffi (TLS) / httpx
├── services/                # Бизнес-логика
│   ├── chat_service.py      #   Оркестрация одного запроса целиком
│   ├── conversation.py      #   Промпты и ключи сессий
│   ├── threads.py           #   Создание тредов с ротацией аккаунтов
│   ├── attachments.py       #   Загрузка изображений (мультимодальность)
│   ├── streaming.py         #   Чтение бэкенда с keepalive
│   ├── stream_events.py     #   Классификация SSE-кадров бэкенда
│   ├── render.py            #   Кадры → delta-чанки OpenAI
│   ├── tool_bridge.py       #   Промпт-контракт для tool-calling
│   ├── tool_mode.py         #   Один ход клиентского tool-calling
│   └── accounts.py          #   Проверка сессий (с кэшем)
├── adapters/                # Внешний мир
│   ├── api/                 #   FastAPI-приложение, роутеры, DI, дашборд
│   ├── backend/             #   HTTP-клиент Hyperagent
│   └── session/             #   Токены сессий из конфига
└── server.py                # Точка входа (start.py делегирует сюда)
```

---

## 🧪 Тестирование

Запуск юнит-тестов:
```bash
python3 -m pytest tests
```

---

## 📄 Лицензия

[MIT](LICENSE)
