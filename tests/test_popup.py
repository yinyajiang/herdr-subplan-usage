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

from subplan.herdr import session_dir
from subplan.model import UsageError
from subplan.popup import begin, close_owned, ensure, finish, heartbeat, is_visible
from subplan.storage import atomic_json, read_json
from subplan.ui import DockUI

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = "a" * 32


class PopupTests(unittest.TestCase):
    def test_only_explicit_show_opens_manifest_popup_and_repeated_show_reuses_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}), patch("subplan.popup.call", return_value={}) as api:
            self.assertIsNone(ensure())
            api.assert_not_called()
            identity = ensure(reveal=True)
            self.assertEqual(ensure(reveal=True), identity)
            api.assert_called_once()
            args = api.call_args.args[0]
            self.assertEqual(args[:3], ["plugin", "pane", "open"])
            self.assertNotIn("--placement", args)
            self.assertNotIn("--target-pane", args)
            self.assertNotIn("--direction", args)
            self.assertIn("SUBPLAN_POPUP_ID=" + identity, args)
            self.assertTrue(heartbeat(identity, os.terminal_size((50, 30))))
            self.assertTrue(is_visible())
            self.assertEqual(ensure(reveal=True), identity)
            self.assertEqual(api.call_count, 1)
            finish(identity)
            self.assertFalse(is_visible())

    def test_open_failure_cleans_pending_state_without_closing_other_popups(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}), patch("subplan.popup.call", side_effect=UsageError("已有其它弹窗")) as api:
            with self.assertRaises(UsageError):
                ensure(reveal=True)
            self.assertEqual(read_json(session_dir() / "popup.json"), {})
            self.assertEqual(api.call_count, 1)
            self.assertNotIn("close", api.call_args.args[0])

    def test_close_targets_only_our_instance_and_pauses_visibility_immediately(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory, "SUBPLAN_POPUP_ID": INSTANCE}), patch("subplan.popup.call") as api:
            atomic_json(session_dir() / "popup.json", {"instance": INSTANCE})
            self.assertEqual(begin(), INSTANCE)
            self.assertTrue(is_visible())
            close_owned()
            self.assertFalse(is_visible())
            self.assertFalse(heartbeat(INSTANCE))
            api.assert_not_called()
            finish(INSTANCE)
            self.assertFalse((session_dir() / ("popup-close-" + INSTANCE)).exists())
            self.assertTrue((session_dir() / "wake").exists())

    def test_dead_or_stale_viewer_cannot_keep_polling_and_old_exit_cannot_erase_new_popup(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            atomic_json(session_dir() / "popup.json", {"instance": INSTANCE, "pid": os.getpid(), "updated_at": time.time() - 10})
            self.assertFalse(is_visible())
            atomic_json(session_dir() / "popup.json", {"instance": INSTANCE, "pid": os.getpid(), "updated_at": time.time()})
            with patch("subplan.popup.process_alive", return_value=False):
                self.assertFalse(is_visible())
            finish("b" * 32)
            self.assertEqual(read_json(session_dir() / "popup.json")["instance"], INSTANCE)

    def test_popup_toolbar_has_no_collapse_and_escape_closes_only_from_usage(self):
        close, collapse = Mock(), Mock()
        ui = DockUI(collapse, Mock(), close, floating=True)
        self.assertNotIn("收起", ui.frame({}, 52, 35))
        self.assertFalse(any(hit[0] == "collapse" for hit in ui.hits))
        ui.event(("text", "c"))
        collapse.assert_not_called()
        ui.event(("key", "escape"))
        close.assert_called_once()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SUBPLAN_CONFIG": directory + "/config.json"}):
            ui.activate("settings")
            ui.event(("key", "escape"))
            self.assertEqual(ui.mode, "usage")
            self.assertEqual(close.call_count, 1)

    @unittest.skipIf(os.name == "nt", "PTY integration uses POSIX")
    def test_actual_popup_viewer_renders_and_exits_on_escape_without_a_pane_id(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory, "SUBPLAN_POPUP_ID": INSTANCE}
            env.pop("HERDR_PANE_ID", None)
            with patch.dict(os.environ, env):
                session = session_dir()
            atomic_json(session / "popup.json", {"instance": INSTANCE, "requested_at": time.time()})
            atomic_json(Path(directory) / "usage.json", {"providers": {"codex": {"status": "ok", "updated_at": time.time(), "windows": {"5h": {"remaining_percent": 80, "resets_at": None}}}}})
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 52, 0, 0))
            process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "popup-view"], stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            output = b""
            try:
                until = time.monotonic() + 5
                while time.monotonic() < until:
                    if select.select([master], [], [], .1)[0]:
                        output += os.read(master, 65536)
                    if read_json(session / "popup.json", {}).get("rendered"):
                        break
                state = read_json(session / "popup.json", {})
                self.assertTrue(state.get("rendered"), output.decode(errors="replace"))
                self.assertEqual((state["columns"], state["rows"]), (52, 35))
                text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output.decode(errors="replace"))
                self.assertIn("80%", text)
                self.assertIn("[设置]", text)
                self.assertNotIn("收起", text)
                os.write(master, b"\x1b")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(read_json(session / "popup.json"), {})
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
