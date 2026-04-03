# CLAUDE.md

## What This Is

`pplx-proxy` is a self-hosted reverse proxy for Perplexity.ai. Uses your Pro/Max subscription cookie to access all models through OpenAI-compatible REST API and MCP server.

## Architecture

Single FastAPI app (`server.py`) that:

1. Receives OpenAI-format or MCP tool call requests
2. Translates to Perplexity's internal SSE (`POST /rest/sse/perplexity_ask`)
3. Uses `curl_cffi` with Chrome TLS fingerprinting to bypass Cloudflare
4. Streams responses back in OpenAI SSE or MCP format
5. Background tasks: session keep-alive (6h) + model discovery (24h)

## Key Concepts

**Account Tiers** (`ACCOUNT_TYPE` in .env):
- `free`: only `auto`
- `pro`: all models except Opus
- `max`: all models including Opus
- Tier filtering applies to API, MCP, model listing, and discovery

**Model Map**: dict of `{model_id: (mode, internal_pref)}`. Loaded from `.models.json` (persisted) or defaults. Filtered by tier at runtime. All models use `mode="copilot"` (Perplexity Pro Search).

**Auto-Discovery**: every `PROBE_INTERVAL_HOURS`, checks if models are alive. Dead models get version-incremented (e.g., `gpt54` → `gpt55`) up to +1.0. Thinking variants auto-follow base model. Sends ntfy on upgrade.

## File Structure

```
server.py            # Everything: FastAPI, Perplexity client, MCP, admin, discovery
inject_cookie.sh     # Helper to inject cookie + restart
test.sh              # Smoke test
pplx-proxy.service   # systemd unit
.env.example         # All config params
.gitignore
```

## Runtime Files (gitignored)

```
.env                 # Secrets + config
.cookie_cache.json   # Cached cookie + timestamp
.models.json         # Persisted model map
```

## Endpoints

**Public**: `GET /health`

**Auth required** (Bearer token):
- `GET /v1/models` — tier-filtered model list
- `POST /v1/chat/completions` — OpenAI chat
- `GET /admin/models` — full model map
- `POST /admin/update-models` — modify models
- `POST /admin/refresh-cookie` — inject new token
- `POST /admin/discover-models` — manual discovery run

**MCP** (no auth, MCP session):
- `POST /mcp/mcp` — Streamable HTTP
- `GET /sse/sse` — SSE transport

## MCP Tools

| Tool | Params |
|------|--------|
| `perplexity_search` | `query`, `model="default"`, `sources="web"`, `language` |
| `perplexity_ask` | `query`, `language` |
| `perplexity_reason` | `query`, `model="default"`, `language` |
| `perplexity_research` | `query`, `language` |
| `perplexity_models` | (none) — lists tier-available models |

All tools validate: empty query, invalid model, invalid sources, tier restrictions.

## Discovery Probe Strategy

Only base models are probed. Thinking variants auto-follow.

- `sonar` (`experimental`) → alive check only, no version pattern
- `gpt` (`gpt54`) → gpt55...gpt64 (max 10)
- `sonnet` (`claude46sonnet`) → claude47...claude56 (max 10)
- `opus` (`claude46opus`) → claude47...claude56 (max 10)
- `gemini` (`gemini31pro_high`) → gemini32...gemini41 (max 10)
- `nemotron` (`nv_nemotron_3_super`) → nv_nemotron_4 (max 1)

## Code Style

- No spaces around `=`: `x=1`
- One space after commas
- camelCase for locals, ALL_UPPERCASE for module constants
- Opening brace on same line

## Dependencies

- `fastapi` + `uvicorn` — HTTP server
- `curl_cffi` — TLS fingerprinting (critical)
- `mcp` — MCP SDK (FastMCP)
- `python-dotenv` — .env loading
- `httpx` — ntfy notifications (transitive dep of mcp)

## Common Tasks

```bash
# Add model
curl -X POST /admin/update-models -d '{"models":{"new":["pro","pref"]},"merge":true}'

# Update cookie
curl -X POST /admin/refresh-cookie -d '{"session_token":"NEW"}'

# Run discovery
curl -X POST /admin/discover-models

# Change tier: edit ACCOUNT_TYPE in .env, restart
```
