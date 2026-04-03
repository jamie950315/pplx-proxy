# CLAUDE.md — Project Context for Claude Code

## What This Is

`pplx-proxy` is a self-hosted reverse proxy for Perplexity.ai that exposes Pro Search, Reasoning, and Deep Research through OpenAI-compatible REST API and MCP (Model Context Protocol) server. It uses the user's existing Perplexity Pro subscription cookie for authentication instead of an API key.

## Architecture

Single-file FastAPI application (`server.py`, ~700 lines) that:

1. Receives OpenAI-format requests at `/v1/chat/completions` or MCP tool calls at `/mcp/mcp`
2. Translates them into Perplexity's internal SSE format (`POST /rest/sse/perplexity_ask`)
3. Uses `curl_cffi` with Chrome TLS fingerprinting to bypass Cloudflare
4. Streams responses back in OpenAI SSE or MCP format

```
Client → FastAPI → curl_cffi (Chrome TLS) → Perplexity SSE endpoint
  ↑                                              ↓
  └──── OpenAI/MCP format ← parse SSE chunks ←──┘
```

## Key Components

- **PerplexityClient**: Async SSE client using `curl_cffi.requests.AsyncSession` with `impersonate` for TLS fingerprinting. The `search()` method is an async generator yielding delta chunks.
- **MODEL_MAP**: Dynamic dict mapping model IDs (e.g., `pplx-pro`) to `(mode, internal_pref)` tuples. Persisted to `.models.json`. Mode maps to Perplexity's `concise`/`copilot` parameter; internal_pref maps to their model backend identifier.
- **MCP server**: FastMCP mounted as sub-app. Streamable HTTP at `/mcp`, SSE at `/sse`. Lifespan is manually wired into FastAPI's lifespan to initialize the MCP session manager's TaskGroup.
- **Keep-alive**: Background asyncio task pings `/api/auth/session` every `KEEPALIVE_HOURS` to prevent cookie expiry.
- **ntfy**: Sends push notification on 401/403 from Perplexity, rate-limited to one per `NTFY_COOLDOWN_SECS`.

## Code Style

- No spaces around `=` in assignments: `x=1` not `x = 1`
- One space after commas
- camelCase for locals, ALL_UPPERCASE for module-level constants
- Opening brace on same line
- Minimal blank lines

## File Structure

```
server.py          # Everything: FastAPI app, Perplexity client, MCP tools, admin endpoints
inject_cookie.sh   # Helper script to inject cookie and restart service
test.sh            # Basic smoke test script
pplx-proxy.service # systemd unit file
.env.example       # Config template (all configurable params)
.gitignore         # Excludes .env, .cookie_cache.json, .models.json, venv/
```

## Runtime Files (git-ignored)

```
.env               # Actual config with secrets
.cookie_cache.json # Cached cookie + timestamp (written by /admin/refresh-cookie)
.models.json       # Persisted model map (written by /admin/update-models)
```

## Configuration

All config via environment variables loaded from `.env` by `python-dotenv`. Zero hardcoded values. See `.env.example` for all parameters.

## Endpoints

### Public
- `GET /health` — no auth

### Requires API key (Bearer token)
- `GET /v1/models`
- `POST /v1/chat/completions`
- `GET /admin/models`
- `POST /admin/update-models`
- `POST /admin/refresh-cookie`

### MCP (no API key, uses MCP session management)
- `POST /mcp/mcp` — Streamable HTTP
- `GET /sse/sse` — SSE transport

## MCP Tools

| Tool | Params | Notes |
|------|--------|-------|
| `perplexity_search` | `query`, `model="default"`, `sources="web"`, `language="en-US"` | Validates model ID, sources |
| `perplexity_ask` | `query`, `language` | Auto mode, no model selection |
| `perplexity_reason` | `query`, `model="default"`, `language` | Accepts full IDs or shorthand (gpt5/claude/gemini/kimi/grok) |
| `perplexity_research` | `query`, `language` | Deep Research, slow (30s+) |
| `perplexity_models` | (none) | Lists all model IDs grouped by mode |

## Common Tasks

### Adding a new model
```bash
curl -X POST /admin/update-models \
  -d '{"models": {"pplx-pro-newmodel": ["pro", "newmodel_internal"]}, "merge": true}'
```

### Updating cookie without restart
```bash
curl -X POST /admin/refresh-cookie \
  -d '{"session_token": "new_token_value"}'
```

### Perplexity changes their API version
Update `PPLX_API_VERSION` in `.env` and restart.

## Dependencies

- `fastapi` + `uvicorn` — HTTP server
- `curl_cffi` — HTTP client with TLS fingerprinting (critical for bypassing Cloudflare)
- `mcp` — Model Context Protocol SDK (FastMCP)
- `python-dotenv` — .env loading
- `httpx` — used only for ntfy notifications (already a transitive dep of `mcp`)

## Known Limitations

- Perplexity's internal API (`/rest/sse/perplexity_ask`) can change without notice
- Cookie may be force-revoked by Perplexity (security events, account changes)
- Deep Research mode is slow (30s+) and may timeout on short-timeout clients
- Rate limits are enforced by Perplexity server-side (429 responses)
- No automatic cookie refresh — Perplexity uses magic-link login and Cloudflare Turnstile blocks headless browsers on arm64
