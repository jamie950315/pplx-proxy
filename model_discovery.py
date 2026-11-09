"""Authenticated Perplexity model-picker discovery and cache validation.

Perplexity's web model picker is exposed at /rest/models/config/v2. Its
``search_config`` entries connect display labels and subscription tiers to the
actual ``model_preference`` identifiers accepted by /rest/sse/perplexity_ask.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Optional

MODEL_CONFIG_URL = "https://www.perplexity.ai/rest/models/config/v2"
AUTH_SESSION_URL = "https://www.perplexity.ai/api/auth/session"
TIER_RANK = {"free": 0, "pro": 1, "max": 2}


class DiscoveryError(RuntimeError):
    """The upstream catalogue could not be safely interpreted."""


def sanitize_discovery_text(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", str(text)).strip()
    clean = re.sub(r"(session[-_]?token|cookie|authorization)=[^;\s&]+", r"\1=[REDACTED]", clean, flags=re.IGNORECASE)
    clean = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", clean, flags=re.IGNORECASE)
    clean = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT REDACTED]", clean)
    return clean


def format_discovery_exception(exc: Exception, stage: Optional[str] = None) -> str:
    prefix = f"[{stage}] " if stage else ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        status_code = getattr(resp, "status_code", None)
        raw_url = str(getattr(resp, "url", ""))
        if raw_url:
            parsed = urllib.parse.urlparse(raw_url)
            clean_url = parsed.path or parsed.netloc or raw_url
        else:
            clean_url = "unknown"
        headers = getattr(resp, "headers", {}) or {}
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        if ";" in content_type:
            content_type = content_type.split(";")[0].strip()
        body_text = ""
        try:
            body_text = getattr(resp, "text", "") or ""
        except Exception:
            pass
        clean_body = sanitize_discovery_text(body_text[:150]) if body_text else ""
        status_str = f"HTTP {status_code}" if status_code else "HTTP error"
        details = [status_str, f"at {clean_url}"]
        if content_type:
            details.append(f"({content_type})")
        if clean_body:
            details.append(f": {clean_body}")
        return f"{prefix}{' '.join(details)}"

    err_type = type(exc).__name__
    err_msg = sanitize_discovery_text(str(exc))
    if err_msg and err_msg != err_type:
        return f"{prefix}{err_type}: {err_msg}"
    return f"{prefix}{err_type}"



def parse_catalogue(payload: object, account_tier: str) -> dict:
    """Return directly invokable search picker IDs and safe display metadata."""
    if not isinstance(payload, dict) or payload.get("config_schema") != "v2":
        raise DiscoveryError("Perplexity returned an unsupported model catalogue")
    models = payload.get("models")
    choices = payload.get("search_config")
    if not isinstance(models, dict) or not models or not isinstance(choices, list):
        raise DiscoveryError("Perplexity model catalogue is incomplete")
    if account_tier not in TIER_RANK:
        raise DiscoveryError("Perplexity account subscription tier is unavailable")
    result = {}
    for choice in choices:
        if not isinstance(choice, dict):
            raise DiscoveryError("Perplexity model catalogue contains an invalid picker entry")
        required = choice.get("subscription_tier")
        if required not in TIER_RANK:
            raise DiscoveryError("Perplexity model catalogue contains an unknown subscription tier")
        label = choice.get("label")
        if not isinstance(label, str) or not label.strip():
            raise DiscoveryError("Perplexity model catalogue contains an unnamed picker entry")
        for field in ("non_reasoning_model", "reasoning_model"):
            model_id = choice.get(field)
            if model_id is None:
                continue
            if not isinstance(model_id, str) or not model_id.strip():
                raise DiscoveryError("Perplexity model catalogue contains an invalid model identifier")
            metadata = models.get(model_id)
            if not isinstance(metadata, dict) or not isinstance(metadata.get("label"), str):
                raise DiscoveryError("Perplexity picker references a model with missing metadata")
            mode = metadata.get("mode")
            if mode not in {"search", "browser_agent", "asi", "research", "agentic_research", "study"}:
                raise DiscoveryError("Perplexity picker contains an unsupported model mode")
            if mode != "search" or TIER_RANK[required] > TIER_RANK[account_tier]:
                # Non-search modes need a different invocation protocol.
                continue
            model = {
                "mode": "pro",
                "pref": model_id,
                "label": metadata["label"],
                "tier": required,
                "source": "discovered",
                "thinking": field == "reasoning_model",
            }
            for key in ("description", "provider", "capabilities", "context_length", "max_output_tokens"):
                value = metadata.get(key)
                if isinstance(value, (str, int, float, bool, list, dict)):
                    model[key] = value
            result[model_id] = model
        base_id = choice.get("non_reasoning_model")
        reasoning_id = choice.get("reasoning_model")
        if base_id in result and reasoning_id in result:
            result[base_id]["thinking_pref"] = reasoning_id
    if not result:
        raise DiscoveryError("Perplexity catalogue contains no invokable search models")
    return result


def account_tier_from_session(payload: object, configured_tier: str) -> str:
    """Use authenticated session tier when available; otherwise configured tier."""
    if isinstance(payload, dict):
        user = payload.get("user")
        if isinstance(user, dict):
            tier = user.get("subscription_tier")
            status = user.get("subscription_status")
            if status in {"active", "trialing"} and tier in TIER_RANK:
                return tier
            if status and status not in {"active", "trialing"}:
                return "free"
    return configured_tier if configured_tier in TIER_RANK else "pro"


class ModelRegistry:
    """Atomic last-known-good catalogue with serialized refresh attempts."""

    def __init__(self, cache_path: Path, fallback: dict, refresh_seconds: int = 86400):
        self.cache_path = Path(cache_path)
        self.fallback = dict(fallback)
        self.refresh_seconds = refresh_seconds
        self.models = dict(fallback)
        self.source = "fallback"
        self.last_success = None
        self.last_attempt = None
        self.last_error = None
        self.account_tier = None
        self._lock = __import__("threading").Lock()
        self._load_cache()

    def _load_cache(self):
        try:
            cached = json.loads(self.cache_path.read_text())
            models = cached["models"]
            if not isinstance(models, dict) or not models:
                return
            for mid, entry in models.items():
                if not isinstance(mid, str) or not isinstance(entry, dict) or entry.get("pref") != mid:
                    return
                if entry.get("source") != "discovered":
                    return
            self.models = {**self.fallback, **models}
            self.last_success = float(cached["last_success"])
            self.account_tier = cached.get("account_tier")
            self.source = "cache"
        except (OSError, ValueError, TypeError, KeyError):
            return

    def refresh(self, catalogue: dict, account_tier: str, *, force: bool = False) -> dict:
        """Validate then atomically publish; failed input leaves state untouched."""
        if not self._lock.acquire(blocking=False):
            return {"status": "already_running", "model_count": len(self.models)}
        try:
            now = time.time()
            self.last_attempt = now
            if not force and self.last_success and now - self.last_success < self.refresh_seconds:
                return {"status": "fresh", "model_count": len(self.models)}
            discovered = parse_catalogue(catalogue, account_tier)
            updated = {**self.fallback, **discovered}
            timestamp = now
            data = {"last_success": timestamp, "account_tier": account_tier, "models": discovered}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(dir=self.cache_path.parent, prefix=".models-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream)
                os.chmod(temp_name, 0o600)
                os.replace(temp_name, self.cache_path)
            finally:
                Path(temp_name).unlink(missing_ok=True)
            self.models = updated
            self.last_success = timestamp
            self.account_tier = account_tier
            self.source = "live"
            self.last_error = None
            return {"status": "ok", "model_count": len(discovered)}
        except Exception as exc:
            err_msg = format_discovery_exception(exc, stage="catalogue_parse")
            self.last_error = err_msg
            return {"status": "error", "error": err_msg, "model_count": len(self.models)}
        finally:
            self._lock.release()

    def status(self) -> dict:
        return {
            "source": self.source,
            "last_success": self.last_success,
            "last_attempt": self.last_attempt,
            "last_error": self.last_error,
            "model_count": len(self.models),
        }
