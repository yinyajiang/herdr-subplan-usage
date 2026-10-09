"""Clickable terminal controls and a masked, local-only API key editor."""
import re
import time

from .display import NAMES, cell_width, clip_cells, dock_lines
from .model import UsageError
from .storage import load_config, save_api_key
from .colors import paint, paint_usage, paint_spans


class InputDecoder:
    def __init__(self):
        self.buffer = ""
        self.pending_at = 0

    def feed(self, data):
        self.buffer += data
        if len(self.buffer) > 65536:
            self.buffer = ""
            return [("key", "escape")]
        if data:
            self.pending_at = time.monotonic()
        events = []
        while self.buffer:
            if self.buffer.startswith("\x1b[200~"):
                end = self.buffer.find("\x1b[201~", 6)
                if end < 0:
                    break
                events.append(("paste", self.buffer[6:end].strip()))
                self.buffer = self.buffer[end + 6:]
            elif self.buffer.startswith("\x1b[<"):
                match = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])", self.buffer)
                if not match:
                    if len(self.buffer) > 64:
                        self.buffer = ""
                    break
                button, x, y, action = match.groups()
                if int(button) == 0 and action == "M":
                    events.append(("click", (int(x), int(y))))
                self.buffer = self.buffer[match.end():]
            elif self.buffer.startswith("\x1b["):
                match = re.match(r"\x1b\[([0-9;]*)([A-Za-z~])", self.buffer)
                if not match:
                    break
                number, suffix = match.groups()
                key = {"C": "right", "D": "left", "H": "home", "F": "end"}.get(suffix)
                if suffix == "~":
                    key = {"1": "home", "3": "delete", "4": "end"}.get(number)
                if key:
                    events.append(("key", key))
                self.buffer = self.buffer[match.end():]
            elif self.buffer.startswith("\x1b"):
                if len(self.buffer) == 1:
                    break
                events.append(("key", "escape"))
                self.buffer = self.buffer[1:]
            else:
                char, self.buffer = self.buffer[0], self.buffer[1:]
                key = {"\r": "enter", "\n": "enter", "\x7f": "backspace", "\b": "backspace",
                       "\x15": "clear", "\x01": "home", "\x05": "end", "\x03": "escape"}.get(char)
                events.append(("key", key) if key else ("text", char))
        return events

    def flush_escape(self):
        if self.buffer == "\x1b" and time.monotonic() - self.pending_at > 0.08:
            self.buffer = ""
            return [("key", "escape")]
        return []


class DockUI:
    def __init__(self, toggle, request_refresh, close=lambda: None):
        self.toggle = toggle
        self.request_refresh = request_refresh
        self.close = close
        self.mode = "usage"
        self.provider = "zhipu"
        self.secret = ""
        self.cursor = 0
        self.replace = False
        self.message = ""
        self.collapsed = False
        self.hits = []

    def activate(self, action):
        self.message = ""
        try:
            if action == "close":
                self.secret = ""
                self.close()
            elif action == "collapse":
                self.toggle(not self.collapsed)
                self.collapsed = not self.collapsed
                self.mode = "usage"
                self.secret = ""
            elif action == "settings":
                self.mode = "settings"
                self.secret = ""
            elif action == "back":
                self.mode = "usage"
                self.secret = ""
            elif action.startswith("edit:"):
                self.provider = action.split(":", 1)[1]
                self.secret = load_config()[self.provider].get("api_key", "")
                self.cursor = len(self.secret)
                self.replace = True
                self.mode = "edit"
            elif action == "cancel":
                self.secret = ""
                self.mode = "settings"
            elif action == "save":
                save_api_key(self.provider, self.secret)
                self.secret = ""
                self.mode = "settings"
                self.message = "已保存，正在刷新用量"
                self.request_refresh()
        except UsageError as error:
            self.message = str(error)

    def event(self, event):
        kind, value = event
        if kind == "click":
            x, y = value
            for action, row, start, end in self.hits:
                if row == y and start <= x <= end:
                    self.activate(action)
                    break
            return
        if self.mode == "edit":
            if kind in ("text", "paste"):
                value = value if kind == "paste" else value if value.isprintable() else ""
                if not value:
                    return
                if self.replace:
                    self.secret, self.cursor, self.replace = "", 0, False
                value = value[:max(0, 4096 - len(self.secret))]
                self.secret = self.secret[:self.cursor] + value + self.secret[self.cursor:]
                self.cursor += len(value)
            elif kind == "key":
                if value == "enter":
                    self.activate("save")
                elif value == "escape":
                    self.activate("cancel")
                elif value == "clear":
                    self.secret, self.cursor, self.replace = "", 0, False
                elif value in ("left", "right", "home", "end"):
                    self.replace = False
                    self.cursor = {"left": max(0, self.cursor - 1), "right": min(len(self.secret), self.cursor + 1),
                                   "home": 0, "end": len(self.secret)}[value]
                elif value in ("delete", "backspace"):
                    if self.replace:
                        self.secret, self.cursor, self.replace = "", 0, False
                    elif value == "backspace" and self.cursor:
                        self.secret = self.secret[:self.cursor - 1] + self.secret[self.cursor:]
                        self.cursor -= 1
                    elif value == "delete":
                        self.secret = self.secret[:self.cursor] + self.secret[self.cursor + 1:]
            return
        if kind == "key" and value == "escape":
            self.activate("back")
        elif kind == "text":
            if value.lower() == "s":
                self.activate("settings")
            elif value.lower() == "r":
                self.request_refresh()
            elif value.lower() == "c":
                self.activate("collapse")
            elif self.mode == "settings" and value in ("1", "2"):
                self.activate("edit:" + ("zhipu" if value == "1" else "kimi"))

    def frame(self, snapshot, width, height):
        usable = max(0, width - 1)
        self.hits = []
        rows = {}
        styles = {}
        spans = {}

        def text(row, value, role="body"):
            if 1 <= row <= height:
                rows[row] = clip_cells(value, usable)
                styles[row] = role

        def button(row, label, action, x=1):
            if 1 <= row <= height and x + cell_width(label) - 1 <= usable:
                prefix = rows.get(row, "") + " " * max(0, x - cell_width(rows.get(row, "")) - 1)
                rows[row] = prefix + label
                role = "error" if action == "close" else "success" if action == "save" else "cancel" if action == "cancel" else action.split(":", 1)[1] if action.startswith("edit:") else "button"
                spans.setdefault(row, []).append((len(prefix), len(rows[row]), role))
                self.hits.append((action, row, x, x + cell_width(label) - 1))

        if self.collapsed:
            button(1, "[展开 >]" if usable >= 12 else "[>]" if usable >= 7 else ">", "collapse")
            label = "[×]" if usable >= 7 else "×"
            button(1, label, "close", max(3, usable - cell_width(label) + 1))
        else:
            button(1, "[设置]" if self.mode == "usage" else "[返回]", "settings" if self.mode == "usage" else "back")
            label, close_label = ("[收起 <]", "[关闭]") if usable >= 22 else ("[<]", "[×]")
            close_x = usable - cell_width(close_label) + 1
            button(1, label, "collapse", max(8, close_x - cell_width(label) - 1))
            button(1, close_label, "close", close_x)
            if self.mode == "usage":
                lines = dock_lines(snapshot, width, max(0, height - (3 if self.message else 2)))
                for i, line in enumerate(lines, 3):
                    text(i, line, "usage")
                if self.message:
                    text(len(lines) + 3, self.message, "error")
            elif self.mode == "settings":
                text(3, "Coding Plan Key 配置", "title")
                button(5, "1. Zhipu CN [编辑]", "edit:zhipu")
                button(7, "2. Kimi CN [编辑]", "edit:kimi")
                text(10, self.message or "点击编辑，或按 1 / 2", "success" if self.message.startswith("已保存") else "error" if self.message else "hint")
                text(12, "Key 仅保存在本机", "hint")
            else:
                text(3, NAMES[self.provider] + " Key", self.provider)
                text(5, "[" + "*" * min(len(self.secret), max(0, usable - 3)) + "]", "field")
                text(6, "已选中，输入可替换" if self.replace else "Ctrl+U 清空 · 方向键移动", "hint")
                button(8, "[保存]", "save")
                button(8, "[取消]", "cancel", 10)
                text(10, self.message or "Enter 保存 · Esc 取消", "error" if self.message else "hint")
        rendered = []
        for row, value in sorted(rows.items()):
            colored = paint_spans(value, spans[row]) if row in spans else paint_usage(value) if styles.get(row) == "usage" else paint(value, styles.get(row, "body"))
            rendered.append(f"\x1b[{row};1H{colored}")
        return "\x1b[?25l\x1b[0m\x1b[2J" + "".join(rendered)
