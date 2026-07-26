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


class SessionKeepaliveTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
