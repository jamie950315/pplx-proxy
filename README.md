# pplx-proxy

Reverse proxy for [Perplexity.ai](https://www.perplexity.ai) — use your existing **Pro subscription cookie** to access Pro Search, Reasoning, and Deep Research via standard APIs.

Exposes two interfaces:
- **OpenAI-compatible REST API** (`/v1/chat/completions`)
- **MCP server** (Streamable HTTP + SSE)

## How It Works

Perplexity's web frontend communicates with its backend through an internal SSE endpoint (`/rest/sse/perplexity_ask`). This proxy authenticates with your session cookie via [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprinting), translates requests/responses into OpenAI and MCP formats, and keeps your session alive automatically.

No official API key needed — just your Pro subscription.

## Features

- **12+ models**: GPT-5.4, Claude 4.6 Sonnet, Gemini 3.1 Pro, Nemotron 3 Super, Sonar, and thinking variants
- **Dynamic model management**: Add/remove models at runtime via admin API, persisted to disk
- **Session keep-alive**: Background task pings Perplexity periodically to prevent cookie expiry
- **Push notifications**: [ntfy.sh](https://ntfy.sh) alerts when cookie expires and needs manual refresh
- **Full input validation**: Proper error messages for every malformed request
- **Zero hardcoded values**: Every configurable parameter lives in `.env`

## Quick Start

```bash
git clone https://github.com/jamie950315/pplx-proxy.git
cd pplx-proxy
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env — set PPLX_COOKIE (see "Getting Your Cookie" below)

venv/bin/uvicorn server:app --host 0.0.0.0 --port 8892
```

## Getting Your Cookie

1. Log in to [perplexity.ai](https://www.perplexity.ai)
2. Open DevTools (F12) → **Application** → **Cookies** → `www.perplexity.ai`
3. Copy the value of `next-auth.session-token`
4. Set `PPLX_COOKIE=<value>` in `.env`

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Health check (includes cookie age) |
| `GET` | `/v1/models` | Yes | List available models with mode/pref info |
| `POST` | `/v1/chat/completions` | Yes | OpenAI-compatible chat (streaming + non-streaming) |
| `POST` | `/mcp/mcp` | No | MCP Streamable HTTP |
| `GET` | `/sse/sse` | No | MCP SSE (legacy) |
| `GET` | `/admin/models` | Yes | Full model map with internals |
| `POST` | `/admin/update-models` | Yes | Add/replace models at runtime |
| `POST` | `/admin/refresh-cookie` | Yes | Inject new session token without restart |

## Usage

### OpenAI-compatible API

```bash
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "pplx-pro-gpt5",
    "messages": [{"role": "user", "content": "Latest AI news"}],
    "stream": true
  }'
```

### MCP (Claude Code / Cursor / Windsurf)

```bash
# Claude Code
claude mcp add pplx-proxy --transport http http://localhost:8892/mcp/mcp

# MCP config JSON
{
  "mcpServers": {
    "pplx-proxy": {
      "transport": "streamable-http",
      "url": "http://localhost:8892/mcp/mcp"
    }
  }
}
```

**MCP Tools:**

| Tool | Description |
|------|-------------|
| `perplexity_search` | Pro Search with model/source selection |
| `perplexity_ask` | Quick auto-mode Q&A |
| `perplexity_reason` | Step-by-step reasoning (multiple model backends) |
| `perplexity_research` | Deep Research (comprehensive, 30s+) |
| `perplexity_models` | List all available models and IDs |

## Available Models

Internal identifiers sourced from Perplexity's web frontend (April 2026).

### Pro Search

| Model ID | Backend | Internal Pref |
|----------|---------|---------------|
| `pplx-auto` | Perplexity Best (auto-select) | `pplx_pro` |
| `pplx-pro-sonar` | Sonar | `experimental` |
| `pplx-pro-gpt5` | OpenAI GPT-5.4 | `gpt54` |
| `pplx-pro-claude` | Anthropic Claude 4.6 Sonnet | `claude46sonnet` |
| `pplx-pro-gemini` | Google Gemini 3.1 Pro (always thinking) | `gemini31pro_high` |
| `pplx-pro-nemotron` | NVIDIA Nemotron 3 Super (always thinking) | `nv_nemotron_3_super` |
| `pplx-pro-opus` | Anthropic Claude 4.6 Opus (**Max only**) | `claude46opus` |

### Thinking / Reasoning

| Model ID | Backend | Internal Pref |
|----------|---------|---------------|
| `pplx-pro-gpt5-thinking` | GPT-5.4 Thinking | `gpt54_thinking` |
| `pplx-pro-claude-thinking` | Claude 4.6 Sonnet Thinking | `claude46sonnetthinking` |
| `pplx-pro-opus-thinking` | Claude 4.6 Opus Thinking (**Max only**) | `claude46opusthinking` |

> Gemini 3.1 Pro and Nemotron 3 Super have thinking permanently enabled — use their Pro Search IDs.

### Deep Research & Labs

| Model ID | Backend | Internal Pref |
|----------|---------|---------------|
| `pplx-deep-research` | Perplexity Alpha | `pplx_alpha` |
| `pplx-labs` | Perplexity Labs (files & apps) | `pplx_beta` |

Models can be added/removed at runtime via `POST /admin/update-models`.

## Configuration

All settings in `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | — | Perplexity session token (**required**) |
| `PPLX_PROXY_API_KEY` | — | Bearer token for API auth (empty = no auth) |
| `PPLX_PROXY_PORT` | `8892` | Listen port |
| `DEFAULT_MODEL` | `pplx-pro-gpt5` | Default model when not specified |
| `KEEPALIVE_HOURS` | `6` | Session keep-alive ping interval |
| `NTFY_TOPIC` | `pplx-proxy` | ntfy.sh topic for expiry alerts |
| `NTFY_COOLDOWN_SECS` | `3600` | Min interval between notifications |
| `PUBLIC_URL` | `http://localhost:8892` | Public URL (used in ntfy messages) |
| `PPLX_API_VERSION` | `2.18` | Perplexity internal API version |
| `PPLX_IMPERSONATE` | `chrome` | curl_cffi TLS fingerprint target |
| `USER_AGENT` | Chrome/130 Linux | HTTP User-Agent string |
| `COOKIE_MAX_AGE_HOURS` | `168` | Max cookie cache age before stale |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment (systemd)

```bash
sudo cp pplx-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pplx-proxy
sudo journalctl -u pplx-proxy -f
```

## Cookie Lifecycle

```
Manual inject (one-time) → keep-alive pings every 6h → session stays alive
                                                       ↓
                                    If Perplexity force-revokes session:
                                    ntfy push notification → manual re-inject
```

Re-inject without SSH:

```bash
curl -X POST https://your-domain/admin/refresh-cookie \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_token": "NEW_TOKEN"}'
```

## Updating Models

When Perplexity adds new models, update at runtime:

```bash
curl -X POST https://your-domain/admin/update-models \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models": {"pplx-pro-newmodel": ["pro", "new_internal_pref"]}, "merge": true}'
```

Find internal preference strings by inspecting Perplexity's frontend JS bundles or checking community reverse-engineering projects.

## Disclaimer

This is an unofficial reverse proxy for personal use. It relies on Perplexity's internal web API which may change without notice. Use responsibly and respect Perplexity's terms of service.

## License

MIT
