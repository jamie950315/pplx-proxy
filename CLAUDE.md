# CLAUDE.md

## What This Is

`pplx-proxy` is a self-hosted reverse proxy for Perplexity.ai. Uses your Pro/Max subscription cookie to access all models through OpenAI-compatible REST API and MCP server.

## Architecture

Single FastAPI app (`server.py`, ~1440 lines) that:

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

**Model Map**: dict of `{model_id: (mode, internal_pref)}`. Loaded from `.models.json` (persisted) or defaults. Filtered by tier at runtime.

**Thinking Variants**: activated via `thinking: true` or `reasoning_effort != "none"`. Maps from `_THINKING_MAP` (e.g., `gpt → gpt54_thinking`, `sonnet → claude46sonnetthinking`). Perplexity does NOT expose internal thinking blocks — `reasoning_content` is populated from search steps (queries, URLs, plan goals).

**Tool Calling**: implemented via prompt injection (Perplexity has no native tool calling). 3-layer defense against false positives:
1. **Relevance heuristic** (`_should_inject_tools`): only inject tool prompt if user message has keyword overlap with tool names/descriptions
2. **Schema validation** (`_validate_tool_calls`): validates function name, required params present, no empty values
3. **XML cleanup** (`_strip_xml_wrapper`): strips `<response>`, `<answer>` wrappers when not a tool call

**Context Management**: system prompt / conversation history / current message separated. Empty assistant messages (from tool_calls) → `[done]` placeholder. Assistant messages truncated to 200 chars. Last 16 items (~8 turns) kept. Tool results formatted as `Result: {content}`.

**Response Cleaning** (`_clean_response`): strips `[1]` `[2]` citations, `<grok:*>` tags, `<?xml?>` declarations, `<response>` wrappers, `<script>` tags.

**Auto-Discovery**: every `PROBE_INTERVAL_HOURS`, checks if models are alive. Dead models get version-incremented (e.g., `gpt54` → `gpt55`) up to +1.0. Sends ntfy on upgrade.

## File Structure

```
server.py            # Everything: FastAPI, Perplexity client, MCP, admin, discovery
static/chat.html     # Debug chat UI with OpenAI format validator
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

**Public**:
- `GET /health` — health check
- `GET /chat` — debug chat UI with OpenAI format validator (test tool calling, streaming, thinking, format compliance)

**Auth required** (Bearer token):
- `GET /v1/models` — tier-filtered model list (OpenAI-compatible format)
- `POST /v1/chat/completions` — chat (streaming + non-streaming, tool calling, thinking)
- `GET /admin/models` — full model map with internal details
- `POST /admin/update-models` — modify models
- `POST /admin/refresh-cookie` — inject new token
- `POST /admin/discover-models` — manual discovery run

**MCP** (API key in URL path):
- `POST /<api-key>/mcp` — Streamable HTTP
- `GET /<api-key>/sse` — SSE transport
- `POST /messages/?session_id=...` — SSE message relay (session_id is auth)
- Without `PPLX_PROXY_API_KEY`: falls back to `/mcp/mcp` + `/sse/sse` (no auth)

## OpenAI Format Compliance

All responses strictly follow the OpenAI Chat Completions spec:

**Non-streaming**: `id` (chatcmpl-*), `object` (chat.completion), `created`, `model`, `system_fingerprint` (null), `choices[].index`, `choices[].logprobs` (null), `choices[].finish_reason`, `choices[].message.role`, `choices[].message.content`, `choices[].message.tool_calls`, `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens` (always = prompt + completion)

**Streaming**: `object` (chat.completion.chunk), consistent `id` across all chunks, `system_fingerprint` in every chunk, `logprobs` in every choice, first chunk has `delta.role=assistant`, last chunk has `finish_reason` + empty `delta`, ends with `data: [DONE]`

**Debug page**: `GET /chat` has a "Format ✓" tab that validates every response against the OpenAI spec in real-time with PASS/FAIL badges per field.

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

Only base models are probed. Thinking variants auto-derived from `_THINKING_MAP`.

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
# Test format compliance visually
open http://localhost:8892/chat

# Add model
curl -X POST /admin/update-models -d '{"models":{"new":["pro","pref"]},"merge":true}'

# Update cookie
curl -X POST /admin/refresh-cookie -d '{"session_token":"NEW"}'

# Run discovery
curl -X POST /admin/discover-models

# Change tier: edit ACCOUNT_TYPE in .env, restart
```
