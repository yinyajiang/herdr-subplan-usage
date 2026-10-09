import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from subplan.herdr import enabled, session_dir, watch
from subplan.model import UsageError
from subplan.providers import codex_usage

ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    def test_codex_actual_stdio_handshake_notifications_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_codex.py"
            script.write_text('''import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg["method"] == "initialize":
        assert msg["params"]["clientInfo"]["name"] == "herdr_subplan_usage"
        print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
    elif msg["method"] == "initialized":
        initialized = True
    elif msg["method"] == "account/rateLimits/read":
        assert initialized
        print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
        print(json.dumps({"id": msg["id"], "result": {"rateLimits": {
          "primary": {"usedPercent": 30, "windowDurationMins": 300},
          "secondary": {"usedPercent": 50, "windowDurationMins": 10080}
        }}}), flush=True)
''', encoding="utf-8")
            real_popen = subprocess.Popen

            def spawn(argv, **kwargs):
                self.assertEqual(argv, ["test-codex", "app-server"])
                process = real_popen([sys.executable, str(script)], **kwargs)
                processes.append(process)
                return process

            processes = []
            with patch("subplan.providers.subprocess.Popen", side_effect=spawn):
                parsed = codex_usage("test-codex", 5)
            self.assertEqual(parsed["windows"]["5h"]["remaining_percent"], 70)
            self.assertEqual(parsed["windows"]["7d"]["remaining_percent"], 50)
            self.assertIsNotNone(processes[0].poll())

    def test_codex_hung_process_is_terminated(self):
        real_popen = subprocess.Popen
        processes = []

        def spawn(argv, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            return process

        with patch("subplan.providers.subprocess.Popen", side_effect=spawn):
            with self.assertRaisesRegex(UsageError, "超时"):
                codex_usage("test-codex", 0.1)
        self.assertIsNotNone(processes[0].poll())

    def test_plugin_enabled_uses_json_and_disabling_exits(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake/server.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            with patch("subplan.herdr.call", return_value={"plugins": [
                    {"plugin_id": "herdr-subplan-usage", "enabled": True}]}) as api:
                self.assertTrue(enabled())
                api.assert_called_with(["plugin", "list", "--json"])
            with patch("subplan.herdr.enabled", return_value=False), patch("subplan.herdr.clear") as clear, patch("subplan.dock.close_owned") as close:
                watch()
                self.assertEqual(clear.call_count, 2)
                close.assert_called_once()
            self.assertEqual(json.loads((session_dir() / "health.json").read_text())["status"], "stopped")

    def test_standalone_cli_with_disabled_providers_and_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(json.dumps({name: {"enabled": False}
                                          for name in ("zhipu", "kimi", "codex", "cursor")}))
            env = {**os.environ, "SUBPLAN_CONFIG": str(config), "HERDR_PLUGIN_STATE_DIR": directory}
            process = subprocess.run([sys.executable, str(ROOT / "main.py"), "json"],
                                     env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr)
            parsed = json.loads(process.stdout)
            self.assertTrue(all(item["status"] == "disabled" for item in parsed["providers"].values()))
            self.assertEqual(parsed, json.loads((Path(directory) / "usage.json").read_text()))


if __name__ == "__main__":
    unittest.main()
