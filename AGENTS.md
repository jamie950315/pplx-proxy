# AGENTS.md

## What This Is

`pplx-proxy` is a self-hosted reverse proxy for Perplexity.ai. Uses your Pro/Max subscription cookie to access all models through OpenAI-compatible REST API and MCP server.

## Current Work and Deployment (2026-09-09)

- Release: image input, local file storage, and the experimental Responses function bridge are included in `main`. Production deployment target: Pi5, `pplx-proxy.service`, port **8892**.
- The user authorized merging `codex/attachments-function-tools` into `main` and deploying this release. Use isolated runtime data and a separate localhost port for further testing; verify the running service before claiming a successful rollout.
- Real PNG image input passed an official OpenAI SDK test.
- `/v1/files` local upload/read/delete passed; limits are 20 MiB per file and 200 MiB total. Local storage success is not proof of upstream document reading.
- Documents remain **incomplete**: upstream upload must be followed by `/rest/sse/attachment_processing/subscribe` and confirmed completion. Cloudflare currently returns 403; document requests explicitly fail with 502. Do not report document support as complete.
- Responses function bridge is **experimental and unreliable**. One official SDK auto-selection/client-execution/stream-continuation loop passed; a repeat returned non-JSON output and correctly failed with `response.failed` / `tool_protocol_error`, so SDK `get_final_response()` had no completed response. It is **prompt-mediated**, not native Perplexity function calling. Do not claim stable or completed support; do not add fallback or automatic retry to hide failures.
- Invalid protocol JSON/schema/tool choices fail with 502 (`tool_protocol_error`) or `response.failed`; never fall back to successful text. Function streams wait for complete validation before emitting callable items. Never execute client functions in the proxy.
- Chat Completions function tools still return 400. When a stored function continuation omits tool definitions, preserve the full context and effective definitions but use `tool_choice: none`. Explicitly resend definitions to enable further calls.

## Architecture

FastAPI app (`server.py`) with focused attachment, file-storage, and function-protocol modules that:

1. Receives OpenAI-format chat/completions or MCP requests
2. Translates to Perplexity's internal SSE (`POST /rest/sse/perplexity_ask`)
3. Uses `curl_cffi` with Chrome TLS fingerprinting to bypass Cloudflare
4. Streams responses back in OpenAI SSE or MCP format
5. Background tasks: session keep-alive (6h) + model discovery (24h)

**Critical parameter**: `search_focus: "internet"` must be set in requests to Perplexity. Without it, Perplexity defaults to `"writing"` mode and models will say "I cannot access real-time data" even though search results are found.

## Key Concepts

**Account Tiers** (`ACCOUNT_TYPE` in .env):
- `free`: only `auto`
- `pro`: all models except Opus
- `max`: all models including Opus
- Tier filtering applies to API, MCP, model listing, and discovery

**Model Map**: dict of `{model_id: (mode, internal_pref)}`. Loaded from `.models.json` (persisted) or defaults. Filtered by tier at runtime.

**Thinking Variants**: activated via `thinking: true` or `reasoning_effort != "none"`. Maps from `_THINKING_MAP` (e.g., `gpt → gpt56_terra_thinking`, `sonnet → claude50sonnetthinking`). Perplexity does NOT expose internal thinking blocks — `reasoning_content` is populated from search steps (queries, URLs, plan goals).


**Context Management**: request payloads are assembled as JSON with `instructions` / `history` / `query`. Queries exceeding 96K characters are rejected before contacting Perplexity. Consecutive assistant messages deduped (keeps last — fixes LibreChat branch artifacts). Generic clients still use whitelist-filtered system prompts from `.prompt_whitelist.txt`, but LobeHub requests now discard upstream system/developer prompt content entirely and prepend local `CUSTOM_PROMPTS` on every turn.

**Session Continuity**: the proxy tracks Perplexity's `backend_uuid` per conversation turn. On generic follow-up turns without explicit instructions (detected by hashing conversation history), only the raw user query is sent with `last_backend_uuid`. LobeHub and requests with instructions always rebuild the complete payload. Perplexity's server-side session memory handles context. Sessions expire after 1 hour. Falls back to full payload on cache miss.

**Response Cleaning** (`_clean_response`): strips `[1]` `[2]` citations, `<grok:*>` tags, `<?xml?>` declarations, `<response>` wrappers, `<script>` tags.

**Auto-Discovery**: every `PROBE_INTERVAL_HOURS`, checks if models are alive. Dead models get version-incremented (e.g., `gpt54` → `gpt55`) up to +1.0, capped at 10 probes. If Perplexity substitutes another model, that is treated as temporary unavailability, not a dead version, so discovery does not bump IDs. Pinned names such as `sonnet-4.6` are never auto-upgraded onto a later generation. It also probes known missing model names from `_MODEL_REGISTRY`, so a persisted `.models.json` can gain newly added IDs such as Grok, Haiku, GPT Mini/Nano, and Gemini Flash. Sends ntfy on upgrade or new-model discovery.

## File Structure

```
server.py            # FastAPI, Perplexity client, MCP, admin, discovery, feature integration
attachments.py       # Resolve/upload attachments and wait for upstream document processing
file_store.py        # Bounded local /v1/files storage
function_tools.py    # Validate prompt-mediated function proposals; never executes tools
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
.responses_store.json # Stored Responses API objects and function history for continuation
uploads/             # Local uploaded files under DATA_DIR (gitignored)
CUSTOM_PROMPTS       # Local prompt block prepended to every LobeHub request
```

## Startup Modes

- Manual Python runs only pplx-proxy with `uvicorn server:app --host 0.0.0.0 --port 8892`.
- Docker Compose is the complete self-hosted stack: pplx-proxy, FlareSolverr, and the `pplx-data` runtime volume.
- In Compose, `DATA_DIR=/data` and `FLARESOLVERR_URL=http://flaresolverr:8191`.
- FlareSolverr is optional for chat, but required for `/health` quota fields and quota exhaustion checks.

## Endpoints

**Public**:
- `GET /health` — health check

**Auth required** (Bearer token):
- `GET /v1/models` — tier-filtered model list (OpenAI-compatible format)
- `POST /v1/chat/completions` — chat (streaming + non-streaming, thinking). Unsupported function tools and generation controls are rejected with HTTP 400.
- `POST /v1/responses` — OpenAI Responses API compatibility; image input and experimental validated prompt-mediated function calls
- `POST` / `GET /v1/files` — local upload/list
- `GET` / `DELETE /v1/files/{id}` — local metadata/delete
- `GET /v1/files/{id}/content` — local bytes
- `GET /v1/responses/{id}` — retrieve stored response
- `DELETE /v1/responses/{id}` — delete stored response
- `POST /v1/responses/{id}/cancel` — cancel background in-progress response
- `GET /v1/responses/{id}/input_items` — list stored input items
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

**Non-streaming**: `id` (chatcmpl-*), `object` (chat.completion), `created`, `model`, `system_fingerprint` (null), `choices[].index`, `choices[].logprobs` (null), `choices[].finish_reason`, `choices[].message.role`, `choices[].message.content`, `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens` (always = prompt + completion)

**Streaming**: `object` (chat.completion.chunk), consistent `id` across all chunks, `system_fingerprint` in every chunk, `logprobs` in every choice, first chunk has `delta.role=assistant`, successful last chunk has `finish_reason` + empty `delta`, ends with `data: [DONE]`. Failed streams emit an error and never a successful finish

**Debug page**: `GET /chat` has a "Format ✓" tab that validates every response against the OpenAI spec in real-time with PASS/FAIL badges per field.

## MCP Tools

| Tool | Params |
|------|--------|
| `perplexity_search` | `query`, `model="default"`, `sources="web"`, `language` |
| `perplexity_ask` | `query`, `language` |
| `perplexity_reason` | `query`, `model="default"`, `language` |
| `perplexity_research` | `query`, `language` |
| `perplexity_models` | (none) — lists tier-available models |

Validates: empty query, invalid model, invalid sources, tier restrictions.

## Discovery Probe Strategy

Only base models are probed. Thinking variants auto-derived from `_THINKING_MAP`.

- `sonar` (`experimental`) → alive check only, no version pattern
- `gpt` (`gpt56_terra`) → gpt57...gpt66 (max 10)
- `sonnet` (`claude50sonnet`) → claude51...claude60 (max 10)
- `opus` (`claude48opus`) → claude49...claude58 (max 10)
- `opus-4.6` (`claude46opus`) → claude47...claude56 (max 10)
- `gemini` (`gemini31pro_high`) → gemini32...gemini41 (max 10)
- `grok` (`grok46low`) → grok47low...grok56low (max 10)
- `grok-reasoning` (`grok420reasoning`) → grok421reasoning... (max 10, not unbounded +1.0)
- `nemotron` (`nv_nemotron_3_super`) → nv_nemotron_4 (max 1)
- `kimi-k3` (`kimik3`) → alive check only

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

## Rate Limit Tracking

The proxy tracks Perplexity Pro Search quota via FlareSolverr:

```
Startup → FlareSolverr fetch (background, ~10s)
  ↓
Every API/MCP request → local decrement (remaining_pro -= 1)
  ↓
Every 1 hour → FlareSolverr re-sync (background)
  ↓
/health request → shows remaining_pro + triggers refresh if stale
```

### Notice Injection
At multiples of 5 (or ≤5), appended to response content:
`[Remaining Pro Search: 155]`

If Perplexity answers with a different model than requested, the answer is still returned and this is appended:
`[Substituted by Perplexity with GPT-5 Nano]`

Both notices are stripped from message history via `_strip_appended_notices` before sending to Perplexity. Tiny auxiliary tails from `gpt5_nano` after the selected model already wrote the answer are not treated as substitution.

### Quota Exhaustion
When `remaining_pro <= 0`, non-auto requests fail with HTTP 429; callers may explicitly select `auto`.
Applied in both `/v1/chat/completions` and `/v1/responses` handlers.

### FlareSolverr Dependency
Rate limit fetching requires FlareSolverr at `FLARESOLVERR_URL` (default `http://localhost:8191`). Docker Compose runs it as `http://flaresolverr:8191`. Uses `__Secure-next-auth.session-token` cookie injection to authenticate. The Perplexity REST endpoints (`/rest/rate-limit/all`) are behind Cloudflare challenge — curl_cffi cannot bypass it, only FlareSolverr (headless browser) works. FlareSolverr is optional for chat functionality; without it, `/health` marks `flaresolverr.status` as unavailable and quota fields remain null.

## Critical: Why Models Say "I Can't Access Real-Time Data"

Three layers cause Perplexity models to ignore search results and claim they can't access data. All three must be addressed:

### Layer 1: `search_focus` Parameter (Affects ALL Clients)

Perplexity's internal SSE API has a `search_focus` parameter. If omitted, it defaults to `"writing"` mode — the search engine still runs (visible in reasoning/thinking output as `Searching: ...` and `Found: [...]`), but **the model is instructed not to incorporate search results into its answer**. The model sees the results but deliberately ignores them.

**Fix:** Always set `search_focus: "internet"` in the request params. This is the single most critical parameter in the entire proxy.

### Layer 2: System Prompt Pollutes Search Results (Affects Clients with Long System Prompts)

Perplexity searches **ALL text** in the query, including system prompts. If the system prompt contains phrases like `"You are Jarvis, a personal assistant"` or `"You are Lobe, an AI Agent"`, Perplexity searches for those phrases and finds AI chatbot tutorial pages, LobeChat documentation, and prompt engineering guides. The model sees these results and concludes it's a tool-less chatbot — so it says "I don't have real-time quotes."

**Fix:** Never forward raw upstream prompt blocks blindly. Generic clients keep only whitelist-approved lines (for example language preference). LobeHub requests discard upstream system/developer prompt content entirely and use local `CUSTOM_PROMPTS` as the only `instructions` payload.

### Layer 3: System Prompts Arriving as `role: user` (Affects LobeHub Specifically)

LobeHub can send custom prompt content as `role: user` (not `role: system` or `role: developer`). The proxy still detects those messages using system-prompt keywords so it can classify the request as LobeHub and keep them out of chat history.

**Fix:** Detect user messages that contain system-prompt keywords (`you are`, `you must`, `ccsearch`, `技能`, `available_skills`) and reclassify them as `system` role. For LobeHub, those reclassified prompt blocks are used only for source detection and are not forwarded to Perplexity.

### Layer 4: Local Prompt Injection for LobeHub

LobeHub requests should always prepend the local `CUSTOM_PROMPTS` file on every turn. The final payload sent to Perplexity is:
- first turn: `instructions=[CUSTOM_PROMPTS]` + `query`
- later turns: `instructions=[CUSTOM_PROMPTS]` + `history` + `query`

This keeps behavior consistent across the whole conversation while preventing LobeHub's own XML/tool/memory prompt blocks from polluting search.

### How to Verify

If models start saying "I can't access real-time data" again:

0. Check cookie name is `__Secure-next-auth.session-token` (NOT `next-auth.session-token`). Wrong name = free-tier turbo for ALL models.
1. Check `search_focus: "internet"` is in the request params (line ~194 in `search()` method)
2. Check server logs for the query text — for generic clients, only whitelist-approved system lines should remain; for LobeHub, the payload should contain `instructions=[CUSTOM_PROMPTS]` and no upstream XML/tool prompt content.
3. Check if system prompt content is arriving as `role: user` and bypassing reclassification.

## Request Processing Pipeline — How Content Flows Through the Proxy

### Overview

All requests arrive at one of two endpoints, get processed through a shared pipeline, and are sent to Perplexity's internal SSE API. The key challenge: Perplexity does NOT accept OpenAI-format message arrays — it takes a single `query_str` text blob. The proxy builds a structured JSON string with `instructions`, `history`, and `query`, while filtering or replacing prompt content that would pollute search results.

```
Client Request
  ↓
Endpoint Router (/v1/chat/completions OR /v1/responses)
  ↓
Message Extraction & Role Normalization
  ↓
System Prompt Detection & Reclassification
  ↓
Source Detection (generic client vs LobeHub)
  ↓
Instruction Selection (whitelist-filtered system prompt OR local CUSTOM_PROMPTS)
  ↓
History Processing (dedup + current query separation)
  ↓
Query Assembly (`instructions` + `history` + `query` JSON)
  ↓
Perplexity SSE Request (search_focus=internet, model_preference, etc.)
  ↓
Response Parsing (blocks: markdown, web_results, thinking, finance_widget)
  ↓
Response Cleaning (strip citations [1][2], XML wrappers, script tags)
  ↓
Format Conversion (OpenAI chat.completion OR Responses API format)
  ↓
Client Response
```

---

### Scenario 1: curl / Generic OpenAI Client → `/v1/chat/completions`

**Input format:**
```json
{"model":"sonnet", "messages":[
  {"role":"system", "content":"Reply in Chinese"},
  {"role":"user", "content":"NVDA stock price"}
], "stream":false}
```

**Processing:**
1. Auth: Bearer token checked against `PPLX_PROXY_API_KEY`
2. Messages parsed: `system` → `system_msg`, `user` → `history[]`
3. System prompt filter: only whitelist-approved lines kept
4. Query assembled as JSON: `{"instructions":[...],"query":"NVDA stock price"}`
5. Sent to Perplexity with `search_focus: "internet"`, `model_preference: "claude46sonnet"`
6. Response parsed from SSE blocks, cleaned, returned as `chat.completion` JSON

**Simplest path — no special handling needed.**

---

### Scenario 2: LobeHub (Web Search OFF) → `/v1/chat/completions`

**Input format (3 messages with developer role):**
```json
{"model":"sonnet", "stream":true, "messages":[
  {"role":"developer", "content":"You are Lobe, an AI Agent...<available_skills>...(21KB)"},
  {"role":"user", "content":"- You are Jarvis...- You must use ccsearch tool...(2.6KB)"},
  {"role":"user", "content":"NVDA stock price (22B)"}
]}
```

**Processing:**
1. Auth: Bearer token checked
2. Role normalization: `developer` → `system`
3. System-prompt-like user message detection: second message is reclassified as `system`
4. Request source detected as `lobehub`
5. Upstream LobeHub prompt blocks are discarded
6. Local `CUSTOM_PROMPTS` is loaded and used as `instructions`
7. Query assembled as JSON: first turn `{"instructions":[CUSTOM_PROMPTS],"query":"NVDA stock price"}`; later turns add `history`
8. Sent to Perplexity, response streamed as SSE `chat.completion.chunk` events

**Key special handling:**
- `developer` role mapping
- System-prompt-like user message detection
- LobeHub source detection
- Replace upstream prompt blocks with local `CUSTOM_PROMPTS`
- Consecutive assistant branch dedup

---

### Scenario 3: LobeHub (Web Search ON) → `/v1/responses`

**Input format (Responses API with web_search tool):**
```json
{"stream":true, "model":"sonnet", "reasoning":{"effort":"low"},
 "input":[
   {"role":"developer", "content":"You are Lobe...(21KB)"},
   {"role":"user", "content":"- You are Jarvis...(2.6KB)"},
   {"role":"user", "content":"NVDA stock price"}
 ],
 "tools":[{"type":"web_search_preview_2025_03_11"}]
}
```

**Processing:**
1. Auth: Bearer token checked
2. Input array parsed: each item's `role` and `content` extracted
3. `developer` → `system`, system-prompt-like user messages → `system`
4. Request source detected as `lobehub`
5. `web_search_preview` maps to the built-in search behavior (`search_focus: "internet"`); unsupported tools are rejected
6. Upstream prompt blocks discarded; local `CUSTOM_PROMPTS` becomes `instructions`
7. Query built directly as JSON (no httpx self-call), sent to Perplexity client
8. Response streamed as Responses API SSE events

**Key special handling:**
- Responses API format translation (input→messages, output→response object)
- Built-in web search compatibility; unsupported tools are rejected
- LobeHub source detection + local prompt replacement
- Reasoning summary events for thinking block display
- Calls Perplexity client directly (not through internal HTTP)

---

### Scenario 4: LibreChat → `/v1/chat/completions`

**Input format (with conversation branches):**
```json
{"model":"sonnet", "stream":true, "messages":[
  {"role":"system", "content":"- You are Jarvis...- You must use ccsearch..."},
  {"role":"user", "content":"TSMC stock price"},
  {"role":"assistant", "content":"I can't access real-time data..."},
  {"role":"assistant", "content":"Sorry, I don't have..."},
  {"role":"assistant", "content":"I need to use tools..."},
  {"role":"user", "content":"just give me the price"}
]}
```

**Processing:**
1. Auth checked
2. System prompt filter: strips tool/skill refs, keeps whitelist-approved lines
3. Consecutive assistant dedup: 3 assistant messages → keep only last one
4. History built as JSON `history` array
5. Current user message separated into `query`
6. Query assembled and sent to Perplexity
7. Response streamed as `chat.completion.chunk` SSE events

**Key special handling:**
- Generic-client whitelist prompt filtering
- Consecutive assistant dedup
- Structured JSON query assembly

---

### Scenario 6: MCP Client → `/{API_KEY}/mcp` or `/{API_KEY}/sse`

**Processing:**
1. Auth via API key in URL path (not Bearer header)
2. MCP protocol: initialize → tools/list → tools/call
3. Each tool (`perplexity_search`, `perplexity_ask`, etc.) calls `client.search()` directly
4. No message array processing — query string goes directly to Perplexity
5. Response returned as MCP tool result (plain text)

**No system prompt filter, no history processing, no dedup — just direct search.**

---

### The Perplexity SSE Request (shared by all scenarios)

Regardless of which endpoint or client, all queries are sent via:

```
POST https://www.perplexity.ai/rest/sse/perplexity_ask

{
  "query_str": "<JSON string with instructions/history/query>",
  "params": {
    "search_focus": "internet",          ← CRITICAL: enables search results in answer
    "mode": "copilot",                   ← "concise" for auto model only
    "model_preference": "claude46sonnet", ← internal Perplexity model ID
    "sources": ["web"],
    "use_schematized_api": true,
    "supported_block_use_cases": ["answer_modes", "finance_widgets", ...],
    "timezone": "Asia/Taipei",
    "version": "2.18",
    ... (13 other params)
  }
}
```

### The Perplexity SSE Response (shared parsing)

Perplexity returns SSE events containing `blocks[]` with these types:

| Block `intended_usage` | Contains | How We Use It |
|---|---|---|
| `ask_text_0_markdown` | Answer text chunks | → `content` in response |
| `web_results` | Search result URLs + snippets | → `reasoning_content` (Found: URLs) |
| `pro_search_steps` | Search queries executed | → `reasoning_content` (Searching: query) |
| `plan` | Reasoning plan goals | → `reasoning_content` |
| `finance_widget` | Structured stock data (JSON) | Currently ignored (model writes price in text) |
| `sources_answer_mode` | Citation sources | Currently ignored |


## Review and Validation (2026-09-09)

- `store=false` Responses are not retained. Corrupt runtime stores and write failures surface as errors.
- Health checks return cached quota immediately and schedule stale refreshes without waiting for a browser. Check `flaresolverr.status` and `last_error` for quota-fetch failures.
- This release includes verified PNG input and local file storage. Responses function proposals remain experimental after a successful loop and a failed repeat; document input still fails during upstream processing. Chat function tools remain 400. Custom sampling/output limits and strict text-output schema enforcement are rejected; function parameter validation is separate.
- Upstream streams must terminate correctly; interrupted or malformed streams fail. Resources and background tasks close on shutdown. MCP is a required dependency; incompatible installations fail at startup.
- Prompt bodies are logged only at DEBUG. Docker excludes cookies, response history, and browser state.
- Pre-release feature verification: 147 Python tests and 7 Node tests passed locally; Docker build and its 147 Python tests passed. Isolated ordinary Chat, streaming, and Responses smoke checks passed. These tests validate proxy behavior, not model protocol compliance, and do not remove the document-processing blocker or the observed function reliability failure.
- Verify with `venv/bin/python -m unittest discover`, `node --test test_chat.js`, `venv/bin/python -m compileall -q server.py smoke_test.py`, Docker build, and `./test.sh URL` against an isolated service connected to Perplexity.
- Deploy `main` to Pi5 via `pplx-proxy.service` on port 8892. Production rollout verification must cover ordinary Chat, streaming, and Responses, with existing credentials preserved. Use isolated runtime data and a separate localhost port for further feature tests.
