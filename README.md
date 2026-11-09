# pplx-proxy

Reverse proxy for [Perplexity.ai](https://www.perplexity.ai) — use your existing **Pro/Max subscription cookie** to access all models via standard APIs.

Exposes three interfaces:
- **OpenAI-compatible REST API** (`/v1/chat/completions`, `/v1/responses`) — streaming, thinking
- **MCP server** (Streamable HTTP + SSE) — 5 built-in tools
- **Debug chat UI** (`/chat`) — test everything with real-time OpenAI format validation

## Feature limitations

- Local `/v1/files` storage is limited to 20 MiB per file and 200 MiB total. Storage does not establish that Perplexity can read a document.
- Document processing requires an upstream subscription endpoint that has returned Cloudflare 403; failures are surfaced as HTTP 502.
- Responses function calling is experimental and prompt-mediated; invalid output fails explicitly. Chat Completions function tools are unsupported (HTTP 400).

## How It Works

Perplexity's web frontend talks to its backend through an internal SSE endpoint (`/rest/sse/perplexity_ask`). This proxy authenticates with your session cookie via [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprinting), translates requests/responses into OpenAI and MCP formats, and keeps your session alive automatically.

No official API key needed — just your subscription.

All queries use `search_focus: "internet"` — Perplexity's built-in web search is always active, so models return real-time data (stock prices, weather, news) directly in their answers.

## Features

- **Full OpenAI format compliance** — `system_fingerprint`, `logprobs`, proper `usage` arithmetic, all fields per spec
- **Thinking/reasoning** — `thinking: true` or `reasoning_effort` param, reasoning streamed as `reasoning_content`
- **Account tier support** — free/pro/max — only exposes models your tier can access
- **Auto-discovery** — authenticated picker refreshes at startup and periodically; explicit mappings are preserved
- **Response cleaning** — strips Perplexity citations `[1][2]`, `<grok:*>` tags, `<?xml?>` declarations, `<script>` tags
- **Rate limit tracking** — tracks Pro Search quota, explicit error when the requested paid model has no remaining quota, notices at every 5th decrement
- **Substitution notice** — if Perplexity swaps the requested model, the answer still returns with `[Substituted by Perplexity with ...]` at the end
- **Session continuity** — uses Perplexity `backend_uuid` for eligible plain-text follow-ups; explicit instructions and function conversations retain their complete context
- **Session keep-alive** — validates at startup and every 6 hours, then persists any rotated cookie returned by Perplexity
- **Push notifications** — [ntfy.sh](https://ntfy.sh) alerts on cookie expiry or authentication failure
- **Debug chat UI** — `/chat` page with tools toggle, thinking toggle, streaming toggle, and **OpenAI format validator**
- **Dynamic model management** — add/remove models at runtime via admin API
- **Full input validation** — proper error messages for every malformed request

## Quick Start

Choose one startup mode:

| Mode | Command | Includes |
|------|---------|----------|
| Manual Python | `venv/bin/uvicorn server:app --host 0.0.0.0 --port 8892` | pplx-proxy only |
| Docker Compose | `docker compose up -d --build` | pplx-proxy, FlareSolverr, persistent runtime volume |

Manual Python is best for local development. Docker Compose is the full self-hosted stack and is recommended when you want quota data in `/health`.

### Manual Python

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

### Docker Compose

For the complete setup, including pplx-proxy, FlareSolverr, and persistent runtime storage:

```bash
cp .env.example .env
# Edit .env — set PPLX_COOKIE, PPLX_PROXY_API_KEY, and ACCOUNT_TYPE
docker compose up -d --build
```

This starts:
- `pplx-proxy` on `http://localhost:8892`
- `flaresolverr` inside the Compose network at `http://flaresolverr:8191`
- `pplx-data` volume for session cache, model fallback/discovery caches, uploaded files, and Responses history

FlareSolverr is optional for chat, streaming, MCP, and Responses API. Without it, `/health` still reports service and cookie age, but `remaining_pro` and `remaining_research` stay `null` and quota exhaustion cannot be checked locally.

## Getting Your Cookie

1. Log in to [perplexity.ai](https://www.perplexity.ai)
2. F12 → **Application** → **Cookies** → `www.perplexity.ai`
3. Copy `__Secure-next-auth.session-token`
4. Set `PPLX_COOKIE=<value>` in `.env`

## Models

The live, account-specific catalogue is the source of truth. Use authenticated `GET /v1/models` to inspect the exact IDs and names currently available to the configured Perplexity account, including newly added models and reasoning variants. Pass each returned `id` unchanged to inference; `name` is display metadata. `source` distinguishes live picker entries from configured aliases and fallback entries.

The static compatibility map retains existing client IDs during picker outages. Built-in rolling defaults are `gpt` → GPT-6.1 Sol, `sonnet` → Claude Sonnet 5.5, `grok` → Grok 4.7, `nemotron` → Nemotron 3.5 Super VL, and `gemini-flash` → Gemini 3.8 Flash. `gemini` retains the Pro-family mapping. Explicit mappings loaded from `.models.json` or saved through `/admin/update-models` are preserved, including across refresh and restart; they do not silently roll to a different preference. Older IDs remain selectable, but Perplexity may substitute an unavailable model. Responses include a substitution notice when detected.

These friendly IDs use preferences mapped to these upstream preferences:

| Proxy ID | Perplexity preference | Reasoning |
|----------|-----------------------|-----------|
| `gpt-6.1-sol` | `gpt6_1_sol` | Paired `gpt6_1_sol_thinking` |
| `gemini-3.8-flash` | `gemini38flash` | Paired `gemini38flashthinking` |
| `sonnet-5.5` | `claude55sonnet` | Paired `claude55sonnetthinking` |
| `glm-5.3` | `glm_5_3_thinking` | Reasoning-only |
| `grok-4.7` | `grok47` | Paired `grok47thinking` |
| `nemotron-3.5-super-vl` | `nv_nemotron_3_5_super` | Reasoning-only |

Free accounts expose only `auto`. Pro/Max picker entries are filtered by the authenticated subscription tier. Exact reasoning IDs are directly invokable. For picker-verified base models, `thinking: true` or `reasoning_effort` uses only the paired reasoning ID; if none exists, the request returns HTTP 400 instead of inventing a suffix. A reasoning-only model can be used directly with or without the thinking flag. Perplexity's private reasoning blocks are not exposed.

## API Endpoints

Bearer authentication applies when `PPLX_PROXY_API_KEY` is configured; an empty key disables it.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | No | Health check |
| `GET` | `/chat` | No | **Debug chat UI with OpenAI format validator** |
| `GET` | `/debug` | No | Redirect to `/chat` |
| `GET` | `/v1/models` | Yes | List tier-available models |
| `POST` | `/v1/chat/completions` | Yes | Chat (streaming + non-streaming + thinking) |
| `POST` | `/v1/responses` | Yes | OpenAI Responses API (stream, store, previous_response_id; experimental function bridge and image input) |
| `GET` | `/v1/responses/{id}` | Yes | Retrieve a stored response |
| `DELETE` | `/v1/responses/{id}` | Yes | Delete a stored response |
| `POST` | `/v1/responses/{id}/cancel` | Yes | Cancel an in-progress background response |
| `GET` | `/v1/responses/{id}/input_items` | Yes | List input items for a stored response |
| `POST` / `GET` | `/v1/files` | Yes | Upload/list locally stored files |
| `GET` / `DELETE` | `/v1/files/{id}` | Yes | Retrieve metadata/delete a stored file |
| `GET` | `/v1/files/{id}/content` | Yes | Read stored bytes |
| `POST` | `/<api-key>/mcp` | Key in URL | MCP Streamable HTTP |
| `GET` | `/<api-key>/sse` | Key in URL | MCP SSE |
| `GET` | `/admin/models` | Yes | Full model map |
| `POST` | `/admin/update-models` | Yes | Add/replace models |
| `POST` | `/admin/refresh-cookie` | Yes | Inject new session token |
| `POST` | `/admin/discover-models` | Yes | Run model discovery |

## Usage

### OpenAI-compatible clients

Set the base URL to `http://localhost:8892/v1` (or your HTTPS deployment), the API key to `PPLX_PROXY_API_KEY`, and choose an `id` returned by `GET /v1/models`. Streaming chat is supported. Chat Completions function tools are rejected; Responses function calls are experimental and prompt-mediated. When the picker provides both ordinary and reasoning IDs, `thinking: true` or `reasoning_effort` selects that exact paired reasoning ID; the reasoning ID is also listed directly. The proxy does not expose Perplexity's private reasoning blocks. See the API compatibility notes below.

### Responses API

`POST /v1/responses` accepts OpenAI Responses API requests (`input`, `instructions`, `previous_response_id`, `stream`, `store`, `reasoning`, `text.format`). Responses are stored by default so you can `GET` / `DELETE` them and continue a thread with `previous_response_id`. With `store: false`, they are not retained or retrievable.

Built-in web search is always on. Responses web-search tools are accepted because they use that existing search behavior. Background requests require `store: true`; only active background requests can be cancelled. Custom sampling values, output-token limits, automatic truncation, and strict **text-output** JSON schemas are rejected. JSON-object and non-strict text schema modes are prompt-based requests, not guaranteed structured output. File search, code interpreter, computer use, and image generation remain unsupported.

Function definitions in `tools` enable an **experimental prompt-mediated bridge**. Invalid protocol output fails with `tool_protocol_error`; reliability depends on the upstream model. No retry or fallback hides these failures.

The bridge works as follows: Perplexity proposes a JSON call, the proxy validates its name and arguments against the supplied schema, and the client application executes it. Malformed JSON, invalid arguments, or a violated tool choice produce HTTP 502 (`tool_protocol_error`), or `response.failed` after streaming has started. There is no ordinary-text fallback and the proxy never executes client functions. Streaming sends function events only after the entire upstream answer has passed validation.

Function `tool_choice` supports `auto`, `required`, `none`, and `{"type":"function","name":"..."}`. `parallel_tool_calls: false` limits the result to one call. Function parameter schemas, including `strict: true`, are checked locally; this does not provide native constrained generation. Send each result as `function_call_output` with the returned `call_id`. When tool definitions are omitted on a stored continuation, the proxy preserves the function context but uses `tool_choice: "none"`; resend the definitions to enable more calls.

### Function loop with the official Python SDK

Set `PPLX_PROXY_BASE_URL` to your deployed proxy, including `/v1` (for example, `https://proxy.example.com/v1`). Use the existing proxy API key. Function calling remains experimental; clients must handle explicit failures.

```python
import json
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["PPLX_PROXY_BASE_URL"],
    api_key=os.environ["PPLX_PROXY_API_KEY"],
)
tools = [{
    "type": "function", "name": "lookup_inventory",
    "description": "Read the application's current stock count for a SKU.",
    "parameters": {
        "type": "object", "properties": {"sku": {"type": "string"}},
        "required": ["sku"], "additionalProperties": False,
    },
    "strict": True,
}]
first = client.responses.create(
    model="gpt", tools=tools, tool_choice="auto",
    input="Use lookup_inventory to check the current stock count for DEMO.",
)
calls = [item for item in first.output if item.type == "function_call"]
if not calls:
    raise RuntimeError("The model did not request the inventory lookup")
results = []
for call in calls:
    if call.name != "lookup_inventory":
        raise RuntimeError("Unexpected function")
    sku = json.loads(call.arguments)["sku"]
    # Client-side execution; replace this sample data with your own lookup.
    quantity = {"DEMO": 7}[sku]
    results.append({"type": "function_call_output", "call_id": call.call_id,
                    "output": json.dumps({"sku": sku, "quantity": quantity})})
for event in client.responses.create(
    model="gpt", previous_response_id=first.id, input=results, stream=True,
):
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
    elif event.type == "response.failed":
        raise RuntimeError(event.response.error)
```

### Image and file input

Use a Responses `input_image` content part alongside `input_text`. `/v1/files` accepts multipart `file` and `purpose` fields and returns a local `file_id`; its read/delete endpoints manage those stored bytes. Limits are **20 MiB per file** and **200 MiB total local storage**.

Document upload and model-readable document input are different steps. Document processing must finish through Perplexity's `/rest/sse/attachment_processing/subscribe` endpoint before a query can use it. That endpoint currently receives Cloudflare 403, so document input is **not working** and returns an explicit **502**. Do not treat successful local file upload as a successful document-reading test.

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

```

### Debug Chat UI

Open **http://localhost:8892/chat** (or `https://your-domain/chat`) in a browser:

- Toggle **Test unsupported tools** to verify that function-tool requests produce a visible error
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


## OpenAI Format Compliance

All responses strictly match the [OpenAI Chat Completions API spec](https://platform.openai.com/docs/api-reference/chat/object):

- `id` (chatcmpl-*), `object`, `created`, `model`, `system_fingerprint` (null)
- `choices[].index`, `choices[].logprobs` (null), `choices[].finish_reason`
- `usage.total_tokens` = `prompt_tokens` + `completion_tokens`
- Successful streaming: consistent `id`, `system_fingerprint` in every chunk, proper `[DONE]` termination

**Use `/chat` to visually verify** — the Format ✓ tab runs 20+ checks per response.

## Dynamic Model Discovery

The proxy fetches Perplexity's authenticated web picker at `/rest/models/config/v2` using the existing session cookie and API session. The picker maps display labels and subscription tiers to the actual `model_preference` IDs accepted by the inference endpoint. Discovered entries include only choices marked as searchable and available to the authenticated account tier; compatibility mappings are retained separately. Returned picker IDs are passed through unchanged; display names are metadata, not invocation aliases. Browser-agent, computer/ASI, and research workflow modes use different invocation protocols and are not exposed by the current `perplexity_ask` path; an unknown catalogue schema is rejected so the last known good catalogue remains active.

Discovery runs once during startup and every `PROBE_INTERVAL_HOURS` (default 24). A validated last-known-good picker is atomically cached in `DATA_DIR/.discovered_models.<account-hash>.json`; failed, unauthenticated, timed-out, or malformed refreshes retain that account's catalogue and leave `MODEL_MAP` intact rather than stripping it to fallback aliases. The cache filename contains a one-way hash, not a credential. When neither live discovery nor a cache is available, the existing `.models.json`/built-in mapping remains as a clearly marked fallback. `GET /health` reports `model_discovery` status, model counts, and sanitized stage/HTTP error diagnostics (`last_error`, e.g. `[auth_session] HTTP 401 at /api/auth/session`). `GET /v1/models` includes `name` and `source` metadata (`discovered`, `configured_alias`, or `fallback`) in addition to the OpenAI model fields. `/admin/models` shows the internal preference and source.

Refresh manually with `POST /admin/discover-models` and the configured Bearer API key. The route uses the same session and picker as startup. Built-in family aliases follow matching picker choices; explicitly persisted/admin mappings keep their configured preferences. Known friendly version IDs are added when their exact preference is present in the picker. Existing legacy aliases remain available even when absent from the picker and can be substituted upstream; their presence is not proof of current model availability. Newly discovered picker IDs can be selected immediately, without rebuilding the image. The cache stores only picker metadata and timestamps, never credentials.

The request uses the existing `PerplexityClient` AsyncSession, cookies, User-Agent, and TLS impersonation. The picker requires coherent same-origin AJAX metadata (`Origin`, `Referer`, `sec-fetch-dest: empty`, `sec-fetch-mode: cors`), rather than the navigation headers used for session checks. Keep curl_cffi's normal HTTP negotiation; forced HTTP/1.1 has received Cloudflare 403.

Picker failures remain visible in `/health.model_discovery.last_error` and warning logs; diagnostics contain stage/status/path, never arbitrary upstream bodies or transport exception messages. Cached/configured models remain usable, but no automatic inference-probe fallback masks picker failures. Disable picker fetches and the scheduled task with `PROBE_DISCOVERY_ENABLED=false`.

Unlike the old numeric version probes, the picker can expose genuinely new preference names immediately. The retained legacy probe helpers can only test registry names or bounded numeric patterns (`MAX_VERSION_UPGRADE_PROBES=10`), cannot invent a renamed family/suffix, and are not the scheduled discovery path.

To rebuild and redeploy after code updates:
- **systemd**: `git pull && venv/bin/pip install -r requirements.txt && sudo systemctl restart pplx-proxy.service`
- **Docker Compose**: `docker compose build && docker compose up -d`
- **Verify**: `curl -fsS http://localhost:8892/health` and `curl -fsS http://localhost:8892/v1/models -H "Authorization: Bearer $PPLX_PROXY_API_KEY"`

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PPLX_COOKIE` | — | Session token (**required**) |
| `PPLX_PROXY_API_KEY` | — | Bearer auth (empty = no auth) |
| `ACCOUNT_TYPE` | `pro` | `free`, `pro`, or `max` |
| `DEFAULT_MODEL` | `gpt` | Default when not specified |
| `PPLX_PROXY_PORT` | `8892` | Port used by the built-in runner and Compose host mapping; pass `--port` explicitly to uvicorn |
| `DATA_DIR` | project directory (`.` in `.env.example`) | Runtime cookie, model and response stores, discovery caches and uploads |
| `PROBE_DISCOVERY_ENABLED` | `true` | Enable authenticated model picker discovery |
| `PROBE_INTERVAL_HOURS` | `24` | Picker refresh interval; must be positive |
| `PROBE_TIMEOUT_MINUTES` | `15` | Timeout in minutes for session/catalogue requests; must be positive |
| `CUSTOM_PROMPTS` | file | Local prompt block prepended to every LobeHub request |
| `KEEPALIVE_HOURS` | `6` | Session ping interval |
| `FLARESOLVERR_URL` | `http://localhost:8191` | FlareSolverr endpoint for `/health` quota data |
| `NTFY_TOPIC` | disabled | Unique ntfy.sh topic: maximum 64 characters using letters, numbers, `_`, or `-`; the shared public `pplx-proxy` name is rejected |
| `NTFY_URL` | `https://ntfy.sh` | ntfy server URL |
| `NTFY_COOLDOWN_SECS` | `3600` | Min interval between alerts |
| `PUBLIC_URL` | `http://localhost:8892` | URL in ntfy messages |
| `PPLX_API_VERSION` | `2.18` | Perplexity internal API ver |
| `PPLX_IMPERSONATE` | `chrome` | curl_cffi TLS fingerprint |
| `USER_AGENT` | Chrome/130 | HTTP User-Agent |
| `COOKIE_MAX_AGE_HOURS` | `168` | Cookie cache max age |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment (systemd)

Edit `pplx-proxy.service` to match your user, installation paths and port before installing it.

```bash
sudo cp pplx-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pplx-proxy
```

## Cookie Lifecycle

```
Manual inject → validate at startup and every 6h → persist returned cookie rotation → restart uses the latest session
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

## Critical Implementation Notes

**Why models say "I can't access real-time data":** This proxy must handle three issues that cause Perplexity models to ignore their own search results:

1. **`search_focus: "internet"`** must be set in every request. Without it, Perplexity defaults to `"writing"` mode where models don't incorporate search results. This is the single most important parameter.

2. **System prompts must be stripped or replaced** before sending to Perplexity. Perplexity searches ALL query text — if the system prompt says "You are an AI assistant", Perplexity finds chatbot tutorial pages and the model gets confused. Generic clients keep only whitelist-approved lines; LobeHub requests discard upstream prompt content entirely.

3. **LobeHub requests always prepend local `CUSTOM_PROMPTS`.** The proxy still detects `role: developer` and system-prompt-like user messages so it can classify the request source, but those upstream prompt blocks are never forwarded. Each LobeHub turn sends `instructions=[CUSTOM_PROMPTS]` plus preserved `history` and current `query`.

4. **Rate limit tracking** uses FlareSolverr (`FLARESOLVERR_URL`, default `http://localhost:8191`) to poll Perplexity's `/rest/rate-limit/all` endpoint with the session cookie. Requires FlareSolverr for quota fields in `/health`. When `remaining_pro` reaches 0, non-auto requests receive HTTP 429; select `auto` explicitly to use the free model.

See AGENTS.md for the full technical breakdown and MANUAL.md troubleshooting section for diagnosis steps.

## Disclaimer

Unofficial reverse proxy for personal use. Relies on Perplexity's internal web API which may change without notice. Use responsibly.

## License

MIT

### Verification and session updates

Run `./test.sh [base-url]` with the project's Python environment installed. It validates health, models, complete chat responses, streaming completion, and Responses output. HTTP errors, malformed output, and interrupted streams cause a nonzero exit status.

Run `node --test test_chat.js` for debug-page protocol checks.

Run `./inject_cookie.sh` for a hidden token prompt, or pipe a token on standard input. The helper reads the project `.env`, calls the running local service, and only replaces the session after validation. It does not restart the service. Avoid placing tokens in shell command arguments or history.

Requests exceeding the 96,000-character query limit receive an explicit error; the proxy does not silently truncate conversation content. `/health` reports cached quota immediately and schedules a refresh when needed. A successful chat stream ends with `[DONE]`; a failed stream emits an error instead of reporting success.

Chat Completions rejects unsupported generation controls with HTTP 400, including custom sampling, output-token limits, structured response formats, multiple choices, stop sequences, nondefault penalties, seeds, logprobs, and streaming usage requests. These options are not silently ignored.
