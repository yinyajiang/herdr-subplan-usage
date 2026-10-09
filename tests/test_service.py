import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from subplan.display import percent, dock_lines, text_view
from subplan.model import UsageError, complete
from subplan.service import collect, failed
from subplan.storage import PROVIDERS, atomic_json, load_config, lock


def config():
    return {"timeout_seconds": 5, **{provider: {} for provider in PROVIDERS}}


class ServiceTests(unittest.TestCase):
    def test_independent_providers_and_safe_errors(self):
        def fetch(provider, *_):
            if provider == "zhipu":
                raise UsageError("请配置 Coding Plan Key")
            if provider == "kimi":
                raise ValueError("secret-key-here")
            return complete(provider, {})

        snapshot = collect(config(), fetcher=fetch)
        self.assertEqual(snapshot["providers"]["zhipu"]["status"], "error")
        self.assertEqual(snapshot["providers"]["codex"]["status"], "unavailable")
        self.assertNotIn("secret-key-here", json.dumps(snapshot))

    def test_stale_cache_retains_age_and_discards_expired_windows(self):
        previous = {"kimi": {"status": "ok", "updated_at": 900, "windows": {
            "5h": {"remaining_percent": 80, "resets_at": 950},
            "7d": {"remaining_percent": 90, "resets_at": 2000}}}}
        saved = failed("kimi", "网络不可用", previous, 1000)
        self.assertEqual(saved["status"], "stale")
        self.assertEqual(saved["updated_at"], 900)
        self.assertIsNone(saved["windows"]["5h"]["remaining_percent"])
        self.assertEqual(saved["windows"]["7d"]["remaining_percent"], 90)
        self.assertEqual(failed("kimi", "网络不可用", previous, 2000)["status"], "error")

    def test_disabled_never_reads_credentials(self):
        settings = config()
        settings["cursor"]["enabled"] = False

        def fetch(provider, *_):
            self.assertNotEqual(provider, "cursor")
            return complete(provider, {})
        self.assertEqual(collect(settings, fetcher=fetch)["providers"]["cursor"]["status"], "disabled")

    def test_formatting_unknown_low_and_stale(self):
        self.assertEqual(percent(None), "—")
        self.assertEqual(percent(0.4), "0.4%")
        self.assertEqual(percent(99.6), "99.6%")
        snapshot = {"providers": {"codex": {"status": "stale", "updated_at": 0, "windows": {
            "5h": {"remaining_percent": 20, "resets_at": None}}}}}
        self.assertIn("~20%", text_view(snapshot))
        self.assertIn("缓存", text_view(snapshot))
        self.assertIn("重置未知", text_view(snapshot))

    def test_config_validation_and_atomic_private_storage(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            path = Path(directory) / "config.json"
            atomic_json(path, {"refresh_seconds": 0})
            with self.assertRaises(UsageError):
                load_config()
            atomic_json(path, {"kimi": {"enabled": "false"}})
            with self.assertRaises(UsageError):
                load_config()
            atomic_json(path, {})
            self.assertEqual(load_config()["refresh_seconds"], 60)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with lock(Path(directory) / "worker.lock") as first:
                self.assertTrue(first)
                with lock(Path(directory) / "worker.lock") as second:
                    self.assertFalse(second)

    def test_cursor_dock_shows_monthly_spend_and_reset(self):
        snapshot = {"providers": {"cursor": {"status": "ok", "updated_at": time.time(), "windows": {
            "month": {"remaining_percent": 75, "resets_at": None, "used_amount": 5, "limit_amount": 20}}}}}
        lines = "\n".join(dock_lines(snapshot, 35, 30))
        self.assertIn("已用 $5.00/$20.00", lines)
        self.assertIn("30d 75%", lines)

    def test_cursor_percentage_display_and_expired_cache(self):
        entry = {"status": "ok", "updated_at": 900, "windows": {"month": {
            "quota_source": "percentage", "remaining_percent": 64.4488, "used_percent": 35.5512,
            "api_remaining_percent": 97.2444,
            "resets_at": 1100, "used_amount": None, "limit_amount": None}}}
        snapshot = {"providers": {"cursor": entry}}
        with patch("subplan.display.time.time", return_value=1000):
            lines = "\n".join(dock_lines(snapshot, 40, 30))
            self.assertIn("30d 64%", lines)
            self.assertIn("api 97%", lines)
            self.assertNotIn("Included", lines)
            self.assertNotIn("$20.00", lines)
        saved = failed("cursor", "网络不可用", {"cursor": entry}, 1200)
        self.assertIsNone(saved["windows"]["month"]["remaining_percent"])
        self.assertIsNone(saved["windows"]["month"]["used_percent"])
        self.assertIsNone(saved["windows"]["month"]["api_remaining_percent"])
        with patch("subplan.display.time.time", return_value=1200):
            lines = "\n".join(dock_lines(snapshot, 40, 30))
            self.assertIn("api ~—", lines)


if __name__ == "__main__":
    unittest.main()
