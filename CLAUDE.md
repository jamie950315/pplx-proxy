# CLAUDE.md

## What This Is

`pplx-proxy` is a self-hosted reverse proxy for Perplexity.ai. Uses your Pro/Max subscription cookie to access all models through OpenAI-compatible REST API and MCP server.

## Architecture

Single FastAPI app (`server.py`, ~1750 lines) that:

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

**Thinking Variants**: activated via `thinking: true` or `reasoning_effort != "none"`. Maps from `_THINKING_MAP` (e.g., `gpt → gpt54_thinking`, `sonnet → claude46sonnetthinking`). Perplexity does NOT expose internal thinking blocks — `reasoning_content` is populated from search steps (queries, URLs, plan goals).


**Context Management**: request payloads are assembled as JSON with `instructions` / `history` / `query`. Total query capped at 96K chars (~32K tokens). Consecutive same-role messages deduped (keeps last — fixes LibreChat branch artifacts). Generic clients still use whitelist-filtered system prompts from `.prompt_whitelist.txt`, but LobeHub requests now discard upstream system/developer prompt content entirely and prepend local `CUSTOM_PROMPTS` on every turn.

**Session Continuity**: the proxy tracks Perplexity's `backend_uuid` per conversation turn. On follow-up turns (detected by hashing conversation history), only the raw user query is sent with `last_backend_uuid` — no instructions, no history. Perplexity's server-side session memory handles context. Sessions expire after 1 hour. Falls back to full payload on cache miss.

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
CUSTOM_PROMPTS       # Local prompt block prepended to every LobeHub request
```

## Endpoints

**Public**:
- `GET /health` — health check

**Auth required** (Bearer token):
- `GET /v1/models` — tier-filtered model list (OpenAI-compatible format)
- `POST /v1/chat/completions` — chat (streaming + non-streaming, thinking). `tools` parameter silently ignored.
- `POST /v1/responses` — OpenAI Responses API compatibility (translates to chat/completions internally, used by LobeHub web search mode)
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

Validates: empty query, invalid model, invalid sources, tier restrictions.

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

Stripped from message history via `_REMAINING_NOTICE_RE` regex before sending to Perplexity.

### Quota Fallback
When `remaining_pro <= 0`: all non-auto models auto-downgrade to `auto` (pplx_pro).
Applied in both `/v1/chat/completions` and `/v1/responses` handlers.

### FlareSolverr Dependency
Rate limit fetching requires FlareSolverr at `http://localhost:8191`. Uses `__Secure-next-auth.session-token` cookie injection to authenticate. The Perplexity REST endpoints (`/rest/rate-limit/all`) are behind Cloudflare challenge — curl_cffi cannot bypass it, only FlareSolverr (headless browser) works.

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
5. `web_search_preview` tool silently ignored (we always have `search_focus: "internet"`)
6. Upstream prompt blocks discarded; local `CUSTOM_PROMPTS` becomes `instructions`
7. Query built directly as JSON (no httpx self-call), sent to Perplexity client
8. Response streamed as Responses API SSE events

**Key special handling:**
- Responses API format translation (input→messages, output→response object)
- `web_search_preview` tool silently dropped
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

# currentDate
Today's date is 2026-04-05.

      IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context unless it is highly relevant to your task.

### How to Verify

If models start saying "I can't access real-time data" again:

0. Check cookie name is `__Secure-next-auth.session-token` (NOT `next-auth.session-token`). Wrong name = free-tier turbo for ALL models.
1. Check `search_focus: "internet"` is in the request params (line ~194 in `search()` method)
2. Check server logs for the query text — if it contains system prompt content (role-play, tool refs, AI agent descriptions), the filter is broken
3. Check if system prompt content is arriving as `role: user` and bypassing the filter

## Request Processing Pipeline — How Content Flows Through the Proxy

### Overview

All requests arrive at one of two endpoints, get processed through a shared pipeline, and are sent to Perplexity's internal SSE API. The key challenge: Perplexity does NOT accept OpenAI-format message arrays — it takes a single `query_str` text blob. The proxy must flatten conversations into text while filtering content that pollutes search results.

```
Client Request
  ↓
Endpoint Router (/v1/chat/completions OR /v1/responses)
  ↓
Message Extraction & Role Normalization
  ↓
System Prompt Detection & Reclassification
  ↓
System Prompt Filter (strip everything except language preference)
  ↓
History Processing (truncation, dedup, topic separation)
  ↓
Query Assembly (system instruction + history + current request)
  ↓
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
3. System prompt filter: only language preference kept
4. Query assembled: `[Reply language: ...]\n[You have built-in web search...]\n\nNVDA stock price`
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
3. **System prompt detection on user messages**: second message starts with `"you are "` and contains `"ccsearch"` → reclassified as `system`
4. Now we have: `system`(21KB) + `system`(2.6KB) + `user`(22B)
5. Multiple system messages concatenated into one `system_msg`
6. **System prompt filter**: 23.6KB of system prompt → scanned line by line → only language preference line kept (e.g., "Always reply in Traditional Chinese") → everything else stripped
7. Query assembled: `[Reply language: Always reply in Traditional Chinese...]\n[You have built-in web search...]\n\nNVDA stock price`
8. **Consecutive assistant dedup** applies if regeneration branches exist
9. Sent to Perplexity, response streamed as SSE `chat.completion.chunk` events

**Key special handling:**
- `developer` role mapping
- System-prompt-like user message detection
- Aggressive system prompt stripping (23.6KB → ~100 chars)
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
4. `web_search_preview` tool silently ignored (we always have `search_focus: "internet"`)
5. System prompt filter: same aggressive stripping as Scenario 2
6. Query built directly (no httpx self-call), sent to Perplexity client
7. Response streamed as Responses API SSE events:
   - `response.created`
   - `response.reasoning_summary_text.delta` (search steps: Found URLs, Searching queries)
   - `response.reasoning_summary_text.done`
   - `response.output_text.delta` (answer chunks)
   - `response.output_text.done`
   - `response.completed`

**Key special handling:**
- Responses API format translation (input→messages, output→response object)
- `web_search_preview` tool silently dropped
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
2. System prompt filter: strips tool/skill refs, keeps language pref
3. **Consecutive assistant dedup**: 3 assistant messages → keep only last one
4. History built: `[user: "TSMC stock price", assistant: "I need to use tools...(last branch)"]`
5. **Topic separation**: current message `"just give me the price"` prefixed with `User's current request:` to prevent topic bleeding from history
6. Query assembled and sent to Perplexity
7. Response streamed as `chat.completion.chunk` SSE events

**Key special handling:**
- Consecutive assistant dedup (branch artifacts)
- Topic separation prefix

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
  "query_str": "<flattened query text>",
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
