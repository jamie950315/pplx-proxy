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
import copy
import threading
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
DATA_DIR=Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_FILE=DATA_DIR / ".cookie_cache.json"
MODELS_FILE=DATA_DIR / ".models.json"
RESPONSES_FILE=DATA_DIR / ".responses_store.json"
DEFAULT_MODEL=os.getenv("DEFAULT_MODEL", "gpt")
ACCOUNT_TYPE=os.getenv("ACCOUNT_TYPE", "pro").lower()  # free, pro, max
PUBLIC_URL=os.getenv("PUBLIC_URL", "http://localhost:8892")
PPLX_API_VERSION=os.getenv("PPLX_API_VERSION", "2.18")
PPLX_IMPERSONATE=os.getenv("PPLX_IMPERSONATE", "chrome")
COOKIE_MAX_AGE_HOURS=int(os.getenv("COOKIE_MAX_AGE_HOURS", "168"))
NTFY_COOLDOWN_SECS=int(os.getenv("NTFY_COOLDOWN_SECS", "3600"))
MODEL_PREFLIGHT_TTL_SECS=int(os.getenv("MODEL_PREFLIGHT_TTL_SECS", "900"))
USER_AGENT=os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ENV_FILE=Path(__file__).parent / ".env"
FLARESOLVERR_URL=os.getenv("FLARESOLVERR_URL", "http://localhost:8191").rstrip("/")

_MODEL_DISPLAY_ALIASES={
    "pplx_pro": {"pplx_pro", "turbo"},
}
_model_preflight_cache={}
_configured_session_state={"status": "unchecked", "source": None, "message": None}

def _model_matches_preference(requested_model: str, actual_model: str) -> bool:
    """Return whether Perplexity used the requested model or its known alias."""
    if not actual_model:
        return False
    if actual_model == requested_model:
        return True
    if actual_model in _MODEL_DISPLAY_ALIASES.get(requested_model, set()):
        return True
    if requested_model.endswith("thinking") and actual_model == requested_model.removesuffix("thinking"):
        return True
    return False

# ─── Rate Limit Tracker ────────────────────────────────────────────────────

_rate_limit={"remaining_pro": None, "remaining_research": None, "updated_at": 0, "last_error": None}
_rate_limit_lock=None  # initialized in startup
_rate_limit_refresh_task=None

def _fetch_rate_limit_sync():
    """Fetch rate limits from Perplexity via FlareSolverr. ~10s per call."""
    import urllib.request
    try:
        cookies=load_cookies()
        token=cookies.get("__Secure-next-auth.session-token", "")
        if not token:
            return None
        req=urllib.request.Request(f"{FLARESOLVERR_URL}/v1",
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
        _rate_limit["last_error"]=None
        log.info(f"Rate limit synced: pro={_rate_limit['remaining_pro']}, research={_rate_limit['remaining_research']}")
        return d
    except Exception as e:
        _rate_limit["last_error"]=str(e)
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

async def _refresh_rate_limit(block: bool=False):
    """Refresh rate limits with in-flight dedupe."""
    global _rate_limit_refresh_task
    if _rate_limit_refresh_task and not _rate_limit_refresh_task.done():
        if block:
            await _rate_limit_refresh_task
        return

    async def _run():
        loop=asyncio.get_event_loop()
        await loop.run_in_executor(None, _fetch_rate_limit_sync)

    _rate_limit_refresh_task=asyncio.create_task(_run())
    if block:
        await _rate_limit_refresh_task

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

# Default model registry — overridden by .models.json if it exists.
# IDs are stable proxy-facing names. Values use Perplexity web model_preference IDs.
_MODEL_REGISTRY={
    "auto": {"entry": ("pro", "pplx_pro"), "tier": "free", "label": "Perplexity Best"},
    "sonar": {"entry": ("pro", "experimental"), "tier": "pro", "label": "Sonar 2"},
    "gpt": {"entry": ("pro", "gpt56_terra"), "tier": "pro", "label": "GPT-5.6 Terra", "thinking": ("pro", "gpt56_terra_thinking")},
    "gpt-5.6-terra": {"entry": ("pro", "gpt56_terra"), "tier": "pro", "label": "GPT-5.6 Terra", "thinking": ("pro", "gpt56_terra_thinking")},
    "gpt-5.6-sol": {"entry": ("pro", "gpt56_sol"), "tier": "max", "label": "GPT-5.6 Sol", "thinking": ("pro", "gpt56_sol_thinking")},
    "gpt-5.5": {"entry": ("pro", "gpt55"), "tier": "pro", "label": "GPT-5.5", "thinking": ("pro", "gpt55_thinking")},
    "gpt-mini": {"entry": ("pro", "gpt5_mini"), "tier": "pro", "label": "GPT-5 Mini"},
    "gpt-nano": {"entry": ("pro", "gpt5_nano"), "tier": "pro", "label": "GPT-5 Nano"},
    "gemini": {"entry": ("pro", "gemini31pro_high"), "tier": "pro", "label": "Gemini 3.1 Pro"},
    "gemini-flash": {"entry": ("pro", "gemini35flash"), "tier": "pro", "label": "Gemini 3.5 Flash"},
    "gemini-flash-lite": {"entry": ("pro", "gemini31flashlite"), "tier": "pro", "label": "Gemini 3.1 Flash Lite", "enabled": False},
    "sonnet": {"entry": ("pro", "claude50sonnet"), "tier": "pro", "label": "Claude Sonnet 5", "thinking": ("pro", "claude50sonnetthinking")},
    "sonnet-5": {"entry": ("pro", "claude50sonnet"), "tier": "pro", "label": "Claude Sonnet 5", "thinking": ("pro", "claude50sonnetthinking")},
    "sonnet-4.6": {"entry": ("pro", "claude46sonnet"), "tier": "pro", "label": "Claude Sonnet 4.6", "thinking": ("pro", "claude46sonnetthinking")},
    "haiku": {"entry": ("pro", "claude45haiku"), "tier": "pro", "label": "Claude Haiku 4.5", "enabled": False},
    "opus": {"entry": ("pro", "claude48opus"), "tier": "max", "label": "Claude Opus 4.8", "thinking": ("pro", "claude48opusthinking")},
    "opus-4.8": {"entry": ("pro", "claude48opus"), "tier": "max", "label": "Claude Opus 4.8", "thinking": ("pro", "claude48opusthinking")},
    "opus-4.7": {"entry": ("pro", "claude47opus"), "tier": "max", "label": "Claude Opus 4.7", "thinking": ("pro", "claude47opusthinking")},
    "opus-4.6": {"entry": ("pro", "claude46opus"), "tier": "max", "label": "Claude Opus 4.6", "thinking": ("pro", "claude46opusthinking")},
    "grok": {"entry": ("pro", "grok46low"), "tier": "pro", "label": "Grok 4.6"},
    "grok-4.6": {"entry": ("pro", "grok46low"), "tier": "pro", "label": "Grok 4.6"},
    "grok-4.5": {"entry": ("pro", "grok45low"), "tier": "pro", "label": "Grok 4.5", "thinking": ("pro", "grok45medium")},
    "grok-4": {"entry": ("pro", "grok4"), "tier": "pro", "label": "Grok 4"},
    "grok-reasoning": {"entry": ("pro", "grok420reasoning"), "tier": "pro", "label": "Grok 4.20 Reasoning"},
    "grok-non-reasoning": {"entry": ("pro", "grok420nonreasoning"), "tier": "pro", "label": "Grok 4.20 Non Reasoning"},
    "grok-multi": {"entry": ("pro", "grok420multiagent"), "tier": "max", "label": "Grok 4.20 Multi-Agent", "enabled": False},
    "nemotron": {"entry": ("pro", "nv_nemotron_3_ultra"), "tier": "pro", "label": "Nemotron 3 Ultra"},
    "nemotron-3-super": {"entry": ("pro", "nv_nemotron_3_super"), "tier": "pro", "label": "Nemotron 3 Super"},
    "glm-5.2": {"entry": ("pro", "glm_5_2"), "tier": "pro", "label": "GLM-5.2"},
    "kimi-k2.6": {"entry": ("pro", "kimik26instant"), "tier": "pro", "label": "Kimi K2.6", "thinking": ("pro", "kimik26thinking")},
    "kimi-k3": {"entry": ("pro", "kimik3"), "tier": "pro", "label": "Kimi K3"},
}

# All known models (superset)
_ALL_MODELS={k: v["entry"] for k, v in _MODEL_REGISTRY.items()}
_MODEL_LABELS={k: v["label"] for k, v in _MODEL_REGISTRY.items()}
_PREF_LABELS={}
for _spec in _MODEL_REGISTRY.values():
    _PREF_LABELS[_spec["entry"][1]]=_spec["label"]
    if "thinking" in _spec:
        _PREF_LABELS[_spec["thinking"][1]]=f"{_spec['label']} Thinking"
_ENABLED_MODEL_IDS={k for k, v in _MODEL_REGISTRY.items() if v.get("enabled", True)}

def _substitution_notice(requested_pref, actual_model) -> str:
    """Visible marker when Perplexity answered with a different model."""
    if not actual_model or _model_matches_preference(requested_pref, actual_model):
        return ""
    label=_PREF_LABELS.get(actual_model, actual_model)
    return f"\n\n[Substituted by Perplexity with {label}]"

def _response_suffix(requested_pref, actual_model) -> str:
    return _substitution_notice(requested_pref, actual_model)+_remaining_notice()

# Thinking variants — activated via thinking=true parameter
_THINKING_MAP={k: v["thinking"] for k, v in _MODEL_REGISTRY.items() if "thinking" in v}

# Model availability per account tier
_TIER_MODELS={
    "free": {"auto"},
    "pro": {k for k, v in _MODEL_REGISTRY.items() if k in _ENABLED_MODEL_IDS and v["tier"] in {"free", "pro"}},
    "max": set(_ENABLED_MODEL_IDS),
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
            if model_name not in _ENABLED_MODEL_IDS:
                return f"Model '{model_name}' is tracked as a candidate, but no working Perplexity web preference is verified yet"
            # Model exists but not in this tier
            needed=_MODEL_REGISTRY.get(model_name, {}).get("tier", "pro")
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

    def sync_cookies_from_session(self) -> bool:
        """Copy cookies accepted by the live session into the restart-safe cache."""
        if not self.session:
            return False
        live_cookies=_cookie_dict(getattr(self.session, "cookies", {}))
        if not live_cookies:
            return False
        merged=dict(self.cookies)
        merged.update(live_cookies)
        if not merged.get("__Secure-next-auth.session-token"):
            log.warning("Session cookie sync skipped: no secure session cookie")
            return False
        changed=merged != self.cookies
        self.cookies=merged
        return changed

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
        answer_usage=None
        actual_model=None
        substituted=False
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

            blocks=chunk.get("blocks", [])
            display_model=chunk.get("display_model")
            if isinstance(display_model, str) and display_model:
                if _model_matches_preference(model_pref, display_model):
                    if not substituted:
                        actual_model=display_model
                elif not substituted:
                    switch_answer_blocks=[
                        block for block in blocks
                        if block.get("intended_usage", "").startswith("ask_text")
                        and block.get("markdown_block")
                    ]
                    switch_answer_block=next(
                        (block for block in switch_answer_blocks if block.get("intended_usage") == answer_usage),
                        switch_answer_blocks[0] if switch_answer_blocks else None,
                    )
                    switch_chars=0
                    if switch_answer_block:
                        switch_mb=switch_answer_block["markdown_block"]
                        switch_text="".join(switch_mb.get("chunks", []))
                        if switch_mb.get("progress") == "DONE":
                            switch_chars=max(0, len(switch_text) - seen_len)
                        else:
                            switch_chars=len(switch_text)
                    selected_model=chunk.get("user_selected_model", "")
                    minor_auxiliary_tail=(
                        _model_matches_preference(model_pref, selected_model)
                        and seen_len > 0
                        and (
                            switch_chars == 0
                            or (switch_chars <= 16 and switch_chars / seen_len <= 0.05)
                        )
                    )
                    if minor_auxiliary_tail:
                        log.info(
                            f"Perplexity used auxiliary model '{display_model}' for a "
                            f"{switch_chars}-character tail after '{actual_model}' produced {seen_len} characters"
                        )
                    else:
                        message=f"Perplexity substituted requested model '{model_pref}' with '{display_model}'"
                        log.warning(message)
                        substituted=True
                        actual_model=display_model

            if "backend_uuid" in chunk:
                backend_uuid=chunk["backend_uuid"]
            if "web_results" in chunk:
                web_results=chunk["web_results"]

            # Extract thinking content from search/plan blocks
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

            # Perplexity can mirror the same answer through both ask_text and
            # ask_text_0_markdown. Select one stream so chunks are not duplicated.
            answer_blocks=[
                block for block in blocks
                if block.get("intended_usage", "").startswith("ask_text")
                and block.get("markdown_block")
            ]
            if not answer_blocks:
                continue
            answer_block=None
            if answer_usage:
                answer_block=next(
                    (block for block in answer_blocks if block.get("intended_usage") == answer_usage),
                    None,
                )
            if answer_block is None:
                answer_block=next(
                    (block for block in answer_blocks if block.get("intended_usage") == "ask_text"),
                    answer_blocks[0],
                )
                answer_usage=answer_block.get("intended_usage")

            mb=answer_block["markdown_block"]
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

        yield {
            "delta": "",
            "answer": full_answer,
            "backend_uuid": backend_uuid,
            "web_results": web_results,
            "requested_model": model_pref,
            "actual_model": actual_model,
            "model_fallback": substituted or not _model_matches_preference(model_pref, actual_model),
            "done": True,
        }


# ─── Cookie Management ──────────────────────────���──────────────────────────

def _load_cached_cookies() -> dict:
    """Load a fresh cookie cache without falling back to configuration."""
    if COOKIE_FILE.exists():
        try:
            data=json.loads(COOKIE_FILE.read_text())
            ts=data.get("timestamp", 0)
            age_h=(time.time() - ts) / 3600
            cookies=_cookie_dict(data.get("cookies", {}))
            if age_h < COOKIE_MAX_AGE_HOURS and cookies:
                log.info(f"Loaded cached cookies (age: {age_h:.1f}h)")
                return cookies
        except Exception as e:
            log.warning(f"Cookie cache read error: {e}")
    return {}


def _configured_cookies() -> dict:
    """Parse the cookie value configured in .env."""
    if not PPLX_COOKIE:
        return {}
    try:
        cookies=json.loads(PPLX_COOKIE)
        return _cookie_dict(cookies) if isinstance(cookies, dict) else {}
    except json.JSONDecodeError:
        return {"__Secure-next-auth.session-token": PPLX_COOKIE}


def load_cookies() -> dict:
    """Load cookies from cache file, .env, or return empty."""
    cached=_load_cached_cookies()
    if cached:
        return cached

    return _configured_cookies()

def _cookie_dict(cookies) -> dict:
    """Return a plain string dictionary from a cookie mapping or cookie jar."""
    try:
        items=cookies.items()
    except AttributeError:
        return {}
    return {str(name): str(value) for name, value in items if name and value}

def save_cookies(cookies: dict, last_keepalive: float=None):
    """Atomically save cookies so a restart always reads a complete cache."""
    now=time.time()
    data={"cookies": _cookie_dict(cookies), "timestamp": now}
    if last_keepalive is not None:
        data["last_keepalive"]=last_keepalive
    tmp_file=COOKIE_FILE.with_name(f".{COOKIE_FILE.name}.{os.getpid()}.tmp")
    tmp_file.write_text(json.dumps(data, indent=2))
    tmp_file.replace(COOKIE_FILE)
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


async def _validate_session_cookies(cookies: dict) -> Optional[dict]:
    """Return validated, rotated cookies without changing the active session."""
    cookies=_cookie_dict(cookies)
    if not cookies.get("__Secure-next-auth.session-token"):
        return None
    session=cffi_requests.AsyncSession(
        headers=DEFAULT_HEADERS.copy(),
        cookies=cookies,
        impersonate=PPLX_IMPERSONATE,
    )
    try:
        resp=await session.get(PPLX_AUTH_SESSION)
        if resp.status_code != 200:
            return None
        data=resp.json() if hasattr(resp, "json") else {}
        if not isinstance(data, dict) or not data.get("user"):
            return None
        validated=dict(cookies)
        validated.update(_cookie_dict(getattr(session, "cookies", {})))
        return validated
    except Exception as e:
        log.warning(f"Configured session validation failed: {type(e).__name__}")
        return None
    finally:
        await session.close()


async def reconcile_configured_session() -> bool:
    """Adopt a changed .env session only after it validates successfully."""
    global _client
    configured=_configured_cookies()
    cached=_load_cached_cookies()
    configured_token=configured.get("__Secure-next-auth.session-token", "")
    cached_token=cached.get("__Secure-next-auth.session-token", "")

    if not configured_token:
        _configured_session_state.update({
            "status": "missing",
            "source": "cache" if cached_token else None,
            "message": "No session is configured in .env",
        })
        return bool(cached_token)

    if configured_token == cached_token:
        _configured_session_state.update({"status": "active", "source": "cache", "message": None})
        return True

    validated=await _validate_session_cookies(configured)
    if not validated:
        _configured_session_state.update({
            "status": "invalid",
            "source": "cache" if cached_token else None,
            "message": "Configured .env session is not authenticated",
        })
        log.warning("Configured .env session is invalid; retaining the existing cookie cache")
        return False

    save_cookies(validated, last_keepalive=time.time())
    if _client:
        _client.reset(validated)
    _model_preflight_cache.clear()
    _configured_session_state.update({"status": "active", "source": "env", "message": None})
    log.info("Validated and activated the changed .env session")
    return True


class OpenAIAPIError(Exception):
    def __init__(self, status_code: int, message: str, err_type: str="invalid_request_error", param: str=None, code: str=None):
        self.status_code=status_code
        self.message=message
        self.err_type=err_type
        self.param=param
        self.code=code
        super().__init__(message)


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

@app.exception_handler(OpenAIAPIError)
async def openai_api_error_handler(request: Request, exc: OpenAIAPIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": exc.message, "type": exc.err_type, "param": exc.param, "code": exc.code}},
    )


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
    # Populate the first rate-limit result before returning; later stale refreshes are backgrounded.
    if _rate_limit["remaining_pro"] is None:
        for attempt in range(2):
            await _refresh_rate_limit(block=True)
            if _rate_limit["remaining_pro"] is not None:
                break
            if attempt == 0:
                await asyncio.sleep(1)
    elif (time.time() - _rate_limit["updated_at"]) > 300:
        await _refresh_rate_limit(block=False)
    rl_age=int(time.time() - _rate_limit["updated_at"]) if _rate_limit["updated_at"] else None
    flaresolverr_status="ok" if _rate_limit.get("updated_at") else "unavailable" if _rate_limit.get("last_error") else "unknown"
    return {
        "status": "ok", "service": "pplx-proxy", "cookie_age_hours": cache_age,
        "configured_session": dict(_configured_session_state),
        "remaining_pro": _rate_limit.get("remaining_pro"),
        "remaining_research": _rate_limit.get("remaining_research"),
        "rate_limit_age_seconds": rl_age,
        "flaresolverr": {
            "status": flaresolverr_status,
            "url": FLARESOLVERR_URL,
            "last_error": _rate_limit.get("last_error"),
        },
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
_SUBSTITUTION_NOTICE_RE=_re.compile(r'\s*\[(?:Substituted by Perplexity with|被 Perplexity 替換成) [^\]]+\]\s*')

def _strip_appended_notices(text: str) -> str:
    text=_REMAINING_NOTICE_RE.sub("", text or "")
    text=_SUBSTITUTION_NOTICE_RE.sub("", text)
    return text.strip()

def _message_content_text(content) -> str:
    if isinstance(content, list):
        parts=[]
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            typ=item.get("type")
            if typ in ("input_text", "output_text", "text", "summary_text"):
                text=item.get("text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif typ == "refusal":
                text=item.get("refusal") or item.get("text") or ""
                if text:
                    parts.append(str(text))
            elif typ == "input_image":
                url=item.get("image_url")
                if isinstance(url, dict):
                    url=url.get("url")
                if isinstance(url, str) and url:
                    parts.append(f"[image: {url}]")
            elif typ == "input_file":
                name=item.get("filename") or item.get("file_id") or "file"
                parts.append(f"[file: {name}]")
        return " ".join(parts)
    if isinstance(content, dict):
        if content.get("type"):
            return _message_content_text([content])
        return str(content)
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)

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


# ─── Responses API store and helpers ───────────────────────────────────────

_RESPONSES_MAX_ENTRIES=300
_RESPONSES_MAX_AGE=86400 * 7
_SYSTEM_PROMPT_HINTS=("you are ", "you must ", "your role", "ccsearch", "加載", "技能", "available_skills", "<skill", "<user_memory", "<available_tools", "<tool_selection", "<credentials", "<best_practices", "<memory_effort", "<session_context")
_responses_store={}
_conversations_index={}
_responses_tasks={}
_responses_loaded=False
_responses_file_lock=threading.Lock()

def _responses_reset_memory():
    global _responses_loaded
    _responses_store.clear()
    _conversations_index.clear()
    _responses_tasks.clear()
    _responses_loaded=False

def _responses_load():
    global _responses_loaded, _responses_store, _conversations_index
    if _responses_loaded:
        return
    _responses_loaded=True
    try:
        if not RESPONSES_FILE.exists():
            return
        data=json.loads(RESPONSES_FILE.read_text())
        recs=data.get("responses") if isinstance(data, dict) else data
        convs=data.get("conversations") if isinstance(data, dict) else {}
        now=time.time()
        restored={}
        for rid, rec in (recs or {}).items():
            if not isinstance(rec, dict):
                continue
            created=rec.get("created_at") or 0
            if now - created > _RESPONSES_MAX_AGE:
                continue
            if rec.get("status") == "in_progress":
                rec["status"]="failed"
                rec["error"]={"code": "server_error", "message": "Response interrupted by server restart"}
            restored[rid]=rec
        _responses_store=restored
        _conversations_index={k: v for k, v in (convs or {}).items() if v in restored}
    except Exception as e:
        log.warning(f"Failed to load responses store: {e}")

def _responses_persist():
    try:
        RESPONSES_FILE.parent.mkdir(parents=True, exist_ok=True)
        durable={k: v for k, v in _responses_store.items() if v.get("store", True)}
        payload={"responses": durable, "conversations": {k: v for k, v in _conversations_index.items() if v in durable}}
        tmp=RESPONSES_FILE.with_name(f".{RESPONSES_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False))
        tmp.replace(RESPONSES_FILE)
    except Exception as e:
        log.warning(f"Failed to persist responses store: {e}")

def _responses_evict():
    now=time.time()
    expired=[rid for rid, rec in _responses_store.items() if now - (rec.get("created_at") or 0) > _RESPONSES_MAX_AGE]
    for rid in expired:
        _responses_store.pop(rid, None)
    if len(_responses_store) <= _RESPONSES_MAX_ENTRIES:
        return
    extra=len(_responses_store) - _RESPONSES_MAX_ENTRIES
    oldest=sorted(_responses_store.items(), key=lambda kv: kv[1].get("created_at") or 0)[:extra]
    for rid, _rec in oldest:
        _responses_store.pop(rid, None)

def _responses_put(rec: dict, persist: bool=True):
    _responses_load()
    _responses_store[rec["id"]]=rec
    conv=rec.get("conversation")
    conv_id=conv.get("id") if isinstance(conv, dict) else conv
    if conv_id:
        _conversations_index[conv_id]=rec["id"]
    _responses_evict()
    if persist and rec.get("store", True):
        with _responses_file_lock:
            _responses_persist()

def _responses_get(response_id: str):
    _responses_load()
    return _responses_store.get(response_id)

def _responses_delete(response_id: str) -> bool:
    _responses_load()
    rec=_responses_store.pop(response_id, None)
    if not rec:
        return False
    conv=rec.get("conversation")
    conv_id=conv.get("id") if isinstance(conv, dict) else conv
    if conv_id and _conversations_index.get(conv_id) == response_id:
        _conversations_index.pop(conv_id, None)
    with _responses_file_lock:
        _responses_persist()
    return True

def _responses_public(rec: dict) -> dict:
    return {k: copy.deepcopy(v) for k, v in rec.items() if not str(k).startswith("_")}

def _estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)

def _responses_usage(query: str, output: str, reasoning: str="") -> dict:
    inp=_estimate_tokens(query)
    reason=_estimate_tokens(reasoning)
    out=_estimate_tokens(output)+reason
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp+out,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": reason},
    }

def _sse_pack(event: str, data: dict, seq: int) -> tuple[str, int]:
    payload=dict(data)
    payload.setdefault("type", event)
    payload.setdefault("sequence_number", seq)
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n", seq+1

def _responses_output_items(msg_id: str, text: str, reasoning_text: str="", rs_id: str=None) -> list:
    items=[]
    if reasoning_text:
        items.append({
            "id": rs_id or f"rs_{uuid4().hex[:12]}",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reasoning_text}],
        })
    items.append({
        "id": msg_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    })
    return items

def _responses_normalize_input_items(inp) -> list:
    if inp is None or inp == "":
        return []
    if isinstance(inp, str):
        return [{
            "id": f"msg_{uuid4().hex[:8]}",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": inp}],
        }]
    if not isinstance(inp, list):
        return []
    items=[]
    for item in inp:
        if isinstance(item, str):
            items.append({
                "id": f"msg_{uuid4().hex[:8]}",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": item}],
            })
            continue
        if not isinstance(item, dict):
            continue
        cloned=dict(item)
        cloned.setdefault("id", f"msg_{uuid4().hex[:8]}")
        if "type" not in cloned:
            cloned["type"]="message"
            cloned.setdefault("role", "user")
            if isinstance(cloned.get("content"), str):
                cloned["content"]=[{"type": "input_text", "text": cloned["content"]}]
        items.append(cloned)
    return items

def _responses_parse_input(inp, instructions="") -> list:
    messages=[]
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, str) and item.strip():
                messages.append({"role": "system", "content": item})
            elif isinstance(item, dict):
                content=_message_content_text(item.get("content", item.get("text", "")))
                if content:
                    messages.append({"role": "system", "content": content})
    if isinstance(inp, str):
        if inp.strip():
            messages.append({"role": "user", "content": inp})
        return messages
    if inp is None:
        return messages
    if not isinstance(inp, list):
        raise OpenAIAPIError(400, "input must be a string or an array of items", param="input")
    for i, item in enumerate(inp):
        if isinstance(item, str):
            if item.strip():
                messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            raise OpenAIAPIError(400, f"input[{i}] must be a string or object", param="input")
        typ=item.get("type")
        if typ in (None, "message", "input_message"):
            role=item.get("role", "user")
            if role == "developer":
                role="system"
            content=_message_content_text(item.get("content", ""))
            if content:
                messages.append({"role": role, "content": content})
        elif typ == "function_call_output":
            output=item.get("output", "")
            call_id=item.get("call_id", "")
            messages.append({"role": "user", "content": f"[tool output {call_id}]: {output}"})
        elif typ in ("function_call", "reasoning", "item_reference", "web_search_call", "file_search_call", "computer_call", "mcp_call", "image_generation_call"):
            continue
        else:
            role=item.get("role")
            content=_message_content_text(item.get("content", item.get("text", item.get("output", ""))))
            if role and content:
                if role == "developer":
                    role="system"
                messages.append({"role": role, "content": content})
    return messages

def _responses_apply_previous(messages: list, previous_response_id, conversation):
    prev_id=previous_response_id
    conv_id=None
    if conversation and prev_id:
        raise OpenAIAPIError(400, "previous_response_id cannot be used together with conversation", param="previous_response_id")
    if isinstance(conversation, dict):
        conv_id=conversation.get("id")
    elif isinstance(conversation, str) and conversation:
        conv_id=conversation
    if conv_id and not prev_id:
        _responses_load()
        prev_id=_conversations_index.get(conv_id)
        if not prev_id:
            return messages, None, conv_id
    if not prev_id:
        return messages, None, conv_id
    rec=_responses_get(prev_id)
    if not rec:
        raise OpenAIAPIError(404, f"No model response found with id '{prev_id}'.", param="previous_response_id")
    prior=[{"role": r, "content": c} for r, c in rec.get("_history_messages") or []]
    system_msgs=[m for m in messages if m.get("role") == "system"]
    other=[m for m in messages if m.get("role") != "system"]
    return system_msgs+prior+other, prev_id, conv_id

def _prepare_pplx_from_messages(messages: list, log_source: str, extra_instructions: list=None):
    system_msg=""
    history=[]
    for msg in messages:
        role=msg.get("role", "user")
        if role == "developer":
            role="system"
        content=_message_content_text(msg.get("content"))
        if role == "user":
            _ct=content[:200].lower()
            if any(kw in _ct for kw in _SYSTEM_PROMPT_HINTS):
                role="system"
        content=_strip_appended_notices(content)
        if not content or not content.strip():
            continue
        if role == "system":
            system_msg+=content+"\n"
        elif role == "tool":
            history.append(("user", content))
        elif role in ("user", "assistant"):
            history.append((role, content))
    deduped=[]
    for role, content in history:
        if deduped and role == "assistant" and deduped[-1][0] == "assistant":
            deduped[-1]=(role, content)
        else:
            deduped.append((role, content))
    history=deduped
    current_msg=""
    if history and history[-1][0] == "user":
        current_msg=history[-1][1]
        history=history[:-1]
    request_source=_detect_request_source(system_msg, messages)
    is_lobehub=request_source == "lobehub"
    is_first_user_turn=is_lobehub and not history
    follow_up_uuid=_session_lookup(history)
    extra_instructions=list(extra_instructions or [])
    if follow_up_uuid:
        query=current_msg
        final_instructions=extra_instructions
        if extra_instructions:
            query=("\n".join(extra_instructions)+"\n"+current_msg).strip()
        log.info(f"SESSION CONTINUE [{log_source}] source={request_source} follow_up={follow_up_uuid[:12]}...")
    else:
        custom_prompts=_load_custom_prompts() if is_lobehub else ""
        final_instructions=[]
        if is_lobehub:
            if custom_prompts:
                final_instructions.append(custom_prompts)
        else:
            final_instructions=_filter_system_prompt(system_msg) if system_msg else []
        final_instructions.extend(extra_instructions)
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
    _log_prompt_payload(log_source, request_source, system_msg, final_instructions, history, current_msg, query, is_first_user_turn, not bool(follow_up_uuid) and bool(final_instructions))
    return {
        "system_msg": system_msg,
        "history": history,
        "current_msg": current_msg,
        "request_source": request_source,
        "is_first_user_turn": is_first_user_turn,
        "follow_up_uuid": follow_up_uuid,
        "final_instructions": final_instructions,
        "query": query,
    }

def _responses_resolve_model(model_name: str, use_thinking: bool):
    mm=get_model_map()
    tier_err=check_tier(model_name)
    if tier_err:
        raise OpenAIAPIError(403, tier_err, param="model")
    if model_name not in mm:
        raise OpenAIAPIError(400, f"Unknown model: {model_name}. Available: {list(mm.keys())}", param="model")
    if use_thinking and model_name in _THINKING_MAP:
        mode, model_pref=_THINKING_MAP[model_name]
        log.info(f"thinking on → {model_name} using {model_pref}")
    else:
        try:
            mode, model_pref=mm[model_name]
        except (ValueError, TypeError):
            raise OpenAIAPIError(500, f"Corrupted model entry for {model_name}", err_type="server_error", param="model")
    if _rate_limit.get("remaining_pro") is not None and _rate_limit["remaining_pro"] <= 0 and model_name != "auto":
        log.warning(f"Pro quota exhausted (remaining_pro={_rate_limit['remaining_pro']}), falling back {model_name}→auto")
        mode, model_pref=mm.get("auto", ("pro", "pplx_pro"))
        model_name="auto"
    return model_name, mode, model_pref

def _responses_new_record(model_name, instructions, max_output_tokens, previous_response_id, reasoning, store_flag, temperature, text_cfg, tool_choice, tools_raw, top_p, truncation, metadata, user, background, conv_id, input_items, query, parallel_tool_calls):
    resp_id=f"resp_{uuid4().hex}"
    msg_id=f"msg_{uuid4().hex[:12]}"
    rs_id=f"rs_{uuid4().hex[:12]}"
    created=int(time.time())
    if isinstance(instructions, list):
        instructions_value=json.dumps(instructions, ensure_ascii=False)
    elif isinstance(instructions, str) and instructions:
        instructions_value=instructions
    else:
        instructions_value=None
    return {
        "id": resp_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "instructions": instructions_value,
        "max_output_tokens": max_output_tokens,
        "model": model_name,
        "output": [],
        "parallel_tool_calls": parallel_tool_calls,
        "previous_response_id": previous_response_id,
        "reasoning": reasoning if isinstance(reasoning, dict) else None,
        "store": store_flag,
        "temperature": temperature,
        "text": text_cfg if text_cfg else {"format": {"type": "text"}},
        "tool_choice": tool_choice if tool_choice is not None else "auto",
        "tools": tools_raw if isinstance(tools_raw, list) else [],
        "top_p": top_p,
        "truncation": truncation or "disabled",
        "usage": None,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "output_text": "",
        "user": user,
        "background": bool(background),
        "conversation": {"id": conv_id} if conv_id else None,
        "_input_items": input_items,
        "_history_messages": [],
        "_backend_uuid": None,
        "_query": query,
        "_msg_id": msg_id,
        "_rs_id": rs_id,
    }

def _responses_finalize(rec, query, full, thinking_parts, backend_uuid, actual_model, history, current_msg, mode, model_pref):
    full=_clean_response(full)
    _session_store(history, current_msg, full, backend_uuid)
    if mode != "auto":
        _decrement_pro()
    notice=_response_suffix(model_pref, actual_model)
    if notice:
        full+=notice
    reasoning_text="\n".join(thinking_parts) if thinking_parts else ""
    rec["output"]=_responses_output_items(rec["_msg_id"], full, reasoning_text, rec.get("_rs_id"))
    rec["output_text"]=full
    rec["status"]="completed"
    rec["error"]=None
    rec["usage"]=_responses_usage(query, _strip_appended_notices(full), reasoning_text)
    rec["_backend_uuid"]=backend_uuid
    new_hist=list(history)
    if current_msg:
        new_hist.append(("user", current_msg))
    new_hist.append(("assistant", _strip_appended_notices(full)))
    rec["_history_messages"]=new_hist
    rec["_query"]=query
    _responses_put(rec, persist=rec.get("store", True))
    return rec

async def _responses_collect_answer(client, query, mode, model_pref, follow_up_uuid):
    full=""
    thinking_parts=[]
    backend_uuid=None
    actual_model=None
    async for ch in client.search(query, mode, model_pref, ["web"], "en-US", follow_up_uuid):
        if ch.get("backend_uuid"):
            backend_uuid=ch["backend_uuid"]
        if ch.get("actual_model"):
            actual_model=ch["actual_model"]
        if ch.get("error"):
            return {"error": ch["error"], "full": full, "thinking_parts": thinking_parts, "backend_uuid": backend_uuid, "actual_model": actual_model}
        if ch.get("thinking"):
            thinking_parts.append(ch["thinking"])
            continue
        if ch.get("done"):
            full=ch.get("answer", full)
            actual_model=ch.get("actual_model", actual_model)
            break
        full=ch.get("answer", full)
    return {"error": None, "full": full, "thinking_parts": thinking_parts, "backend_uuid": backend_uuid, "actual_model": actual_model}

async def _responses_background_job(resp_id, query, mode, model_pref, follow_up_uuid, history, current_msg):
    try:
        client=get_client()
        result=await _responses_collect_answer(client, query, mode, model_pref, follow_up_uuid)
        rec=_responses_get(resp_id)
        if not rec or rec.get("status") == "cancelled":
            return
        if result["error"]:
            rec["status"]="failed"
            rec["error"]={"code": "server_error", "message": str(result["error"])}
            _responses_put(rec, persist=rec.get("store", True))
            return
        _responses_finalize(rec, query, result["full"], result["thinking_parts"], result["backend_uuid"], result["actual_model"], history, current_msg, mode, model_pref)
    except asyncio.CancelledError:
        rec=_responses_get(resp_id)
        if rec and rec.get("status") == "in_progress":
            rec["status"]="cancelled"
            rec["incomplete_details"]={"reason": "cancelled"}
            _responses_put(rec, persist=rec.get("store", True))
        raise
    except Exception as e:
        log.exception("background response failed")
        rec=_responses_get(resp_id)
        if rec:
            rec["status"]="failed"
            rec["error"]={"code": "server_error", "message": str(e)}
            _responses_put(rec, persist=rec.get("store", True))
    finally:
        _responses_tasks.pop(resp_id, None)

async def _stream_responses_api(client, rec, query, mode, model_pref, follow_up_uuid, history, current_msg):
    seq=0
    msg_id=rec["_msg_id"]
    rs_id=rec["_rs_id"]
    def _wrap(event, rec_obj):
        public=_responses_public(rec_obj)
        payload=dict(public)
        payload["response"]=public
        return _sse_pack(event, payload, seq)
    chunk, seq=_wrap("response.created", rec)
    yield chunk
    chunk, seq=_wrap("response.in_progress", rec)
    yield chunk

    full=""
    backend_uuid=None
    thinking_parts=[]
    thinking_started=False
    thinking_closed=False
    message_started=False
    actual_model=None
    msg_output_index=0

    async for ch in client.search(query, mode, model_pref, ["web"], "en-US", follow_up_uuid):
        if ch.get("backend_uuid"):
            backend_uuid=ch["backend_uuid"]
        if ch.get("actual_model"):
            actual_model=ch["actual_model"]
        if ch.get("error"):
            rec["status"]="failed"
            rec["error"]={"code": "server_error", "message": str(ch["error"])}
            _responses_put(rec, persist=rec.get("store", True))
            chunk, seq=_wrap("response.failed", rec)
            yield chunk
            chunk, seq=_sse_pack("error", {"error": ch["error"]}, seq)
            yield chunk
            return
        if ch.get("thinking"):
            t=ch["thinking"]
            thinking_parts.append(t)
            if not thinking_started:
                thinking_started=True
                item={"id": rs_id, "type": "reasoning", "summary": []}
                chunk, seq=_sse_pack("response.output_item.added", {"output_index": 0, "item": item}, seq)
                yield chunk
                chunk, seq=_sse_pack("response.reasoning_summary_part.added", {"item_id": rs_id, "output_index": 0, "summary_index": 0, "part": {"type": "summary_text", "text": ""}}, seq)
                yield chunk
            evt={"item_id": rs_id, "output_index": 0, "summary_index": 0, "delta": t+"\n"}
            chunk, seq=_sse_pack("response.reasoning_summary_text.delta", evt, seq)
            yield chunk
            continue
        if ch.get("done"):
            full=ch.get("answer", full)
            actual_model=ch.get("actual_model", actual_model)
            break
        delta=ch.get("delta", "")
        if delta:
            delta=_clean_response(delta, strip=False)
        if not delta:
            if ch.get("answer"):
                full=ch.get("answer", full)
            continue
        if thinking_started and not thinking_closed:
            thinking_closed=True
            think_full="\n".join(thinking_parts)
            chunk, seq=_sse_pack("response.reasoning_summary_text.done", {"item_id": rs_id, "output_index": 0, "summary_index": 0, "text": think_full}, seq)
            yield chunk
            chunk, seq=_sse_pack("response.reasoning_summary_part.done", {"item_id": rs_id, "output_index": 0, "summary_index": 0, "part": {"type": "summary_text", "text": think_full}}, seq)
            yield chunk
            chunk, seq=_sse_pack("response.output_item.done", {"output_index": 0, "item": {"id": rs_id, "type": "reasoning", "summary": [{"type": "summary_text", "text": think_full}]}}, seq)
            yield chunk
            msg_output_index=1
        if not message_started:
            message_started=True
            item={"id": msg_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
            chunk, seq=_sse_pack("response.output_item.added", {"output_index": msg_output_index, "item": item, "id": msg_id, "role": "assistant"}, seq)
            yield chunk
            chunk, seq=_sse_pack("response.content_part.added", {"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}, seq)
            yield chunk
        evt={"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "delta": delta}
        chunk, seq=_sse_pack("response.output_text.delta", evt, seq)
        yield chunk

    if thinking_started and not thinking_closed:
        thinking_closed=True
        think_full="\n".join(thinking_parts)
        chunk, seq=_sse_pack("response.reasoning_summary_text.done", {"item_id": rs_id, "output_index": 0, "summary_index": 0, "text": think_full}, seq)
        yield chunk
        chunk, seq=_sse_pack("response.reasoning_summary_part.done", {"item_id": rs_id, "output_index": 0, "summary_index": 0, "part": {"type": "summary_text", "text": think_full}}, seq)
        yield chunk
        chunk, seq=_sse_pack("response.output_item.done", {"output_index": 0, "item": {"id": rs_id, "type": "reasoning", "summary": [{"type": "summary_text", "text": think_full}]}}, seq)
        yield chunk
        msg_output_index=1

    rec=_responses_finalize(rec, query, full, thinking_parts, backend_uuid, actual_model, history, current_msg, mode, model_pref)
    final_text=rec.get("output_text") or ""
    raw_full=_clean_response(full)
    streamed_notice=final_text[len(raw_full):] if final_text.startswith(raw_full) else ""
    if not message_started:
        message_started=True
        item={"id": msg_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
        chunk, seq=_sse_pack("response.output_item.added", {"output_index": msg_output_index, "item": item, "id": msg_id, "role": "assistant"}, seq)
        yield chunk
        chunk, seq=_sse_pack("response.content_part.added", {"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}, seq)
        yield chunk
        if raw_full:
            chunk, seq=_sse_pack("response.output_text.delta", {"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "delta": raw_full}, seq)
            yield chunk
    if streamed_notice:
        chunk, seq=_sse_pack("response.output_text.delta", {"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "delta": streamed_notice}, seq)
        yield chunk
    chunk, seq=_sse_pack("response.output_text.done", {"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "text": final_text}, seq)
    yield chunk
    chunk, seq=_sse_pack("response.content_part.done", {"item_id": msg_id, "output_index": msg_output_index, "content_index": 0, "part": {"type": "output_text", "text": final_text, "annotations": []}}, seq)
    yield chunk
    msg_item={"id": msg_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": final_text, "annotations": []}]}
    chunk, seq=_sse_pack("response.output_item.done", {"output_index": msg_output_index, "item": msg_item}, seq)
    yield chunk
    chunk, seq=_wrap("response.completed", rec)
    yield chunk

@app.post("/v1/responses")
async def responses_api(request: Request, _=Depends(verify_api_key)):
    """OpenAI Responses API compatibility: create, stream, store, and chain turns."""
    try:
        body=await request.json()
    except Exception:
        raise OpenAIAPIError(400, "Invalid or empty JSON body")
    if not isinstance(body, dict):
        raise OpenAIAPIError(400, "Request body must be an object")

    stream=body.get("stream", False)
    if isinstance(stream, dict):
        stream=True
    stream=bool(stream)
    background=bool(body.get("background", False))
    if background and stream:
        raise OpenAIAPIError(400, "background=true cannot be combined with stream=true", param="background")

    model_name=body.get("model", DEFAULT_MODEL)
    inp=body.get("input", "")
    instructions=body.get("instructions", "")
    tools_raw=body.get("tools", [])
    previous_response_id=body.get("previous_response_id")
    conversation=body.get("conversation")
    store_flag=True if body.get("store") is None else bool(body.get("store"))
    metadata=body.get("metadata") or {}
    reasoning=body.get("reasoning")
    temperature=body.get("temperature", 1.0)
    top_p=body.get("top_p", 1.0)
    max_output_tokens=body.get("max_output_tokens")
    tool_choice=body.get("tool_choice", "auto")
    truncation=body.get("truncation") or "disabled"
    parallel_tool_calls=True if body.get("parallel_tool_calls") is None else bool(body.get("parallel_tool_calls"))
    user=body.get("user")
    text_cfg=body.get("text") if isinstance(body.get("text"), dict) else {}
    log.info(f"Responses API: model={model_name}, stream={stream}, background={background}")

    effort=None
    if isinstance(reasoning, dict):
        effort=reasoning.get("effort")
    use_thinking=bool(effort) and str(effort).lower() not in ("none", "null")

    model_name, mode, model_pref=_responses_resolve_model(model_name, use_thinking)
    messages=_responses_parse_input(inp, instructions)
    extra_instructions=[]
    fmt=text_cfg.get("format") if isinstance(text_cfg.get("format"), dict) else {}
    if fmt.get("type") == "json_object":
        extra_instructions.append("Return a valid JSON object only, with no markdown.")
    elif fmt.get("type") == "json_schema":
        schema=fmt.get("schema") or (fmt.get("json_schema") or {}).get("schema") or {}
        extra_instructions.append("Return JSON matching this schema: "+json.dumps(schema, ensure_ascii=False))

    messages, prev_id, conv_id=_responses_apply_previous(messages, previous_response_id, conversation)
    if not messages or not any(m.get("role") == "user" for m in messages):
        raise OpenAIAPIError(400, "No user message found in input", param="input")

    prepared=_prepare_pplx_from_messages(messages, "responses_api", extra_instructions)
    query=prepared["query"]
    if not (query or "").strip():
        raise OpenAIAPIError(400, "Empty query after processing", param="input")

    rec=_responses_new_record(
        model_name, instructions, max_output_tokens, prev_id, reasoning, store_flag,
        temperature, text_cfg, tool_choice, tools_raw, top_p, truncation, metadata, user,
        background, conv_id, _responses_normalize_input_items(inp), query, parallel_tool_calls,
    )
    rec["_history_messages"]=list(prepared["history"])+[("user", prepared["current_msg"])] if prepared["current_msg"] else list(prepared["history"])
    _responses_put(rec, persist=store_flag)

    if background:
        task=asyncio.create_task(_responses_background_job(
            rec["id"], query, mode, model_pref, prepared["follow_up_uuid"],
            prepared["history"], prepared["current_msg"],
        ))
        _responses_tasks[rec["id"]]=task
        return _responses_public(rec)

    client=get_client()
    if stream:
        return StreamingResponse(
            _stream_responses_api(client, rec, query, mode, model_pref, prepared["follow_up_uuid"], prepared["history"], prepared["current_msg"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    result=await _responses_collect_answer(client, query, mode, model_pref, prepared["follow_up_uuid"])
    if result["error"]:
        rec["status"]="failed"
        rec["error"]={"code": "server_error", "message": str(result["error"])}
        _responses_put(rec, persist=store_flag)
        raise OpenAIAPIError(502, str(result["error"]), err_type="server_error")
    rec=_responses_finalize(rec, query, result["full"], result["thinking_parts"], result["backend_uuid"], result["actual_model"], prepared["history"], prepared["current_msg"], mode, model_pref)
    return _responses_public(rec)


@app.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, _=Depends(verify_api_key)):
    rec=_responses_get(response_id)
    if not rec:
        raise OpenAIAPIError(404, f"No model response found with id '{response_id}'.", param="response_id")
    return _responses_public(rec)


@app.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, _=Depends(verify_api_key)):
    if not _responses_delete(response_id):
        raise OpenAIAPIError(404, f"No model response found with id '{response_id}'.", param="response_id")
    task=_responses_tasks.pop(response_id, None)
    if task:
        task.cancel()
    return {"id": response_id, "object": "response", "deleted": True}


@app.post("/v1/responses/{response_id}/cancel")
async def cancel_response(response_id: str, _=Depends(verify_api_key)):
    rec=_responses_get(response_id)
    if not rec:
        raise OpenAIAPIError(404, f"No model response found with id '{response_id}'.", param="response_id")
    if rec.get("status") != "in_progress":
        raise OpenAIAPIError(400, "Only in-progress responses can be cancelled.", param="response_id")
    task=_responses_tasks.get(response_id)
    if task:
        task.cancel()
    rec["status"]="cancelled"
    rec["incomplete_details"]={"reason": "cancelled"}
    _responses_put(rec, persist=rec.get("store", True))
    return _responses_public(rec)


@app.get("/v1/responses/{response_id}/input_items")
async def list_response_input_items(response_id: str, limit: int=20, after: str=None, before: str=None, order: str="asc", _=Depends(verify_api_key)):
    rec=_responses_get(response_id)
    if not rec:
        raise OpenAIAPIError(404, f"No model response found with id '{response_id}'.", param="response_id")
    items=list(rec.get("_input_items") or [])
    if (order or "asc").lower() == "desc":
        items=list(reversed(items))
    if after:
        ids=[x.get("id") for x in items]
        if after in ids:
            items=items[ids.index(after)+1:]
    if before:
        ids=[x.get("id") for x in items]
        if before in ids:
            items=items[:ids.index(before)]
    try:
        limit=int(limit)
    except Exception:
        limit=20
    limit=min(max(limit, 1), 100)
    has_more=len(items) > limit
    items=items[:limit]
    return {
        "object": "list",
        "data": items,
        "first_id": items[0].get("id") if items else None,
        "last_id": items[-1].get("id") if items else None,
        "has_more": has_more,
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
        content=_message_content_text(msg.get("content"))
        # Detect user messages that are actually system prompts (LobeHub sends
        # Jamie's custom system prompt as role:user after the developer message)
        if role=="user":
            _ct=content[:200].lower()
            if any(kw in _ct for kw in ["you are ", "you must ", "your role", "ccsearch", "加載", "技能", "available_skills", "<skill", "<user_memory", "<available_tools", "<tool_selection", "<credentials", "<best_practices", "<memory_effort", "<session_context"]):
                role="system"
        # Strip rate limit notices from previous responses
        content=_strip_appended_notices(content)
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
    actual_model=None
    async for chunk in client.search(query, mode, model_pref, sources, language, follow_up_uuid):
        if chunk.get("backend_uuid"):
            resp_backend_uuid=chunk["backend_uuid"]
        if chunk.get("actual_model"):
            actual_model=chunk["actual_model"]
        if chunk.get("error"):
            raise HTTPException(502, chunk)
        if chunk.get("thinking"):
            thinking_parts.append(chunk["thinking"])
            continue
        if chunk.get("done"):
            full=chunk.get("answer", full)
            actual_model=chunk.get("actual_model", actual_model)
            break
        full=chunk.get("answer", full)
    reasoning_content="\n".join(thinking_parts) if thinking_parts else None
    full=_clean_response(full)

    # Store session for next turn
    _session_store(history, current_msg, full, resp_backend_uuid)

    # Rate limit: decrement + append notices
    if mode != "auto":  # Pro queries only (copilot mode)
        _decrement_pro()
    notice=_response_suffix(model_pref, actual_model)
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
    _actual_model=None
    async for chunk in client.search(query, mode, model_pref, sources, language, follow_up_uuid):
        if chunk.get("backend_uuid"):
            _resp_backend_uuid=chunk["backend_uuid"]
        if chunk.get("actual_model"):
            _actual_model=chunk["actual_model"]
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

            # Rate limit decrement + notices
            _decrement_pro()
            notice=_response_suffix(model_pref, chunk.get("actual_model", _actual_model))
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
    # gpt54 / gpt56_terra → major=5, minor=4/6
    (_re.compile(r"^(gpt)(\d)(\d)((?:_.*)?)$"), "{prefix}{ma}{mi}{suffix}"),
    # claude46sonnet → major=4, minor=6
    (_re.compile(r"^(claude)(\d)(\d)(sonnet(?:thinking)?)$"), "{prefix}{ma}{mi}{suffix}"),
    (_re.compile(r"^(claude)(\d)(\d)(opus(?:thinking)?)$"), "{prefix}{ma}{mi}{suffix}"),
    # gemini31pro_high → major=3, minor=1
    (_re.compile(r"^(gemini)(\d)(\d)(pro(?:_high)?)$"), "{prefix}{ma}{mi}{suffix}"),
    # grok420reasoning / grok45low → major=4, minor=20/5
    (_re.compile(r"^(grok)(\d)(\d+)((?:non)?reasoning|multiagent|low|medium)?$"), "{prefix}{ma}{mi}{suffix}"),
    # nv_nemotron_3_super → gen=3
    (_re.compile(r"^(nv_nemotron_)(\d)(_super|_ultra)$"), "{prefix}{ma}{suffix}"),
]

def _increment_version(major: int, minor: int, minor_width: int=1) -> tuple:
    """Increment version: 5.4 → 5.5, 5.9 → 6.0, 4.20 → 4.21."""
    minor+=1
    if minor_width == 1 and minor >= 10:
        minor=0
        major+=1
    return major, minor

def _version_distance(orig_ma, orig_mi, cur_ma, cur_mi, minor_width: int=1) -> float:
    """Calculate version distance: e.g., 5.4 → 7.4 = 2.0"""
    return (cur_ma - orig_ma) + (cur_mi - orig_mi) / (10.0 ** minor_width)

MAX_VERSION_UPGRADE_PROBES=10

def _version_upgrade_candidates(pref: str):
    """Yield next version prefs within +1.0, capped to prevent two-digit runaway."""
    for pattern, template in _VERSION_PATTERNS:
        m=pattern.match(pref)
        if not m:
            continue
        groups=m.groups()
        if len(groups) == 4:
            prefix, orig_ma_s, orig_mi_s, suffix=groups
            suffix=suffix or ""
            orig_ma, orig_mi=int(orig_ma_s), int(orig_mi_s)
            minor_width=len(orig_mi_s)
            ma, mi=orig_ma, orig_mi
            for _ in range(MAX_VERSION_UPGRADE_PROBES):
                ma, mi=_increment_version(ma, mi, minor_width)
                if _version_distance(orig_ma, orig_mi, ma, mi, minor_width) > 1.0:
                    return
                yield template.format(prefix=prefix, ma=ma, mi=mi, suffix=suffix)
        elif len(groups) == 3:
            prefix, gen_s, suffix=groups
            orig_gen=int(gen_s)
            yield template.format(prefix=prefix, ma=orig_gen+1, suffix=suffix)
        return


PROBE_ALIVE="alive"
PROBE_SUBSTITUTED="substituted"
PROBE_DEAD="dead"

async def probe_model_status(client, pref) -> str:
    """Classify a model_preference as alive, substituted, or dead."""
    try:
        query="Explain one concrete tradeoff between optimistic and pessimistic database locking in 80 to 120 words."
        async for chunk in client.search(query, "pro", pref, ["web"], "en-US"):
            if chunk.get("error"):
                return PROBE_DEAD
            if chunk.get("done"):
                if not chunk.get("answer", "").strip():
                    return PROBE_DEAD
                if _model_matches_preference(pref, chunk.get("actual_model", "")):
                    return PROBE_ALIVE
                return PROBE_SUBSTITUTED
        return PROBE_DEAD
    except Exception:
        return PROBE_DEAD

async def probe_model(client, pref) -> bool:
    """Test if a model_preference is valid."""
    return await probe_model_status(client, pref) == PROBE_ALIVE

def _is_pinned_model_id(model_id: str) -> bool:
    """Fixed generation names such as sonnet-4.6 must not jump to another model."""
    return bool(_re.search(r"\d+\.\d+", model_id) or _re.search(r"-\d+$", model_id))

async def _try_upgrade_model(client, model_id, mode, pref, report=None, sleep_seconds=2.0) -> bool:
    """Upgrade a dead versioned model. Skip when Perplexity only substituted it."""
    global MODEL_MAP
    status=await probe_model_status(client, pref)
    if report is not None:
        report["probed"]=report.get("probed", 0)+1
    if status == PROBE_ALIVE:
        if report is not None:
            report.setdefault("alive", []).append(model_id)
        return False
    if status == PROBE_SUBSTITUTED:
        log.info(f"Discovery: {model_id} substituted by Perplexity, skipping version upgrade")
        if report is not None:
            report.setdefault("unavailable", []).append({"model": model_id, "pref": pref, "reason": "provider substituted another model"})
        return False
    if _is_pinned_model_id(model_id):
        log.info(f"Discovery: {model_id} is a pinned version name, skipping version upgrade")
        if report is not None:
            report.setdefault("dead", []).append({"model": model_id, "pref": pref, "reason": "pinned version name, no upgrade"})
        return False
    found=False
    for new_pref in _version_upgrade_candidates(pref):
        if report is not None:
            report["probed"]=report.get("probed", 0)+1
        log.info(f"Discovery: {model_id} dead, trying {new_pref}...")
        if await probe_model_status(client, new_pref) == PROBE_ALIVE:
            MODEL_MAP[model_id]=(mode, new_pref)
            if report is not None:
                report.setdefault("upgraded", {})[model_id]={"old": pref, "new": new_pref}
            log.info(f"Discovery: {model_id} upgraded {pref} → {new_pref}")
            found=True
            break
        await asyncio.sleep(sleep_seconds)
    if not found and report is not None:
        report.setdefault("dead", []).append({"model": model_id, "pref": pref, "reason": "no valid version within +1.0 or probe cap"})
    return found


async def ensure_model_available(client, pref) -> bool:
    """Use a short-lived verified result before accepting an explicit model."""
    now=time.monotonic()
    cached=_model_preflight_cache.get(pref)
    if cached and now-cached[0] < MODEL_PREFLIGHT_TTL_SECS:
        return cached[1]
    available=await probe_model(client, pref)
    _model_preflight_cache[pref]=(now, available)
    return available


async def _discover_known_missing_models(client, report: dict, sleep_seconds: float=2.0) -> bool:
    """Probe known model names that are absent from a persisted .models.json."""
    global MODEL_MAP
    allowed_tiers={"free"} if ACCOUNT_TYPE == "free" else {"free", "pro"} if ACCOUNT_TYPE == "pro" else {"free", "pro", "max"}
    changed=False
    report.setdefault("added", {})
    report.setdefault("unavailable", [])
    for model_id, (mode, pref) in _ALL_MODELS.items():
        tier=_MODEL_REGISTRY.get(model_id, {}).get("tier", "pro")
        if model_id in MODEL_MAP or tier not in allowed_tiers:
            continue
        report["probed"]+=1
        log.info(f"Discovery: probing new model name {model_id} ({pref})...")
        if await probe_model(client, pref):
            MODEL_MAP[model_id]=(mode, pref)
            report["added"][model_id]={"pref": pref, "label": _MODEL_LABELS.get(model_id, model_id)}
            changed=True
            log.info(f"Discovery: added new model {model_id} ({pref})")
        else:
            report["unavailable"].append({"model": model_id, "pref": pref, "reason": "known candidate did not respond"})
        await asyncio.sleep(sleep_seconds)
    return changed


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

    report={"alive": [], "upgraded": {}, "added": {}, "unavailable": [], "dead": [], "probed": 0}

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
            status=await probe_model_status(client, pref)
            if status == PROBE_ALIVE:
                report["alive"].append(model_id)
            elif status == PROBE_SUBSTITUTED:
                report["unavailable"].append({"model": model_id, "pref": pref, "reason": "provider substituted another model"})
            else:
                report["dead"].append({"model": model_id, "pref": pref, "reason": "non-versioned, no upgrade path"})
            await asyncio.sleep(2)
            continue

        await _try_upgrade_model(client, model_id, mode, pref, report)
        await asyncio.sleep(2)

    added=await _discover_known_missing_models(client, report)

    if report["upgraded"] or added:
        save_model_map(MODEL_MAP)

    return {
        "status": "ok",
        "alive": len(report["alive"]),
        "upgraded": len(report["upgraded"]),
        "added": len(report["added"]),
        "dead": len(report["dead"]),
        "unavailable": len(report["unavailable"]),
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
        actual_model=None
        async for ch in client.search(query, mode, pref, src, language):
            if ch.get("error"): return f"Error: {ch['error']}"
            if ch.get("actual_model"): actual_model=ch["actual_model"]
            if ch.get("done"):
                r=ch.get("answer", r)
                actual_model=ch.get("actual_model", actual_model)
                break
            r=ch.get("answer", r)
        return r+_substitution_notice(pref, actual_model)

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
        actual_model=None
        async for ch in client.search(query, mode, pref, ["web"], language):
            if ch.get("error"): return f"Error: {ch['error']}"
            if ch.get("actual_model"): actual_model=ch["actual_model"]
            if ch.get("done"):
                r=ch.get("answer", r)
                actual_model=ch.get("actual_model", actual_model)
                break
            r=ch.get("answer", r)
        return r+_substitution_notice(pref, actual_model)

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
            await reconcile_configured_session()
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

def _normalize_ntfy_topic(value: str) -> str:
    """Accept only private topic names that conform to ntfy's documented format."""
    topic=str(value or "").strip()
    if not topic or topic == "pplx-proxy":
        return ""
    if len(topic) > 64 or not re.fullmatch(r"[-_A-Za-z0-9]+", topic):
        return ""
    return topic


_NTFY_TOPIC_CONFIG=os.getenv("NTFY_TOPIC", "")
NTFY_TOPIC=_normalize_ntfy_topic(_NTFY_TOPIC_CONFIG)
NTFY_URL=os.getenv("NTFY_URL", "https://ntfy.sh")
_last_ntfy_ts=0.0

if _NTFY_TOPIC_CONFIG.strip() and not NTFY_TOPIC:
    log.warning("Invalid or shared public NTFY_TOPIC is disabled; configure a unique topic of at most 64 characters")

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

async def session_keepalive_once() -> bool:
    """Validate the session and persist any cookie rotation from Perplexity."""
    try:
        client=get_client()
        await client.init()
        resp=await client.session.get(PPLX_AUTH_SESSION)
        if resp.status_code != 200:
            log.warning(f"Keep-alive failed: HTTP {resp.status_code}")
            if resp.status_code in (401, 403):
                await notify_cookie_expired(f"Keep-alive returned HTTP {resp.status_code}")
            return False
        data=resp.json() if hasattr(resp, "json") else {}
        if not isinstance(data, dict) or not data.get("user"):
            log.warning("Keep-alive failed: unauthenticated session response")
            await notify_cookie_expired("Keep-alive returned an unauthenticated session response")
            return False
        rotated=client.sync_cookies_from_session()
        save_cookies(client.cookies, last_keepalive=time.time())
        state="rotated and persisted" if rotated else "validated and persisted"
        log.info(f"Keep-alive OK: {resp.status_code}, cookie {state}")
        return True
    except Exception as e:
        log.error(f"Keep-alive error: {e}")
        return False

async def session_keepalive_loop():
    """Validate on startup, then periodically keep the session alive."""
    log.info(f"Session keep-alive enabled: every {KEEPALIVE_HOURS}h")
    while True:
        await session_keepalive_once()
        await asyncio.sleep(KEEPALIVE_HOURS * 3600)


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
    validated_cookies=await _validate_session_cookies(cookies)
    if not validated_cookies:
        _configured_session_state.update({
            "status": "invalid",
            "source": "cache" if _load_cached_cookies() else None,
            "message": "Submitted session token is not authenticated",
        })
        raise HTTPException(
            status_code=401,
            detail="Unable to validate the submitted Perplexity session token",
        )
    save_cookies(validated_cookies, last_keepalive=time.time())
    global _client, _rate_limit_refresh_task, _model_preflight_cache
    if _client:
        _client.reset(validated_cookies)
    else:
        _client=PerplexityClient(validated_cookies)
    if _rate_limit_refresh_task and not _rate_limit_refresh_task.done():
        _rate_limit_refresh_task.cancel()
    _rate_limit_refresh_task=None
    _rate_limit["remaining_pro"]=None
    _rate_limit["remaining_research"]=None
    _rate_limit["updated_at"]=0
    _rate_limit["last_error"]=None
    _model_preflight_cache.clear()
    # Reload model map from file if it exists
    global MODEL_MAP
    MODEL_MAP=load_model_map()
    _configured_session_state.update({"status": "active", "source": "admin", "message": None})
    return {"status": "ok", "message": "Cookie updated and validated", "models_loaded": len(MODEL_MAP)}


async def auto_discover_loop():
    """Background task: run model discovery every PROBE_INTERVAL_HOURS."""
    log.info(f"Auto-discovery enabled: every {PROBE_INTERVAL_HOURS}h")
    while True:
        await asyncio.sleep(PROBE_INTERVAL_HOURS * 3600)
        log.info("Scheduled model discovery starting...")
        try:
            client=get_client()
            await client.init()
            report={"added": {}, "unavailable": [], "probed": 0}
            if await _discover_known_missing_models(client, report):
                save_model_map(MODEL_MAP)
                await notify_cookie_expired(f"New models discovered: {', '.join(sorted(report['added']))}")
            mm=get_model_map()
            base_models={}
            for mid, (mode, pref) in mm.items():
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
                upgraded=await _try_upgrade_model(client, model_id, mode, pref)
                if upgraded:
                    save_model_map(MODEL_MAP)
                    new_pref=MODEL_MAP[model_id][1]
                    log.info(f"Auto-discovery: {model_id} upgraded {pref} → {new_pref}")
                    await notify_cookie_expired(f"Model {model_id} auto-upgraded: {pref} → {new_pref}")
                await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Auto-discovery error: {e}")


# startup tasks moved into _combined_lifespan above


# ─── Entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())
