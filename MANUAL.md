# pplx-proxy Usage Manual

A comprehensive guide to installing, configuring, and using pplx-proxy — a self-hosted reverse proxy that turns your Perplexity Pro/Max subscription into a standard OpenAI-compatible API and MCP server.

---

## Feature limitations

Image input and local file storage are supported. Document input depends on an upstream processing endpoint that has returned Cloudflare 403; processing failures return HTTP 502. The Responses function bridge is experimental and prompt-mediated, and invalid output fails explicitly.

## Table of Contents

1. [What Is pplx-proxy?](#1-what-is-pplx-proxy)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [Getting Your Cookie](#4-getting-your-cookie)
5. [Configuration](#5-configuration)
6. [Running the Service](#6-running-the-service)
7. [API Reference](#7-api-reference)
8. [Chat Completions API](#8-chat-completions-api)
9. [Streaming](#9-streaming)
11. [Thinking / Reasoning](#11-thinking--reasoning)
12. [MCP Server Integration](#12-mcp-server-integration)
13. [Debug Chat UI](#13-debug-chat-ui)
14. [Admin Endpoints](#14-admin-endpoints)
15. [Context Management](#15-context-management)
16. [Production Deployment](#16-production-deployment)
17. [Cookie Lifecycle](#17-cookie-lifecycle)
18. [Troubleshooting](#18-troubleshooting)
19. [Known Limitations](#19-known-limitations)

---

## 1. What Is pplx-proxy?

pplx-proxy is a FastAPI application that sits between your application and Perplexity.ai's web backend. Instead of using Perplexity's official (paid) API, it authenticates with your existing Pro or Max subscription cookie, translates requests into Perplexity's internal SSE protocol, and exposes the result through industry-standard interfaces.

Depending on account and upstream availability, this provides access to GPT-6.1 Sol, Claude Sonnet 5.5, Gemini 3.8 Flash, Grok 4.7, GLM-5.3, Nemotron 3.5 Super VL, and other models Perplexity offers — all through the same API format your tools already speak.

Three interfaces are available:

- **OpenAI-compatible REST API** at `/v1/chat/completions` — works with any OpenAI SDK or client.
- **MCP server** via Streamable HTTP and SSE — integrates directly with Claude Code, Claude Desktop, and other MCP clients.
- **Debug chat UI** at `/chat` — a browser-based test interface with real-time OpenAI format validation.

---

## 2. Prerequisites

- Python 3.11 or later
- A Perplexity Pro or Max subscription (free tier works but only exposes the `auto` model)
- A modern browser to extract the session cookie

---

## 3. Installation

Clone the repository and set up a virtual environment:

```bash
git clone https://github.com/jamie950315/pplx-proxy.git
cd pplx-proxy

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy the example configuration and fill in your values:

```bash
cp .env.example .env
```

At minimum, you need to set `PPLX_COOKIE`. See the next two sections.

---

## 4. Getting Your Cookie

pplx-proxy authenticates by replaying the session cookie from your browser.

**Step-by-step:**

1. Open [perplexity.ai](https://www.perplexity.ai) and sign in to your Pro or Max account.
2. Open browser DevTools (F12 or Cmd+Option+I).
3. Navigate to **Application** → **Cookies** → `https://www.perplexity.ai`.
4. Find the cookie named `__Secure-next-auth.session-token`.
5. Copy its **Value** (a long JWT-like string).
6. Paste it into your `.env` file:

```
PPLX_COOKIE=eyJhbGciOiJkaX...your_token_here...
```

Cookie validity is controlled by Perplexity. The proxy periodically validates the session and persists rotated cookies.

---

## 5. Configuration

All settings live in the `.env` file. Here is the full reference:

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | *(required)* | Your `__Secure-next-auth.session-token` from Perplexity |
| `PPLX_PROXY_API_KEY` | *(empty)* | Bearer token for API auth. If empty, no authentication is required |
| `ACCOUNT_TYPE` | `pro` | Your subscription tier: `free`, `pro`, or `max` |
| `DEFAULT_MODEL` | `gpt` | Model used when no model is specified in the request |
| `PPLX_PROXY_PORT` | `8892` | Port used by the built-in runner and Compose host mapping; pass `--port` explicitly to uvicorn |
| `DATA_DIR` | project directory | Runtime cookie, model and response stores, discovery caches, and uploads |
| `FLARESOLVERR_URL` | `http://localhost:8191` | Endpoint for quota data |
| `KEEPALIVE_HOURS` | `6` | Interval between session validation and cookie-rotation persistence |
| `PROBE_DISCOVERY_ENABLED` | `true` | Fetch authenticated picker at startup and schedule refreshes |
| `PROBE_INTERVAL_HOURS` | `24` | Positive interval between picker refreshes |
| `PROBE_TIMEOUT_MINUTES` | `15` | Positive timeout in minutes for each discovery session/catalogue request |
| `NTFY_TOPIC` | disabled | Unique ntfy.sh topic: maximum 64 characters using letters, numbers, `_`, or `-`; the shared public `pplx-proxy` name is rejected |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server URL |
| `NTFY_COOLDOWN_SECS` | `3600` | Minimum interval between push notifications |
| `PUBLIC_URL` | `http://localhost:8892` | Public URL shown in ntfy messages and used for MCP host validation |
| `PPLX_API_VERSION` | `2.18` | Perplexity internal API version string |
| `PPLX_IMPERSONATE` | `chrome` | TLS fingerprint target for curl_cffi |
| `USER_AGENT` | Chrome 130 string | HTTP User-Agent header |
| `COOKIE_MAX_AGE_HOURS` | `168` | Maximum age before cookie cache is considered stale |
| `LOG_LEVEL` | `INFO` | Python logging level |

---

## 6. Running the Service

There are two supported startup modes:

| Mode | Command | Includes |
|------|---------|----------|
| Manual Python | `uvicorn server:app --host 0.0.0.0 --port 8892` | pplx-proxy only |
| Docker Compose | `docker compose up -d --build` | pplx-proxy, FlareSolverr, persistent runtime volume |

Manual Python is best for development. Docker Compose is the complete self-hosted stack and is recommended when you want `/health` quota fields.

**Development (foreground):**

```bash
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8892
```

**Docker Compose (full stack):**

```bash
cp .env.example .env
# Edit .env — set PPLX_COOKIE, PPLX_PROXY_API_KEY, and ACCOUNT_TYPE
docker compose up -d --build
```

The Compose stack starts:

- `pplx-proxy` on `http://localhost:8892`
- `flaresolverr` inside the Compose network at `http://flaresolverr:8191`
- `pplx-data` volume for `.cookie_cache.json`, `.models.json`, and `.responses_store.json`

**Quick smoke test:**

```bash
# Health check
curl http://localhost:8892/health

# Chat (replace YOUR_KEY with your PPLX_PROXY_API_KEY)
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"Hello"}]}'
```

**Open the debug UI:** visit `http://localhost:8892/chat` in your browser.

---

## 7. API Reference

### Available Models

| Model ID | Backend Model | Min. Tier | Notes |
|----------|---------------|-----------|-------|
| `auto` | Perplexity Best | free | Default Perplexity experience |
| `sonar` | Sonar 2 | pro | Perplexity's experimental model |
| `gpt` / `gpt-6.1-sol` | GPT-6.1 Sol | pro | Paired reasoning variant |
| `gpt-5.6-terra` | GPT-5.6 Terra | pro | Legacy compatibility ID |
| `gpt-5.6-sol` | GPT-5.6 Sol | max | Highest-capability GPT-5.6 variant |
| `gpt-5.5` | GPT-5.5 | pro | Previous GPT model |
| `gpt-mini` | GPT-5 Mini | pro | Smaller GPT model |
| `gpt-nano` | GPT-5 Nano | pro | Fastest GPT model |
| `sonnet` / `sonnet-5.5` | Claude Sonnet 5.5 | pro | Paired reasoning variant |
| `sonnet-5` | Claude Sonnet 5 | pro | Legacy compatibility ID |
| `sonnet-4.6` | Claude Sonnet 4.6 | pro | Previous Sonnet version |
| `gemini` | Gemini 3.1 Pro | pro | Google's flagship |
| `gemini-flash` / `gemini-3.8-flash` | Gemini 3.8 Flash | pro | Paired reasoning variant |
| `grok` / `grok-4.7` | Grok 4.7 | pro | Paired reasoning variant |
| `grok-4.6` | Grok 4.6 | pro | Legacy compatibility ID |
| `grok-4.5` | Grok 4.5 | pro | Previous Grok version |
| `grok-4` | Grok 4 | pro | Previous Grok version |
| `grok-reasoning` | Grok 4.20 Reasoning | pro | xAI reasoning model |
| `grok-non-reasoning` | Grok 4.20 Non Reasoning | pro | xAI non-reasoning model |
| `nemotron` / `nemotron-3.5-super-vl` | Nemotron 3.5 Super VL | pro | Reasoning-only `nv_nemotron_3_5_super` |
| `nemotron-3-super` | Nemotron 3 Super | pro | Previous Nemotron version |
| `glm-5.3` | GLM-5.3 | pro | Reasoning-only `glm_5_3_thinking` |
| `glm-5.2` | GLM-5.2 | pro | Legacy compatibility ID |
| `kimi-k2.6` | Kimi K2.6 | pro | Moonshot AI model |
| `kimi-k3` | Kimi K3 | pro | Moonshot AI model (thinking always on) |
| `opus` / `opus-4.8` | Claude Opus 4.8 | max | Anthropic's most capable |
| `opus-4.7` | Claude Opus 4.7 | max | Previous Opus version |
| `opus-4.6` | Claude Opus 4.6 | max | Previous Opus flagship |

This table describes built-in compatibility defaults, not the current upstream catalogue. Use authenticated `GET /v1/models` for exact picker IDs, labels, tier availability, and source. New searchable picker names appear automatically. Explicit mappings in `.models.json` or saved through `/admin/update-models` remain unchanged across refresh/restart; therefore a configured alias may differ from this table. Legacy IDs are retained, but Perplexity may substitute an unavailable model, with a visible response notice.

Static candidates such as Haiku or Multi-Agent remain disabled unless verified picker entries make their actual upstream IDs available. Custom IDs are available on Pro/Max accounts; Free accounts expose only `auto`.

Discovery uses the existing authenticated AsyncSession with same-origin AJAX metadata, not navigation headers. Navigation metadata and forced HTTP/1.1 have returned Cloudflare 403; retain normal HTTP negotiation. A failed refresh preserves the private last-known-good cache and reports `/health.model_discovery.last_error` plus a warning. No automatic inference fallback conceals the failure. Cache files are `DATA_DIR/.discovered_models.<account-hash>.json`, atomically written with mode 0600 and without credentials. Disable discovery with `PROBE_DISCOVERY_ENABLED=false`; no periodic task is started.

The retained numeric-probe helpers are only explicit diagnostics/tests. They cannot discover arbitrary names, changed suffixes, or new families; only the picker provides general name discovery.

### Endpoint Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check + cookie age |
| GET | `/chat` | No | Debug chat UI |
| GET | `/debug` | No | Redirect to `/chat` |
| GET | `/v1/models` | Bearer | List available models |
| POST | `/v1/chat/completions` | Bearer | Chat completions |
| POST | `/v1/responses` | Bearer | OpenAI Responses API create; image input and experimental function bridge |
| GET | `/v1/responses/{id}` | Bearer | Retrieve a stored response |
| DELETE | `/v1/responses/{id}` | Bearer | Delete a stored response |
| POST | `/v1/responses/{id}/cancel` | Bearer | Cancel an in-progress background response |
| GET | `/v1/responses/{id}/input_items` | Bearer | List input items for a stored response |
| POST / GET | `/v1/files` | Bearer | Upload/list local files |
| GET / DELETE | `/v1/files/{id}` | Bearer | read metadata/delete local file |
| GET | `/v1/files/{id}/content` | Bearer | read local file bytes |
| POST | `/{api_key}/mcp` | URL key | MCP Streamable HTTP |
| GET | `/{api_key}/sse` | URL key | MCP SSE transport |
| GET | `/admin/models` | Bearer | Full model map |
| POST | `/admin/update-models` | Bearer | Modify model map |
| POST | `/admin/refresh-cookie` | Bearer | Inject new cookie |
| POST | `/admin/discover-models` | Bearer | Trigger model discovery |

### Authentication

**REST API:** when `PPLX_PROXY_API_KEY` is set, pass it as a Bearer token; an empty key disables REST authentication:
```
Authorization: Bearer YOUR_KEY
```

**MCP:** the API key is embedded in the URL path:
```
https://your-domain/YOUR_KEY/mcp
https://your-domain/YOUR_KEY/sse
```

Bare `/mcp` and `/sse` paths without a key return 401.

---

## 8. Chat Completions API

The main endpoint follows the [OpenAI Chat Completions spec](https://platform.openai.com/docs/api-reference/chat/create).

### Request Body

```json
{
  "model": "sonnet",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"}
  ],
  "stream": false,
  "temperature": 0.7
}
```

**Supported fields:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | One of the model IDs above |
| `messages` | array | Conversation history (system, developer, user, assistant roles) |
| `stream` | boolean | Enable SSE streaming (default: false) |
| `tools` | array | Function tools are unsupported and rejected |
| `tool_choice` | string | `auto` or `none`; function calling is unsupported |
| `thinking` | boolean | Enable reasoning output |
| `reasoning_effort` | string | `none`, `low`, `medium`, `high` |

### Response (non-streaming)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1712188800,
  "model": "sonnet",
  "system_fingerprint": null,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The capital of France is Paris."
      },
      "finish_reason": "stop",
      "logprobs": null
    }
  ],
  "usage": {
    "prompt_tokens": 25,
    "completion_tokens": 12,
    "total_tokens": 37
  }
}
```

### Multi-Turn Conversations

Include previous messages in the `messages` array:

```json
{
  "model": "sonnet",
  "messages": [
    {"role": "user", "content": "My name is Alice."},
    {"role": "assistant", "content": "Nice to meet you, Alice!"},
    {"role": "user", "content": "What is my name?"}
  ]
}
```

The proxy flattens the conversation into a structured text block that Perplexity understands, preserving context across turns.

---

## 9. Streaming

Set `"stream": true` to receive Server-Sent Events:

```bash
curl -N http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"Tell me a joke"}]}'
```

Each chunk follows the OpenAI streaming format:

```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"content":"Why"},"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

Key guarantees:

- The `id` is consistent across all chunks.
- The first chunk contains `delta.role`.
- The final chunk has an empty `delta` and `finish_reason: "stop"`.
- Successful streams terminate with `[DONE]`. Failed streams report an explicit error and do not send a successful completion marker.

---


## 11. Thinking / Reasoning

Some models offer a paired reasoning preference. Picker-verified base models use that exact pair; a missing pair returns HTTP 400 rather than generating a preference name. Reasoning-only models are directly invokable. Visible reasoning content contains search/planning summaries, not private internal reasoning.

Enable it with either:

```json
{"thinking": true}
```

or:

```json
{"reasoning_effort": "high"}
```

The reasoning output appears in `choices[0].message.reasoning_content`:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "The answer is 42.",
      "reasoning_content": "Searching the web\nSearching: answer to life\nFound: [Wikipedia]..."
    }
  }]
}
```

In streaming mode, reasoning chunks arrive before content chunks with `delta.reasoning_content`.

**Note:** Perplexity does not expose the model's internal chain-of-thought. The `reasoning_content` is populated from Perplexity's visible search steps — search queries, URLs found, and plan goals.

| Model | Thinking Variant |
|-------|-----------------|
| `gpt` / `gpt-6.1-sol` | `gpt6_1_sol_thinking` |
| `gpt-5.6-terra` | `gpt56_terra_thinking` |
| `gpt-5.6-sol` | `gpt56_sol_thinking` |
| `gpt-5.5` | `gpt55_thinking` |
| `sonnet` / `sonnet-5.5` | `claude55sonnetthinking` |
| `sonnet-5` | `claude50sonnetthinking` |
| `sonnet-4.6` | `claude46sonnetthinking` |
| `grok-4.5` | `grok45medium` |
| `kimi-k2.6` | `kimik26thinking` |
| `opus` / `opus-4.8` | `claude48opusthinking` |
| `opus-4.7` | `claude47opusthinking` |
| `opus-4.6` | `claude46opusthinking` |

### Rate Limit Tracking

pplx-proxy tracks your Perplexity Pro Search quota (`remaining_pro`) and provides visibility and protection:

**How it works:**
- On startup, a background task fetches rate limits from Perplexity via FlareSolverr (headless browser, ~10s).
- Every 1 hour, the counter re-syncs with Perplexity's actual data.
- After each Pro Search query (copilot mode), the local counter decrements by 1.
- When `/health` is requested with missing or stale quota data, it returns cached/null values immediately and schedules refresh in the background.

**Quota notice:** When `remaining_pro` hits a multiple of 5 (160, 155, 150, ..., 5, 0), the response includes:
```
[Remaining Pro Search: 155]
```
This notice is automatically stripped from message history in subsequent API calls so it doesn't pollute Perplexity search results.

**Quota exhaustion:** When `remaining_pro` reaches 0, non-auto model requests return HTTP 429. Select `auto` (pplx_pro, free tier) explicitly to continue with the free model.

**Requirements:** FlareSolverr must be running at `FLARESOLVERR_URL` (default `http://localhost:8191`) for rate limit syncing. It is optional for chat, streaming, MCP, and Responses API. Without it, `/health` shows `flaresolverr.status: "unavailable"` and quota fields stay `null`.

### Responses API (`/v1/responses`)

The `/v1/responses` endpoint implements OpenAI Responses API compatibility. It is used by LobeHub when "built-in web search" is enabled, and also works with the official OpenAI SDK `client.responses.create()`.

Supported:

- `POST /v1/responses` — create a response (`input` string or item list, `instructions`, `stream`, `store`, `previous_response_id`, `conversation`, `reasoning`, `background`, `text.format`)
- `GET /v1/responses/{id}` — retrieve a stored response
- `DELETE /v1/responses/{id}` — delete a stored response
- `POST /v1/responses/{id}/cancel` — cancel an in-progress background response
- `GET /v1/responses/{id}/input_items` — list the original input items

Multi-turn: pass `previous_response_id` from the previous response. Previous `instructions` are not reused; send new ones if needed. `conversation` can also continue the last stored turn for that conversation id.

Stored responses live in `.responses_store.json` (max 300, 7-day TTL). `store: false` does not retain the response, so retrieval and continuation by that response ID are unavailable.

Background requests require `store: true`; only active background responses can be cancelled. Built-in web-search requests accept only `tool_choice: "auto"`; the experimental function choices are described below. Custom `temperature`/`top_p` values other than 1, `max_output_tokens`, `truncation: "auto"`, and strict text-output JSON schemas are rejected. JSON-object mode and non-strict text schemas use prompt instructions only; output conformance is not guaranteed. Store read/write failures are reported instead of treating failed persistence as success.

For ordinary text requests, successful streaming emits these SSE events in order (the reasoning events are omitted when no reasoning is available):

1. `response.created` — response object with status `in_progress`
2. `response.in_progress` — same object, still running
3. `response.output_item.added` — reasoning item (only if search/thinking steps exist)
4. `response.reasoning_summary_part.added` — reasoning block starts
5. `response.reasoning_summary_text.delta` — search step info (Found URLs, search queries), repeated per step
6. `response.reasoning_summary_text.done` — full reasoning text
7. `response.reasoning_summary_part.done` — reasoning block ends
8. `response.output_item.done` — reasoning item complete
9. `response.output_item.added` — assistant message item
10. `response.content_part.added` — text part starts
11. `response.output_text.delta` — answer text chunks, repeated
12. `response.output_text.done` — full answer text
13. `response.content_part.done` / `response.output_item.done` — message complete
14. `response.completed` — final response with output and usage

The `developer` role is accepted and mapped to `system` internally. Built-in web search tools (`web_search` and `web_search_preview`) are accepted since `search_focus: "internet"` is always active.

File search, code interpreter, computer use, image generation, and encrypted reasoning are not implemented. Unsupported features return explicit errors.

#### Experimental Responses function calling

The proxy provides **prompt-mediated Responses function calling**, not native Perplexity tools. Invalid protocol output produces `response.failed` / `tool_protocol_error`. Reliability depends on the upstream model. There is no automatic retry to conceal this limitation. Perplexity produces a JSON proposal; the proxy validates the complete envelope, function name, tool choice, and argument schema before returning any callable item. The proxy does not execute functions.

- Define functions using Responses `tools` entries with `type`, `name`, `description`, and `parameters` (JSON schema). Explicit `strict: true` requires strict schema constraints and is validated locally, not by native constrained generation.
- Use `tool_choice: "auto"`, `"required"`, `"none"`, or `{"type":"function","name":"lookup_inventory"}`. Set `parallel_tool_calls: false` for at most one call.
- Read `function_call` output items. Parse `arguments`, execute the named function in your application, and submit `{"type":"function_call_output","call_id":"...","output":"..."}` with the exact returned `call_id`.
- Continue with `previous_response_id` or send the full input/output history for `store: false`. All outstanding calls need matching results.
- A stored continuation that omits tool definitions keeps its context but disables new calls (`tool_choice: "none"`). Resend the definitions to allow further calls.
- Malformed model JSON, unavailable functions, invalid arguments, and violated tool choices produce HTTP 502 with `tool_protocol_error`; an already-open stream emits `response.failed`. There is no fallback to an ordinary successful text answer.
- Function streams are buffered until validation finishes. Each validated call emits `response.output_item.added`, `response.function_call_arguments.delta`, `response.function_call_arguments.done`, and `response.output_item.done`, followed by `response.completed`.

See the [minimal SDK function loop](README.md#function-loop-with-the-official-python-sdk). Chat Completions function-tool requests still return HTTP 400, including the debug UI's unsupported-tools check.

#### Images and local files

Responses image input uses `input_image` alongside `input_text`; Chat Completions uses its `image_url` content shape.

`POST /v1/files` accepts multipart `file` and `purpose` fields and returns a local file ID. `GET /v1/files/{id}` reads metadata, `GET /v1/files/{id}/content` reads bytes, and `DELETE /v1/files/{id}` removes the file. Limits are **20 MiB per file** and **200 MiB total local storage**. Successful local storage does not establish that Perplexity can parse the file.

Document input remains **incomplete**. After uploading a document upstream, the proxy must wait for `/rest/sse/attachment_processing/subscribe` to report processing completion. Cloudflare currently returns **403** from that endpoint; the proxy returns an explicit **502**, rather than submitting a query with an unreadable document. Do not advertise document reading as supported on the strength of upload tests or deployment alone.

---

## 12. MCP Server Integration

pplx-proxy includes a built-in [Model Context Protocol](https://modelcontextprotocol.io/) server with 5 tools.

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `perplexity_search` | Pro Search with model/source/language selection |
| `perplexity_ask` | Quick general-purpose Q&A |
| `perplexity_reason` | Deep Research with extended reasoning |
| `perplexity_research` | Academic-focused search (scholar sources) |
| `perplexity_models` | List available models and account info |

### Connecting from Claude Code

```bash
claude mcp add pplx-proxy --transport http https://your-domain/YOUR_API_KEY/mcp
```

### Connecting from Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pplx-proxy": {
      "url": "https://your-domain/YOUR_API_KEY/sse"
    }
  }
}
```

### Authentication

The API key is part of the URL path. This is necessary because MCP clients generally do not support custom authentication headers.

- Streamable HTTP: `POST https://your-domain/{API_KEY}/mcp`
- SSE: `GET https://your-domain/{API_KEY}/sse`

Bare paths (`/mcp`, `/sse`) without the API key return 401.

---

## 13. Debug Chat UI

Visit `/chat` in your browser for an interactive test interface.

### Features

- **Model selection** — dropdown for all available models
- **Stream toggle** — switch between streaming and non-streaming
- **Test unsupported tools toggle** — attach demo function tools to verify that unsupported requests return a visible error
- **Thinking toggle** — enable reasoning output
- **Raw tab** — shows the full request JSON and raw response data
- **Format tab** — runs 20+ OpenAI spec compliance checks with pass/fail badges

### What the Format Validator Checks

**Non-streaming:** `id` format, `object` value, `created` type, `model` presence, `system_fingerprint`, `logprobs`, `finish_reason`, `role`, `content`, and `usage` arithmetic.


---

## 14. Admin Endpoints

All admin endpoints require Bearer authentication.

### GET /admin/models

Returns the full internal model map with mode, preference name, and tier info.

### POST /admin/update-models

Add or replace explicit configured mappings at runtime. They survive catalogue refresh/restart; discovered exact IDs stay bound to their verified preference and cannot be remapped. Replacement retains picker IDs and verified friendly version aliases:

```bash
curl -X POST http://localhost:8892/admin/update-models \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models": {"newmodel": ["pro", "newmodel_v1"]}, "merge": true}'
```

Set `"merge": false` to replace the entire model map.

### POST /admin/refresh-cookie

Inject a new session token without restarting:

```bash
curl -X POST http://localhost:8892/admin/refresh-cookie \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_token": "eyJhbGci..."}'
```

### POST /admin/discover-models

Manually refresh the authenticated account picker catalogue:

```bash
curl -X POST http://localhost:8892/admin/discover-models \
  -H "Authorization: Bearer YOUR_KEY"
```

---

## 15. Context Management

Because Perplexity does not accept OpenAI-format message arrays, pplx-proxy translates the conversation into a structured JSON string with `instructions`, `history`, and `query`.

### How It Works

Given a multi-turn conversation, the proxy:

1. Extracts upstream system/developer content for source detection.
2. For **generic clients**, filters system prompts through `.prompt_whitelist.txt` and uses surviving lines as `instructions`.
3. For **LobeHub**, discards upstream system/developer prompt content entirely and loads local `CUSTOM_PROMPTS` as `instructions` on every turn.
4. Builds a `history` array from previous `user` / `assistant` messages.
5. Separates the current user message into `query`.
6. Serializes the final payload as JSON before sending it to Perplexity.

A typical final payload looks like:

```json
{
  "instructions": ["...custom prompt or filtered system lines..."],
  "history": [{"role": "user", "content": "..."}],
  "query": "current question"
}
```

`history` is omitted when empty. For a first-turn LobeHub request, the payload is just `instructions + query`.

### Session Continuity

The proxy tracks Perplexity's `backend_uuid` (returned in every SSE response) and maps it to conversation history via SHA-256 hashing. On follow-up turns within the same conversation:

1. The proxy hashes the incoming `history` and looks up the cache.
2. **Cache hit**: sends only the raw user query text plus `last_backend_uuid` — no instructions, no history JSON. Perplexity's server-side session memory provides the full conversation context.
3. **Cache miss**: falls back to the full payload (instructions + history + query).
4. After each successful response, the new conversation state is stored for the next turn.

Session cache entries expire after **1 hour** (`_SESSION_MAX_AGE`), capped at **200 entries** (`_SESSION_MAX_ENTRIES`). This dramatically reduces payload size for multi-turn conversations and avoids re-sending `CUSTOM_PROMPTS` and history on every turn.

### Input Limits and Dedup

To prevent Perplexity's input from growing too large:

- Total serialized query is limited to **96K characters** (~32K tokens); larger queries are rejected without truncation.
- Consecutive same-role messages are deduplicated (keeps the last one) — this handles LibreChat-style branching artifacts.
- Rate-limit notices are stripped from prior assistant messages before they enter `history`.
- Empty assistant messages become `[done]` before reaching Perplexity.

There is no longer a `User's current request:` prefix or labeled text block assembly — the proxy now uses structured JSON fields instead.

---

## 16. Production Deployment

### systemd Service

Edit the included unit to match your user, installation paths and port before installing it.

```bash
sudo cp pplx-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pplx-proxy
```

The included unit file has `Restart=always` and `RestartSec=3`.

### Reverse Proxy

Point your domain to `localhost:8892`. If using Cloudflare Tunnel:

```yaml
- hostname: pplx.yourdomain.com
  service: http://localhost:8892
```

Set `PUBLIC_URL=https://pplx.yourdomain.com` in `.env` so MCP host validation allows external access.

### Security Checklist

- Set a strong `PPLX_PROXY_API_KEY`.
- Keep `.env` out of version control (already in `.gitignore`).
- The cookie is never exposed in API responses.
- MCP endpoints require the API key in the URL path.
- Bare `/mcp` and `/sse` are blocked with 401.
- Consider restricting `/chat` if exposed to the internet.

---

## 17. Cookie Lifecycle

```
Browser login
  -> Extract cookie
  -> Set PPLX_COOKIE in .env (the value of the `__Secure-next-auth.session-token` cookie from your Perplexity browser session)
  -> pplx-proxy starts
  -> Keep-alive pings every 6h
  -> Persist rotated cookie; validity remains controlled by Perplexity
  -> (If Perplexity revokes)
  -> ntfy push notification
  -> Re-inject through /admin/refresh-cookie or ./inject_cookie.sh
```

Cookie age is reported in `/health`:

```json
{"status": "ok", "service": "pplx-proxy", "cookie_age_hours": 48.2}
```

---

## 18. Troubleshooting

### "No cookies available" on startup
Set `PPLX_COOKIE` in `.env`.

### HTTP 403/401 from Perplexity
Cookie expired. Extract a fresh one and use `POST /admin/refresh-cookie` or update `.env` + restart.

### Tool calls not firing
`/v1/chat/completions` does not support function tools. Use `/v1/responses` and the experimental function bridge described above. HTTP 502 with `tool_protocol_error` means the model did not return a valid call proposal; inspect the error rather than treating it as text.

### Model says "I can't access real-time data"

This is the most critical issue in pplx-proxy. There are three independent causes, and all three must be addressed:

**Cause 1: Missing `search_focus` parameter.** Perplexity defaults to `"writing"` mode when `search_focus` is not set. In writing mode, the search engine still runs (you can see `Searching: ...` in the reasoning output), but the model is instructed to NOT use the search results in its answer. Fix: ensure `search_focus: "internet"` is present in the request params inside `client.search()`.

**Cause 2: System prompt content pollutes search results.** Perplexity searches ALL text in the query, including prompt instructions. If the prompt says `"You are Jarvis, a personal assistant"` or `"You are Lobe, an AI Agent"`, Perplexity searches for those phrases and finds AI chatbot tutorial pages and prompt engineering guides. The model sees these results and concludes it is a chatbot without search capabilities. Fix: never forward raw upstream prompt blocks blindly. Generic clients keep only whitelist-approved lines; LobeHub uses local `CUSTOM_PROMPTS` instead.

**Cause 3: System prompts arriving as `role: user`.** Some clients (notably LobeHub) send the user's custom system prompt as a `role: user` message instead of `role: system`. The proxy detects these with system-prompt keywords, reclassifies them as system content, and keeps them out of `history`. For LobeHub, the reclassified prompt is used only for source detection and is never forwarded to Perplexity.

**Cause 4: Missing local prompt injection for LobeHub.** If `CUSTOM_PROMPTS` is empty, missing, or not included in `instructions`, LobeHub turns will lose the local behavior you expect. Fix: ensure `CUSTOM_PROMPTS` exists in the project directory and logs show `custom_prompts_loaded=true` for LobeHub requests.

**How to diagnose:** Check server logs for `PROMPT DEBUG` and `PPLX REQUEST QUERY`. Generic clients should show whitelist-filtered `instructions`. LobeHub should show `instructions=[CUSTOM_PROMPTS]` on every turn, plus `history` and `query` as applicable.

### Model says "I don't have access to tools"
The Responses bridge uses a prompt-mediated proposal, not native Perplexity function execution. A model can refuse or return invalid JSON; the bridge reports that failure explicitly. Verify the selected endpoint, function definitions, and returned error. The client application must execute valid calls and return their results.

### Streaming hangs
Ensure your client handles SSE properly. Use `stream=True` in Python requests and iterate over lines.

### MCP "Invalid Host header"
Set `PUBLIC_URL` in `.env` to your external domain. The proxy adds it to the MCP allowed hosts.

### Format validator shows failures
Hard-refresh the `/chat` page (Ctrl+Shift+R). The page has no-cache headers but your browser may have a stale copy.

---

## 19. Known Limitations

- **Function calling is experimental and unreliable** — upstream responses may contain invalid protocol output. Only Responses has the bridge; Chat function tools remain HTTP 400. Invalid proposals fail with HTTP 502 or `response.failed`; no fallback or automatic retry is added.
- **Document input is incomplete** — Cloudflare 403 blocks upstream processing subscription; document-reading requests fail with HTTP 502 despite successful local upload.
- **Image and document inputs differ** — local file storage does not guarantee upstream document processing.
- **Citation stripping** removes `[N]` patterns from ordinary text, which may affect content like `array[0]`. Function arguments are preserved without this cleaning.
- **Context window**: 96K char total query limit (~32K tokens).
- **Perplexity API changes** may break the proxy without notice. Picker discovery can find new searchable names only while its schema, authentication and Cloudflare access remain compatible; failures are visible and retain cached/configured models.
- **Single session** — one cookie per instance. Multiple instances with the same cookie may conflict.

---

## License

MIT

### Explicit failure behavior and support checks

The assembled query limit is 96,000 characters. Oversized input is rejected rather than silently truncated. `/health` returns cached quota immediately and schedules a refresh when stale; it does not wait for FlareSolverr to complete.

`./test.sh [base-url]` verifies complete non-streaming chat, streaming chat, and Responses output, in addition to health and models. It exits nonzero on HTTP errors, invalid output, or incomplete streams. `node --test test_chat.js` runs the debug-page protocol checks.

`./inject_cookie.sh` reads the new token using a hidden prompt (or standard input when piped). It reads `.env` beside the script and updates the running local service through `/admin/refresh-cookie`, which validates the token before persisting it. No service restart is needed. The service must already be running; use `.env` for initial setup. Avoid passing the token as a command argument, where shell history or process listings could expose it.

Chat Completions rejects unsupported generation controls with HTTP 400, including custom sampling, output-token limits, structured response formats, multiple choices, stop sequences, nondefault penalties, seeds, logprobs, and streaming usage requests. These options are not silently ignored.
