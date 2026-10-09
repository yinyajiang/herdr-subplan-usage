import copy
import os
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from subplan.display import cell_width, dock_frame, dock_lines
from subplan.dock import close_owned, ensure, hide, is_visible, pane_preferences, set_collapsed
from subplan.herdr import session_dir
from subplan.storage import atomic_json, read_json


def snapshot():
    return {"focused_tab_id": "w1:t1", "panes": [{"pane_id": "w1:p1", "label": "shell"}],
            "layouts": [{"tab_id": "w1:t1", "area": {"width": 160, "height": 45}, "zoomed": False,
                         "panes": [{"pane_id": "w1:p1", "rect": {"x": 0, "y": 0, "width": 160, "height": 45}}]}]}


class DockTests(unittest.TestCase):
    def test_close_persists_hidden_before_terminating_its_own_pane(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            data = snapshot()
            data["panes"].extend([{"pane_id": "w1:p2", "label": "订阅用量"}, {"pane_id": "w1:p3", "label": "订阅用量"}])
            atomic_json(session_dir() / "docks.json", {"w1:t1": "w1:p2", "w1:t2": "w1:p3"})

            def api(args):
                if args == ["api", "snapshot"]:
                    return {"snapshot": data}
                self.assertEqual(args, ["plugin", "pane", "close", "w1:p2"])
                self.assertEqual(read_json(session_dir() / "hidden.json"), {"w1:t1": True})
                self.assertEqual(read_json(session_dir() / "docks.json"), {"w1:t2": "w1:p3"})
                self.assertTrue((session_dir() / "wake").exists())
                return {}

            with patch("subplan.dock.call", side_effect=api):
                hide("w1:p2")
            with patch("subplan.dock.call", return_value={"snapshot": data}) as api:
                self.assertIsNone(ensure())
                self.assertEqual(api.call_count, 1)

    def test_explicit_show_reopens_a_closed_tab(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            atomic_json(session_dir() / "hidden.json", {"w1:t1": True, "w1:t2": True})
            with patch("subplan.dock.call", side_effect=[{"snapshot": snapshot()},
                    {"plugin_pane": {"pane": {"pane_id": "w1:p2"}}}, {}, {}]):
                self.assertEqual(ensure(reveal=True), "w1:p2")
            self.assertEqual(read_json(session_dir() / "hidden.json"), {"w1:t2": True})
            self.assertTrue((session_dir() / "wake").exists())

    def test_only_the_current_visible_tab_enables_subscription_refresh(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            data = snapshot()
            data["panes"].append({"pane_id": "w1:p2", "label": "订阅用量"})
            atomic_json(session_dir() / "docks.json", {"w1:t1": "w1:p2"})
            with patch("subplan.dock.snapshot", return_value=data):
                self.assertTrue(is_visible())
                atomic_json(session_dir() / "hidden.json", {"w1:t1": True})
                self.assertFalse(is_visible())
                atomic_json(session_dir() / "hidden.json", {})
                data["focused_tab_id"] = "w1:t2"
                self.assertFalse(is_visible())
                data["focused_tab_id"] = "w1:t1"
                data["layouts"][0]["zoomed"] = True
                data["focused_pane_id"] = "w1:p1"
                self.assertFalse(is_visible())
                data["focused_pane_id"] = "w1:p2"
                self.assertTrue(is_visible())

    def test_open_left_dock_once_without_changing_focus_or_creating_agents(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            data = snapshot()
            commands = []

            def api(args):
                commands.append(args)
                if args == ["api", "snapshot"]:
                    return {"snapshot": copy.deepcopy(data)}
                if args[:3] == ["plugin", "pane", "open"]:
                    data["panes"].append({"pane_id": "w1:p2", "label": "订阅用量"})
                    return {"plugin_pane": {"pane": {"pane_id": "w1:p2"}}}
                return {}

            with patch("subplan.dock.call", side_effect=api):
                self.assertEqual(ensure(), "w1:p2")
                self.assertEqual(ensure(), "w1:p2")
            opened = [c for c in commands if c[:3] == ["plugin", "pane", "open"]]
            self.assertEqual(len(opened), 1)
            self.assertIn("--no-focus", opened[0])
            self.assertIn(["pane", "swap", "--source-pane", "w1:p1", "--target-pane", "w1:p2"], commands)
            self.assertFalse(any(c[0] == "agent" or "report-metadata" in c for c in commands))

    def test_existing_narrow_left_column_gets_dock_above_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            data = snapshot()
            data["layouts"][0]["panes"][0]["rect"]["width"] = 34
            with patch("subplan.dock.call", side_effect=[
                    {"snapshot": data}, {"plugin_pane": {"pane": {"pane_id": "w1:p2"}}}, {}]) as api:
                ensure()
                opened = api.call_args_list[1].args[0]
                self.assertEqual(opened[opened.index("--direction") + 1], "down")
                api.assert_called_with(["pane", "swap", "--source-pane", "w1:p1", "--target-pane", "w1:p2"])

    def test_stop_closes_only_saved_owned_panes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            data = snapshot()
            data["panes"].append({"pane_id": "w1:p2", "label": "订阅用量"})
            atomic_json(session_dir() / "docks.json", {"w1:t1": "w1:p2", "w1:t2": "w1:p1"})
            with patch("subplan.dock.call", side_effect=[{"snapshot": data}, {}]) as api:
                close_owned()
                api.assert_called_with(["plugin", "pane", "close", "w1:p2"])

    def test_zoom_and_stop_do_not_rearrange_layout(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            data = snapshot()
            data["layouts"][0]["zoomed"] = True
            with patch("subplan.dock.call", return_value={"snapshot": data}) as api:
                self.assertIsNone(ensure())
                self.assertEqual(api.call_count, 1)
            (session_dir() / "stop").touch()
            with patch("subplan.dock.call") as api:
                self.assertIsNone(ensure())
                api.assert_not_called()

    def test_responsive_frame_stays_inside_cells_and_at_top(self):
        values = {"providers": {"codex": {"status": "ok", "updated_at": time.time(), "windows": {
            "5h": {"remaining_percent": 80, "resets_at": time.time() + 1000},
            "7d": {"remaining_percent": 60, "resets_at": time.time() + 10000}}}}}
        for width, height in ((24, 18), (42, 45), (20, 10)):
            lines = dock_lines(values, width, height)
            self.assertLessEqual(len(lines), height)
            self.assertTrue(all(cell_width(line) < width for line in lines))
            frame = dock_frame(values, width, height)
            rows = [int(r) for r in re.findall(r"\x1b\[(\d+);1H", frame)]
            self.assertTrue(all(1 <= r <= height for r in rows))
            self.assertEqual(rows[0], 1)
        normal = "\n".join(dock_lines(values, 32, 45))
        self.assertIn("5h 80%", normal)
        self.assertIn("7d 60%", normal)

    def test_collapse_and_expand_restore_original_ratio(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HERDR_SOCKET_PATH": "/fake.sock", "HERDR_PLUGIN_STATE_DIR": directory}):
            layout = {"zoomed": False, "panes": [{"pane_id": "w1:p2", "rect": {"x": 0, "y": 0, "width": 42, "height": 45}}],
                      "splits": [{"direction": "right", "ratio": 0.28, "rect": {"x": 0, "y": 0, "width": 150, "height": 45}}]}
            with patch("subplan.dock.call", side_effect=[{"layout": copy.deepcopy(layout)}, {}]) as api:
                set_collapsed("w1:p2", True)
                args = api.call_args.args[0]
                self.assertEqual(args[args.index("--direction") + 1], "left")
                self.assertAlmostEqual(float(args[-1]), 0.18)
            self.assertTrue(pane_preferences("w1:p2")["collapsed"])
            layout["splits"][0]["ratio"] = 0.1
            layout["panes"][0]["rect"]["width"] = 15
            with patch("subplan.dock.call", side_effect=[{"layout": layout}, {}]) as api:
                set_collapsed("w1:p2", False)
                args = api.call_args.args[0]
                self.assertEqual(args[args.index("--direction") + 1], "right")
                self.assertAlmostEqual(float(args[-1]), 0.18)
            self.assertFalse(pane_preferences("w1:p2")["collapsed"])


if __name__ == "__main__":
    unittest.main()
