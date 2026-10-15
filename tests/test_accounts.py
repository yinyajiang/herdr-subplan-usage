from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from subplan.display import dock_lines, text_view
from subplan.model import UsageError, complete, window
from subplan.providers import codex_usage
from subplan.service import collect
from subplan.storage import (PROVIDERS, account_configs, atomic_json, expand_account_path,
                             load_config, remove_account, save_account)
from subplan.ui import DockUI, InputDecoder


def base_config():
    return {"timeout_seconds": 5, **{p: {"enabled": p == "codex"} for p in PROVIDERS}}


class AccountTests(unittest.TestCase):
    def test_legacy_config_and_add_edit_delete_preserve_existing_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            path = Path(directory) / "config.json"
            atomic_json(path, {"zhipu": {"api_key": "original-private-key"}, "custom": 7})
            self.assertEqual(len(account_configs(load_config())), 4)
            save_account("codex2", "codex", {"codex_home": "$HOME/.codex2", "name": "Work"})
            save_account("codex2", "codex", {"name": "Second"})
            save_account("codex2", "codex", {"name": "Second", "enabled": False})
            config = load_config()
            self.assertEqual(config["accounts"][0]["codex_home"], "$HOME/.codex2")
            self.assertEqual(config["accounts"][0]["name"], "Second")
            self.assertEqual(config["zhipu"]["api_key"], "original-private-key")
            self.assertEqual(config["custom"], 7)
            remove_account("codex2")
            self.assertEqual(load_config()["accounts"], [])
            with self.assertRaises(UsageError):
                remove_account("codex")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_account_ids_types_and_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            path = Path(directory) / "config.json"
            account = {"id": "second", "provider": "codex", "codex_home": "~/.codex2"}
            for accounts in ("bad", [account, account], [{**account, "id": "codex"}], [{**account, "provider": "invalid"}], [{**account, "name": "bad\x1b[31m"}], [{**account, "codex_home": []}]):
                atomic_json(path, {"accounts": accounts})
                with self.assertRaises(UsageError):
                    load_config()

    def test_home_paths_expand_without_executing_shell_syntax(self):
        with tempfile.TemporaryDirectory() as directory, patch("subplan.storage.Path.home", return_value=Path(directory)):
            for value in ("~/.codex2", "$HOME/.codex2", "${HOME}/.codex2", ".codex2"):
                self.assertEqual(expand_account_path(value), Path(directory) / ".codex2")
            self.assertIn("$(touch", str(expand_account_path("$(touch should-not-exist)")))

    def test_parallel_codex_children_get_distinct_homes_without_mutating_parent(self):
        script = '''import json,os,sys
for line in sys.stdin:
    value=json.loads(line)
    if value['method']=='initialize':
        print(json.dumps({'id':value['id'],'result':{}}),flush=True)
    elif value['method']=='account/rateLimits/read':
        used=10 if os.environ['CODEX_HOME'].endswith('one') else 80
        print(json.dumps({'id':value['id'],'result':{'rateLimits':{'primary':{'usedPercent':used,'windowDurationMins':300}}}}),flush=True)
'''
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODEX_HOME": "parent-home"}):
            homes = [Path(directory) / "one", Path(directory) / "two"]
            for home in homes:
                home.mkdir()
            real_popen = subprocess.Popen
            processes = []
            def spawn(argv, **kwargs):
                process = real_popen([sys.executable, "-c", script], **kwargs)
                processes.append(process)
                return process
            with patch("subplan.providers.subprocess.Popen", side_effect=spawn), ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda home: codex_usage("codex", 5, str(home)), homes))
            self.assertEqual([r["windows"]["5h"]["remaining_percent"] for r in results], [90, 20])
            self.assertEqual(os.environ["CODEX_HOME"], "parent-home")
            self.assertTrue(all(process.poll() is not None for process in processes))

    def test_missing_explicit_codex_home_does_not_fall_back_to_default(self):
        with tempfile.TemporaryDirectory() as directory, patch("subplan.providers.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(UsageError, "目录不存在"):
                codex_usage("codex", 5, str(Path(directory) / "missing"))
            spawn.assert_not_called()

    def test_account_results_and_failed_caches_do_not_mix(self):
        config = base_config()
        config["accounts"] = [{"id": "codex2", "provider": "codex", "name": "Second", "codex_home": "~/.codex2"}]
        def fetch(provider, settings, timeout):
            return complete(provider, {"5h": window(80 if settings.get("codex_home") else 10)})
        first = collect(config, fetcher=fetch)
        self.assertEqual(first["providers"]["codex"]["windows"]["5h"]["remaining_percent"], 90)
        self.assertEqual(first["providers"]["codex2"]["windows"]["5h"]["remaining_percent"], 20)
        def fail_second(provider, settings, timeout):
            if settings.get("codex_home"):
                raise UsageError("网络不可用")
            return fetch(provider, settings, timeout)
        saved = collect(config, first["providers"], fetcher=fail_second)
        self.assertEqual(saved["providers"]["codex2"]["status"], "stale")
        self.assertEqual(saved["providers"]["codex2"]["windows"]["5h"]["remaining_percent"], 20)
        config["accounts"][0]["codex_home"] = "~/.different-user"
        changed = collect(config, first["providers"], fetcher=fail_second)
        self.assertEqual(changed["providers"]["codex2"]["status"], "error")
        self.assertEqual(changed["providers"]["codex2"]["windows"], {})
        self.assertNotIn(".codex2", json.dumps(first))

    def test_extra_keys_do_not_use_default_environment_and_disabled_accounts_skip_fetch(self):
        config = base_config()
        config["accounts"] = [{"id": "kimi2", "provider": "kimi"}, {"id": "codex2", "provider": "codex", "enabled": False}]
        fetch = Mock(side_effect=lambda provider, settings, timeout: complete(provider, {}))
        snapshot = collect(config, fetcher=fetch)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(snapshot["providers"]["kimi2"]["status"], "error")
        self.assertEqual(snapshot["providers"]["codex2"]["status"], "disabled")

    def test_only_additional_account_can_be_enabled(self):
        config = {"timeout_seconds": 5, **{p: {"enabled": False} for p in PROVIDERS}}
        config["accounts"] = [{"id": "kimi2", "provider": "kimi", "api_key": "second-key", "name": "Second"}]
        fetch = Mock(side_effect=lambda provider, settings, timeout: complete(provider, {"5h": window(20)}))
        snapshot = collect(config, fetcher=fetch)
        fetch.assert_called_once_with("kimi", config["accounts"][0], 5)
        self.assertIn("Kimi CN · Second", text_view(snapshot))
        self.assertNotIn("second-key", json.dumps(snapshot))

    def test_multiple_provider_accounts_render_labels_and_cursor_api(self):
        snapshot = {"providers": {
            "codex": complete("codex", {"5h": window(10)}),
            "codex2": {**complete("codex", {"5h": window(80)}), "name": "Work"},
            "cursor2": {**complete("cursor", {"month": {"remaining_percent": 60, "api_remaining_percent": 97, "resets_at": None}}), "name": "Cursor 2"},
        }}
        for output in (text_view(snapshot), "\n".join(dock_lines(snapshot, 40, 100))):
            self.assertIn("Codex · Work", output)
            self.assertIn("5h 90%", output)
            self.assertIn("5h 20%", output)
            self.assertIn("Cursor 2", output)
            self.assertIn("api 97%", output)

    def test_ui_add_codex_edit_home_save_and_delete(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            path = Path(directory) / "config.json"
            atomic_json(path, {"kimi": {"api_key": "original-private-key"}})
            request = Mock()
            ui = DockUI(Mock(), request)
            ui.activate("settings")
            ui.activate("add")
            ui.activate("new:codex")
            ui.activate("field:codex_home")
            ui.event(("paste", "$HOME/.codex2"))
            self.assertIn("$HOME/.codex2", ui.frame({}, 40, 30))
            ui.event(("key", "enter"))
            ui.activate("save-account")
            saved = load_config()
            self.assertEqual(saved["accounts"][0]["codex_home"], "$HOME/.codex2")
            self.assertEqual(saved["accounts"][0]["name"], "Codex 2")
            self.assertEqual(saved["kimi"]["api_key"], "original-private-key")
            request.assert_called_once()
            ui.activate("account:codex-2")
            ui.activate("delete")
            ui.activate("keep-account")
            self.assertEqual(len(load_config()["accounts"]), 1)
            ui.activate("delete")
            ui.activate("confirm-delete")
            self.assertEqual(load_config()["accounts"], [])

    def test_ui_new_key_is_masked_and_cancel_does_not_add_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            ui = DockUI(Mock(), Mock())
            ui.activate("new:kimi")
            ui.activate("field:api_key")
            ui.event(("paste", "private-second-key"))
            self.assertNotIn("private-second-key", ui.frame({}, 40, 30))
            ui.event(("key", "enter"))
            self.assertNotIn("private-second-key", ui.frame({}, 40, 30))
            ui.activate("settings")
            self.assertFalse(Path(directory, "config.json").exists())

    def test_plaintext_path_editor_rejects_terminal_control_sequences(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            ui = DockUI(Mock(), Mock())
            ui.activate("new:codex")
            ui.activate("field:codex_home")
            ui.event(("paste", "~/.codex2\x1b[31m"))
            self.assertEqual(ui.secret, "")
            self.assertIn("控制字符", ui.message)
            ui.activate("new:codex")
            ui.activate("save-account")
            self.assertIn("CODEX_HOME", ui.message)
            self.assertFalse(Path(directory, "config.json").exists())

    def test_settings_paginate_and_usage_scrolls_to_later_accounts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            config = base_config()
            config["accounts"] = [{"id": f"codex{i}", "provider": "codex", "name": f"Codex {i}", "codex_home": f"~/.codex{i}"} for i in range(2, 10)]
            atomic_json(Path(directory) / "config.json", config)
            ui = DockUI(Mock(), Mock())
            ui.activate("settings")
            ui.frame({}, 32, 20)
            self.assertTrue(any(hit[0] == "next-page" for hit in ui.hits))
            ui.activate("next-page")
            self.assertIn("Codex 2", ui.frame({}, 32, 20))
            ui.activate("back")
            snapshot = collect(config, fetcher=lambda provider, settings, timeout: complete(provider, {"5h": window(10)}))
            self.assertIn("滚动", ui.frame(snapshot, 32, 18))
            for _ in range(20):
                ui.event(("key", "down"))
                frame = ui.frame(snapshot, 32, 18)
            self.assertIn("Codex 9", re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", frame))
            self.assertEqual(InputDecoder().feed("\x1b[<65;2;3M"), [("key", "down")])


if __name__ == "__main__":
    unittest.main()
