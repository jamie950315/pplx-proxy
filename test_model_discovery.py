import json
import tempfile
import unittest
from pathlib import Path

from model_discovery import DiscoveryError, ModelRegistry, account_tier_from_session, parse_catalogue
import httpx
import server
from unittest.mock import patch


def catalogue():
    return {
        "config_schema": "v2",
        "models": {
            "model_new": {"label": "New Model", "mode": "search"},
            "model_new_thinking": {"label": "New Model Thinking", "mode": "search"},
            "locked": {"label": "Max Model", "mode": "search"},
            "computer_only": {"label": "Computer", "mode": "asi"},
        },
        "search_config": [
            {"label": "New Model", "subscription_tier": "pro", "non_reasoning_model": "model_new", "reasoning_model": "model_new_thinking"},
            {"label": "Max Model", "subscription_tier": "max", "reasoning_model": "locked", "non_reasoning_model": None},
            {"label": "Computer", "subscription_tier": "pro", "non_reasoning_model": "computer_only", "reasoning_model": None},
        ],
    }


class CatalogueParsingTests(unittest.TestCase):
    def test_extracts_invokable_ids_and_excludes_other_modes_and_tiers(self):
        result = parse_catalogue(catalogue(), "pro")
        self.assertEqual(set(result), {"model_new", "model_new_thinking"})
        self.assertEqual(result["model_new"]["pref"], "model_new")
        self.assertEqual(result["model_new"]["thinking_pref"], "model_new_thinking")
        self.assertEqual(result["model_new"]["label"], "New Model")

    def test_rejects_missing_or_malformed_catalogue(self):
        for payload in (None, {}, {"config_schema": "v1"}, {"config_schema": "v2", "models": {}, "search_config": []}):
            with self.subTest(payload=payload), self.assertRaises(DiscoveryError):
                parse_catalogue(payload, "pro")

    def test_session_tier_is_used_when_authenticated(self):
        self.assertEqual(account_tier_from_session({"user": {"subscription_tier": "max", "subscription_status": "active"}}, "pro"), "max")
        self.assertEqual(account_tier_from_session({"user": {"subscription_status": "canceled"}}, "max"), "free")

    def test_discovered_thinking_variant_resolves_exactly(self):
        models = parse_catalogue(catalogue(), "pro")
        with patch.dict(server.MODEL_REGISTRY.models, models):
            self.assertEqual(server._thinking_model_entry("model_new", ("pro", "model_new")), ("pro", "model_new_thinking"))


class RegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "models.json"
        self.fallback = {"legacy": {"mode": "pro", "pref": "old", "source": "fallback"}}
        self.registry = ModelRegistry(self.path, self.fallback, refresh_seconds=60)

    def tearDown(self):
        self.temp.cleanup()

    def test_refresh_is_cached_and_atomic_and_failed_refresh_retains_last_good(self):
        result = self.registry.refresh(catalogue(), "pro", force=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.registry.models["model_new"]["pref"], "model_new")
        self.assertEqual(set(json.loads(self.path.read_text())["models"]), {"model_new", "model_new_thinking"})
        old = dict(self.registry.models)
        failed = self.registry.refresh({}, "pro", force=True)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(self.registry.models, old)
        restored = ModelRegistry(self.path, self.fallback)
        self.assertEqual(restored.source, "cache")
        self.assertIn("model_new", restored.models)

    def test_failed_initial_refresh_uses_fallback(self):
        result = self.registry.refresh({}, "pro", force=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.registry.models, self.fallback)

    def test_non_forced_refresh_observes_refresh_interval(self):
        self.registry.refresh(catalogue(), "pro", force=True)
        self.assertEqual(self.registry.refresh({}, "pro")["status"], "fresh")

    def test_concurrent_refresh_is_rejected(self):
        self.registry._lock.acquire()
        try:
            self.assertEqual(self.registry.refresh(catalogue(), "pro")["status"], "already_running")
        finally:
            self.registry._lock.release()


class DiscoveryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")
        self.fake = type("FakeClient", (), {
            "search": self.search,
        })()
        self.calls = []
        self.patches = [
            patch.object(server, "API_KEY", "secret"),
            patch.object(server, "get_model_map", return_value={"model_new": ("pro", "model_new")}),
            patch.object(server, "get_client", return_value=self.fake),
            patch.object(server, "_remaining_notice", return_value=""),
            patch.object(server, "_session_store"),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    async def search(self, query, mode, preference, *_args, **_kwargs):
        self.calls.append((mode, preference))
        yield {"delta": "answer", "answer": "answer", "actual_model": preference, "done": True}

    async def test_models_endpoint_lists_resolvable_picker_ids(self):
        result = await self.client.get("/v1/models", headers={"Authorization": "Bearer secret"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("model_new", {item["id"] for item in result.json()["data"]})

    async def test_discovered_id_invokes_its_exact_internal_identifier(self):
        result = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret"}, json={
            "model": "model_new", "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.calls, [("pro", "model_new")])

    async def test_unknown_model_fails_and_admin_refresh_requires_auth(self):
        unknown = await self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret"}, json={
            "model": "unknown", "messages": [{"role": "user", "content": "hello"}],
        })
        self.assertEqual(unknown.status_code, 400)
        with patch.object(server, "_refresh_live_model_catalogue", return_value={"status": "ok"}):
            unauthorized = await self.client.post("/admin/discover-models")
            authorized = await self.client.post("/admin/discover-models", headers={"Authorization": "Bearer secret"})
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)

    async def test_discovery_auth_failure_and_timeout_keep_previous_catalogue(self):
        class Response:
            def __init__(self, code=200, payload=None):
                self.status_code = code
                self.payload = payload or {"user": {"subscription_tier": "pro", "subscription_status": "active"}}
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError("upstream request failed")
            def json(self):
                return self.payload

        class Session:
            def __init__(self, error=None):
                self.error = error
                self.cookies = {}
            async def get(self, *_args, **_kwargs):
                if self.error:
                    raise self.error
                return Response(401)

        class Client:
            def __init__(self, session):
                self.session = session
            async def init(self):
                return None

        old_models = dict(server.MODEL_REGISTRY.models)
        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=Client(Session())):
            auth_result = await server._refresh_live_model_catalogue(force=True)
        self.assertEqual(auth_result["status"], "error")
        self.assertEqual(server.MODEL_REGISTRY.models, old_models)
        with patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=Client(Session(TimeoutError()))):
            timeout_result = await server._refresh_live_model_catalogue(force=True)
        self.assertEqual(timeout_result["status"], "error")
        self.assertEqual(server.MODEL_REGISTRY.models, old_models)


class AccountCacheIsolationTests(unittest.TestCase):
    def test_cache_isolation_uses_hashed_account_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fallback = {"legacy": {"mode": "pro", "pref": "old", "source": "fallback"},
                        "auto": {"mode": "pro", "pref": "pplx_pro", "source": "fallback"}}
            fake = lambda token: type("Client", (), {"cookies": {"__Secure-next-auth.session-token": token}})()
            with patch.object(server, "MODELS_FILE", root / ".models.json"), \
                 patch.object(server, "MODEL_MAP", {"legacy": ("pro", "old"), "auto": ("pro", "pplx_pro")}), \
                 patch.object(server, "MODEL_REGISTRY", ModelRegistry(root / "pending.json", fallback)), \
                 patch.object(server, "_THINKING_MAP", dict(server._THINKING_MAP)), \
                 patch.object(server, "DEFAULT_MODEL", "auto"), \
                 patch.object(server, "ACCOUNT_TYPE", "pro"):
                server._activate_account_model_cache(fake("account-one-secret"))
                first_path = server.MODEL_REGISTRY.cache_path
                server.MODEL_REGISTRY.refresh(catalogue(), "pro", force=True)
                server._install_discovered_models({k: v for k, v in server.MODEL_REGISTRY.models.items() if v.get("source") == "discovered"})
                server._activate_account_model_cache(fake("account-two-secret"))
                second_path = server.MODEL_REGISTRY.cache_path
                self.assertNotEqual(first_path, second_path)
                self.assertNotIn("model_new", server.MODEL_MAP)
                server._activate_account_model_cache(fake("account-one-secret"))
                self.assertEqual(server.MODEL_REGISTRY.source, "cache")
                self.assertIn("model_new", server.MODEL_MAP)
                self.assertNotIn("account-one-secret", str(first_path))

    def test_generic_alias_tracks_latest_verified_picker_and_thinking_pair(self):
        fallback = {
            "gpt": {"mode": "pro", "pref": "old_gpt", "label": "GPT-5.6 Terra", "source": "fallback"},
            "gpt-5.6-terra": {"mode": "pro", "pref": "old_gpt", "label": "GPT-5.6 Terra", "source": "fallback"},
            "auto": {"mode": "pro", "pref": "pplx_pro", "label": "Perplexity Best", "source": "fallback"},
        }
        picker = {
            "old_gpt": {"mode": "pro", "pref": "old_gpt", "label": "GPT-5.6 Terra", "source": "discovered", "thinking": False},
            "gpt6_1_sol": {"mode": "pro", "pref": "gpt6_1_sol", "label": "GPT-6.1 Sol", "source": "discovered", "thinking": False, "thinking_pref": "gpt6_1_sol_thinking"},
            "gpt6_1_sol_thinking": {"mode": "pro", "pref": "gpt6_1_sol_thinking", "label": "GPT-6.1 Sol Thinking", "source": "discovered", "thinking": True},
        }
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(server, "MODEL_MAP", {key: (entry["mode"], entry["pref"]) for key, entry in fallback.items()}), \
             patch.object(server, "MODEL_REGISTRY", ModelRegistry(Path(folder) / "cache.json", fallback)), \
             patch.object(server, "_THINKING_MAP", dict(server._THINKING_MAP)), \
             patch.object(server, "DEFAULT_MODEL", "gpt"), \
             patch.object(server, "ACCOUNT_TYPE", "pro"):
            server.MODEL_REGISTRY.models.update(picker)
            server._install_discovered_models(picker)
            self.assertEqual(server.MODEL_MAP["gpt"], ("pro", "gpt6_1_sol"))
            self.assertEqual(server.MODEL_MAP["gpt-5.6-terra"], ("pro", "old_gpt"))
            self.assertEqual(server._thinking_model_entry("gpt", server.MODEL_MAP["gpt"]), ("pro", "gpt6_1_sol_thinking"))
            self.assertEqual(server.MODEL_REGISTRY.models["gpt"]["source"], "configured_alias")


class ComprehensiveDiscoveryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")
        self.orig_models = dict(server.MODEL_REGISTRY.models)
        self.orig_model_map = dict(server.MODEL_MAP)
        self.orig_thinking_map = dict(server._THINKING_MAP)

    async def asyncTearDown(self):
        await self.client.aclose()
        server.MODEL_REGISTRY.models = dict(self.orig_models)
        server.MODEL_MAP = dict(self.orig_model_map)
        server._THINKING_MAP = dict(self.orig_thinking_map)

    async def test_successful_discovery_and_v1_models_exposure_and_aliases_dedup(self):
        test_catalogue = {
            "config_schema": "v2",
            "models": {
                "claude55sonnet": {"label": "Claude Sonnet 5.5", "mode": "search"},
                "claude55sonnetthinking": {"label": "Claude Sonnet 5.5 Thinking", "mode": "search"},
                "gpt56_terra": {"label": "GPT-5.6 Terra", "mode": "search"},
            },
            "search_config": [
                {
                    "label": "Claude Sonnet 5.5",
                    "subscription_tier": "pro",
                    "non_reasoning_model": "claude55sonnet",
                    "reasoning_model": "claude55sonnetthinking",
                },
                {
                    "label": "GPT-5.6 Terra",
                    "subscription_tier": "pro",
                    "non_reasoning_model": "gpt56_terra",
                    "reasoning_model": None,
                },
            ],
        }

        class FakeResponse:
            def __init__(self, payload, code=200, headers=None, url="https://www.perplexity.ai/api/auth/session"):
                self._payload = payload
                self.status_code = code
                self.headers = headers or {"content-type": "application/json"}
                self.url = url
            def raise_for_status(self):
                if self.status_code >= 400:
                    err = server.cffi_requests.errors.RequestsError(f"HTTP {self.status_code}")
                    err.response = self
                    raise err
            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.cookies = {}
            async def get(self, url="", *_args, **_kwargs):
                if "auth/session" in url:
                    return FakeResponse({"user": {"subscription_tier": "pro", "subscription_status": "active"}}, url=url)
                if "models/config/v2" in url:
                    return FakeResponse(test_catalogue, url=url)
                return FakeResponse({}, 404, url=url)

        class FakeClient:
            def __init__(self):
                self.session = FakeSession()
            async def init(self):
                pass

        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=FakeClient()), \
             patch.object(server, "API_KEY", "secret"):
            res = await server._refresh_live_model_catalogue(force=True)
            self.assertEqual(res["status"], "ok")

            models_resp = await self.client.get("/v1/models", headers={"Authorization": "Bearer secret"})
            self.assertEqual(models_resp.status_code, 200)
            data = models_resp.json()["data"]
            model_ids = {m["id"] for m in data}

            self.assertIn("claude55sonnet", model_ids)
            self.assertIn("gpt56_terra", model_ids)
            self.assertIn("auto", model_ids)
            self.assertIn("sonnet", model_ids)

            sonnet_entry = next(m for m in data if m["id"] == "sonnet")
            self.assertEqual(sonnet_entry["internal_pref"], "claude55sonnet")
            self.assertTrue(sonnet_entry["thinking_available"])

    async def test_auth_failure_sanitized_diagnostics(self):
        class FakeResponse:
            def __init__(self):
                self.status_code = 401
                self.headers = {"content-type": "text/html"}
                self.url = "https://www.perplexity.ai/api/auth/session?session_token=secret123"
                self.text = "<html>Unauthorized</html>"
            def raise_for_status(self):
                err = server.cffi_requests.errors.RequestsError("401 Unauthorized")
                err.response = self
                raise err

        class FakeSession:
            def __init__(self):
                self.cookies = {}
            async def get(self, url="", *_args, **_kwargs):
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.session = FakeSession()
            async def init(self):
                pass

        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=FakeClient()):
            res = await server._refresh_live_model_catalogue(force=True)
            self.assertEqual(res["status"], "error")
            self.assertIn("[auth_session]", res["error"])
            self.assertIn("HTTP 401", res["error"])
            self.assertIn("/api/auth/session", res["error"])
            self.assertNotIn("session_token=secret123", res["error"])

    async def test_http_502_error_sanitized_diagnostics(self):
        class FakeResponse:
            def __init__(self):
                self.status_code = 502
                self.headers = {"content-type": "text/plain"}
                self.url = "https://www.perplexity.ai/rest/models/config/v2"
                self.text = "Bad Gateway error"
            def raise_for_status(self):
                err = server.cffi_requests.errors.RequestsError("502 Bad Gateway")
                err.response = self
                raise err
            def json(self, *args, **kwargs):
                return {}

        class OKResponse:
            def __init__(self, data):
                self.status_code = 200
                self._data = data
            def raise_for_status(self):
                pass
            def json(self, *args, **kwargs):
                return self._data

        class FakeSession:
            def __init__(self):
                self.cookies = {}
            async def get(self, url="", *_args, **_kwargs):
                if "auth/session" in url:
                    return OKResponse({"user": {"subscription_tier": "pro", "subscription_status": "active"}})
                return FakeResponse()

        class FakeClient:
            def __init__(self):
                self.session = FakeSession()
            async def init(self):
                pass

        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=FakeClient()):
            res = await server._refresh_live_model_catalogue(force=True)
            self.assertEqual(res["status"], "error")
            self.assertIn("[model_config]", res["error"])
            self.assertIn("HTTP 502", res["error"])
            self.assertIn("Bad Gateway", res["error"])

    async def test_malformed_upstream_response(self):
        class OKResponse:
            def __init__(self, data):
                self.status_code = 200
                self._data = data
            def raise_for_status(self):
                pass
            def json(self, *args, **kwargs):
                return self._data

        class FakeSession:
            def __init__(self):
                self.cookies = {}
            async def get(self, url="", *_args, **_kwargs):
                if "auth/session" in url:
                    return OKResponse({"user": {"subscription_tier": "pro", "subscription_status": "active"}})
                return OKResponse({"config_schema": "invalid"})

        class FakeClient:
            def __init__(self):
                self.session = FakeSession()
            async def init(self):
                pass

        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=FakeClient()):
            res = await server._refresh_live_model_catalogue(force=True)
            self.assertEqual(res["status"], "error")
            self.assertIn("DiscoveryError", res["error"])

    async def test_temporary_failure_retains_last_known_good_catalogue(self):
        test_catalogue = {
            "config_schema": "v2",
            "models": {"m1": {"label": "M1", "mode": "search"}},
            "search_config": [{"label": "M1", "subscription_tier": "pro", "non_reasoning_model": "m1", "reasoning_model": None}],
        }
        class OKResponse:
            def __init__(self, data):
                self.status_code = 200
                self._data = data
            def raise_for_status(self):
                pass
            def json(self, *args, **kwargs):
                return self._data

        class GoodSession:
            def __init__(self):
                self.cookies = {}
            async def get(self, url="", *_args, **_kwargs):
                if "auth/session" in url:
                    return OKResponse({"user": {"subscription_tier": "pro", "subscription_status": "active"}})
                return OKResponse(test_catalogue)

        class BadSession:
            def __init__(self):
                self.cookies = {}
            async def get(self, url="", *_args, **_kwargs):
                raise RuntimeError("Network drop")

        class FakeClient:
            def __init__(self, session):
                self.session = session
            async def init(self):
                pass

        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=FakeClient(GoodSession())):
            good_res = await server._refresh_live_model_catalogue(force=True)
            self.assertEqual(good_res["status"], "ok")
            self.assertIn("m1", server.MODEL_MAP)

        with patch.object(server, "PROBE_DISCOVERY_ENABLED", True), \
             patch.object(server, "_MODEL_REFRESH_LOCK", __import__("asyncio").Lock()), \
             patch.object(server, "get_client", return_value=FakeClient(BadSession())):
            bad_res = await server._refresh_live_model_catalogue(force=True)
            self.assertEqual(bad_res["status"], "error")
            self.assertIn("m1", server.MODEL_MAP)
            self.assertEqual(server.MODEL_REGISTRY.source, "live")

    async def test_health_reports_discovery_status_and_counts(self):
        health_resp = await self.client.get("/health")
        self.assertEqual(health_resp.status_code, 200)
        disc_info = health_resp.json()["model_discovery"]
        self.assertIn("source", disc_info)
        self.assertIn("last_success", disc_info)
        self.assertIn("last_attempt", disc_info)
        self.assertIn("last_error", disc_info)
        self.assertIn("model_count", disc_info)
        self.assertGreater(disc_info["model_count"], 0)


if __name__ == "__main__":
    unittest.main()
