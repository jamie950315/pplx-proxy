# pplx-proxy

Reverse proxy for [Perplexity.ai](https://www.perplexity.ai) — use your existing **Pro/Max subscription cookie** to access all models via standard APIs.

Exposes two interfaces:
- **OpenAI-compatible REST API** (`/v1/chat/completions`)
- **MCP server** (Streamable HTTP + SSE)

## How It Works

Perplexity's web frontend talks to its backend through an internal SSE endpoint (`/rest/sse/perplexity_ask`). This proxy authenticates with your session cookie via [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprinting), translates requests/responses into OpenAI and MCP formats, and keeps your session alive automatically.

No official API key needed — just your subscription.

## Features

- **Account tier support**: free/pro/max — only exposes models your tier can access
- **Auto-discovery**: background task checks model health every 24h, auto-upgrades when versions change
- **Session keep-alive**: periodic pings prevent cookie expiry
- **Push notifications**: [ntfy.sh](https://ntfy.sh) alerts on cookie expiry or model upgrades
- **Dynamic model management**: add/remove models at runtime via admin API
- **Full input validation**: proper error messages for every malformed request
- **Zero hardcoded values**: every parameter lives in `.env`

## Quick Start

```bash
git clone https://github.com/jamie950315/pplx-proxy.git
cd pplx-proxy
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env — set PPLX_COOKIE and ACCOUNT_TYPE

venv/bin/uvicorn server:app --host 0.0.0.0 --port 8892
```

## Getting Your Cookie

1. Log in to [perplexity.ai](https://www.perplexity.ai)
2. F12 → **Application** → **Cookies** → `www.perplexity.ai`
3. Copy `next-auth.session-token`
4. Set `PPLX_COOKIE=<value>` in `.env`

## Models by Account Tier

### FREE ($0/mo)

| Model ID | Backend |
|----------|---------|
| `auto` | Perplexity Best (auto-select) |

### PRO ($20/mo)

| Model ID | Backend | Thinking |
|----------|---------|----------|
| `auto` | Perplexity Best | — |
| `sonar` | Sonar | — |
| `gpt5` | GPT-5.4 | `gpt5-thinking` |
| `sonnet` | Claude Sonnet 4.6 | `sonnet-thinking` |
| `gemini` | Gemini 3.1 Pro | always on |
| `nemotron` | Nemotron 3 Super | always on |

### MAX ($200/mo)

Everything in PRO, plus:

| Model ID | Backend | Thinking |
|----------|---------|----------|
| `opus` | Claude Opus 4.6 | `opus-thinking` |

Using a model outside your tier returns `403` with a clear error message.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Health check |
| `GET` | `/v1/models` | Yes | List tier-available models |
| `POST` | `/v1/chat/completions` | Yes | Chat (streaming + non-streaming) |
| `POST` | `/mcp/mcp` | No | MCP Streamable HTTP |
| `GET` | `/sse/sse` | No | MCP SSE |
| `GET` | `/admin/models` | Yes | Full model map |
| `POST` | `/admin/update-models` | Yes | Add/replace models |
| `POST` | `/admin/refresh-cookie` | Yes | Inject new session token |
| `POST` | `/admin/discover-models` | Yes | Run model discovery |

## Usage

### OpenAI API

```bash
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt5", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'
```

### MCP

```bash
# Claude Code
claude mcp add pplx-proxy --transport http http://localhost:8892/mcp/mcp
```

**MCP Tools:**

| Tool | Description |
|------|-------------|
| `perplexity_search` | Pro Search with model/source selection |
| `perplexity_ask` | Quick auto-mode Q&A |
| `perplexity_reason` | Reasoning with model selection |
| `perplexity_research` | Deep Research |
| `perplexity_models` | List available models for your tier |

## Auto-Discovery

Every `PROBE_INTERVAL_HOURS` (default 24h), pplx-proxy checks if models are still alive. If one dies, it increments the version number (e.g., `gpt54` → `gpt55` → ... up to +1.0) and auto-upgrades. Thinking variants follow their base model automatically.

| Model | Probe strategy |
|-------|---------------|
| `sonar` | alive check only |
| `gpt5` | gpt54 → gpt55...gpt64 (max 10), thinking auto-follows |
| `sonnet` | claude46sonnet → claude47...claude56 (max 10), thinking auto-follows |
| `opus` | claude46opus → claude47...claude56 (max 10), thinking auto-follows |
| `gemini` | gemini31pro_high → gemini32...gemini41 (max 10) |
| `nemotron` | nv_nemotron_3_super → nv_nemotron_4 (max 1) |

Manual trigger: `POST /admin/discover-models`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | — | Session token (**required**) |
| `PPLX_PROXY_API_KEY` | — | Bearer auth (empty = no auth) |
| `ACCOUNT_TYPE` | `pro` | `free`, `pro`, or `max` |
| `DEFAULT_MODEL` | `gpt5` | Default when not specified |
| `PPLX_PROXY_PORT` | `8892` | Listen port |
| `KEEPALIVE_HOURS` | `6` | Session ping interval |
| `PROBE_INTERVAL_HOURS` | `24` | Auto-discovery interval |
| `NTFY_TOPIC` | `pplx-proxy` | ntfy.sh topic |
| `NTFY_COOLDOWN_SECS` | `3600` | Min interval between alerts |
| `PUBLIC_URL` | `http://localhost:8892` | URL in ntfy messages |
| `PPLX_API_VERSION` | `2.18` | Perplexity internal API ver |
| `PPLX_IMPERSONATE` | `chrome` | curl_cffi TLS fingerprint |
| `USER_AGENT` | Chrome/130 | HTTP User-Agent |
| `COOKIE_MAX_AGE_HOURS` | `168` | Cookie cache max age |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment (systemd)

```bash
sudo cp pplx-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pplx-proxy
```

## Cookie Lifecycle

```
Manual inject → keep-alive every 6h → session stays alive indefinitely
                                      ↓ (if Perplexity force-revokes)
                                      ntfy alert → manual re-inject
```

Re-inject without SSH:

```bash
curl -X POST https://your-domain/admin/refresh-cookie \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_token": "NEW_TOKEN"}'
```

## Disclaimer

Unofficial reverse proxy for personal use. Relies on Perplexity's internal web API which may change without notice. Use responsibly.

## License

MIT
