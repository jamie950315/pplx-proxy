# pplx-proxy Usage Manual

A comprehensive guide to installing, configuring, and using pplx-proxy — a self-hosted reverse proxy that turns your Perplexity Pro/Max subscription into a standard OpenAI-compatible API and MCP server.

---

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
10. [Tool Calling](#10-tool-calling)
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

This gives you access to GPT-5.4, Claude Sonnet 4.6, Claude Opus 4.6, Gemini 3.1 Pro, and other models Perplexity offers — all through the same API format your tools already speak.

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
4. Find the cookie named `next-auth.session-token`.
5. Copy its **Value** (a long JWT-like string).
6. Paste it into your `.env` file:

```
PPLX_COOKIE=eyJhbGciOiJkaX...your_token_here...
```

The cookie typically stays valid for 7+ days. pplx-proxy keeps it alive with automatic periodic pings.

---

## 5. Configuration

All settings live in the `.env` file. Here is the full reference:

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | *(required)* | Your `next-auth.session-token` from Perplexity |
| `PPLX_PROXY_API_KEY` | *(empty)* | Bearer token for API auth. If empty, no authentication is required |
| `ACCOUNT_TYPE` | `pro` | Your subscription tier: `free`, `pro`, or `max` |
| `DEFAULT_MODEL` | `gpt` | Model used when no model is specified in the request |
| `PPLX_PROXY_PORT` | `8892` | Port to listen on |
| `KEEPALIVE_HOURS` | `6` | Interval between session keep-alive pings |
| `PROBE_INTERVAL_HOURS` | `24` | Interval between auto-discovery model checks |
| `NTFY_TOPIC` | `pplx-proxy` | ntfy.sh push notification topic |
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

**Development (foreground):**

```bash
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8892
```

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
| `sonar` | Sonar | pro | Perplexity's experimental model |
| `gpt` | GPT-5.4 | pro | OpenAI's latest |
| `sonnet` | Claude Sonnet 4.6 | pro | Anthropic mid-tier |
| `gemini` | Gemini 3.1 Pro | pro | Google's flagship |
| `nemotron` | Nemotron 3 Super | pro | NVIDIA's model |
| `opus` | Claude Opus 4.6 | max | Anthropic's most capable |

### Endpoint Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check + cookie age |
| GET | `/chat` | No | Debug chat UI |
| GET | `/v1/models` | Bearer | List available models |
| POST | `/v1/chat/completions` | Bearer | Chat completions |
| POST | `/v1/responses` | Bearer | OpenAI Responses API compatibility |
| POST | `/{api_key}/mcp` | URL key | MCP Streamable HTTP |
| GET | `/{api_key}/sse` | URL key | MCP SSE transport |
| GET | `/admin/models` | Bearer | Full model map |
| POST | `/admin/update-models` | Bearer | Modify model map |
| POST | `/admin/refresh-cookie` | Bearer | Inject new cookie |
| POST | `/admin/discover-models` | Bearer | Trigger model discovery |

### Authentication

**REST API:** pass your API key as a Bearer token:
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
| `messages` | array | Conversation history (system, user, assistant, tool roles) |
| `stream` | boolean | Enable SSE streaming (default: false) |
| `tools` | array | OpenAI-format tool definitions |
| `tool_choice` | string | `auto`, `none`, or `required` |
| `thinking` | boolean | Enable reasoning output |
| `reasoning_effort` | string | `none`, `low`, `medium`, `high` |
| `temperature` | number | 0.0-1.0 (passed to Perplexity) |

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
- The last content chunk has `finish_reason`.
- The stream always terminates with `[DONE]`.

---

## 10. Tool Calling

pplx-proxy supports OpenAI-style function calling. Since Perplexity has no native tool calling API, this is implemented via prompt injection — a tool schema prompt is appended to the query when the user's message appears relevant to the available tools.

### Basic Example

```bash
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sonnet",
    "messages": [{"role": "user", "content": "Look up user 42"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_user",
        "description": "Look up user by ID",
        "parameters": {
          "type": "object",
          "properties": {"user_id": {"type": "integer"}},
          "required": ["user_id"]
        }
      }
    }]
  }'
```

When the model decides to call a tool, the response includes `tool_calls`:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_user",
          "arguments": "{\"user_id\": 42}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

### Providing Tool Results

After executing the tool yourself, send the result back:

```json
{
  "model": "sonnet",
  "messages": [
    {"role": "user", "content": "Look up user 42"},
    {"role": "assistant", "content": null, "tool_calls": [
      {"id": "call_abc123", "type": "function", "function": {"name": "get_user", "arguments": "{\"user_id\": 42}"}}
    ]},
    {"role": "tool", "tool_call_id": "call_abc123", "content": "{\"name\": \"Alice\", \"email\": \"alice@example.com\"}"},
    {"role": "user", "content": "What is their email?"}
  ]
}
```

### False-Positive Prevention

A 3-layer defense prevents the model from calling tools when it shouldn't:

1. **Relevance heuristic** — tool prompt is only injected if the user's message shares keywords with tool names or descriptions. Saying "Hello" with tools attached will not trigger tool injection.
2. **Schema validation** — parsed tool calls are checked against definitions: function name must exist, required parameters must be present, values must be non-empty.
3. **XML cleanup** — if the model wraps a normal text response in XML tags (a common false-positive pattern), the tags are stripped and clean text is returned.

### tool_choice

- `auto` (default) — model decides whether to call tools
- `none` — tool prompt is suppressed entirely
- `required` — model is instructed to always call a tool

---

## 11. Thinking / Reasoning

Some models support extended thinking, where the model shows its reasoning process before answering.

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

**Note:** Perplexity does not expose the model's internal chain-of-thought. The `reasoning_content` is populated from Perplexity's visible search steps — search queries, URLs found, and plan goals. Gemini and Nemotron have always-on thinking.

| Model | Thinking Variant |
|-------|-----------------|
| `gpt` | `gpt54_thinking` |
| `sonnet` | `claude46sonnetthinking` |
| `opus` | `claude46opusthinking` |
| `gemini` | Always on |
| `nemotron` | Always on |

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
- **Tools toggle** — enable/disable a set of demo tools (get_weather, calculator, search_web, get_user, send_email)
- **Thinking toggle** — enable reasoning output
- **Raw tab** — shows the full request JSON and raw response data
- **Format tab** — runs 20+ OpenAI spec compliance checks with pass/fail badges

### What the Format Validator Checks

**Non-streaming:** `id` format, `object` value, `created` type, `model` presence, `system_fingerprint`, `logprobs`, `finish_reason`, `role`, `content`, and `usage` arithmetic.

**Streaming:** all of the above plus consistent `id` across chunks, `delta.role` in first chunk, `finish_reason` in last chunk, empty `delta` in final chunk, content or tool_calls present, and `[DONE]` termination.

---

## 14. Admin Endpoints

All admin endpoints require Bearer authentication.

### GET /admin/models

Returns the full internal model map with mode, preference name, and tier info.

### POST /admin/update-models

Add or replace models at runtime:

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

Manually trigger the model auto-discovery probe:

```bash
curl -X POST http://localhost:8892/admin/discover-models \
  -H "Authorization: Bearer YOUR_KEY"
```

---

## 15. Context Management

Because Perplexity does not accept OpenAI-format message arrays, pplx-proxy translates the conversation into a structured text block.

### How It Works

Given a multi-turn conversation, the proxy:

1. Extracts the **system message** (if any) and places it first.
2. Builds a **conversation history** section from all previous messages, labeled with `User:`, `Assistant:`, and `Tool result:` prefixes.
3. Separates the **current user message** from history and labels it with `User's current request:` to prevent topic confusion.
4. Appends **tool definitions** (if tools are provided and relevant) as an XML schema block.

### Assistant Messages with Tool Calls

When a previous assistant turn has `tool_calls` but no text content, the proxy extracts the tool call information and formats it as:

```
Assistant: [Called tools: get_user({"user_id": 42})]
```

This gives the model clear context about what tools were invoked.

### Truncation Limits

To prevent Perplexity's input from growing too large:

- System prompts are truncated to **500 characters** and labeled so Perplexity does not search for them.
- Assistant messages are truncated to **600 characters**.
- Tool results are truncated to **400 characters**.
- Consecutive same-role messages are deduplicated (keeps the last one) — this handles LibreChat-style branching artifacts.
- Only the **last 16 items** (~8 turns) of history are kept.

### Topic Separation

The current user message is prefixed with `User's current request:` when conversation history exists. This prevents the model from treating a new question as a continuation of the previous topic.

---

## 16. Production Deployment

### systemd Service

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
  -> Set PPLX_COOKIE in .env
  -> pplx-proxy starts
  -> Keep-alive pings every 6h
  -> Cookie stays alive indefinitely
  -> (If Perplexity revokes)
  -> ntfy push notification
  -> Re-inject via /admin/refresh-cookie or update .env + restart
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
Tool calling relies on keyword matching. Make your request explicit: "Use the calculator to compute 2+2" works better than just "2+2".

### Model says "I can't access real-time data"
This was caused by a missing `search_focus: "internet"` parameter in the Perplexity request. It has been fixed. If you see this on an older version, update to the latest.

### Model says "I don't have access to tools"
Perplexity's model sometimes prefers its built-in web search over provided tools. This is expected behavior.

### Streaming hangs
Ensure your client handles SSE properly. Use `stream=True` in Python requests and iterate over lines.

### MCP "Invalid Host header"
Set `PUBLIC_URL` in `.env` to your external domain. The proxy adds it to the MCP allowed hosts.

### Format validator shows failures
Hard-refresh the `/chat` page (Ctrl+Shift+R). The page has no-cache headers but your browser may have a stale copy.

---

## 19. Known Limitations

- **Tool calling is best-effort** (~95% for relevant queries) via prompt injection, not native API.
- **No tool execution in debug UI** — `/chat` tools are for format testing only.
- **Citation stripping** removes `[N]` patterns, which may affect content like `array[0]`.
- **Context window**: last 16 items, assistant messages truncated to 600 chars.
- **Perplexity API changes** may break the proxy without notice. Auto-discovery catches model changes.
- **Single session** — one cookie per instance. Multiple instances with the same cookie may conflict.

---

## License

MIT
