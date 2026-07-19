import asyncio
import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
