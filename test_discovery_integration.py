"""Regression contracts for authenticated discovery and configured-model ownership."""
import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import server
from model_discovery import ModelRegistry, format_discovery_exception, parse_catalogue


def catalogue(pref="gpt_next", tier="pro", reasoning=None):
    models={pref: {"label": "GPT-9", "mode": "search"}}
    if reasoning:
        models[reasoning]={"label": "GPT-9 Thinking", "mode": "search"}
    return {"config_schema": "v2", "models": models, "search_config": [
        {"label": "GPT-9", "subscription_tier": tier, "non_reasoning_model": pref, "reasoning_model": reasoning}]}


class DiscoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack=contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root=root
        fallback={"auto": {"mode": "pro", "pref": "pplx_pro", "source": "fallback"},
            "gpt": {"mode": "pro", "pref": "gpt56_terra", "source": "fallback"},
            "legacy": {"mode": "pro", "pref": "legacy_pref", "source": "fallback"}}
        self.registry=ModelRegistry(root/"cache.json", fallback)
        for name, value in {"MODEL_REGISTRY": self.registry, "MODEL_MAP": {},
            "_THINKING_MAP": dict(server._THINKING_MAP), "ACCOUNT_TYPE": "pro", "DEFAULT_MODEL": "gpt",
            "MODELS_FILE": root/".models.json", "COOKIE_FILE": root/".cookie_cache.json",
            "API_KEY": "test-key", "PROBE_DISCOVERY_ENABLED": True, "_MODEL_REFRESH_LOCK": asyncio.Lock()}.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test",
            headers={"Authorization": "Bearer test-key"})
        self.addAsyncCleanup(self.client.aclose)

    def install(self, payload=None):
        self.assertEqual(server.MODEL_REGISTRY.refresh(payload or catalogue(), "pro", force=True)["status"], "ok")
        server._install_discovered_models({key: entry for key, entry in server.MODEL_REGISTRY.models.items() if entry.get("source") == "discovered"})

    async def test_admin_add_and_override_survive_refresh_and_restart(self):
        self.install()
        r=await self.client.post("/admin/update-models", json={"models": {"custom": ["pro", "my_pref"], "gpt": ["pro", "pinned_pref"], "custom-mode": ["reasoning", "gpt_next"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(server.MODEL_MAP["custom-mode"], ("reasoning", "gpt_next"))
        self.install(catalogue("gpt_newer"))
        self.assertEqual(server.MODEL_MAP["custom"], ("pro", "my_pref"))
        self.assertEqual(server.MODEL_MAP["gpt"], ("pro", "pinned_pref"))
        saved=server.load_model_map()
        fallback={key: {"mode": value[0], "pref": value[1], "source": "configured_alias"} for key, value in saved.items()}
        restored=ModelRegistry(self.registry.cache_path, fallback)
        with patch.object(server, "MODEL_REGISTRY", restored):
            server._install_discovered_models({key: value for key, value in restored.models.items() if value.get("source") == "discovered"})
            self.assertEqual(server.MODEL_MAP["gpt"], ("pro", "pinned_pref"))
            self.assertEqual(server.MODEL_MAP["custom"], ("pro", "my_pref"))

    async def test_admin_replace_does_not_resurrect_deleted_configured_aliases(self):
        self.install()
        r=await self.client.post("/admin/update-models", json={"models": {"only": ["pro", "only_pref"]}, "merge": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.install()
        self.assertNotIn("legacy", server.MODEL_MAP)
        self.assertNotIn("gpt", server.MODEL_MAP)
        self.assertIn("only", server.MODEL_MAP)
        self.assertIn("gpt_next", server.MODEL_MAP)

    async def test_alias_name_and_thinking_pair_follow_verified_target(self):
        self.install(catalogue(reasoning="verified_reasoning"))
        metadata=server.MODEL_REGISTRY.models["gpt"]
        self.assertEqual(metadata["label"], "GPT-9")
        self.assertEqual(server._thinking_model_entry("gpt", server.MODEL_MAP["gpt"]), ("pro", "verified_reasoning"))
        self.install(catalogue("base_only"))
        self.assertNotIn("gpt", server._THINKING_MAP)
        r=await self.client.post("/v1/chat/completions", json={"model": "gpt", "thinking": True,
            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("no reasoning variant", r.text)
        with self.assertRaises(server.OpenAIAPIError):
            server._responses_resolve_model("gpt", True)

    async def test_picker_tier_overrides_static_alias_tier(self):
        models=parse_catalogue(catalogue(tier="max"), "max")
        server._install_discovered_models(models)
        self.assertIn("requires max", server.check_tier("gpt"))
        self.assertNotIn("gpt", server.get_model_map())

    async def test_cookie_switch_waits_for_inflight_discovery_publication(self):
        entered, release=asyncio.Event(), asyncio.Event()
        class Response:
            def __init__(self, data): self.data=data
            def raise_for_status(self): pass
            def json(self): return self.data
        class Session:
            cookies={}
            async def get(self, url, **_kwargs):
                if url == server.AUTH_SESSION_URL:
                    return Response({"user": {"subscription_tier": "pro", "subscription_status": "active"}})
                entered.set()
                await release.wait()
                return Response(catalogue("account_a_model"))
        class Client:
            def __init__(self):
                self.cookies={"__Secure-next-auth.session-token": "fake-a"}
                self.session=Session()
            async def init(self): pass
            async def reset(self, cookies): self.cookies=cookies
        client=Client()
        class Request:
            headers={"content-type": "application/json"}
            async def json(self): return {"session_token": "fake-b"}
        with patch.object(server, "get_client", return_value=client), patch.object(server, "_client", client), \
             patch.object(server, "_validate_session_cookies", AsyncMock(return_value={"__Secure-next-auth.session-token": "fake-b"})), \
             patch.object(server, "_rate_limit_refresh_task", None), patch.object(server, "_reset_rate_limit"):
            discovery=asyncio.create_task(server._refresh_live_model_catalogue())
            await entered.wait()
            with patch.object(server, "_refresh_live_model_catalogue", AsyncMock(return_value={"status": "ok"})):
                switch=asyncio.create_task(server.refresh_cookie_endpoint(Request()))
                await asyncio.sleep(0)
                self.assertFalse(switch.done())
                self.assertEqual(client.cookies["__Secure-next-auth.session-token"], "fake-a")
                release.set()
                await discovery
                await switch
            self.assertEqual(client.cookies["__Secure-next-auth.session-token"], "fake-b")
            self.assertNotIn("account_a_model", server.MODEL_MAP)
            self.assertFalse(server.MODEL_REGISTRY.cache_path.exists())

    async def test_ajax_headers_existing_session_and_minute_timeout(self):
        calls=[]
        class Response:
            def __init__(self, data): self.data=data
            def raise_for_status(self): pass
            def json(self): return self.data
        class Session:
            cookies={}
            async def get(self, url, **kwargs):
                calls.append((url, dict(kwargs)))
                return Response({"user": {"subscription_tier": "pro", "subscription_status": "active"}} if url == server.AUTH_SESSION_URL else catalogue())
        client=type("Client", (), {"session": Session(), "init": AsyncMock()})()
        with patch.object(server, "get_client", return_value=client), patch.object(server, "PROBE_TIMEOUT_MINUTES", 2.5):
            result=await server._refresh_live_model_catalogue()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([kwargs["timeout"] for _, kwargs in calls], [150, 150])
        headers=calls[-1][1]["headers"]
        self.assertEqual(headers["origin"], server.PPLX_BASE)
        self.assertEqual(headers["referer"], server.PPLX_BASE+"/")
        self.assertEqual(headers["sec-fetch-mode"], "cors")
        self.assertEqual(headers["sec-fetch-dest"], "empty")
        self.assertNotIn("http_version", calls[-1][1])

    async def test_loop_uses_configured_interval(self):
        refresh=AsyncMock()
        with patch.object(server, "PROBE_INTERVAL_HOURS", 0.5), \
             patch.object(server.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])), \
             patch.object(server, "_refresh_live_model_catalogue", refresh):
            with self.assertRaises(asyncio.CancelledError):
                await server.auto_discover_loop()
            server.asyncio.sleep.assert_any_await(1800)
        refresh.assert_awaited_once_with(force=True)

    async def test_lifespan_enabled_disabled_and_shutdown(self):
        @contextlib.asynccontextmanager
        async def transport(_app): yield
        async def job(): await asyncio.Event().wait()
        for enabled in (False, True):
            refresh=AsyncMock(return_value={"status": "ok"})
            loop=AsyncMock(side_effect=job)
            with patch.object(server, "PROBE_DISCOVERY_ENABLED", enabled), patch.object(server, "_transport_lifespan", transport), \
                 patch.object(server, "reconcile_configured_session", AsyncMock()), patch.object(server, "_responses_load"), \
                 patch.object(server, "_refresh_live_model_catalogue", refresh), patch.object(server, "auto_discover_loop", loop), \
                 patch.object(server, "session_keepalive_loop", job), patch.object(server, "_rate_limit_poll_loop", job), \
                 patch.object(server, "_client", None), patch.object(server, "_responses_tasks", {}), patch.object(server, "_rate_limit_refresh_task", None):
                async with server._service_lifespan(server.app):
                    await asyncio.sleep(0)
                self.assertEqual(refresh.await_count, int(enabled))
                self.assertEqual(loop.await_count, int(enabled))

    async def test_cache_is_private_and_contains_only_picker_metadata(self):
        self.install()
        self.assertEqual(self.registry.cache_path.stat().st_mode & 0o777, 0o600)
        data=json.loads(self.registry.cache_path.read_text())
        self.assertEqual(set(data), {"models", "account_tier", "last_success"})
        self.assertEqual(set(data["models"]), {"gpt_next"})


class DiscoveryDiagnosticTests(unittest.TestCase):
    def test_verified_friendly_ids_and_legacy_substituted_ids_remain_available(self):
        with patch.object(server, "ACCOUNT_TYPE", "pro"):
            models=server._default_model_map()
        expected={"gpt-6.1-sol": "gpt6_1_sol", "gemini-3.8-flash": "gemini38flash", "sonnet-5.5": "claude55sonnet",
            "glm-5.3": "glm_5_3_thinking", "grok-4.7": "grok47", "nemotron-3.5-super-vl": "nv_nemotron_3_5_super"}
        for key, pref in expected.items():
            self.assertEqual(models[key], ("pro", pref))
        legacy={"gpt-mini", "gpt-nano", "gemini-flash", "grok-reasoning", "grok-non-reasoning", "gpt-5.5",
            "sonnet-4.6", "grok-4", "nemotron-3-super", "kimi-k3", "kimi-k2.6"}
        self.assertTrue(legacy.issubset(models))

    def test_reflected_credentials_are_not_in_public_diagnostics(self):
        token="eyJ"+"a"*110+"."+"b"*60+"."+"c"*40
        for body in (token, 'cookie: opaque-secret', '{"cookie":"opaque-secret"}', "Bad Gateway"):
            response=type("Response", (), {"status_code": 403, "url": "https://www.perplexity.ai/rest/models/config/v2?token=opaque-secret",
                "headers": {"content-type": "text/plain"}, "text": body})()
            exc=RuntimeError(body)
            exc.response=response
            text=format_discovery_exception(exc, "model_config")
            self.assertEqual(text, "[model_config] HTTP 403 at /rest/models/config/v2 (text/plain)")
            self.assertNotIn(token[:100], text)
        self.assertEqual(format_discovery_exception(RuntimeError("cookie: opaque-secret")), "RuntimeError")
