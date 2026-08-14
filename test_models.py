import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class ModelRegistryTests(unittest.TestCase):
    def test_default_pro_models_include_new_names(self):
        with patch.object(server, "ACCOUNT_TYPE", "pro"):
            model_map=server._default_model_map()
        for model_id in [
            "gpt",
            "gpt-5.6-terra",
            "gpt-5.4",
            "gpt-mini",
            "gpt-nano",
            "sonnet",
            "sonnet-5",
            "gemini",
            "gemini-flash",
            "grok",
            "grok-4.5",
            "grok-reasoning",
            "grok-non-reasoning",
            "nemotron",
            "glm-5.2",
            "kimi-k2.6",
        ]:
            self.assertIn(model_id, model_map)
        self.assertNotIn("opus", model_map)
        self.assertNotIn("gpt-5.6-sol", model_map)
        self.assertNotIn("grok-multi", model_map)
        self.assertNotIn("haiku", model_map)
        self.assertNotIn("gemini-flash-lite", model_map)

    def test_max_tier_includes_max_only_models(self):
        with patch.object(server, "ACCOUNT_TYPE", "max"):
            model_map=server._default_model_map()
        self.assertIn("opus", model_map)
        self.assertEqual(model_map["opus"], ("pro", "claude48opus"))
        self.assertIn("opus-4.6", model_map)
        self.assertIn("gpt-5.6-sol", model_map)
        self.assertNotIn("grok-multi", model_map)

    def test_thinking_map_tracks_latest_defaults(self):
        self.assertEqual(server._THINKING_MAP["gpt"], ("pro", "gpt56_terra_thinking"))
        self.assertEqual(server._THINKING_MAP["gpt-5.4"], ("pro", "gpt54_thinking"))
        self.assertEqual(server._THINKING_MAP["sonnet"], ("pro", "claude50sonnetthinking"))
        self.assertEqual(server._THINKING_MAP["grok"], ("pro", "grok45medium"))
        self.assertEqual(server._THINKING_MAP["opus"], ("pro", "claude48opusthinking"))

    def test_tier_error_uses_model_minimum_tier(self):
        with patch.object(server, "ACCOUNT_TYPE", "free"):
            self.assertIn("requires pro", server.check_tier("sonnet"))
            self.assertIn("requires max", server.check_tier("opus"))
        with patch.object(server, "ACCOUNT_TYPE", "pro"):
            self.assertIn("tracked as a candidate", server.check_tier("haiku"))

    def test_grok_420_version_increment_uses_two_digit_minor(self):
        self.assertEqual(server._increment_version(4, 20, 2), (4, 21))
        self.assertAlmostEqual(server._version_distance(4, 20, 4, 21, 2), 0.01)


class DiscoveryTests(unittest.TestCase):
    def test_known_missing_models_are_added_when_probe_passes(self):
        old_map={"auto": ("pro", "pplx_pro"), "gpt": ("pro", "gpt54")}
        report={"added": {}, "unavailable": [], "probed": 0}

        async def fake_probe(_client, pref):
            return pref in {"claude50sonnet", "grok45low", "claude45haiku"}

        async def no_sleep(_seconds):
            return None

        async def run():
            with patch.object(server, "ACCOUNT_TYPE", "pro"), \
                 patch.object(server, "MODEL_MAP", dict(old_map)), \
                 patch.object(server, "probe_model", fake_probe), \
                 patch.object(server.asyncio, "sleep", new=no_sleep):
                changed=await server._discover_known_missing_models(object(), report, sleep_seconds=0)
                self.assertTrue(changed)
                self.assertEqual(server.MODEL_MAP["sonnet"], ("pro", "claude50sonnet"))
                self.assertEqual(server.MODEL_MAP["grok"], ("pro", "grok45low"))
                self.assertEqual(server.MODEL_MAP["haiku"], ("pro", "claude45haiku"))
                self.assertIn("sonnet", report["added"])
                self.assertIn("grok", report["added"])
                self.assertIn("haiku", report["added"])
                self.assertNotIn("opus", server.MODEL_MAP)

        asyncio.run(run())


class ResponseParsingTests(unittest.TestCase):
    def test_ask_text_blocks_produce_answer_deltas(self):
        class FakeResponse:
            status_code=200

            async def aiter_lines(self, delimiter):
                for payload in [
                    {"blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["Four"]}}]},
                    {"blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "DONE", "chunks": ["Four"]}}]},
                ]:
                    yield f"event: message\r\ndata: {json.dumps(payload)}"
                yield "event: end_of_stream"

        class FakeSession:
            async def post(self, *args, **kwargs):
                return FakeResponse()

        async def run():
            client=server.PerplexityClient({})
            client.session=FakeSession()
            client._initialized=True
            chunks=[chunk async for chunk in client.search("2+2", "pro", "gpt56_terra")]
            self.assertEqual(chunks[0]["delta"], "Four")
            self.assertEqual(chunks[-1]["answer"], "Four")
            self.assertTrue(chunks[-1]["done"])

        asyncio.run(run())

    def test_mirrored_answer_blocks_do_not_duplicate_deltas(self):
        class FakeResponse:
            status_code=200

            async def aiter_lines(self, delimiter):
                payloads=[
                    {"blocks": [
                        {"intended_usage": "ask_text_0_markdown", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["A"]}},
                        {"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["AB"]}},
                    ]},
                    {"blocks": [
                        {"intended_usage": "ask_text_0_markdown", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["BC"]}},
                        {"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["C"]}},
                    ]},
                    {"blocks": [
                        {"intended_usage": "ask_text_0_markdown", "markdown_block": {"progress": "DONE", "chunks": ["ABC"]}},
                        {"intended_usage": "ask_text", "markdown_block": {"progress": "DONE", "chunks": ["AB", "C"]}},
                    ]},
                ]
                for payload in payloads:
                    yield f"event: message\r\ndata: {json.dumps(payload)}"
                yield "event: end_of_stream"

        class FakeSession:
            async def post(self, *args, **kwargs):
                return FakeResponse()

        async def run():
            client=server.PerplexityClient({})
            client.session=FakeSession()
            client._initialized=True
            chunks=[chunk async for chunk in client.search("test", "pro", "gpt56_terra")]
            deltas=[chunk["delta"] for chunk in chunks if chunk.get("delta")]
            self.assertEqual(deltas, ["AB", "C"])
            self.assertEqual(chunks[-1]["answer"], "ABC")
            self.assertTrue(chunks[-1]["done"])

        asyncio.run(run())

    def test_legacy_markdown_answer_block_still_streams(self):
        class FakeResponse:
            status_code=200

            async def aiter_lines(self, delimiter):
                for payload in [
                    {"blocks": [{"intended_usage": "ask_text_0_markdown", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["Legacy"]}}]},
                    {"blocks": [{"intended_usage": "ask_text_0_markdown", "markdown_block": {"progress": "DONE", "chunks": ["Legacy"]}}]},
                ]:
                    yield f"event: message\r\ndata: {json.dumps(payload)}"
                yield "event: end_of_stream"

        class FakeSession:
            async def post(self, *args, **kwargs):
                return FakeResponse()

        async def run():
            client=server.PerplexityClient({})
            client.session=FakeSession()
            client._initialized=True
            chunks=[chunk async for chunk in client.search("test", "pro", "gpt56_terra")]
            self.assertEqual(chunks[0]["delta"], "Legacy")
            self.assertEqual(chunks[-1]["answer"], "Legacy")
            self.assertTrue(chunks[-1]["done"])

        asyncio.run(run())

    def test_model_substitution_is_reported_before_completion(self):
        class FakeResponse:
            status_code=200

            async def aiter_lines(self, delimiter):
                for payload in [
                    {
                        "display_model": "claude50sonnet",
                        "blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["Partial"]}}],
                    },
                    {
                        "display_model": "gpt5_nano",
                        "blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": [" answer"]}}],
                    },
                ]:
                    yield f"event: message\r\ndata: {json.dumps(payload)}"
                yield "event: end_of_stream"

        class FakeSession:
            async def post(self, *args, **kwargs):
                return FakeResponse()

        async def run():
            client=server.PerplexityClient({})
            client.session=FakeSession()
            client._initialized=True
            chunks=[chunk async for chunk in client.search("test", "pro", "claude50sonnet")]
            self.assertTrue(chunks[-1].get("model_fallback"))
            self.assertEqual(chunks[-1].get("actual_model"), "gpt5_nano")
            self.assertIn("substituted", chunks[-1].get("error", ""))

        asyncio.run(run())

    def test_small_auxiliary_tail_does_not_discard_selected_model_answer(self):
        selected_answer="The selected model produced the complete substantive answer with enough detail to identify it as the primary generator"

        class FakeResponse:
            status_code=200

            async def aiter_lines(self, delimiter):
                for payload in [
                    {
                        "display_model": "claude50sonnet",
                        "user_selected_model": "claude50sonnet",
                        "blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": [selected_answer]}}],
                    },
                    {
                        "display_model": "gpt5_nano",
                        "user_selected_model": "claude50sonnet",
                        "blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "IN_PROGRESS", "chunks": ["."]}}],
                    },
                    {
                        "display_model": "gpt5_nano",
                        "user_selected_model": "claude50sonnet",
                        "blocks": [{"intended_usage": "ask_text", "markdown_block": {"progress": "DONE", "chunks": [selected_answer + "."]}}],
                    },
                ]:
                    yield f"event: message\r\ndata: {json.dumps(payload)}"
                yield "event: end_of_stream"

        class FakeSession:
            async def post(self, *args, **kwargs):
                return FakeResponse()

        async def run():
            client=server.PerplexityClient({})
            client.session=FakeSession()
            client._initialized=True
            chunks=[chunk async for chunk in client.search("test", "pro", "claude50sonnet")]
            self.assertEqual(chunks[-1]["answer"], selected_answer + ".")
            self.assertEqual(chunks[-1]["actual_model"], "claude50sonnet")
            self.assertFalse(chunks[-1]["model_fallback"])

        asyncio.run(run())


class ModelProbeTests(unittest.TestCase):
    def test_probe_uses_representative_prompt_instead_of_trivial_router_prompt(self):
        class ComplexitySensitiveClient:
            async def search(self, query, _mode, pref, *_args, **_kwargs):
                if "2+2" in query:
                    yield {
                        "answer": "4",
                        "actual_model": "gpt5_nano",
                        "model_fallback": True,
                        "done": True,
                    }
                else:
                    yield {
                        "answer": "A substantive answer from the selected model.",
                        "actual_model": pref,
                        "model_fallback": False,
                        "done": True,
                    }

        self.assertTrue(asyncio.run(server.probe_model(ComplexitySensitiveClient(), "gemini31pro_high")))

    def test_probe_model_rejects_provider_substitution(self):
        class FallbackClient:
            async def search(self, *_args, **_kwargs):
                yield {"answer": "Four", "actual_model": "claude50sonnet", "done": False}
                yield {
                    "answer": "Four",
                    "actual_model": "gpt5_nano",
                    "model_fallback": True,
                    "done": True,
                }

        self.assertFalse(asyncio.run(server.probe_model(FallbackClient(), "claude50sonnet")))

    def test_probe_model_accepts_matching_provider_model(self):
        class MatchingClient:
            async def search(self, *_args, **_kwargs):
                yield {
                    "answer": "Four",
                    "actual_model": "claude50sonnet",
                    "model_fallback": False,
                    "done": True,
                }

        self.assertTrue(asyncio.run(server.probe_model(MatchingClient(), "claude50sonnet")))


class ModelPreflightTests(unittest.TestCase):
    def test_model_preflight_reuses_a_fresh_verification(self):
        calls=[]

        async def verified(_client, pref):
            calls.append(pref)
            return True

        async def run():
            with patch.object(server, "_model_preflight_cache", {}, create=True), \
                 patch.object(server, "probe_model", new=verified):
                self.assertTrue(await server.ensure_model_available(object(), "claude50sonnet"))
                self.assertTrue(await server.ensure_model_available(object(), "claude50sonnet"))
                self.assertEqual(calls, ["claude50sonnet"])

        asyncio.run(run())


class ExplicitModelAvailabilityTests(unittest.TestCase):
    class ChatRequest:
        async def json(self):
            return {
                "model": "sonnet",
                "messages": [{"role": "user", "content": "test"}],
                "stream": False,
            }

    def test_chat_rejects_an_explicit_model_that_fails_preflight(self):
        class FakeClient:
            async def search(self, *_args, **_kwargs):
                yield {"answer": "Wrong model answer", "done": True}

        async def unavailable(_client, _pref):
            return False

        async def run():
            with patch.object(server, "get_model_map", return_value={"sonnet": ("pro", "claude50sonnet")}), \
                 patch.object(server, "get_client", return_value=FakeClient()), \
                 patch.object(server, "ensure_model_available", new=unavailable):
                with self.assertRaises(server.HTTPException) as context:
                    await server.chat_completions(self.ChatRequest())
                self.assertEqual(context.exception.status_code, 503)

        asyncio.run(run())


class SessionKeepaliveTests(unittest.TestCase):
    def test_public_default_ntfy_topic_is_disabled(self):
        self.assertEqual(server._normalize_ntfy_topic("pplx-proxy"), "")
        self.assertEqual(server._normalize_ntfy_topic(""), "")
        self.assertEqual(
            server._normalize_ntfy_topic("pplx-proxy-private-a1b2c3d4"),
            "pplx-proxy-private-a1b2c3d4",
        )

    def test_keepalive_persists_rotated_cookie_for_restart(self):
        class FakeResponse:
            status_code=200

            def json(self):
                return {"user": {"id": "test-user"}}

        class FakeSession:
            cookies={
                "__Secure-next-auth.session-token": "rotated-token",
                "__cf_bm": "refreshed-browser-cookie",
            }

            async def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def run(cache_file):
            client=server.PerplexityClient({"__Secure-next-auth.session-token": "old-token", "existing": "keep"})
            client.session=FakeSession()
            client._initialized=True
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "get_client", return_value=client):
                self.assertTrue(await server.session_keepalive_once())
                self.assertEqual(server.load_cookies()["__Secure-next-auth.session-token"], "rotated-token")
                self.assertEqual(server.load_cookies()["existing"], "keep")
                self.assertEqual(server.load_cookies()["__cf_bm"], "refreshed-browser-cookie")
                cache=json.loads(cache_file.read_text())
                self.assertIn("last_keepalive", cache)

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))

    def test_unauthenticated_keepalive_does_not_overwrite_cookie(self):
        class FakeResponse:
            status_code=200

            def json(self):
                return {}

        class FakeSession:
            cookies={"__Secure-next-auth.session-token": "old-token"}

            async def get(self, *_args, **_kwargs):
                return FakeResponse()

        async def no_notify(_reason):
            return None

        async def run(cache_file):
            client=server.PerplexityClient({"__Secure-next-auth.session-token": "old-token"})
            client.session=FakeSession()
            client._initialized=True
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "get_client", return_value=client), \
                 patch.object(server, "notify_cookie_expired", new=no_notify):
                server.save_cookies(client.cookies)
                self.assertFalse(await server.session_keepalive_once())
                self.assertEqual(server.load_cookies()["__Secure-next-auth.session-token"], "old-token")

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))


class RefreshCookieEndpointTests(unittest.TestCase):
    class JsonRequest:
        headers={"content-type": "application/json"}

        async def json(self):
            return {"session_token": "candidate-token"}

        async def body(self):
            return b""

    def test_refresh_cookie_rejects_token_that_does_not_validate(self):
        async def rejected(_cookies):
            return None

        async def run(cache_file):
            existing={"__Secure-next-auth.session-token": "known-good-token"}
            client=server.PerplexityClient(existing)
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "_client", client), \
                 patch.object(server, "_validate_session_cookies", new=rejected):
                server.save_cookies(existing)
                with self.assertRaises(server.HTTPException) as context:
                    await server.refresh_cookie_endpoint(self.JsonRequest())
                self.assertEqual(context.exception.status_code, 401)
                self.assertEqual(server.load_cookies()["__Secure-next-auth.session-token"], "known-good-token")
                self.assertIs(server._client, client)

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))

    def test_refresh_cookie_reports_success_only_after_validation(self):
        validation_calls=[]

        async def accepted(cookies):
            validation_calls.append(cookies["__Secure-next-auth.session-token"])
            return {"__Secure-next-auth.session-token": "validated-token"}

        async def run(cache_file):
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "_client", None), \
                 patch.object(server, "_validate_session_cookies", new=accepted):
                response=await server.refresh_cookie_endpoint(self.JsonRequest())
                self.assertEqual(response["status"], "ok")
                self.assertIn("validated", response["message"])
                self.assertEqual(validation_calls, ["candidate-token"])
                self.assertEqual(server.load_cookies()["__Secure-next-auth.session-token"], "validated-token")

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))

    def test_refresh_cookie_clears_model_preflight_cache(self):
        async def accepted(cookies):
            return cookies

        async def run(cache_file):
            preflight_cache={"claude50sonnet": (0.0, False)}
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "_client", None), \
                 patch.object(server, "_model_preflight_cache", preflight_cache), \
                 patch.object(server, "_validate_session_cookies", new=accepted):
                await server.refresh_cookie_endpoint(self.JsonRequest())
                self.assertEqual(preflight_cache, {})

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))


class ConfiguredCookieTests(unittest.TestCase):
    def test_reconcile_replaces_cache_when_configured_cookie_is_valid(self):
        configured={"__Secure-next-auth.session-token": "new-token"}

        async def accepted(cookies):
            self.assertEqual(cookies, configured)
            return {"__Secure-next-auth.session-token": "rotated-new-token"}

        async def run(cache_file):
            state={"status": "unchecked", "source": None, "message": None}
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "PPLX_COOKIE", json.dumps(configured)), \
                 patch.object(server, "_client", None), \
                 patch.object(server, "_configured_session_state", state), \
                 patch.object(server, "_validate_session_cookies", new=accepted):
                server.save_cookies({"__Secure-next-auth.session-token": "old-token"})
                self.assertTrue(await server.reconcile_configured_session())
                self.assertEqual(server.load_cookies()["__Secure-next-auth.session-token"], "rotated-new-token")
                self.assertEqual(state["status"], "active")
                self.assertEqual(state["source"], "env")

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))

    def test_reconcile_keeps_cache_when_configured_cookie_is_invalid(self):
        configured={"__Secure-next-auth.session-token": "invalid-token"}

        async def rejected(_cookies):
            return None

        async def run(cache_file):
            state={"status": "unchecked", "source": None, "message": None}
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "PPLX_COOKIE", json.dumps(configured)), \
                 patch.object(server, "_client", None), \
                 patch.object(server, "_configured_session_state", state), \
                 patch.object(server, "_validate_session_cookies", new=rejected):
                server.save_cookies({"__Secure-next-auth.session-token": "known-good-token"})
                self.assertFalse(await server.reconcile_configured_session())
                self.assertEqual(server.load_cookies()["__Secure-next-auth.session-token"], "known-good-token")
                self.assertEqual(state["status"], "invalid")
                self.assertEqual(state["source"], "cache")

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))


class HealthTests(unittest.TestCase):
    def test_health_reports_configured_session_state(self):
        async def run(cache_file):
            state={"status": "invalid", "source": "cache", "message": "Configured session is invalid"}
            rate_limit={"remaining_pro": 10, "remaining_research": 2, "updated_at": server.time.time(), "last_error": None}
            with patch.object(server, "COOKIE_FILE", cache_file), \
                 patch.object(server, "_configured_session_state", state), \
                 patch.object(server, "_rate_limit", rate_limit):
                response=await server.health()
                self.assertEqual(response["configured_session"], state)

        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(Path(temp_dir) / ".cookie_cache.json"))


if __name__ == "__main__":
    unittest.main()
