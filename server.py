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
DEFAULT_MODEL=os.getenv("DEFAULT_MODEL", "pplx-pro")
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
_DEFAULT_MODEL_MAP={
    # Pro Search (current)
    "pplx-auto": ("pro", "pplx_pro"),
    "pplx-pro-sonar": ("pro", "experimental"),
    "pplx-pro-gpt5": ("pro", "gpt54"),
    "pplx-pro-claude": ("pro", "claude46sonnet"),
    "pplx-pro-gemini": ("pro", "gemini31pro_high"),
    "pplx-pro-nemotron": ("pro", "nv_nemotron_3_super"),
    "pplx-pro-opus": ("pro", "claude46opus"),
    "pplx-pro-grok": ("pro", "grok41nonreasoning"),
    # Thinking
    "pplx-pro-gpt5-thinking": ("pro", "gpt54_thinking"),
    "pplx-pro-claude-thinking": ("pro", "claude46sonnetthinking"),
    "pplx-pro-opus-thinking": ("pro", "claude46opusthinking"),
    "pplx-pro-grok-thinking": ("pro", "grok41reasoning"),
    "pplx-pro-kimi-thinking": ("pro", "kimik2thinking"),
    "pplx-pro-kimi25-thinking": ("pro", "kimik25thinking"),
    # Legacy (removed from UI but still functional)
    "pplx-pro-gpt52": ("pro", "gpt52"),
    "pplx-pro-gpt52-thinking": ("pro", "gpt52_thinking"),
    "pplx-pro-claude45": ("pro", "claude45sonnet"),
    "pplx-pro-claude45-thinking": ("pro", "claude45sonnetthinking"),
    "pplx-pro-gemini30": ("pro", "gemini30pro"),
    "pplx-pro-opus45": ("pro", "claude45opus"),
    # Deep Research & Labs
    "pplx-deep-research": ("pro", "pplx_alpha"),
    "pplx-labs": ("pro", "pplx_beta"),
}

def load_model_map() -> dict:
    """Load model map from .models.json or use defaults."""
    if MODELS_FILE.exists():
        try:
            data=json.loads(MODELS_FILE.read_text())
            # format: {"model_id": ["mode", "internal_pref"]}
            return {k: tuple(v) for k, v in data.items()}
        except Exception as e:
            log.warning(f"Failed to load {MODELS_FILE}: {e}")
    return _DEFAULT_MODEL_MAP.copy()

def save_model_map(mm: dict):
    """Save model map to .models.json."""
    data={k: list(v) for k, v in mm.items()}
    MODELS_FILE.write_text(json.dumps(data, indent=2))
    log.info(f"Model map saved ({len(mm)} models)")

def get_model_map() -> dict:
    """Get current model map (cached in module global)."""
    global MODEL_MAP
    return MODEL_MAP

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

        last_answer=""
        backend_uuid=None
        web_results=[]

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

            if "text" in chunk and chunk["text"]:
                try:
                    text_parsed=json.loads(chunk["text"])
                    if isinstance(text_parsed, list):
                        for step in text_parsed:
                            if step.get("step_type") == "FINAL":
                                fc=step.get("content", {})
                                if "answer" in fc:
                                    ad=json.loads(fc["answer"])
                                    chunk["answer"]=ad.get("answer", "")
                                    chunk["chunks"]=ad.get("chunks", [])
                    chunk["text"]=text_parsed
                except (json.JSONDecodeError, TypeError, KeyError):
                    pass

            if "backend_uuid" in chunk:
                backend_uuid=chunk["backend_uuid"]
            if "web_results" in chunk:
                web_results=chunk["web_results"]

            current_answer=chunk.get("answer", "")
            if current_answer and len(current_answer) > len(last_answer):
                delta=current_answer[len(last_answer):]
                last_answer=current_answer
                yield {"delta": delta, "answer": current_answer, "backend_uuid": backend_uuid, "web_results": web_results, "done": False}

        yield {"delta": "", "answer": last_answer, "backend_uuid": backend_uuid, "web_results": web_results, "done": True}


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
    return {"object": "list", "data": models, "default_model": DEFAULT_MODEL}


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

    # Validate messages
    if messages is None:
        raise HTTPException(400, "Missing required field: messages")
    if not isinstance(messages, list):
        raise HTTPException(400, "messages must be an array")
    if len(messages) == 0:
        raise HTTPException(400, "messages array is empty")
    VALID_ROLES={"system", "user", "assistant"}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise HTTPException(400, f"messages[{i}] must be an object")
        role=msg.get("role")
        if role is None:
            raise HTTPException(400, f"messages[{i}] missing required field: role")
        if role not in VALID_ROLES:
            raise HTTPException(400, f"messages[{i}] invalid role: '{role}'. Must be one of: {sorted(VALID_ROLES)}")
        if "content" not in msg:
            raise HTTPException(400, f"messages[{i}] missing required field: content")

    mm=get_model_map()
    if model_name not in mm:
        raise HTTPException(400, f"Unknown model: {model_name}. Available: {list(mm.keys())}")

    try:
        mode, model_pref=mm[model_name]
    except (ValueError, TypeError):
        raise HTTPException(500, f"Corrupted model entry for {model_name}. Fix via /admin/update-models")

    parts=[]
    for msg in messages:
        role=msg.get("role", "user")
        content=msg.get("content", "")
        if isinstance(content, list):
            text_parts=[c.get("text", "") for c in content if c.get("type") == "text"]
            content=" ".join(text_parts)
        if role == "system":
            parts.append(f"[System]: {content}")
        elif role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[Assistant]: {content}")
    query="\n\n".join(parts)

    client=get_client()
    cid=f"chatcmpl-{uuid4().hex[:12]}"
    created=int(time.time())

    if stream:
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

    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": full}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(query)//4, "completion_tokens": len(full)//4, "total_tokens": (len(query)+len(full))//4},
    }


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




# ─── Model Discovery ───────────────────────────────────────────────────────

# ─── Pattern-based candidate generator ─────────────────────────────────────

def generate_candidates() -> dict:
    """Generate candidate model_preference strings from known naming patterns.
    Covers current + future versions automatically."""
    candidates={}

    # Perplexity internal
    for pref in ["pplx_pro", "experimental", "pplx_alpha", "pplx_beta", "pplx_gamma", "pplx_reasoning", "turbo"]:
        candidates[f"probe-{pref}"]=("pro", pref)

    # OpenAI: gpt{major}{minor} — scan 50..65
    for v in range(50, 66):
        p=f"gpt{v}"
        candidates[f"probe-{p}"]=("pro", p)
        candidates[f"probe-{p}-t"]=("pro", f"{p}_thinking")

    # Anthropic Claude Sonnet: claude{major}{minor}sonnet — scan 45..50
    for v in range(45, 51):
        p=f"claude{v}sonnet"
        candidates[f"probe-{p}"]=("pro", p)
        candidates[f"probe-{p}-t"]=("pro", f"{p}thinking")

    # Anthropic Claude Opus: claude{major}{minor}opus — scan 45..50
    for v in range(45, 51):
        p=f"claude{v}opus"
        candidates[f"probe-{p}"]=("pro", p)
        candidates[f"probe-{p}-t"]=("pro", f"{p}thinking")

    # Google Gemini: gemini{major}{minor}pro / gemini{major}{minor}pro_high — scan 20..35
    for v in range(20, 36):
        candidates[f"probe-gemini{v}pro"]=("pro", f"gemini{v}pro")
        candidates[f"probe-gemini{v}pro-h"]=("pro", f"gemini{v}pro_high")

    # xAI Grok: grok{major}{minor}nonreasoning/reasoning — scan 40..45
    for v in range(40, 46):
        candidates[f"probe-grok{v}"]=("pro", f"grok{v}nonreasoning")
        candidates[f"probe-grok{v}-t"]=("pro", f"grok{v}reasoning")

    # Moonshot Kimi: kimik{ver}thinking — scan k2..k4, k25, k35
    for ver in ["k2", "k25", "k3", "k35", "k4"]:
        candidates[f"probe-kimi{ver}"]=("pro", f"kimi{ver}")
        candidates[f"probe-kimi{ver}-t"]=("pro", f"kimi{ver}thinking")

    # NVIDIA Nemotron
    for gen in ["3", "4", "5"]:
        for suffix in ["super", "ultra"]:
            p=f"nv_nemotron_{gen}_{suffix}"
            candidates[f"probe-{p}"]=("pro", p)

    return candidates


async def probe_model(client, mode, pref) -> bool:
    """Send a simple query to test if a model_preference is valid.
    Returns True if Perplexity returns a non-empty answer."""
    try:
        async for chunk in client.search("What is 2+2? Answer with just the number.", mode, pref, ["web"], "en-US"):
            if chunk.get("error"):
                return False
            answer=chunk.get("answer", "")
            if answer.strip():
                return True
        return False
    except Exception:
        return False


def pref_to_friendly_name(pref: str) -> str:
    """Convert internal pref like gpt54_thinking to friendly model ID like pplx-pro-gpt54-thinking."""
    if pref.endswith("_thinking") or pref.endswith("thinking"):
        base=pref.replace("_thinking", "").replace("thinking", "")
        return f"pplx-{base}-thinking"
    if pref.endswith("nonreasoning"):
        base=pref.replace("nonreasoning", "")
        return f"pplx-{base}"
    if pref.endswith("reasoning"):
        base=pref.replace("reasoning", "")
        return f"pplx-{base}-thinking"
    return f"pplx-{pref}"


@app.post("/admin/discover-models")
async def discover_models(request: Request, _=Depends(verify_api_key)):
    """Probe Perplexity for all working model identifiers.
    Auto-generates candidates from naming patterns (GPT, Claude, Gemini, Grok, Kimi, Nemotron).
    WARNING: Uses ~1 Pro Search query per unknown candidate. Skips already-known prefs."""
    client=get_client()
    await client.init()

    candidates=generate_candidates()
    current_mm=get_model_map()
    known_prefs={v[1] for v in current_mm.values()}

    results={"valid": {}, "invalid": 0, "skipped": 0, "probed": 0}

    for probe_name, (mode, pref) in candidates.items():
        # Skip if this pref is already known
        if pref in known_prefs:
            results["skipped"]+=1
            continue

        results["probed"]+=1
        try:
            ok=await probe_model(client, mode, pref)
            if ok:
                friendly=pref_to_friendly_name(pref)
                results["valid"][friendly]=[mode, pref]
                log.info(f"Discovery: {pref} = VALID → {friendly}")
            else:
                results["invalid"]+=1
        except Exception as e:
            log.warning(f"Discovery probe error {pref}: {e}")

        await asyncio.sleep(3)  # rate limit protection

    # Merge valid discoveries into model map
    if results["valid"]:
        global MODEL_MAP
        for name, (mode, pref) in results["valid"].items():
            MODEL_MAP[name]=tuple([mode, pref])
        save_model_map(MODEL_MAP)

    return {
        "status": "ok",
        "new_models": len(results["valid"]),
        "invalid": results["invalid"],
        "skipped": results["skipped"],
        "probed": results["probed"],
        "total_candidates": len(candidates),
        "total_models": len(MODEL_MAP),
        "discovered": results["valid"],
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
        shorthand={"gpt5": "pplx-pro-gpt5-thinking", "claude": "pplx-pro-claude-thinking",
                    "opus": "pplx-pro-opus-thinking", "gemini": "pplx-pro-gemini",
                    "nemotron": "pplx-pro-nemotron"}
        if model == "default":
            mode, pref="reasoning", "pplx_reasoning"
        elif model in mm:
            mode, pref=mm[model]
        elif model in shorthand and shorthand[model] in mm:
            mode, pref=mm[shorthand[model]]
        else:
            avail=["default"] + list(shorthand.keys()) + [k for k in mm if "reasoning" in k]
            return f"Error: Unknown reasoning model '{model}'. Available: {avail}"
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
        lines=[f"Default model: {DEFAULT_MODEL}", "", "Available models:"]
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
    return {"default": DEFAULT_MODEL, "models": {k: {"mode": v[0], "pref": v[1]} for k, v in mm.items()}}


# ─── Session Keep-Alive ────────────────────────────────────────────────────

KEEPALIVE_HOURS=int(os.getenv("KEEPALIVE_HOURS", "6"))
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
    """Manually inject a new cookie via POST body: {"session_token": "..."}"""
    try:
        body=await request.json()
    except Exception:
        raise HTTPException(400, "Invalid or empty JSON body")
    token=body.get("session_token", "")
    if not token:
        return {"status": "error", "message": "Provide session_token in JSON body"}
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


@app.on_event("startup")
async def startup():
    asyncio.create_task(session_keepalive_loop())
    log.info(f"pplx-proxy started on port {PORT}")


# ─── Entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
