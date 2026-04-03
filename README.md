# pplx-proxy

Reverse proxy for [Perplexity.ai](https://www.perplexity.ai) — use your existing **Pro subscription cookie** to access Pro Search, Reasoning, and Deep Research via standard APIs.

Exposes two interfaces:
- **OpenAI-compatible REST API** (`/v1/chat/completions`)
- **MCP server** (Streamable HTTP + SSE)

## How It Works

Perplexity's web frontend communicates with its backend through an internal SSE endpoint (`/rest/sse/perplexity_ask`). This proxy authenticates with your session cookie via [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprinting), translates requests/responses into OpenAI and MCP formats, and keeps your session alive automatically.

No official API key needed — just your Pro subscription.

## Features

- **13+ models**: Auto, Pro (Sonar/GPT-5.2/Claude 4.5/Grok 4.1), Reasoning (5 model backends), Deep Research
- **Dynamic model management**: Add/remove models at runtime via admin API, persisted to disk
- **Session keep-alive**: Background task pings Perplexity periodically to prevent cookie expiry
- **Push notifications**: [ntfy.sh](https://ntfy.sh) alerts when cookie expires and needs manual refresh
- **Full input validation**: Proper error messages for every malformed request
- **Zero hardcoded values**: Every configurable parameter lives in `.env`

## Quick Start

```bash
# Clone and install
git clone https://github.com/jamie950315/pplx-proxy.git
cd pplx-proxy
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — set PPLX_COOKIE (see "Getting Your Cookie" below)

# Run
venv/bin/uvicorn server:app --host 0.0.0.0 --port 8892
```

## Getting Your Cookie

1. Log in to [perplexity.ai](https://www.perplexity.ai)
2. Open DevTools (F12) → **Application** → **Cookies** → `www.perplexity.ai`
3. Copy the value of `next-auth.session-token`
4. Set `PPLX_COOKIE=<value>` in `.env`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (includes cookie age) |
| `GET` | `/v1/models` | List available models |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat (streaming + non-streaming) |
| `POST` | `/mcp/mcp` | MCP Streamable HTTP |
| `GET` | `/sse/sse` | MCP SSE (legacy) |
| `GET` | `/admin/models` | Full model map with internals |
| `POST` | `/admin/update-models` | Add/replace models |
| `POST` | `/admin/refresh-cookie` | Inject new session token at runtime |

## Usage

### OpenAI-compatible API

```bash
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "pplx-pro",
    "messages": [{"role": "user", "content": "Latest AI news"}],
    "stream": true
  }'
```

### MCP (Claude Code / Claude Desktop / Cursor)

```bash
# Claude Code
claude mcp add pplx-proxy --transport http http://localhost:8892/mcp/mcp

# Or in MCP config JSON
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

| Model ID | Mode | Backend |
|----------|------|---------|
| `pplx-auto` | Auto | Turbo |
| `pplx-pro` | Pro | Sonar Pro |
| `pplx-pro-sonar` | Pro | Sonar |
| `pplx-pro-gpt5` | Pro | GPT-5.2 |
| `pplx-pro-claude` | Pro | Claude 4.5 Sonnet |
| `pplx-pro-grok` | Pro | Grok 4.1 |
| `pplx-reasoning` | Reasoning | Default |
| `pplx-reasoning-gpt5` | Reasoning | GPT-5.2 Thinking |
| `pplx-reasoning-claude` | Reasoning | Claude 4.5 Sonnet Thinking |
| `pplx-reasoning-gemini` | Reasoning | Gemini 3.0 Pro |
| `pplx-reasoning-kimi` | Reasoning | Kimi K2 Thinking |
| `pplx-reasoning-grok` | Reasoning | Grok 4.1 Reasoning |
| `pplx-deep-research` | Deep Research | Alpha |

Models can be added/removed at runtime via `/admin/update-models`.

## Configuration

All settings in `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | — | Perplexity session token (**required**) |
| `PPLX_PROXY_API_KEY` | — | Bearer token for API auth (optional, empty = no auth) |
| `PPLX_PROXY_PORT` | `8892` | Listen port |
| `DEFAULT_MODEL` | `pplx-pro` | Default model when not specified |
| `KEEPALIVE_HOURS` | `6` | Session keep-alive ping interval |
| `NTFY_TOPIC` | `pplx-proxy` | ntfy.sh topic for cookie expiry alerts |
| `NTFY_COOLDOWN_SECS` | `3600` | Min interval between ntfy notifications |
| `PUBLIC_URL` | `http://localhost:8892` | Public URL (used in ntfy messages) |
| `PPLX_API_VERSION` | `2.18` | Perplexity internal API version |
| `PPLX_IMPERSONATE` | `chrome` | curl_cffi TLS fingerprint target |
| `USER_AGENT` | Chrome/130 Linux | HTTP User-Agent string |
| `COOKIE_MAX_AGE_HOURS` | `168` | Max cookie cache age before stale |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment (systemd)

```bash
# Install service
sudo cp pplx-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pplx-proxy

# Logs
sudo journalctl -u pplx-proxy -f
```

Edit `pplx-proxy.service` paths if not installed at `/home/jamie/pplx-proxy`.

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

## Disclaimer

This is an unofficial reverse proxy for personal use. It relies on Perplexity's internal web API which may change without notice. Use responsibly and respect Perplexity's terms of service.

## License

MIT
