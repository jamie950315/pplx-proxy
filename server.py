"""
pplx-proxy: Perplexity Pro reverse proxy
- OpenAI-compatible /v1/chat/completions
- Streamable HTTP MCP server at /mcp + SSE at /sse
- Session keep-alive to prevent cookie expiry
"""

import os
import json
import time
import asyncio
import logging
import re
from uuid import uuid4
from typing import Optional, AsyncGenerator
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from curl_cffi import requests as cffi_requests

load_dotenv(Path(__file__).parent / ".env")

# ─── Config ────────────────────────────────────────────────────────────────

PPLX_COOKIE=os.getenv("PPLX_COOKIE", "")
API_KEY=os.getenv("PPLX_PROXY_API_KEY", "")
PORT=int(os.getenv("PPLX_PROXY_PORT", "8892"))
LOG_LEVEL=os.getenv("LOG_LEVEL", "INFO")
COOKIE_FILE=Path(__file__).parent / ".cookie_cache.json"
MODELS_FILE=Path(__file__).parent / ".models.json"
DEFAULT_MODEL=os.getenv("DEFAULT_MODEL", "gpt")
ACCOUNT_TYPE=os.getenv("ACCOUNT_TYPE", "pro").lower()  # free, pro, max
PUBLIC_URL=os.getenv("PUBLIC_URL", "http://localhost:8892")
PPLX_API_VERSION=os.getenv("PPLX_API_VERSION", "2.18")
PPLX_IMPERSONATE=os.getenv("PPLX_IMPERSONATE", "chrome")
COOKIE_MAX_AGE_HOURS=int(os.getenv("COOKIE_MAX_AGE_HOURS", "168"))
NTFY_COOLDOWN_SECS=int(os.getenv("NTFY_COOLDOWN_SECS", "3600"))
USER_AGENT=os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ENV_FILE=Path(__file__).parent / ".env"

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log=logging.getLogger("pplx-proxy")

# ─── Perplexity Client ─────────────────────────────────────────────────────

PPLX_BASE="https://www.perplexity.ai"
PPLX_SSE_ASK=f"{PPLX_BASE}/rest/sse/perplexity_ask"
PPLX_AUTH_SESSION=f"{PPLX_BASE}/api/auth/session"

DEFAULT_HEADERS={
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "dnt": "1",
    "sec-ch-ua": '"Chromium";v="130", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "user-agent": USER_AGENT,
}

# Default model map — overridden by .models.json if it exists
# All known models (superset)
_ALL_MODELS={
    "auto": ("pro", "pplx_pro"),
    "sonar": ("pro", "experimental"),
    "gpt": ("pro", "gpt54"),
    "gpt-thinking": ("pro", "gpt54_thinking"),
    "gemini": ("pro", "gemini31pro_high"),
    "sonnet": ("pro", "claude46sonnet"),
    "sonnet-thinking": ("pro", "claude46sonnetthinking"),
    "opus": ("pro", "claude46opus"),
    "opus-thinking": ("pro", "claude46opusthinking"),
    "nemotron": ("pro", "nv_nemotron_3_super"),
}

# Model availability per account tier
_TIER_MODELS={
    "free": {"auto"},
    "pro": {"auto", "sonar", "gpt", "gpt-thinking",
            "gemini", "sonnet", "sonnet-thinking",
            "nemotron"},
    "max": {"auto", "sonar", "gpt", "gpt-thinking",
            "gemini", "sonnet", "sonnet-thinking",
            "nemotron", "opus", "opus-thinking"},
}

def _default_model_map() -> dict:
    """Return default model map filtered by account tier."""
    allowed=_TIER_MODELS.get(ACCOUNT_TYPE, _TIER_MODELS["pro"])
    return {k: v for k, v in _ALL_MODELS.items() if k in allowed}

def load_model_map() -> dict:
    """Load model map from .models.json or use defaults."""
    if MODELS_FILE.exists():
        try:
            data=json.loads(MODELS_FILE.read_text())
            # format: {"model_id": ["mode", "internal_pref"]}
            return {k: tuple(v) for k, v in data.items()}
        except Exception as e:
            log.warning(f"Failed to load {MODELS_FILE}: {e}")
    return _default_model_map()

def save_model_map(mm: dict):
    """Save model map to .models.json."""
    data={k: list(v) for k, v in mm.items()}
    MODELS_FILE.write_text(json.dumps(data, indent=2))
    log.info(f"Model map saved ({len(mm)} models)")

def check_tier(model_name: str) -> str:
    """Check if model is available for current account tier. Returns error msg or empty string."""
    allowed=_TIER_MODELS.get(ACCOUNT_TYPE, _TIER_MODELS["pro"])
    if model_name not in allowed:
        if model_name in _ALL_MODELS:
            # Model exists but not in this tier
            needed="max" if model_name in _TIER_MODELS["max"] else "pro"
            return f"Model '{model_name}' requires {needed} tier (current: {ACCOUNT_TYPE})"
        return ""  # unknown model, let model_map handle it
    return ""

def get_model_map() -> dict:
    """Get current model map filtered by account tier."""
    global MODEL_MAP
    allowed=_TIER_MODELS.get(ACCOUNT_TYPE, _TIER_MODELS["pro"])
    return {k: v for k, v in MODEL_MAP.items() if k in allowed}

MODEL_MAP=load_model_map()


class PerplexityClient:
    """Async Perplexity client using SSE endpoint with curl_cffi."""

    def __init__(self, cookies: dict):
        self.cookies=cookies
        self.session: Optional[cffi_requests.AsyncSession]=None
        self._initialized=False

    async def init(self):
        if self._initialized:
            return
        self.session=cffi_requests.AsyncSession(
            headers=DEFAULT_HEADERS.copy(),
            cookies=self.cookies,
            impersonate=PPLX_IMPERSONATE,
        )
        try:
            resp=await self.session.get(PPLX_AUTH_SESSION)
            log.info(f"Session init: {resp.status_code}")
        except Exception as e:
            log.error(f"Session init failed: {e}")
        self._initialized=True

    def reset(self, cookies: dict):
        """Reset client with new cookies."""
        self.cookies=cookies
        self.session=None
        self._initialized=False
        log.info("Client reset with new cookies")

    async def search(
        self,
        query: str,
        mode: str="auto",
        model_pref: str="turbo",
        sources: list=None,
        language: str="en-US",
        follow_up_uuid: str=None,
    ) -> AsyncGenerator[dict, None]:
        if sources is None:
            sources=["web"]
        await self.init()

        pplx_mode="concise" if mode == "auto" else "copilot"

        json_data={
            "query_str": query,
            "params": {
                "attachments": [],
                "frontend_context_uuid": str(uuid4()),
                "frontend_uuid": str(uuid4()),
                "is_incognito": False,
                "language": language,
                "last_backend_uuid": follow_up_uuid,
                "mode": pplx_mode,
                "model_preference": model_pref,
                "source": "default",
                "sources": sources,
                "version": PPLX_API_VERSION,
            },
        }

        log.info(f"Query: mode={mode}, pref={model_pref}, q={query[:80]}...")

        try:
            resp=await self.session.post(PPLX_SSE_ASK, json=json_data, stream=True)
        except Exception as e:
            log.error(f"Request failed: {e}")
            yield {"error": str(e)}
            return

        if resp.status_code != 200:
            body=resp.text[:500] if hasattr(resp, 'text') else str(resp.status_code)
            log.error(f"Perplexity {resp.status_code}: {body}")
            yield {"error": f"HTTP {resp.status_code}", "detail": body}
            if resp.status_code in (401, 403):
                asyncio.create_task(notify_cookie_expired(f"Perplexity returned HTTP {resp.status_code}"))
            return

        full_answer=""
        backend_uuid=None
        web_results=[]
        seen_len=0  # track cumulative answer length to deduplicate

        async for line in resp.aiter_lines(delimiter=b"\r\n\r\n"):
            content=line.decode("utf-8") if isinstance(line, bytes) else line
            if not content.startswith("event: message\r\n"):
                if content.startswith("event: end_of_stream"):
                    break
                continue

            data_str=content[len("event: message\r\ndata: "):]
            try:
                chunk=json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if "backend_uuid" in chunk:
                backend_uuid=chunk["backend_uuid"]
            if "web_results" in chunk:
                web_results=chunk["web_results"]

            # Extract streaming text from blocks[].markdown_block
            blocks=chunk.get("blocks", [])
            for block in blocks:
                usage=block.get("intended_usage", "")
                if "markdown" not in usage:
                    continue
                mb=block.get("markdown_block", {})
                if not mb:
                    continue
                progress=mb.get("progress", "")
                chunks=mb.get("chunks", [])
                if not chunks:
                    continue
                if progress == "DONE":
                    # Final: full cumulative text
                    full_answer="".join(chunks)
                else:
                    # Incremental: extract only new text
                    chunk_text="".join(chunks)
                    cumulative=full_answer + chunk_text
                    if len(cumulative) > seen_len:
                        delta=cumulative[seen_len:]
                        full_answer=cumulative
                        seen_len=len(cumulative)
                        yield {"delta": delta, "answer": full_answer, "backend_uuid": backend_uuid, "web_results": web_results, "done": False}

        yield {"delta": "", "answer": full_answer, "backend_uuid": backend_uuid, "web_results": web_results, "done": True}


# ─── Cookie Management ──────────────────────────���──────────────────────────

def load_cookies() -> dict:
    """Load cookies from cache file, .env, or return empty."""
    # 1. try cache file (freshest)
    if COOKIE_FILE.exists():
        try:
            data=json.loads(COOKIE_FILE.read_text())
            ts=data.get("timestamp", 0)
            age_h=(time.time() - ts) / 3600
            if age_h < COOKIE_MAX_AGE_HOURS:
                log.info(f"Loaded cached cookies (age: {age_h:.1f}h)")
                return data["cookies"]
        except Exception as e:
            log.warning(f"Cookie cache read error: {e}")

    # 2. try env var
    if PPLX_COOKIE:
        try:
            cookies=json.loads(PPLX_COOKIE)
            return cookies
        except json.JSONDecodeError:
            return {"next-auth.session-token": PPLX_COOKIE}

    return {}

def save_cookies(cookies: dict):
    """Save cookies to cache file."""
    data={"cookies": cookies, "timestamp": time.time()}
    COOKIE_FILE.write_text(json.dumps(data, indent=2))
    log.info(f"Cookies saved to {COOKIE_FILE}")


# ─── Singleton client ──────────────────────────────────────────────────────

_client: Optional[PerplexityClient]=None

def get_client() -> PerplexityClient:
    global _client
    if _client is None:
        cookies=load_cookies()
        if not cookies:
            raise RuntimeError("No cookies available. Set PPLX_COOKIE in .env or run cookie refresh.")
        _client=PerplexityClient(cookies)
    return _client


# ─── Auth middleware ───────────────────────────────────────────────────────

async def verify_api_key(request: Request):
    if not API_KEY:
        return
    auth=request.headers.get("authorization", "")
    token=auth[7:] if auth.lower().startswith("bearer ") else auth
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ─── FastAPI App ───────────────────────────────────────────────────────────

app=FastAPI(title="pplx-proxy", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    cache_age=None
    if COOKIE_FILE.exists():
        try:
            data=json.loads(COOKIE_FILE.read_text())
            cache_age=round((time.time() - data.get("timestamp", 0)) / 3600, 1)
        except Exception:
            pass
    return {"status": "ok", "service": "pplx-proxy", "cookie_age_hours": cache_age}


@app.get("/v1/models")
async def list_models(_=Depends(verify_api_key)):
    mm=get_model_map()
    models=[]
    for mid, (mode, pref) in mm.items():
        models.append({"id": mid, "object": "model", "created": 1700000000, "owned_by": "perplexity", "mode": mode, "internal_pref": pref})
    return {"object": "list", "data": models, "default_model": DEFAULT_MODEL, "account_type": ACCOUNT_TYPE}


# ─── Tool Calling Support ──────────────────────────────────────────────────

import xml.etree.ElementTree as _ET
import re as _re

def _build_tool_prompt(tool_defs: str, tool_choice: str="auto") -> str:
    base=(
        "You have access to tools. When calling a tool, use this EXACT XML format:\n"
        "<tool_call>\n<function_name>\n<param>value</param>\n</function_name>\n</tool_call>\n\n"
        "Available tools:\n" + tool_defs + "\n\n"
        "Example — get_weather(location=Tokyo):\n"
        "<tool_call>\n<get_weather>\n<location>Tokyo</location>\n</get_weather>\n</tool_call>\n\n"
        "Rules: Each param is its own XML element. Root element = function name. "
        "Multiple calls = multiple <tool_call> blocks. "
        "When calling a tool, output ONLY the XML, nothing else."
    )
    if tool_choice == "required":
        return base + "\nYou MUST call at least one tool. Do NOT respond with plain text."
    elif tool_choice == "none":
        return ""  # no tool prompt
    else:  # auto
        return base + "\nOnly use tools when the user\'s request specifically needs one. If you can answer directly, respond normally WITHOUT <tool_call> tags."


def _tools_to_xml(tools: list) -> str:
    defs=[]
    for tool in tools:
        if tool.get("type") != "function":
            continue
        fn=tool.get("function", {})
        name=fn.get("name", "unknown")
        desc=fn.get("description", "No description")
        params=fn.get("parameters", {})
        props=params.get("properties", {})
        required=set(params.get("required", []))
        plines=[]
        for pname, ps in props.items():
            pt=ps.get("type", "string")
            pd=ps.get("description", "")
            req="required" if pname in required else "optional"
            plines.append(f'    <parameter name="{pname}" type="{pt}" {req}="true">{pd}</parameter>')
        pblock="\n".join(plines) if plines else "    (none)"
        defs.append(f'  <tool name="{name}">\n    <description>{desc}</description>\n    <parameters>\n{pblock}\n    </parameters>\n  </tool>')
    return "\n".join(defs)


_TOOL_CALL_RE=_re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", _re.DOTALL)

def _parse_tool_calls(text: str) -> tuple:
    matches=_TOOL_CALL_RE.findall(text)
    if not matches:
        return [], text
    tool_calls=[]
    for xml_str in matches:
        try:
            root=_ET.fromstring(f"<root>{xml_str.strip()}</root>")
            for child in root:
                fn_name=child.tag
                arguments={}
                for param in child:
                    val=(param.text or "").strip()
                    if val.lower() in ("true", "false"):
                        arguments[param.tag]=val.lower() == "true"
                    else:
                        try:
                            arguments[param.tag]=int(val)
                        except ValueError:
                            try:
                                arguments[param.tag]=float(val)
                            except ValueError:
                                arguments[param.tag]=val
                tool_calls.append({
                    "id": f"call_{uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": fn_name, "arguments": json.dumps(arguments)},
                })
        except _ET.ParseError:
            continue
    remaining=_TOOL_CALL_RE.sub("", text).strip()
    return tool_calls, remaining



@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _=Depends(verify_api_key)):
    try:
        body=await request.json()
    except Exception:
        raise HTTPException(400, "Invalid or empty JSON body")
    model_name=body.get("model", DEFAULT_MODEL)
    messages=body.get("messages", None)
    stream=body.get("stream", False)
    language=body.get("language", "en-US")
    sources=body.get("sources", ["web"])
    tools=body.get("tools", None)
    tool_choice=body.get("tool_choice", "auto")

    # Validate messages
    if messages is None:
        raise HTTPException(400, "Missing required field: messages")
    if not isinstance(messages, list):
        raise HTTPException(400, "messages must be an array")
    if len(messages) == 0:
        raise HTTPException(400, "messages array is empty")
    VALID_ROLES={"system", "user", "assistant", "tool"}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise HTTPException(400, f"messages[{i}] must be an object")
        role=msg.get("role")
        if role is None:
            raise HTTPException(400, f"messages[{i}] missing required field: role")
        if role not in VALID_ROLES:
            raise HTTPException(400, f"messages[{i}] invalid role: '{role}'. Must be one of: {sorted(VALID_ROLES)}")
        # content can be null/missing for assistant (tool_calls) and tool messages
        if "content" not in msg and role not in ("assistant", "tool"):
            raise HTTPException(400, f"messages[{i}] missing required field: content")

    mm=get_model_map()
    tier_err=check_tier(model_name)
    if tier_err:
        raise HTTPException(403, tier_err)
    if model_name not in mm:
        raise HTTPException(400, f"Unknown model: {model_name}. Available: {list(mm.keys())}")

    try:
        mode, model_pref=mm[model_name]
    except (ValueError, TypeError):
        raise HTTPException(500, f"Corrupted model entry for {model_name}. Fix via /admin/update-models")

    parts=[]
    for msg in messages:
        role=msg.get("role", "user")
        content=msg.get("content") or ""
        if isinstance(content, list):
            text_parts=[c.get("text", "") for c in content if c.get("type") == "text"]
            content=" ".join(text_parts)
        if role == "system":
            parts.append(f"[System]: {content}")
        elif role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            parts.append(f"[Tool Result]: {content}")
    query="\n\n".join(parts)

    # Tool calling: inject tool definitions into query
    if tools and isinstance(tools, list) and len(tools) > 0 and tool_choice != "none":
        tool_defs=_tools_to_xml(tools)
        if tool_defs:
            tool_prompt=_build_tool_prompt(tool_defs, tool_choice)
            if tool_prompt:
                query=f"{query}\n\n---\n{tool_prompt}"

    client=get_client()
    cid=f"chatcmpl-{uuid4().hex[:12]}"
    created=int(time.time())

    if stream:
        # With tools: buffer response to detect tool calls, then emit appropriately
        if tools and isinstance(tools, list) and len(tools) > 0:
            return StreamingResponse(
                _stream_openai_with_tools(client, query, mode, model_pref, model_name, cid, created, sources, language),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return StreamingResponse(
            _stream_openai(client, query, mode, model_pref, model_name, cid, created, sources, language),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    full=""
    async for chunk in client.search(query, mode, model_pref, sources, language):
        if chunk.get("error"):
            raise HTTPException(502, chunk)
        if chunk.get("done"):
            full=chunk.get("answer", full)
            break
        full=chunk.get("answer", full)

    # Check for tool calls in response
    if tools and isinstance(tools, list):
        tool_calls, remaining=_parse_tool_calls(full)
        if tool_calls:
            msg={"role": "assistant", "content": remaining if remaining else None, "tool_calls": tool_calls}
            return {
                "id": cid, "object": "chat.completion", "created": created, "model": model_name,
                "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": len(query)//4, "completion_tokens": len(full)//4, "total_tokens": (len(query)+len(full))//4},
            }

    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": full}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(query)//4, "completion_tokens": len(full)//4, "total_tokens": (len(query)+len(full))//4},
    }


async def _stream_openai_with_tools(client, query, mode, model_pref, model_name, cid, created, sources, language):
    """Stream with tool call detection: buffer response, check for tool_calls, then emit."""
    full=""
    async for chunk in client.search(query, mode, model_pref, sources, language):
        if chunk.get("error"):
            e={"id": f"chatcmpl-{uuid4().hex[:12]}", "object": "chat.completion.chunk", "created": int(time.time()), "model": model_name,
               "choices": [{"index": 0, "delta": {"content": f"[Error: {chunk['error']}]"}, "finish_reason": None}]}
            yield f"data: {json.dumps(e)}\n\n"
            yield "data: [DONE]\n\n"
            return
        if chunk.get("done"):
            full=chunk.get("answer", full)
            break
        full=chunk.get("answer", full)

    cid=f"chatcmpl-{uuid4().hex[:12]}"
    created=int(time.time())

    # Check for tool calls
    tool_calls, remaining=_parse_tool_calls(full)
    if tool_calls:
        # Emit as tool_calls in a single chunk
        msg_delta={"role": "assistant", "tool_calls": []}
        for i, tc in enumerate(tool_calls):
            msg_delta["tool_calls"].append({
                "index": i, "id": tc["id"], "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
            })
        init={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
              "choices": [{"index": 0, "delta": msg_delta, "finish_reason": None}]}
        yield f"data: {json.dumps(init)}\n\n"
        stop={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        yield f"data: {json.dumps(stop)}\n\n"
        yield "data: [DONE]\n\n"
    else:
        # No tool calls — emit buffered text as stream
        init={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
              "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        yield f"data: {json.dumps(init)}\n\n"
        # Emit content in chunks
        for i in range(0, len(full), 50):
            chunk_text=full[i:i+50]
            d={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
               "choices": [{"index": 0, "delta": {"content": chunk_text}, "finish_reason": None}]}
            yield f"data: {json.dumps(d)}\n\n"
        stop={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(stop)}\n\n"
        yield "data: [DONE]\n\n"


async def _stream_openai(client, query, mode, model_pref, model_name, cid, created, sources, language):
    init={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
          "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    yield f"data: {json.dumps(init)}\n\n"

    async for chunk in client.search(query, mode, model_pref, sources, language):
        if chunk.get("error"):
            e={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
               "choices": [{"index": 0, "delta": {"content": f"[Error: {chunk['error']}]"}, "finish_reason": None}]}
            yield f"data: {json.dumps(e)}\n\n"
            break

        dt=chunk.get("delta", "")
        if dt:
            # Break large deltas into word-sized chunks for real streaming
            if len(dt) > 100:
                words=dt.split(" ")
                buf=""
                for w in words:
                    buf+=(" " if buf else "") + w
                    if len(buf) >= 20:
                        d={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                           "choices": [{"index": 0, "delta": {"content": buf}, "finish_reason": None}]}
                        yield f"data: {json.dumps(d)}\n\n"
                        buf=""
                        await asyncio.sleep(0.02)
                if buf:
                    d={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                       "choices": [{"index": 0, "delta": {"content": buf}, "finish_reason": None}]}
                    yield f"data: {json.dumps(d)}\n\n"
            else:
                d={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                   "choices": [{"index": 0, "delta": {"content": dt}, "finish_reason": None}]}
                yield f"data: {json.dumps(d)}\n\n"

        if chunk.get("done"):
            wr=chunk.get("web_results", [])
            if wr:
                cites="\n\n---\nSources:\n"
                for i, w in enumerate(wr[:10]):
                    url=w.get("url", w) if isinstance(w, dict) else str(w)
                    cites+=f"[{i+1}] {url}\n"
                c={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                   "choices": [{"index": 0, "delta": {"content": cites}, "finish_reason": None}]}
                yield f"data: {json.dumps(c)}\n\n"

            stop={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(stop)}\n\n"
            break

    yield "data: [DONE]\n\n"


# ─── Cookie Refresh Endpoint ──────────────────────────────────────────────




# ─── Model Discovery ──────────────────────────────────────────────────────

import re as _re

# Patterns to extract version from known prefs and generate next versions
_VERSION_PATTERNS=[
    # gpt54 → major=5, minor=4
    (_re.compile(r"^(gpt)(\d)(\d)((?:_thinking)?)$"), "{prefix}{ma}{mi}{suffix}"),
    # claude46sonnet → major=4, minor=6
    (_re.compile(r"^(claude)(\d)(\d)(sonnet(?:thinking)?)$"), "{prefix}{ma}{mi}{suffix}"),
    (_re.compile(r"^(claude)(\d)(\d)(opus(?:thinking)?)$"), "{prefix}{ma}{mi}{suffix}"),
    # gemini31pro_high → major=3, minor=1
    (_re.compile(r"^(gemini)(\d)(\d)(pro(?:_high)?)$"), "{prefix}{ma}{mi}{suffix}"),
    # grok41nonreasoning → major=4, minor=1
    (_re.compile(r"^(grok)(\d)(\d)((?:non)?reasoning)$"), "{prefix}{ma}{mi}{suffix}"),
    # nv_nemotron_3_super → gen=3
    (_re.compile(r"^(nv_nemotron_)(\d)(_super|_ultra)$"), "{prefix}{ma}{suffix}"),
]

def _increment_version(major: int, minor: int) -> tuple:
    """Increment version: 5.4 → 5.5, 5.9 → 6.0"""
    minor+=1
    if minor >= 10:
        minor=0
        major+=1
    return major, minor

def _version_distance(orig_ma, orig_mi, cur_ma, cur_mi) -> float:
    """Calculate version distance: e.g., 5.4 → 7.4 = 2.0"""
    return (cur_ma - orig_ma) + (cur_mi - orig_mi) / 10.0


async def probe_model(client, pref) -> bool:
    """Test if a model_preference is valid."""
    try:
        async for chunk in client.search("2+2=?", "pro", pref, ["web"], "en-US"):
            if chunk.get("error"):
                return False
            if chunk.get("answer", "").strip():
                return True
        return False
    except Exception:
        return False


@app.post("/admin/discover-models")
async def discover_models(request: Request, _=Depends(verify_api_key)):
    """Smart model discovery:
    1. Skip thinking variants (they follow their base model)
    2. Check if each base model still works
    3. If dead, increment version until found or +1.0 reached
    4. Auto-upgrade thinking variant along with base
    """
    client=get_client()
    await client.init()

    mm=get_model_map()

    # Split into base models and thinking variants
    thinking_map={}  # base_id → thinking_id
    base_models={}   # base_id → (mode, pref)
    for mid, (mode, pref) in mm.items():
        if mid.endswith("-thinking"):
            base_id=mid.replace("-thinking", "")
            thinking_map[base_id]=mid
        else:
            base_models[mid]=(mode, pref)

    report={"alive": [], "upgraded": {}, "dead": [], "skipped_thinking": list(thinking_map.values()), "probed": 0}

    for model_id, (mode, pref) in base_models.items():
        # Match against version patterns
        matched=False
        for pattern, template in _VERSION_PATTERNS:
            m=pattern.match(pref)
            if m:
                matched=True
                break

        if not matched:
            # Non-versioned (pplx_pro, experimental, etc.) — just check alive
            report["probed"]+=1
            ok=await probe_model(client, pref)
            if ok:
                report["alive"].append(model_id)
            else:
                report["dead"].append({"model": model_id, "pref": pref, "reason": "non-versioned, no upgrade path"})
            await asyncio.sleep(2)
            continue

        # Versioned — check if alive
        report["probed"]+=1
        ok=await probe_model(client, pref)
        if ok:
            report["alive"].append(model_id)
            await asyncio.sleep(2)
            continue

        # Dead — search for next version
        groups=m.groups()
        if len(groups) == 4:
            prefix, orig_ma_s, orig_mi_s, suffix=groups
            orig_ma, orig_mi=int(orig_ma_s), int(orig_mi_s)
            ma, mi=orig_ma, orig_mi
            found=False

            while True:
                ma, mi=_increment_version(ma, mi)
                if _version_distance(orig_ma, orig_mi, ma, mi) > 1.0:
                    break
                new_pref=template.format(prefix=prefix, ma=ma, mi=mi, suffix=suffix)
                report["probed"]+=1
                log.info(f"Discovery: {model_id} dead, trying {new_pref}...")
                if await probe_model(client, new_pref):
                    # Upgrade base
                    global MODEL_MAP
                    MODEL_MAP[model_id]=(mode, new_pref)
                    upgrade={"old": pref, "new": new_pref}

                    # Auto-upgrade thinking variant if exists
                    if model_id in thinking_map:
                        t_id=thinking_map[model_id]
                        old_t_pref=mm[t_id][1]
                        # Derive new thinking pref from new base pref
                        if "_thinking" in old_t_pref:
                            new_t_pref=new_pref+"_thinking" if not new_pref.endswith("_thinking") else new_pref
                        else:
                            new_t_pref=new_pref+"thinking"
                        MODEL_MAP[t_id]=(mode, new_t_pref)
                        upgrade["thinking_old"]=old_t_pref
                        upgrade["thinking_new"]=new_t_pref
                        log.info(f"Discovery: {t_id} auto-upgraded {old_t_pref} → {new_t_pref}")

                    report["upgraded"][model_id]=upgrade
                    log.info(f"Discovery: {model_id} upgraded {pref} → {new_pref}")
                    found=True
                    break
                await asyncio.sleep(2)

            if not found:
                report["dead"].append({"model": model_id, "pref": pref, "reason": "no valid version within +1.0"})

        elif len(groups) == 3:
            prefix, gen_s, suffix=groups
            orig_gen=int(gen_s)
            found=False
            for gen in range(orig_gen+1, orig_gen+2):
                new_pref=template.format(prefix=prefix, ma=gen, suffix=suffix)
                report["probed"]+=1
                if await probe_model(client, new_pref):
                    MODEL_MAP[model_id]=(mode, new_pref)
                    report["upgraded"][model_id]={"old": pref, "new": new_pref}
                    found=True
                    break
                await asyncio.sleep(2)
            if not found:
                report["dead"].append({"model": model_id, "pref": pref, "reason": "no next gen found"})

    if report["upgraded"]:
        save_model_map(MODEL_MAP)

    return {
        "status": "ok",
        "alive": len(report["alive"]),
        "upgraded": len(report["upgraded"]),
        "dead": len(report["dead"]),
        "probed": report["probed"],
        "skipped_thinking": len(report["skipped_thinking"]),
        "details": report,
    }







# ─── MCP Server ────────────────────────────────────────────────────────────

try:
    from mcp.server.fastmcp import FastMCP
    HAS_MCP=True
except ImportError:
    HAS_MCP=False
    log.warning("mcp package not installed, MCP endpoints disabled.")

if HAS_MCP:
    mcp=FastMCP("pplx-proxy", instructions="Perplexity Pro Search reverse proxy.")

    @mcp.tool()
    async def perplexity_search(query: str, model: str="default", sources: str="web", language: str="en-US") -> str:
        """Pro Search: Enhanced web search with Perplexity Pro.
        Model: default (uses DEFAULT_MODEL from config), or any model ID from perplexity_models().
        Sources: web, scholar, social (comma-separated)."""
        if not query or not query.strip():
            return "Error: query cannot be empty"
        mm=get_model_map()
        model_id=DEFAULT_MODEL if model == "default" else model
        tier_err=check_tier(model_id)
        if tier_err:
            return f"Error: {tier_err}"
        if model_id not in mm:
            avail=", ".join(sorted(mm.keys()))
            return f"Error: Unknown model '{model_id}'. Available models: {avail}"
        mode, pref=mm[model_id]
        VALID_SOURCES={"web", "scholar", "social"}
        src=[s.strip() for s in sources.split(",")]
        invalid_src=[s for s in src if s not in VALID_SOURCES]
        if invalid_src:
            return f"Error: Invalid sources: {invalid_src}. Valid: {sorted(VALID_SOURCES)}"
        client=get_client()
        r=""
        async for ch in client.search(query, mode, pref, src, language):
            if ch.get("error"): return f"Error: {ch['error']}"
            if ch.get("done"): r=ch.get("answer", r); break
            r=ch.get("answer", r)
        return r

    @mcp.tool()
    async def perplexity_ask(query: str, language: str="en-US") -> str:
        """Auto Search: Quick general-purpose Q&A."""
        if not query or not query.strip():
            return "Error: query cannot be empty"
        client=get_client()
        r=""
        async for c in client.search(query, "auto", "turbo", ["web"], language):
            if c.get("error"): return f"Error: {c['error']}"
            if c.get("done"): r=c.get("answer", r); break
            r=c.get("answer", r)
        return r

    @mcp.tool()
    async def perplexity_reason(query: str, model: str="default", language: str="en-US") -> str:
        """Reasoning: Step-by-step reasoning through complex problems.
        Model: default, pplx-reasoning-gpt5, pplx-reasoning-claude, pplx-reasoning-gemini, pplx-reasoning-kimi, pplx-reasoning-grok, or shorthand: gpt5, claude, gemini, kimi, grok."""
        if not query or not query.strip():
            return "Error: query cannot be empty"
        mm=get_model_map()
        shorthand={"gpt": "gpt-thinking", "claude": "sonnet-thinking", "opus": "opus-thinking", "gemini": "gemini", "nemotron": "nemotron"}
        resolved=model
        if model == "default":
            resolved="gpt-thinking"
        elif model in shorthand:
            resolved=shorthand[model]
        tier_err=check_tier(resolved)
        if tier_err:
            return f"Error: {tier_err}"
        if resolved not in mm:
            avail=["default"] + list(shorthand.keys()) + [k for k in mm if "thinking" in k]
            return f"Error: Unknown reasoning model '{model}'. Available: {avail}"
        mode, pref=mm[resolved]
        client=get_client()
        r=""
        async for ch in client.search(query, mode, pref, ["web"], language):
            if ch.get("error"): return f"Error: {ch['error']}"
            if ch.get("done"): r=ch.get("answer", r); break
            r=ch.get("answer", r)
        return r

    @mcp.tool()
    async def perplexity_research(query: str, language: str="en-US") -> str:
        """Deep Research: Comprehensive in-depth research. Takes longer (30s+)."""
        if not query or not query.strip():
            return "Error: query cannot be empty"
        client=get_client()
        r=""
        async for c in client.search(query, "deep research", "pplx_alpha", ["web"], language):
            if c.get("error"): return f"Error: {c['error']}"
            if c.get("done"): r=c.get("answer", r); break
            r=c.get("answer", r)
        return r

    @mcp.tool()
    async def perplexity_models() -> str:
        """List all available Perplexity models with their modes and IDs.
        Use these IDs as the 'model' parameter in other tools."""
        mm=get_model_map()
        lines=[f"Default model: {DEFAULT_MODEL}", f"Account type: {ACCOUNT_TYPE}", "", "Available models:"]
        by_mode={}
        for mid, (mode, pref) in mm.items():
            by_mode.setdefault(mode, []).append(mid)
        for mode in ["auto", "pro", "reasoning", "deep research"]:
            if mode in by_mode:
                lines.append(f"\n[{mode}]")
                for mid in by_mode[mode]:
                    marker=" (default)" if mid == DEFAULT_MODEL else ""
                    lines.append(f"  - {mid}{marker}")
        return "\n".join(lines)

    from contextlib import asynccontextmanager as _acm

    mcp_http_app=mcp.streamable_http_app()
    mcp_sse_app=mcp.sse_app()

    # Wrap FastAPI lifespan to include MCP streamable HTTP session manager init
    _orig_lifespan=app.router.lifespan_context

    @_acm
    async def _combined_lifespan(a):
        async with mcp_http_app.router.lifespan_context(mcp_http_app):
            log.info("MCP streamable HTTP lifespan started")
            asyncio.create_task(session_keepalive_loop())
            asyncio.create_task(auto_discover_loop())
            log.info(f"pplx-proxy started on port {PORT}")
            yield
        log.info("MCP streamable HTTP lifespan stopped")

    app.router.lifespan_context=_combined_lifespan
    app.mount("/mcp", mcp_http_app)
    app.mount("/sse", mcp_sse_app)
    log.info("MCP mounted at /mcp (streamable HTTP) + /sse (SSE)")


# ─── Model Management ──────────────────────────────────────────────────

@app.post("/admin/update-models")
async def update_models_endpoint(request: Request, _=Depends(verify_api_key)):
    """Update available model map. POST body: full model map or partial additions.
    Format: {"models": {"model-id": ["mode", "internal_pref"], ...}, "merge": true/false}
    merge=true (default): add/update entries. merge=false: replace entire map.
    """
    try:
        body=await request.json()
    except Exception:
        raise HTTPException(400, "Invalid or empty JSON body")
    new_models=body.get("models", {})
    if not isinstance(new_models, dict):
        raise HTTPException(400, "models must be a dict: {model_id: [mode, internal_pref]}")
    for k, v in new_models.items():
        if not isinstance(v, (list, tuple)) or len(v) != 2:
            raise HTTPException(400, f"Model '{k}' must be [mode, internal_pref] (2 elements), got: {v}")
        if not all(isinstance(x, str) for x in v):
            raise HTTPException(400, f"Model '{k}' values must be strings, got: {v}")
    merge=body.get("merge", True)

    global MODEL_MAP
    if merge:
        MODEL_MAP.update({k: tuple(v) for k, v in new_models.items()})
    else:
        MODEL_MAP={k: tuple(v) for k, v in new_models.items()}

    save_model_map(MODEL_MAP)
    return {"status": "ok", "model_count": len(MODEL_MAP), "models": list(MODEL_MAP.keys())}


@app.get("/admin/models")
async def get_models_admin(_=Depends(verify_api_key)):
    """Get full model map with internal details."""
    mm=get_model_map()
    return {"default": DEFAULT_MODEL, "account_type": ACCOUNT_TYPE, "models": {k: {"mode": v[0], "pref": v[1]} for k, v in mm.items()}}


# ─── Session Keep-Alive ────────────────────────────────────────────────────

KEEPALIVE_HOURS=int(os.getenv("KEEPALIVE_HOURS", "6"))
PROBE_INTERVAL_HOURS=int(os.getenv("PROBE_INTERVAL_HOURS", "24"))
NTFY_TOPIC=os.getenv("NTFY_TOPIC", "pplx-proxy")
NTFY_URL=os.getenv("NTFY_URL", "https://ntfy.sh")
_last_ntfy_ts=0.0

async def notify_cookie_expired(reason: str):
    """Send push notification via ntfy.sh when cookie needs manual update."""
    global _last_ntfy_ts
    now=time.time()
    if now - _last_ntfy_ts < NTFY_COOLDOWN_SECS:
        return
    _last_ntfy_ts=now
    if not NTFY_TOPIC:
        return
    try:
        import httpx
        async with httpx.AsyncClient() as hc:
            await hc.post(
                f"{NTFY_URL}/{NTFY_TOPIC}",
                headers={
                    "Title": "pplx-proxy: Cookie Expired",
                    "Priority": "high",
                    "Tags": "warning,key",
                    "Actions": f"view, Open Admin, {PUBLIC_URL}/health",
                },
                content=f"Perplexity session cookie 失效，需要手動更新。\n\n原因: {reason}\n\ncurl -X POST {PUBLIC_URL}/admin/refresh-cookie -H \"Authorization: Bearer YOUR_KEY\" -H \"Content-Type: application/json\" -d '{{\"session_token\": \"NEW_TOKEN\"}}\'",
            )
        log.warning(f"ntfy notification sent: {reason}")
    except Exception as e:
        log.error(f"ntfy send failed: {e}")

async def session_keepalive_loop():
    """Periodically hit Perplexity session endpoint to keep cookie alive."""
    log.info(f"Session keep-alive enabled: every {KEEPALIVE_HOURS}h")
    while True:
        await asyncio.sleep(KEEPALIVE_HOURS * 3600)
        try:
            client=get_client()
            await client.init()
            resp=await client.session.get(PPLX_AUTH_SESSION)
            if resp.status_code == 200:
                data=resp.json() if hasattr(resp, 'json') else {}
                user=data.get("user", {}).get("email", "unknown") if isinstance(data, dict) else "unknown"
                log.info(f"Keep-alive OK: {resp.status_code}, user={user}")
                # Update timestamp in cache
                if COOKIE_FILE.exists():
                    try:
                        cache=json.loads(COOKIE_FILE.read_text())
                        cache["timestamp"]=time.time()
                        cache["last_keepalive"]=time.time()
                        COOKIE_FILE.write_text(json.dumps(cache, indent=2))
                    except Exception:
                        pass
            else:
                log.warning(f"Keep-alive failed: HTTP {resp.status_code}")
                if resp.status_code in (401, 403):
                    await notify_cookie_expired(f"Keep-alive returned HTTP {resp.status_code}")
        except Exception as e:
            log.error(f"Keep-alive error: {e}")


@app.post("/admin/refresh-cookie")
async def refresh_cookie_endpoint(request: Request, _=Depends(verify_api_key)):
    """Inject new cookie. Accepts JSON {"session_token": "..."} or plain text body."""
    ct=request.headers.get("content-type", "")
    token=""
    if "json" in ct:
        try:
            body=await request.json()
            token=body.get("session_token", "")
        except Exception:
            pass
    if not token:
        # Try reading body as plain text
        raw=await request.body()
        token=raw.decode("utf-8", errors="ignore").strip()
    if not token:
        return {"status": "error", "message": "Send session token as plain text body or JSON {\"session_token\": \"...\"}"}
    cookies={"next-auth.session-token": token}
    save_cookies(cookies)
    global _client
    if _client:
        _client.reset(cookies)
    else:
        _client=PerplexityClient(cookies)
    # Reload model map from file if it exists
    global MODEL_MAP
    MODEL_MAP=load_model_map()
    return {"status": "ok", "message": "Cookie updated and client reset", "models_loaded": len(MODEL_MAP)}


async def auto_discover_loop():
    """Background task: run model discovery every PROBE_INTERVAL_HOURS."""
    log.info(f"Auto-discovery enabled: every {PROBE_INTERVAL_HOURS}h")
    while True:
        await asyncio.sleep(PROBE_INTERVAL_HOURS * 3600)
        log.info("Scheduled model discovery starting...")
        try:
            client=get_client()
            await client.init()
            mm=get_model_map()
            thinking_map={}
            base_models={}
            for mid, (mode, pref) in mm.items():
                if mid.endswith("-thinking"):
                    thinking_map[mid.replace("-thinking", "")]=mid
                else:
                    base_models[mid]=(mode, pref)
            for model_id, (mode, pref) in base_models.items():
                matched=False
                for pattern, template in _VERSION_PATTERNS:
                    m=pattern.match(pref)
                    if m:
                        matched=True
                        break
                if not matched:
                    continue
                ok=await probe_model(client, pref)
                if ok:
                    continue
                # Dead — try upgrading
                groups=m.groups()
                if len(groups)==4:
                    prefix,oma_s,omi_s,suffix=groups
                    oma,omi=int(oma_s),int(omi_s)
                    ma,mi=oma,omi
                    while True:
                        ma,mi=_increment_version(ma,mi)
                        if _version_distance(oma,omi,ma,mi)>1.0:
                            break
                        new_pref=template.format(prefix=prefix,ma=ma,mi=mi,suffix=suffix)
                        if await probe_model(client, new_pref):
                            MODEL_MAP[model_id]=(mode, new_pref)
                            if model_id in thinking_map:
                                t_id=thinking_map[model_id]
                                old_tp=mm[t_id][1]
                                new_tp=new_pref+("_thinking" if "_thinking" in old_tp else "thinking")
                                MODEL_MAP[t_id]=(mode, new_tp)
                            save_model_map(MODEL_MAP)
                            log.info(f"Auto-discovery: {model_id} upgraded {pref} → {new_pref}")
                            await notify_cookie_expired(f"Model {model_id} auto-upgraded: {pref} → {new_pref}")
                            break
                        await asyncio.sleep(2)
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Auto-discovery error: {e}")


# startup tasks moved into _combined_lifespan above


# ─── Entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
