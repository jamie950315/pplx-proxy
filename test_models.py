import asyncio
import unittest
from unittest.mock import patch

import server


class ModelRegistryTests(unittest.TestCase):
    def test_default_pro_models_include_new_names(self):
        with patch.object(server, "ACCOUNT_TYPE", "pro"):
            model_map=server._default_model_map()
        for model_id in [
            "gpt",
            "gpt-5.4",
            "gpt-mini",
            "gpt-nano",
            "sonnet",
            "gemini",
            "gemini-flash",
            "grok",
            "grok-reasoning",
            "grok-non-reasoning",
            "nemotron",
        ]:
            self.assertIn(model_id, model_map)
        self.assertNotIn("opus", model_map)
        self.assertNotIn("grok-multi", model_map)
        self.assertNotIn("haiku", model_map)
        self.assertNotIn("gemini-flash-lite", model_map)

    def test_max_tier_includes_max_only_models(self):
        with patch.object(server, "ACCOUNT_TYPE", "max"):
            model_map=server._default_model_map()
        self.assertIn("opus", model_map)
        self.assertEqual(model_map["opus"], ("pro", "claude47opus"))
        self.assertIn("opus-4.6", model_map)
        self.assertNotIn("grok-multi", model_map)

    def test_thinking_map_tracks_latest_defaults(self):
        self.assertEqual(server._THINKING_MAP["gpt"], ("pro", "gpt55_thinking"))
        self.assertEqual(server._THINKING_MAP["gpt-5.4"], ("pro", "gpt54_thinking"))
        self.assertEqual(server._THINKING_MAP["opus"], ("pro", "claude47opusthinking"))

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
            return pref in {"claude46sonnet", "grok4", "claude45haiku"}

        async def no_sleep(_seconds):
            return None

        async def run():
            with patch.object(server, "ACCOUNT_TYPE", "pro"), \
                 patch.object(server, "MODEL_MAP", dict(old_map)), \
                 patch.object(server, "probe_model", fake_probe), \
                 patch.object(server.asyncio, "sleep", new=no_sleep):
                changed=await server._discover_known_missing_models(object(), report, sleep_seconds=0)
                self.assertTrue(changed)
                self.assertEqual(server.MODEL_MAP["sonnet"], ("pro", "claude46sonnet"))
                self.assertEqual(server.MODEL_MAP["grok"], ("pro", "grok4"))
                self.assertEqual(server.MODEL_MAP["haiku"], ("pro", "claude45haiku"))
                self.assertIn("sonnet", report["added"])
                self.assertIn("grok", report["added"])
                self.assertIn("haiku", report["added"])
                self.assertNotIn("opus", server.MODEL_MAP)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
