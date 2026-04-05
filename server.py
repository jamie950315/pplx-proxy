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
import hashlib
from uuid import uuid4
from typing import Optional, AsyncGenerator
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
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

# ─── Rate Limit Tracker ────────────────────────────────────────────────────

_rate_limit={"remaining_pro": None, "remaining_research": None, "updated_at": 0}
_rate_limit_lock=None  # initialized in startup

def _fetch_rate_limit_sync():
    """Fetch rate limits from Perplexity via FlareSolverr. ~10s per call."""
    import urllib.request
    try:
        cookies=load_cookies()
        token=cookies.get("__Secure-next-auth.session-token", "")
        if not token:
            return None
        req=urllib.request.Request("http://localhost:8191/v1",
            data=json.dumps({
                "cmd": "request.get",
                "url": "https://www.perplexity.ai/rest/rate-limit/all",
                "maxTimeout": 20000,
                "cookies": [{"name": "__Secure-next-auth.session-token", "value": token,
                             "domain": ".perplexity.ai", "path": "/", "secure": True, "httpOnly": True}]
            }).encode(), headers={"Content-Type": "application/json"})
        resp=urllib.request.urlopen(req, timeout=25)
        fs=json.loads(resp.read())
        body=fs.get("solution", {}).get("response", "")
        import re as _rl_re
        m=_rl_re.search(r"<pre[^>]*>(.*?)</pre>", body, _rl_re.DOTALL)
        raw=m.group(1) if m else body
        d=json.loads(raw)
        _rate_limit["remaining_pro"]=d.get("remaining_pro")
        _rate_limit["remaining_research"]=d.get("remaining_research")
        _rate_limit["updated_at"]=int(time.time())
        log.info(f"Rate limit synced: pro={_rate_limit['remaining_pro']}, research={_rate_limit['remaining_research']}")
        return d
    except Exception as e:
        log.warning(f"Rate limit fetch failed: {e}")
        return None

async def _rate_limit_poll_loop():
    """Background task: sync rate limit every 1 hour."""
    while True:
        await asyncio.sleep(3600)  # 1 hour
        try:
            loop=asyncio.get_event_loop()
            await loop.run_in_executor(None, _fetch_rate_limit_sync)
        except Exception as e:
            log.warning(f"Rate limit poll failed: {e}")

def _decrement_pro():
    """Decrement local remaining_pro counter after a successful Pro query."""
    if _rate_limit["remaining_pro"] is not None and _rate_limit["remaining_pro"] > 0:
        _rate_limit["remaining_pro"] -= 1

def _should_show_remaining() -> bool:
    """Show remaining notice at multiples of 5 or when ≤5."""
    rp = _rate_limit.get("remaining_pro")
    if rp is None:
        return False
    return rp <= 5 or rp % 5 == 0

def _remaining_notice() -> str:
    """Build the remaining notice string, or empty if not needed."""
    if not _should_show_remaining():
        return ""
    rp = _rate_limit["remaining_pro"]
    return f"\n\n[Remaining Pro Search: {rp}]"

# ─── Prompt Whitelist / Custom Prompts (file-based, hot-reloadable) ─────────

_WHITELIST_FILE=Path(__file__).parent / ".prompt_whitelist.txt"
_CUSTOM_PROMPTS_FILE=Path(__file__).parent / "CUSTOM_PROMPTS"
_whitelist_cache={"patterns": [], "mtime": 0}
_custom_prompts_cache={"text": "", "mtime": 0}

def _load_whitelist() -> list:
    """Load regex patterns from .prompt_whitelist.txt. Hot-reloads on file change."""
    try:
        mtime=_WHITELIST_FILE.stat().st_mtime
    except FileNotFoundError:
        return []
    if mtime == _whitelist_cache["mtime"]:
        return _whitelist_cache["patterns"]
    patterns=[]
    try:
        for line in _WHITELIST_FILE.read_text().splitlines():
            line=line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                patterns.append(re.compile(line))
            except re.error as e:
                log.warning(f"Invalid whitelist regex: {line!r} — {e}")
        _whitelist_cache["patterns"]=patterns
        _whitelist_cache["mtime"]=mtime
        log.info(f"Loaded {len(patterns)} whitelist patterns from {_WHITELIST_FILE}")
    except Exception as e:
        log.warning(f"Failed to load whitelist: {e}")
    return patterns

def _load_custom_prompts() -> str:
    """Load custom prompts from CUSTOM_PROMPTS. Hot-reloads on file change."""
    try:
        mtime=_CUSTOM_PROMPTS_FILE.stat().st_mtime
    except FileNotFoundError:
        return ""
    if mtime == _custom_prompts_cache["mtime"]:
        return _custom_prompts_cache["text"]
    try:
        text=_CUSTOM_PROMPTS_FILE.read_text().strip()
        _custom_prompts_cache["text"]=text
        _custom_prompts_cache["mtime"]=mtime
        log.info(f"Loaded custom prompts from {_CUSTOM_PROMPTS_FILE} ({len(text)} chars)")
        return text
    except Exception as e:
        log.warning(f"Failed to load custom prompts: {e}")
        return ""


def _filter_system_prompt(system_msg: str) -> list:
    """Filter system prompt: only lines matching a whitelist pattern survive.
    Pre-processes <skill> XML tags to extract their inner text as standalone lines."""
    whitelist=_load_whitelist()

    # Pre-process: extract text inside <skill> tags (LobeHub wraps custom prompts in XML)
    # Handles multi-line: <skill name="...">line1\nline2\n...</skill>
    import re as _sp_re
    # First extract all <skill>...</skill> blocks and replace with their inner text
    _processed=_sp_re.sub(r"<skill[^>]*>(.*?)</skill>", lambda m: m.group(1), system_msg, flags=_sp_re.DOTALL)
    # Also strip any remaining XML tags (orphan opening/closing tags)
    _processed=_sp_re.sub(r"</?[a-zA-Z_][^>]*>", "", _processed)
    _expanded=_processed.splitlines()

    kept=[]
    for line in _expanded:
        ls=line.strip().lstrip("- *")
        if not ls:
            continue
        if len(ls) > 150:
            continue  # Skip long lines (skill descriptions, XML noise)
        if whitelist and any(p.search(ls) for p in whitelist):
            kept.append(ls)
    # Always append search instruction
    kept.append("You have built-in web search. Answer questions directly using search results. Never say you cannot access data or need external tools.")
    return kept

def _detect_request_source(system_msg: str, messages: list) -> str:
    """Best-effort request source detection for logging/debugging."""
    raw=(system_msg or "")
    lowered=raw.lower()
    score=0
    if "you are lobe" in lowered:
        score+=3
    if any(tag in lowered for tag in ["<available_skills>", "<user_memory>", "<available_tools>", "<tool.instructions>", "<skill name="]):
        score+=3
    if any(term in lowered for term in ["activateskill", "activatetools", "runskill", "lobe-skill-store", "lobe-creds"]):
        score+=2
    if any(msg.get("role") == "developer" for msg in messages if isinstance(msg, dict)):
        score+=1
    return "lobehub" if score >= 3 else "generic_openai_client"


def _log_prompt_payload(source: str, request_source: str, system_msg: str, final_instructions: list, history: list, current_msg: str, query: str, is_first_user_turn: bool=False, custom_prompts_loaded: bool=False):
    """Log raw/final prompt payloads for debugging prompt filtering."""
    raw_system=system_msg.strip()
    instructions_text="\n".join(final_instructions) if final_instructions else ""
    history_json=json.dumps([{"role": r, "content": ct} for r, ct in history], ensure_ascii=False, indent=2) if history else "[]"
    current_text=current_msg or ""
    log.info(
        f"PROMPT DEBUG [{source}] source_guess={request_source} is_first_user_turn={str(is_first_user_turn).lower()} custom_prompts_loaded={str(custom_prompts_loaded).lower()}\n"
        f"--- RAW SYSTEM PROMPT START ---\n{raw_system or '[empty]'}\n--- RAW SYSTEM PROMPT END ---\n"
        f"--- FINAL INSTRUCTIONS START ---\n{instructions_text or '[empty]'}\n--- FINAL INSTRUCTIONS END ---\n"
        f"--- HISTORY START ---\n{history_json}\n--- HISTORY END ---\n"
        f"--- CURRENT QUERY START ---\n{current_text or '[empty]'}\n--- CURRENT QUERY END ---\n"
        f"--- FINAL QUERY JSON START ---\n{query}\n--- FINAL QUERY JSON END ---"
    )


logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log=logging.getLogger("pplx-proxy")


# ─── Perplexity Session Cache ─────────────────────────────────────────────
# Tracks backend_uuid per conversation so follow-up turns can skip history/instructions.
# Key = hash of conversation history, Value = {backend_uuid, timestamp}

_SESSION_MAX_AGE=3600  # 1 hour TTL
_SESSION_MAX_ENTRIES=200
_session_cache={}

def _session_key(history: list) -> str:
    """Compute a stable hash of conversation history for session lookup."""
    h=hashlib.sha256()
    for role, content in history:
        h.update(f"{role}:{content}\n".encode())
    return h.hexdigest()[:16]

def _session_lookup(history: list) -> str | None:
    """Look up a stored backend_uuid for this conversation history. Returns None on miss."""
    if not history:
        return None
    key=_session_key(history)
    entry=_session_cache.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > _SESSION_MAX_AGE:
        del _session_cache[key]
        return None
    log.info(f"SESSION HIT: key={key} backend_uuid={entry['backend_uuid']}")
    return entry["backend_uuid"]

def _session_store(history: list, current_msg: str, response_text: str, backend_uuid: str):
    """Store backend_uuid keyed by the conversation state AFTER this turn."""
    if not backend_uuid:
        return
    new_history=list(history) + [("user", current_msg), ("assistant", response_text)]
    key=_session_key(new_history)
    _session_cache[key]={"backend_uuid": backend_uuid, "ts": time.time()}
    log.info(f"SESSION STORE: key={key} backend_uuid={backend_uuid} entries={len(_session_cache)}")
    # Evict oldest if over limit
    if len(_session_cache) > _SESSION_MAX_ENTRIES:
        oldest=min(_session_cache, key=lambda k: _session_cache[k]["ts"])
        del _session_cache[oldest]


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
    "gemini": ("pro", "gemini31pro_high"),
    "sonnet": ("pro", "claude46sonnet"),
    "opus": ("pro", "claude46opus"),
    "nemotron": ("pro", "nv_nemotron_3_super"),
}

# Thinking variants — activated via thinking=true parameter
_THINKING_MAP={
    "gpt": ("pro", "gpt54_thinking"),
    "sonnet": ("pro", "claude46sonnetthinking"),
    "opus": ("pro", "claude46opusthinking"),
}

# Model availability per account tier
_TIER_MODELS={
    "free": {"auto"},
    "pro": {"auto", "sonar", "gpt", "gemini", "sonnet", "nemotron"},
    "max": {"auto", "sonar", "gpt", "gemini", "sonnet", "nemotron", "opus"},
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
        model_pref: str="pplx_pro",
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
                "search_focus": "internet",
                "search_recency_filter": None,
                "timezone": "Asia/Taipei",
                "visitor_id": str(uuid4()),
                "user_nextauth_id": str(uuid4()),
                "prompt_source": "user",
                "query_source": "home",
                "browser_history_summary": [],
                "is_related_query": False,
                "is_sponsored": False,
                "is_nav_suggestions_disabled": False,
                "use_schematized_api": True,
                "send_back_text_in_streaming_api": False,
                "supported_block_use_cases": [
                    "answer_modes", "media_items", "knowledge_cards",
                    "inline_entity_cards", "place_widgets", "finance_widgets",
                    "sports_widgets", "shopping_widgets", "search_result_widgets",
                ],
                "client_coordinates": None,
                "version": PPLX_API_VERSION,
            },
        }

        log.info(f"Query: mode={mode}, pref={model_pref}, len={len(query)}")
        log.info(f"PPLX REQUEST QUERY START\n{query}\nPPLX REQUEST QUERY END")

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
        seen_len=0
        _seen_thinking=set()  # dedup thinking content

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

            # Extract thinking content from search/plan blocks
            blocks=chunk.get("blocks", [])
            for block in blocks:
                usage=block.get("intended_usage", "")

                # Thinking: search steps
                if usage == "pro_search_steps":
                    pb=block.get("plan_block", {})
                    for step in pb.get("steps", []):
                        st=step.get("step_type", "")
                        if st == "SEARCH_WEB":
                            queries=[q.get("query","") for q in step.get("search_web_content",{}).get("queries",[])]
                            for q in queries:
                                if q and q not in _seen_thinking:
                                    _seen_thinking.add(q)
                                    yield {"thinking": f"Searching: {q}", "done": False}
                        elif st == "READ_RESULTS":
                            urls=[u for u in step.get("read_results_content",{}).get("urls",[]) if u]
                            for u in urls[:3]:
                                if u not in _seen_thinking:
                                    _seen_thinking.add(u)
                                    yield {"thinking": f"Reading: {u}", "done": False}

                # Thinking: plan goals
                if usage == "plan":
                    pb=block.get("plan_block", {})
                    for goal in pb.get("goals", []):
                        desc=goal.get("description", "")
                        if desc and desc not in _seen_thinking:
                            _seen_thinking.add(desc)
                            yield {"thinking": desc, "done": False}

                # Thinking: web results (capture as they arrive)
                if usage == "web_results":
                    wb=block.get("web_result_block", {})
                    results=wb.get("web_results", [])
                    for r in results[:8]:
                        url=r.get("url","")
                        name=r.get("name","")
                        if url and url not in _seen_thinking:
                            _seen_thinking.add(url)
                            yield {"thinking": f"Found: [{name}]({url})", "done": False}

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
            return {"__Secure-next-auth.session-token": PPLX_COOKIE}

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

# Rate limit startup fetch is in _combined_lifespan below
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Global error handler: unconfigured service → 503
@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, exc: RuntimeError):
    return JSONResponse(status_code=503, content={"error": {"message": str(exc), "type": "service_unavailable"}})


from fastapi.responses import FileResponse as _FileResponse
from pathlib import Path as _StaticPath

@app.get("/chat")
async def chat_ui():
    """Debug chat interface."""
    p=_StaticPath(__file__).parent / "static" / "chat.html"
    if p.exists():
        return _FileResponse(p, media_type="text/html", headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
    raise HTTPException(404, "chat.html not found")

@app.get("/debug")
async def debug_page():
    """Redirect to /chat."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/chat")

@app.get("/health")
async def health():
    cache_age=None
    if COOKIE_FILE.exists():
        try:
            data=json.loads(COOKIE_FILE.read_text())
            cache_age=round((time.time() - data.get("timestamp", 0)) / 3600, 1)
        except Exception:
            pass
    # Trigger background rate limit refresh if stale (never blocks response)
    if _rate_limit["remaining_pro"] is None or (time.time() - _rate_limit["updated_at"]) > 300:
        asyncio.get_event_loop().run_in_executor(None, _fetch_rate_limit_sync)
    rl_age=int(time.time() - _rate_limit["updated_at"]) if _rate_limit["updated_at"] else None
    return {
        "status": "ok", "service": "pplx-proxy", "cookie_age_hours": cache_age,
        "remaining_pro": _rate_limit.get("remaining_pro"),
        "remaining_research": _rate_limit.get("remaining_research"),
        "rate_limit_age_seconds": rl_age,
    }


@app.get("/v1/models")
async def list_models(_=Depends(verify_api_key)):
    mm=get_model_map()
    models=[]
    for mid, (mode, pref) in mm.items():
        models.append({"id": mid, "object": "model", "created": 1700000000, "owned_by": "perplexity", "mode": mode, "internal_pref": pref})
    return {"object": "list", "data": models}


# ─── Tool Calling Support ──────────────────────────────────────────────────

import re as _re


_CITATION_RE=_re.compile(r'\[\d+\]')
_REMAINING_NOTICE_RE=_re.compile(r'\s*\[Remaining Pro Search: \d+\]\s*')
_GROK_TAG_RE=_re.compile(r'<grok:[^>]*>.*?</grok:[^>]*>', _re.DOTALL)
_GROK_SELF_RE=_re.compile(r'<grok:[^>]*/>')
_MULTI_SPACE=_re.compile(r' {2,}')
_MULTI_NL=_re.compile(r'\n{3,}')

def _clean_response(text: str, strip: bool=True) -> str:
    """Strip Perplexity citations and internal tags."""
    text=_re.sub(r'<[?]xml[^?]*[?]>', '', text)
    text=_CITATION_RE.sub('', text)
    text=_GROK_TAG_RE.sub('', text)
    text=_GROK_SELF_RE.sub('', text)
    text=_re.sub(r'</?response[^>]*>', '', text)
    text=_re.sub(r'<script[^>]*>.*?</script>', '', text, flags=_re.DOTALL)
    text=_re.sub(r'</?script[^>]*>', '', text)
    if strip:
        text=_MULTI_SPACE.sub(' ', text)
        text=_MULTI_NL.sub('\n\n', text)
        text=text.strip()
    return text


@app.post("/v1/responses")
async def responses_api(request: Request, _=Depends(verify_api_key)):
    """OpenAI Responses API compatibility. Supports streaming SSE.
    Used by LobeHub when 'use built-in web search' is enabled."""
    body=await request.json()
    stream=body.get("stream", False)
    model_name=body.get("model", DEFAULT_MODEL)
    inp=body.get("input", "")
    instructions=body.get("instructions", "")
    tools_raw=body.get("tools", [])
    log.info(f"Responses API: model={model_name}, stream={stream}")

    # Build messages from Responses API input
    messages=[]
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                role=item.get("role", "user")
                if role=="developer": role="system"
                content=item.get("content", "")
                if isinstance(content, list):
                    text_parts=[ct.get("text","") for ct in content if isinstance(ct, dict) and ct.get("type") in ("input_text","text")]
                    content=" ".join(text_parts) if text_parts else str(content)
                if content:
                    messages.append({"role": role, "content": content})

    if not messages or not any(m.get("role")=="user" for m in messages):
        raise HTTPException(400, "No user message found in input")

    # Build query using same logic as chat/completions
    system_msg=""
    history=[]
    for msg in messages:
        role=msg.get("role","user")
        # Detect user messages that are actually system prompts
        if role=="user":
            _ct=(msg.get("content") or "")[:200].lower()
            if any(kw in _ct for kw in ["you are ", "you must ", "your role", "ccsearch", "加載", "技能", "available_skills", "<skill", "<user_memory", "<available_tools", "<tool_selection", "<credentials", "<best_practices", "<memory_effort", "<session_context"]):
                role="system"
        content=msg.get("content") or ""
        # Strip rate limit notices from previous responses
        content=_REMAINING_NOTICE_RE.sub("", content).strip()
        if role=="system":
            system_msg+=content+"\n"
        elif role=="user":
            history.append(("user", content))
        elif role=="assistant":
            history.append(("assistant", content))

    # Dedup consecutive assistants
    deduped=[]
    for role,content in history:
        if deduped and role=="assistant" and deduped[-1][0]=="assistant":
            deduped[-1]=(role,content)
        else:
            deduped.append((role,content))
    history=deduped

    current_msg=""
    if history and history[-1][0]=="user":
        current_msg=history[-1][1]
        history=history[:-1]

    request_source=_detect_request_source(system_msg, messages)
    is_lobehub=request_source == "lobehub"
    is_first_user_turn=is_lobehub and not history

    # Session continuity: check if we can skip history/instructions
    follow_up_uuid=_session_lookup(history)
    if follow_up_uuid:
        query=current_msg
        final_instructions=[]
        log.info(f"SESSION CONTINUE [responses_api] source={request_source} follow_up={follow_up_uuid[:12]}...")
    else:
        custom_prompts=_load_custom_prompts() if is_lobehub else ""
        final_instructions=[]
        if is_lobehub:
            if custom_prompts:
                final_instructions.append(custom_prompts)
        else:
            final_instructions=_filter_system_prompt(system_msg) if system_msg else []

        # Build query as JSON for clear block separation
        query_obj={}
        if final_instructions:
            query_obj["instructions"]=final_instructions
        if history:
            query_obj["history"]=[{"role": r, "content": ct} for r, ct in history]
        if current_msg:
            query_obj["query"]=current_msg
        elif not history:
            query_obj["query"]=""

        query=json.dumps(query_obj, ensure_ascii=False)
        if len(query) > 96000:
            query=query[-96000:]

    _log_prompt_payload("responses_api", request_source, system_msg, final_instructions, history, current_msg, query, is_first_user_turn, not bool(follow_up_uuid) and bool(final_instructions))
    if not query.strip():
        raise HTTPException(400, "Empty query after processing")

    mm=get_model_map()
    if model_name not in mm:
        raise HTTPException(400, f"Unknown model: {model_name}")
    mode, model_pref=mm[model_name]

    # Quota fallback: auto-downgrade when Pro quota exhausted
    if _rate_limit.get("remaining_pro") is not None and _rate_limit["remaining_pro"] <= 0 and model_name != "auto":
        log.warning(f"Pro quota exhausted (remaining_pro={_rate_limit['remaining_pro']}), falling back {model_name}→auto")
        mode, model_pref=mm.get("auto", ("pro", "pplx_pro"))
        model_name="auto"

    client=get_client()
    resp_id=f"resp_{uuid4().hex[:12]}"
    created=int(time.time())

    if stream:
        async def _stream_responses_api():
            # Emit response.created
            resp_obj={"id": resp_id, "object": "response", "created_at": created,
                      "model": model_name, "status": "in_progress", "output": []}
            yield f"event: response.created\ndata: {json.dumps(resp_obj)}\n\n"

            # Emit output_item.added
            msg_id=f"msg_{uuid4().hex[:8]}"
            yield f"event: response.output_item.added\ndata: {json.dumps({'type': 'message', 'id': msg_id, 'role': 'assistant'})}\n\n"

            # Start reasoning summary part
            yield f"event: response.reasoning_summary_part.added\ndata: {json.dumps({'type': 'reasoning_summary_part', 'item_id': msg_id})}\n\n"

            full=""
            _resp_backend_uuid=None
            _thinking_parts=[]
            _thinking_done=False
            async for ch in client.search(query, mode, model_pref, ["web"], "en-US", follow_up_uuid):
                if ch.get("backend_uuid"):
                    _resp_backend_uuid=ch["backend_uuid"]
                if ch.get("error"):
                    yield f"event: error\ndata: {json.dumps({'error': ch['error']})}\n\n"
                    break
                if ch.get("thinking"):
                    t=ch["thinking"]
                    # Emit as reasoning summary delta (OpenAI Responses API format)
                    _thinking_parts.append(t)
                    evt={"type": "response.reasoning_summary_text.delta", "item_id": msg_id, "delta": t+"\n"}
                    yield f"event: response.reasoning_summary_text.delta\ndata: {json.dumps(evt)}\n\n"
                    continue
                if ch.get("done"):
                    full=ch.get("answer", full)
                    # Close reasoning if still open
                    if not _thinking_done:
                        _thinking_done=True
                        think_full="\n".join(_thinking_parts)
                        yield f"event: response.reasoning_summary_text.done\ndata: {json.dumps({'type': 'response.reasoning_summary_text.done', 'item_id': msg_id, 'text': think_full})}\n\n"
                        yield f"event: response.reasoning_summary_part.done\ndata: {json.dumps({'type': 'reasoning_summary_part', 'item_id': msg_id})}\n\n"
                    break
                # Close reasoning summary on first content chunk
                if not _thinking_done:
                    _thinking_done=True
                    think_full="\n".join(_thinking_parts)
                    yield f"event: response.reasoning_summary_text.done\ndata: {json.dumps({'type': 'response.reasoning_summary_text.done', 'item_id': msg_id, 'text': think_full})}\n\n"
                    yield f"event: response.reasoning_summary_part.done\ndata: {json.dumps({'type': 'reasoning_summary_part', 'item_id': msg_id})}\n\n"
                # Stream delta
                delta=ch.get("delta", "")
                if delta:
                    delta=_clean_response(delta, strip=False)
                    if delta:
                        evt={"type": "response.output_text.delta", "item_id": msg_id, "delta": delta}
                        yield f"event: response.output_text.delta\ndata: {json.dumps(evt)}\n\n"

            full=_clean_response(full)

            # Rate limit decrement + notice
            _decrement_pro()
            notice=_remaining_notice()
            if notice:
                evt_n={"type": "response.output_text.delta", "item_id": msg_id, "delta": notice}
                yield f"event: response.output_text.delta\ndata: {json.dumps(evt_n)}\n\n"
                full+=notice

            # Emit output_text.done
            yield f"event: response.output_text.done\ndata: {json.dumps({'type': 'response.output_text.done', 'item_id': msg_id, 'text': full})}\n\n"

            # Store session for next turn (use cleaned text before notice)
            _clean_full=_REMAINING_NOTICE_RE.sub("", full).strip()
            _session_store(history, current_msg, _clean_full, _resp_backend_uuid)

            # Emit response.completed
            done_resp={"id": resp_id, "object": "response", "created_at": created,
                       "model": model_name, "status": "completed",
                       "output": [{"type": "message", "id": msg_id, "role": "assistant", "status": "completed",
                                   "content": [{"type": "output_text", "text": full, "annotations": []}]}],
                       "usage": {"prompt_tokens": len(query)//4, "completion_tokens": len(full)//4, "total_tokens": (len(query)+len(full))//4}}
            yield f"event: response.completed\ndata: {json.dumps(done_resp)}\n\n"

        return StreamingResponse(_stream_responses_api(), media_type="text/event-stream",
                                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    else:
        # Non-streaming: collect full response
        full=""
        resp_backend_uuid=None
        async for ch in client.search(query, mode, model_pref, ["web"], "en-US", follow_up_uuid):
            if ch.get("backend_uuid"):
                resp_backend_uuid=ch["backend_uuid"]
            if ch.get("error"):
                raise HTTPException(502, ch)
            if ch.get("done"):
                full=ch.get("answer", full)
                break
            full=ch.get("answer", full)
        full=_clean_response(full)

        # Store session for next turn
        _session_store(history, current_msg, full, resp_backend_uuid)

        # Rate limit decrement + notice
        _decrement_pro()
        notice=_remaining_notice()
        if notice:
            full+=notice

        return {
            "id": resp_id, "object": "response", "created_at": created, "model": model_name,
            "output": [{"type": "message", "id": f"msg_{uuid4().hex[:8]}", "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": full, "annotations": []}]}],
            "status": "completed",
            "usage": {"prompt_tokens": len(query)//4, "completion_tokens": len(full)//4,
                      "total_tokens": (len(query)+len(full))//4},
        }


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
    thinking=body.get("thinking", False)
    reasoning_effort=body.get("reasoning_effort", None)  # "none" = no thinking, anything else = thinking

    # Validate messages
    if messages is None:
        raise HTTPException(400, "Missing required field: messages")
    if not isinstance(messages, list):
        raise HTTPException(400, "messages must be an array")
    if len(messages) == 0:
        raise HTTPException(400, "messages array is empty")
    VALID_ROLES={"system", "user", "assistant", "tool", "developer"}
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise HTTPException(400, f"messages[{i}] must be an object")
        role=msg.get("role")
        if role is None:
            raise HTTPException(400, f"messages[{i}] missing required field: role")
        if role not in VALID_ROLES:
            raise HTTPException(400, f"messages[{i}] invalid role: '{role}'. Must be one of: {sorted(VALID_ROLES)}")
        if "content" not in msg and role not in ("assistant", "tool"):
            raise HTTPException(400, f"messages[{i}] missing required field: content")

    mm=get_model_map()
    tier_err=check_tier(model_name)
    if tier_err:
        raise HTTPException(403, tier_err)
    if model_name not in mm:
        raise HTTPException(400, f"Unknown model: {model_name}. Available: {list(mm.keys())}")

    # Thinking mode: thinking=true OR reasoning_effort != "none"
    use_thinking=thinking or (reasoning_effort is not None and reasoning_effort != "none")
    if use_thinking and model_name in _THINKING_MAP:
        mode, model_pref=_THINKING_MAP[model_name]
        log.info(f"thinking on → {model_name} using {model_pref}")
    else:
        try:
            mode, model_pref=mm[model_name]
        except (ValueError, TypeError):
            raise HTTPException(500, f"Corrupted model entry for {model_name}. Fix via /admin/update-models")

    # Quota fallback: auto-downgrade when Pro quota exhausted
    if _rate_limit.get("remaining_pro") is not None and _rate_limit["remaining_pro"] <= 0 and model_name != "auto":
        log.warning(f"Pro quota exhausted (remaining_pro={_rate_limit['remaining_pro']}), falling back {model_name}→auto")
        mode, model_pref=mm.get("auto", ("pro", "pplx_pro"))
        model_name="auto"

    # Build query — extract system, history, and current user message separately
    system_msg=""
    history=[]
    for msg in messages:
        role=msg.get("role", "user")
        if role=="developer": role="system"
        # Detect user messages that are actually system prompts (LobeHub sends
        # Jamie's custom system prompt as role:user after the developer message)
        if role=="user":
            _ct=(msg.get("content") or "")[:200].lower()
            if any(kw in _ct for kw in ["you are ", "you must ", "your role", "ccsearch", "加載", "技能", "available_skills", "<skill", "<user_memory", "<available_tools", "<tool_selection", "<credentials", "<best_practices", "<memory_effort", "<session_context"]):
                role="system"
        content=msg.get("content") or ""
        if isinstance(content, list):
            text_parts=[ct.get("text", "") for ct in content if ct.get("type") == "text"]
            content=" ".join(text_parts)
        # Strip rate limit notices from previous responses
        content=_REMAINING_NOTICE_RE.sub("", content).strip()
        if not content or not content.strip():
            continue
        if role == "system":
            system_msg+=content+"\n"
        elif role == "user":
            history.append(("user", content))
        elif role == "assistant":
            # Keep enough context per assistant message
            history.append(("assistant", content))

    # Deduplicate consecutive assistant messages (LibreChat branch artifacts)
    deduped=[]
    for role, content in history:
        if deduped and role == "assistant" and deduped[-1][0] == "assistant":
            deduped[-1]=(role, content)  # replace with latest
        else:
            deduped.append((role, content))
    history=deduped

    # Keep only last 16 items (~8 turns) to prevent context overflow

    # Separate current user message from history
    current_msg=""
    if history and history[-1][0] == "user":
        current_msg=history[-1][1]
        history=history[:-1]

    request_source=_detect_request_source(system_msg, messages)
    is_lobehub=request_source == "lobehub"
    is_first_user_turn=is_lobehub and not history

    # Session continuity: check if we can skip history/instructions
    follow_up_uuid=_session_lookup(history)
    if follow_up_uuid:
        query=current_msg
        final_instructions=[]
        log.info(f"SESSION CONTINUE [chat_completions] source={request_source} follow_up={follow_up_uuid[:12]}...")
    else:
        custom_prompts=_load_custom_prompts() if is_lobehub else ""
        final_instructions=[]
        if is_lobehub:
            if custom_prompts:
                final_instructions.append(custom_prompts)
        else:
            final_instructions=_filter_system_prompt(system_msg) if system_msg else []

        # Build query as JSON for clear block separation
        query_obj={}
        if final_instructions:
            query_obj["instructions"]=final_instructions
        if history:
            query_obj["history"]=[{"role": r, "content": ct} for r, ct in history]
        if current_msg:
            query_obj["query"]=current_msg
        elif not history:
            query_obj["query"]=""

        query=json.dumps(query_obj, ensure_ascii=False)
        if len(query) > 96000:
            query=query[-96000:]

    _log_prompt_payload("chat_completions", request_source, system_msg, final_instructions, history, current_msg, query, is_first_user_turn, not bool(follow_up_uuid) and bool(final_instructions))

    if not query.strip():
        raise HTTPException(400, "No valid message content after processing. Ensure at least one user message has non-empty content.")

    client=get_client()
    cid=f"chatcmpl-{uuid4().hex[:12]}"
    created=int(time.time())

    if stream:
        return StreamingResponse(
            _stream_openai(client, query, mode, model_pref, model_name, cid, created, sources, language, follow_up_uuid, history, current_msg),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    full=""
    resp_backend_uuid=None
    thinking_parts=[]
    async for chunk in client.search(query, mode, model_pref, sources, language, follow_up_uuid):
        if chunk.get("backend_uuid"):
            resp_backend_uuid=chunk["backend_uuid"]
        if chunk.get("error"):
            raise HTTPException(502, chunk)
        if chunk.get("thinking"):
            thinking_parts.append(chunk["thinking"])
            continue
        if chunk.get("done"):
            full=chunk.get("answer", full)
            break
        full=chunk.get("answer", full)
    reasoning_content="\n".join(thinking_parts) if thinking_parts else None
    full=_clean_response(full)

    # Store session for next turn
    _session_store(history, current_msg, full, resp_backend_uuid)

    # Rate limit: decrement + append notice
    if mode != "auto":  # Pro queries only (copilot mode)
        _decrement_pro()
    notice=_remaining_notice()
    if notice:
        full+=notice

    msg={"role": "assistant", "content": full}
    if reasoning_content:
        msg["reasoning_content"]=reasoning_content
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model_name,
        "system_fingerprint": None,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": len(query)//4, "completion_tokens": len(full)//4, "total_tokens": len(query)//4+len(full)//4},
    }


async def _stream_openai(client, query, mode, model_pref, model_name, cid, created, sources, language, follow_up_uuid=None, history=None, current_msg=None):
    init={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
          "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None, "logprobs": None}]}
    yield f"data: {json.dumps(init)}\n\n"

    _resp_backend_uuid=None
    _full_answer=""
    async for chunk in client.search(query, mode, model_pref, sources, language, follow_up_uuid):
        if chunk.get("backend_uuid"):
            _resp_backend_uuid=chunk["backend_uuid"]
        if chunk.get("answer"):
            _full_answer=chunk["answer"]
        # Stream thinking content as reasoning_content deltas
        if chunk.get("thinking"):
            t={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
               "choices": [{"index": 0, "delta": {"reasoning_content": chunk["thinking"]+"\n"}, "finish_reason": None, "logprobs": None}]}
            yield f"data: {json.dumps(t)}\n\n"
            continue

        if chunk.get("error"):
            e={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
               "choices": [{"index": 0, "delta": {"content": f"[Error: {chunk['error']}]"}, "finish_reason": None, "logprobs": None}]}
            yield f"data: {json.dumps(e)}\n\n"
            stop={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop", "logprobs": None}]}
            yield f"data: {json.dumps(stop)}\n\n"
            break

        dt=chunk.get("delta", "")
        if dt:
            dt=_clean_response(dt, strip=False)
            if dt:
                d={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
                   "choices": [{"index": 0, "delta": {"content": dt}, "finish_reason": None, "logprobs": None}]}
                yield f"data: {json.dumps(d)}\n\n"

        if chunk.get("done"):
            wr=chunk.get("web_results", [])
            if wr:
                cites="\n\n---\nSources:\n"
                for i, w in enumerate(wr[:10]):
                    url=w.get("url", w) if isinstance(w, dict) else str(w)
                    cites+=f"[{i+1}] {url}\n"
                c={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
                   "choices": [{"index": 0, "delta": {"content": cites}, "finish_reason": None, "logprobs": None}]}
                yield f"data: {json.dumps(c)}\n\n"

            # Rate limit decrement + notice
            _decrement_pro()
            notice=_remaining_notice()
            if notice:
                nd={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
                    "choices": [{"index": 0, "delta": {"content": notice}, "finish_reason": None, "logprobs": None}]}
                yield f"data: {json.dumps(nd)}\n\n"
            stop={"id": cid, "object": "chat.completion.chunk", "created": created, "model": model_name, "system_fingerprint": None,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop", "logprobs": None}]}
            yield f"data: {json.dumps(stop)}\n\n"
            break

    # Store session for next turn
    if history is not None and current_msg:
        _clean_full=_clean_response(_full_answer)
        _session_store(history, current_msg, _clean_full, _resp_backend_uuid)

    yield "data: [DONE]\n\n"


# ─── Cookie Refresh Endpoint ──────────────────────────────────────────────


# ─── Model Discovery ──────────────────────────────────────────────────────

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

    base_models=dict(mm)

    report={"alive": [], "upgraded": {}, "dead": [], "probed": 0}

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

                    # Thinking variants auto-derived from _THINKING_MAP

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
    # Configure MCP transport security — allow external domain
    from urllib.parse import urlparse as _urlparse
    _pub_host=_urlparse(PUBLIC_URL).hostname or ""
    _mcp_security=None
    if _pub_host and _pub_host not in ("localhost", "127.0.0.1"):
        from mcp.server.transport_security import TransportSecuritySettings
        _mcp_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", f"{_pub_host}:*", _pub_host],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", f"https://{_pub_host}:*", f"https://{_pub_host}"],
        )
        log.info(f"MCP allowed hosts: localhost + {_pub_host}")
    mcp=FastMCP("pplx-proxy", instructions="Perplexity Pro Search reverse proxy.", transport_security=_mcp_security)

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
        async for c in client.search(query, "concise", "pplx_pro", ["web"], language):
            if c.get("error"): return f"Error: {c['error']}"
            if c.get("done"): r=c.get("answer", r); break
            r=c.get("answer", r)
        return r

    @mcp.tool()
    async def perplexity_reason(query: str, model: str="default", language: str="en-US") -> str:
        """Reasoning: Step-by-step reasoning through complex problems.
        Model: default (gpt thinking), gpt, sonnet, opus, gemini, nemotron, claude (alias for sonnet)."""
        if not query or not query.strip():
            return "Error: query cannot be empty"
        mm=get_model_map()
        # Map shorthand to base model, then look up thinking variant
        shorthand={"claude": "sonnet", "default": "gpt"}
        base=shorthand.get(model, model)
        tier_err=check_tier(base)
        if tier_err:
            return f"Error: {tier_err}"
        if base not in mm:
            avail=["default","gpt","sonnet","opus","gemini","nemotron","claude"]
            return f"Error: Unknown reasoning model '{model}'. Available: {avail}"
        # Prefer thinking variant if available
        if base in _THINKING_MAP:
            mode, pref=_THINKING_MAP[base]
        else:
            mode, pref=mm[base]
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
            asyncio.create_task(_rate_limit_poll_loop())
            # Fetch rate limits on startup (delayed 3s, non-blocking)
            async def _rl_startup():
                await asyncio.sleep(3)
                await asyncio.get_event_loop().run_in_executor(None, _fetch_rate_limit_sync)
            asyncio.create_task(_rl_startup())
            log.info(f"pplx-proxy started on port {PORT}")
            yield
        log.info("MCP streamable HTTP lifespan stopped")

    app.router.lifespan_context=_combined_lifespan
    # MCP Auth: API key in URL path
    # With key: /{API_KEY}/mcp and /{API_KEY}/sse
    # Without:  /mcp/mcp and /sse/sse (backward compat)
    if API_KEY:
        _mcp_prefix=f"/{API_KEY}"
        _mcp_pfx_len=len(_mcp_prefix)

        class _MCPAuthMiddleware:
            """Intercepts /{KEY}/mcp|sse, validates key, calls MCP apps directly."""
            def __init__(self, asgi_app):
                self.app=asgi_app
            async def __call__(self, scope, receive, send):
                if scope["type"] in ("http", "websocket"):
                    path=scope.get("path", "")
                    # Authenticated MCP paths — route directly to MCP apps
                    if path.startswith(_mcp_prefix + "/mcp"):
                        s=dict(scope)
                        s["path"]=path[_mcp_pfx_len:]
                        if s.get("raw_path"):
                            s["raw_path"]=s["raw_path"][_mcp_pfx_len:] if isinstance(s["raw_path"], bytes) else s["raw_path"]
                        await mcp_http_app(s, receive, send)
                        return
                    if path.startswith(_mcp_prefix + "/sse") or path.startswith(_mcp_prefix + "/messages"):
                        s=dict(scope)
                        s["path"]=path[_mcp_pfx_len:]
                        if s.get("raw_path"):
                            s["raw_path"]=s["raw_path"][_mcp_pfx_len:] if isinstance(s["raw_path"], bytes) else s["raw_path"]
                        await mcp_sse_app(s, receive, send)
                        return
                    # Allow /messages for SSE transport (session_id is the auth)
                    if path.startswith("/messages"):
                        s=dict(scope)
                        await mcp_sse_app(s, receive, send)
                        return
                    # Block bare /mcp and /sse without key
                    if path.startswith("/mcp") or path.startswith("/sse"):
                        from starlette.responses import JSONResponse as _JR
                        await _JR({"error": {"message": "MCP requires authentication. Use /<api-key>/mcp or /<api-key>/sse", "type": "auth_error"}}, status_code=401)(scope, receive, send)
                        return
                await self.app(scope, receive, send)

        app.add_middleware(_MCPAuthMiddleware)
        log.info(f"MCP mounted with key auth: /{API_KEY[:8]}***/mcp + /{API_KEY[:8]}***/sse")
    else:
        app.mount("/mcp", mcp_http_app)
        app.mount("/sse", mcp_sse_app)
        log.info("MCP mounted at /mcp/mcp + /sse/sse [NO AUTH]")
        log.warning("MCP has NO authentication! Set PPLX_PROXY_API_KEY to secure it.")


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
    cookies={"__Secure-next-auth.session-token": token}
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
                            # Thinking variants auto-derived from _THINKING_MAP, no separate upgrade needed
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
