import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from subplan.model import UsageError
from subplan.storage import atomic_json, save_api_key
from subplan.ui import DockUI, InputDecoder


class UITests(unittest.TestCase):
    def test_escape_timeout_survives_empty_input_polling(self):
        decoder = InputDecoder()
        with patch("subplan.ui.time.monotonic", side_effect=[100, 100.09]):
            self.assertEqual(decoder.feed("\x1b"), [])
            self.assertEqual(decoder.feed(""), [])
            self.assertEqual(decoder.flush_escape(), [("key", "escape")])

    def test_click_hit_regions_support_wide_characters_and_release_is_ignored(self):
        decoder = InputDecoder()
        self.assertEqual(decoder.feed("\x1b[<0;3;"), [])
        self.assertEqual(decoder.feed("1M"), [("click", (3, 1))])
        self.assertEqual(decoder.feed("\x1b[<0;3;1m"), [])
        toggle, refresh = Mock(), Mock()
        ui = DockUI(toggle, refresh)
        ui.frame({}, 40, 45)
        ui.event(("click", (3, 1)))
        self.assertEqual(ui.mode, "settings")
        ui.frame({}, 40, 45)
        collapse = next(h for h in ui.hits if h[0] == "collapse")
        ui.event(("click", (collapse[2], 1)))
        toggle.assert_called_once_with(True)
        self.assertTrue(ui.collapsed)
        frame = ui.frame({}, 15, 45)
        self.assertIn("展开", frame)
        ui.event(("click", (3, 1)))
        self.assertFalse(ui.collapsed)

    def test_masked_editor_paste_save_and_cancel_preserve_other_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            path = Path(directory) / "config.json"
            atomic_json(path, {"kimi": {"api_key": "old-secret"}, "cursor": {"enabled": False}, "custom_option": 7})
            refresh = Mock()
            ui = DockUI(Mock(), refresh)
            ui.activate("edit:kimi")
            self.assertNotIn("old-secret", ui.frame({}, 40, 30))
            decoder = InputDecoder()
            self.assertEqual(decoder.feed("\x1b[200~new-"), [])
            events = decoder.feed("secret\n\x1b[201~")
            for event in events:
                ui.event(event)
            self.assertEqual(ui.secret, "new-secret")
            self.assertNotIn("new-secret", ui.frame({}, 40, 30))
            ui.event(("key", "enter"))
            saved = json.loads(path.read_text())
            self.assertEqual(saved["kimi"]["api_key"], "new-secret")
            self.assertEqual(saved["custom_option"], 7)
            self.assertFalse(saved["cursor"]["enabled"])
            self.assertEqual(ui.secret, "")
            refresh.assert_called_once()
            ui.activate("edit:kimi")
            ui.event(("text", "replacement"))
            ui.event(("key", "escape"))
            self.assertEqual(json.loads(path.read_text())["kimi"]["api_key"], "new-secret")
            self.assertEqual(ui.secret, "")
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cursor_edit_delete_and_clear(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            atomic_json(Path(directory) / "config.json", {"zhipu": {"api_key": "abc"}})
            ui = DockUI(Mock(), Mock())
            ui.activate("edit:zhipu")
            ui.event(("key", "left"))
            ui.event(("text", "d"))
            self.assertEqual(ui.secret, "abdc")
            ui.event(("key", "backspace"))
            ui.event(("key", "delete"))
            self.assertEqual(ui.secret, "ab")
            ui.event(("key", "clear"))
            self.assertEqual(ui.secret, "")

    def test_invalid_key_rejected_without_leaking_its_value(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            path = Path(directory) / "config.json"
            atomic_json(path, {"zhipu": {"api_key": "old"}})
            with self.assertRaises(UsageError) as error:
                save_api_key("zhipu", "secret\nvalue")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(json.loads(path.read_text())["zhipu"]["api_key"], "old")


if __name__ == "__main__":
    unittest.main()
