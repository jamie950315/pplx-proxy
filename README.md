# pplx-proxy

Reverse proxy for [Perplexity.ai](https://www.perplexity.ai) — use your existing **Pro/Max subscription cookie** to access all models via standard APIs.

Exposes three interfaces:
- **OpenAI-compatible REST API** (`/v1/chat/completions`) — streaming, tool calling, thinking
- **MCP server** (Streamable HTTP + SSE) — 5 built-in tools
- **Debug chat UI** (`/chat`) — test everything with real-time OpenAI format validation

## How It Works

Perplexity's web frontend talks to its backend through an internal SSE endpoint (`/rest/sse/perplexity_ask`). This proxy authenticates with your session cookie via [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprinting), translates requests/responses into OpenAI and MCP formats, and keeps your session alive automatically.

No official API key needed — just your subscription.

All queries use `search_focus: "internet"` — Perplexity's built-in web search is always active, so models return real-time data (stock prices, weather, news) directly in their answers.

## Features

- **Full OpenAI format compliance** — `system_fingerprint`, `logprobs`, proper `usage` arithmetic, all fields per spec
- **Tool calling** — OpenAI-style function calling via prompt injection with 3-layer false-positive defense
- **Thinking/reasoning** — `thinking: true` or `reasoning_effort` param, reasoning streamed as `reasoning_content`
- **Account tier support** — free/pro/max — only exposes models your tier can access
- **Auto-discovery** — background task checks model health every 24h, auto-upgrades when versions change
- **Response cleaning** — strips Perplexity citations `[1][2]`, `<grok:*>` tags, `<?xml?>` declarations, `<script>` tags
- **Session keep-alive** — periodic pings prevent cookie expiry
- **Push notifications** — [ntfy.sh](https://ntfy.sh) alerts on cookie expiry or model upgrades
- **Debug chat UI** — `/chat` page with tools toggle, thinking toggle, streaming toggle, and **OpenAI format validator**
- **Dynamic model management** — add/remove models at runtime via admin API
- **Full input validation** — proper error messages for every malformed request

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

Then open **http://localhost:8892/chat** to test with the debug UI.

## Getting Your Cookie

1. Log in to [perplexity.ai](https://www.perplexity.ai)
2. F12 → **Application** → **Cookies** → `www.perplexity.ai`
3. Copy `next-auth.session-token`
4. Set `PPLX_COOKIE=<value>` in `.env`

## Models

| Model ID | Backend | Tier | Thinking Variant |
|----------|---------|------|-----------------|
| `auto` | Perplexity Best | free+ | — |
| `sonar` | Sonar | pro+ | — |
| `gpt` | GPT-5.4 | pro+ | `gpt54_thinking` |
| `sonnet` | Claude Sonnet 4.6 | pro+ | `claude46sonnetthinking` |
| `gemini` | Gemini 3.1 Pro | pro+ | always on |
| `nemotron` | Nemotron 3 Super | pro+ | always on |
| `opus` | Claude Opus 4.6 | max | `claude46opusthinking` |

Thinking variants are activated via `thinking: true` or `reasoning_effort` parameter — no separate model names needed.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Health check |
| `GET` | `/chat` | No | **Debug chat UI with OpenAI format validator** |
| `GET` | `/v1/models` | Yes | List tier-available models |
| `POST` | `/v1/chat/completions` | Yes | Chat (streaming + non-streaming + tools + thinking) |
| `POST` | `/<api-key>/mcp` | Key in URL | MCP Streamable HTTP |
| `GET` | `/<api-key>/sse` | Key in URL | MCP SSE |
| `GET` | `/admin/models` | Yes | Full model map |
| `POST` | `/admin/update-models` | Yes | Add/replace models |
| `POST` | `/admin/refresh-cookie` | Yes | Inject new session token |
| `POST` | `/admin/discover-models` | Yes | Run model discovery |

## Usage

### OpenAI API

```bash
# Basic chat
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Hello"}], "stream": true}'

# With thinking
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt", "messages": [{"role": "user", "content": "Analyze X"}], "thinking": true}'

# With tool calling
curl -X POST http://localhost:8892/v1/chat/completions \
  -H "Authorization: Bearer YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sonnet",
    "messages": [{"role": "user", "content": "Weather in Tokyo"}],
    "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
  }'
```

### Debug Chat UI

Open **http://localhost:8892/chat** (or `https://your-domain/chat`) in a browser:

- Toggle **Tools ON/OFF** to test tool calling
- Toggle **thinking** to test reasoning mode
- Toggle **stream** for streaming vs non-streaming
- **Raw tab**: shows full request/response JSON
- **Format ✓ tab**: validates every response field against the OpenAI spec with PASS/FAIL badges

### MCP

The API key is part of the URL path for MCP authentication:

```bash
# Claude Code
claude mcp add pplx-proxy --transport http http://localhost:8892/YOUR_API_KEY/mcp

# SSE transport
# Connect to http://localhost:8892/YOUR_API_KEY/sse
```

Without `PPLX_PROXY_API_KEY` set, MCP falls back to unauthenticated `/mcp/mcp` and `/sse/sse`.

**MCP Tools:**

| Tool | Description |
|------|-------------|
| `perplexity_search` | Pro Search with model/source selection |
| `perplexity_ask` | Quick auto-mode Q&A |
| `perplexity_reason` | Reasoning with model selection |
| `perplexity_research` | Deep Research |
| `perplexity_models` | List available models for your tier |

## Tool Calling

Tool calling is implemented via prompt injection (Perplexity has no native tool calling API). The proxy appends a compact tool definition prompt to the query and parses XML responses back into OpenAI tool_calls format.

**3-layer defense against false positives:**

1. **Relevance heuristic**: tool prompt only injected if user message has keyword overlap with tool names/descriptions. "Hello" with tools → no tool prompt injected.
2. **Schema validation**: parsed tool calls validated against definitions — function name must exist, required params must be present and non-empty.
3. **XML cleanup**: if model wraps response in `<response>`, `<answer>` etc. but it's not a real tool call, XML is stripped and clean text is returned.

Supports `tool_choice`: `auto` (default), `none` (suppress tools), `required` (force tool call).

## OpenAI Format Compliance

All responses strictly match the [OpenAI Chat Completions API spec](https://platform.openai.com/docs/api-reference/chat/object):

- `id` (chatcmpl-*), `object`, `created`, `model`, `system_fingerprint` (null)
- `choices[].index`, `choices[].logprobs` (null), `choices[].finish_reason`
- `choices[].message.role`, `.content`, `.tool_calls`
- `usage.total_tokens` = `prompt_tokens` + `completion_tokens`
- Streaming: consistent `id`, `system_fingerprint` in every chunk, proper `[DONE]` termination
- Tool calls: `id` (call_*), `type` (function), `function.name`, `function.arguments` (valid JSON string)

**Use `/chat` to visually verify** — the Format ✓ tab runs 20+ checks per response.

## Auto-Discovery

Every `PROBE_INTERVAL_HOURS` (default 24h), pplx-proxy checks if models are still alive. If one dies, it increments the version number (e.g., `gpt54` → `gpt55` → ... up to +1.0) and auto-upgrades. Thinking variants are auto-derived from `_THINKING_MAP`.

Manual trigger: `POST /admin/discover-models`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | — | Session token (**required**) |
| `PPLX_PROXY_API_KEY` | — | Bearer auth (empty = no auth) |
| `ACCOUNT_TYPE` | `pro` | `free`, `pro`, or `max` |
| `DEFAULT_MODEL` | `gpt` | Default when not specified |
| `PPLX_PROXY_PORT` | `8892` | Listen port |
| `KEEPALIVE_HOURS` | `6` | Session ping interval |
| `PROBE_INTERVAL_HOURS` | `24` | Auto-discovery interval |
| `NTFY_TOPIC` | `pplx-proxy` | ntfy.sh topic |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server URL |
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
