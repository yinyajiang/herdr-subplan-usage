"""Clickable terminal controls and a masked, local-only API key editor."""
import re
import time

from .display import NAMES, account_label, cell_width, clip_cells, dock_lines
from .model import UsageError
from .storage import account_configs, load_config, remove_account, save_account, save_api_key
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
                elif int(button) in (64, 65) and action == "M":
                    events.append(("key", "up" if int(button) == 64 else "down"))
                self.buffer = self.buffer[match.end():]
            elif self.buffer.startswith("\x1b["):
                match = re.match(r"\x1b\[([0-9;]*)([A-Za-z~])", self.buffer)
                if not match:
                    break
                number, suffix = match.groups()
                key = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}.get(suffix)
                if suffix == "~":
                    key = {"1": "home", "3": "delete", "4": "end", "5": "pageup", "6": "pagedown"}.get(number)
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
    def __init__(self, toggle, request_refresh, close=lambda: None, floating=False):
        self.toggle = toggle
        self.request_refresh = request_refresh
        self.close = close
        self.floating = floating
        self.mode = "usage"
        self.provider = "zhipu"
        self.secret = ""
        self.cursor = 0
        self.replace = False
        self.message = ""
        self.collapsed = False
        self.hits = []
        self.account_id = "zhipu"
        self.draft = None
        self.edit_field = "api_key"
        self.return_mode = "settings"
        self.settings_accounts = []
        self.settings_page = 0
        self.page_accounts = []
        self.usage_offset = 0

    def show_settings(self):
        self.mode = "settings"
        self.secret = ""
        self.draft = None
        self.settings_accounts = account_configs(load_config())

    def edit_value(self, field, value, return_mode):
        self.edit_field = field
        self.secret = value
        self.cursor = len(value)
        self.replace = True
        self.return_mode = return_mode
        self.mode = "edit"

    def activate(self, action):
        self.message = ""
        try:
            if action == "close":
                self.secret = ""
                self.draft = None
                self.close()
            elif action == "collapse":
                if self.floating:
                    return
                self.toggle(not self.collapsed)
                self.collapsed = not self.collapsed
                self.mode = "usage"
                self.secret = ""
                self.draft = None
            elif action == "settings":
                self.show_settings()
            elif action == "back":
                if self.floating and self.mode == "usage":
                    self.close()
                elif self.mode in ("account", "choose", "delete-confirm"):
                    self.show_settings()
                else:
                    self.mode = "usage"
                    self.secret = ""
                    self.draft = None
            elif action.startswith("edit:"):
                self.provider = action.split(":", 1)[1]
                self.account_id = self.provider
                self.edit_value("api_key", load_config()[self.provider].get("api_key", ""), "settings")
            elif action.startswith("account:"):
                self.account_id = action.split(":", 1)[1]
                _, self.provider, settings = next(item for item in account_configs(load_config()) if item[0] == self.account_id)
                self.draft = dict(settings)
                self.draft.setdefault("name", NAMES[self.provider])
                self.mode = "account"
            elif action == "add":
                self.mode = "choose"
            elif action.startswith("new:"):
                self.provider = action.split(":", 1)[1]
                used = {item[0] for item in account_configs(load_config())}
                index = 2
                while f"{self.provider}-{index}" in used:
                    index += 1
                self.account_id = f"{self.provider}-{index}"
                self.draft = {"name": NAMES[self.provider] + f" {index}", "enabled": True}
                if self.provider == "codex":
                    self.draft["command"] = load_config()["codex"].get("command", "codex")
                self.mode = "account"
            elif action.startswith("field:"):
                field = action.split(":", 1)[1]
                self.edit_value(field, self.draft.get(field, ""), "account")
            elif action == "toggle-enabled":
                self.draft["enabled"] = not self.draft.get("enabled", True)
            elif action == "save-account":
                save_account(self.account_id, self.provider, self.draft)
                self.show_settings()
                self.message = "已保存，正在刷新用量"
                self.request_refresh()
            elif action == "delete":
                self.mode = "delete-confirm"
            elif action == "confirm-delete":
                remove_account(self.account_id)
                self.show_settings()
                self.message = "账号已删除"
                self.request_refresh()
            elif action == "keep-account":
                self.mode = "account"
            elif action in ("next-page", "previous-page"):
                self.settings_page = max(0, self.settings_page + (1 if action == "next-page" else -1))
            elif action == "cancel":
                self.secret = ""
                self.mode = self.return_mode
            elif action == "save":
                if self.return_mode == "account":
                    if any(not char.isprintable() for char in self.secret):
                        raise UsageError("输入不能包含控制字符")
                    self.draft[self.edit_field] = self.secret.strip()
                    self.secret = ""
                    self.mode = "account"
                else:
                    save_api_key(self.provider, self.secret)
                    self.secret = ""
                    self.show_settings()
                    self.message = "已保存，正在刷新用量"
                    self.request_refresh()
        except UsageError as error:
            self.message = str(error)
        except OSError:
            self.message = "无法保存本机配置，请检查文件权限"
        except StopIteration:
            self.message = "账号配置已变更，请重新打开设置"

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
                if self.edit_field != "api_key" and any(not char.isprintable() for char in value):
                    self.message = "输入不能包含控制字符"
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
        elif kind == "key" and value in ("up", "down", "pageup", "pagedown"):
            if self.mode == "usage":
                self.usage_offset = max(0, self.usage_offset + (-1 if value in ("up", "pageup") else 1) * (8 if value in ("pageup", "pagedown") else 3))
            elif self.mode == "settings":
                self.activate("previous-page" if value in ("up", "pageup") else "next-page")
        elif kind == "text":
            if value.lower() == "s":
                self.activate("settings")
            elif value.lower() == "r":
                self.request_refresh()
            elif value.lower() == "c":
                self.activate("collapse")
            elif self.floating and self.mode == "usage" and value.lower() == "q":
                self.activate("close")
            elif self.mode == "settings" and value == "+":
                self.activate("add")
            elif self.mode == "settings" and value.isdigit() and 1 <= int(value) <= len(self.page_accounts):
                self.activate("account:" + self.page_accounts[int(value) - 1][0])
            elif self.mode == "choose" and value in "1234":
                self.activate("new:" + list(NAMES)[int(value) - 1])

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
                role = "error" if action in ("close", "delete", "confirm-delete") else "success" if action in ("save", "save-account") else "cancel" if action == "cancel" else "button"
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
            if not self.floating:
                button(1, label, "collapse", max(8, close_x - cell_width(label) - 1))
            button(1, close_label, "close", close_x)
            if self.mode == "usage":
                available = max(0, height - (3 if self.message else 2))
                lines = dock_lines(snapshot, width, 10000)
                if len(lines) > available:
                    body = max(0, available - 1)
                    self.usage_offset = min(self.usage_offset, max(0, len(lines) - body))
                    lines = lines[self.usage_offset:self.usage_offset + body]
                    text(3 + len(lines), "↑/↓ 滚动查看更多", "hint")
                else:
                    self.usage_offset = 0
                for i, line in enumerate(lines, 3):
                    text(i, line, "usage")
                if self.message:
                    text(len(lines) + 3, self.message, "error")
            elif self.mode == "settings":
                button(3, "[新增账号 +]", "add")
                page_size = max(1, (height - 12) // 2)
                pages = max(1, (len(self.settings_accounts) + page_size - 1) // page_size)
                self.settings_page = min(self.settings_page, pages - 1)
                self.page_accounts = self.settings_accounts[self.settings_page * page_size:(self.settings_page + 1) * page_size]
                for index, (identity, provider, settings) in enumerate(self.page_accounts):
                    label = account_label(provider, settings) + (" · 停用" if not settings.get("enabled", True) else "")
                    button(5 + index * 2, f"{index + 1}. {clip_cells(label, max(1, usable - 12))} [编辑]", "account:" + identity)
                if pages > 1:
                    button(height - 5, "[上页]", "previous-page")
                    button(height - 5, "[下页]", "next-page", 10)
                text(height - 3, self.message or "点击账号编辑 · + 新增", "success" if self.message.startswith("已保存") else "error" if self.message else "hint")
                text(height - 1, "配置仅保存在本机", "hint")
            elif self.mode == "choose":
                text(3, "选择账号类型", "title")
                for index, provider in enumerate(NAMES):
                    button(5 + index * 2, f"{index + 1}. [{NAMES[provider]}]", "new:" + provider)
            elif self.mode == "account":
                text(3, account_label(self.provider, self.draft), self.provider)
                button(5, "[编辑名称]", "field:name")
                text(6, self.draft.get("name", ""))
                field = {"zhipu": "api_key", "kimi": "api_key", "codex": "codex_home", "cursor": "cli_auth_path"}[self.provider]
                label = {"api_key": "Key", "codex_home": "CODEX_HOME", "cli_auth_path": "CLI 登录路径"}[field]
                button(8, "[编辑 " + label + "]", "field:" + field)
                value = self.draft.get(field, "")
                text(9, ("*" * min(16, len(value)) if field == "api_key" else value) or ("未配置" if self.account_id != self.provider else "使用当前默认登录 / 环境配置"), "hint")
                if self.provider == "cursor":
                    button(11, "[编辑数据库路径]", "field:database_path")
                    text(12, self.draft.get("database_path") or "未指定", "hint")
                button(14, "[已启用 · 点击停用]" if self.draft.get("enabled", True) else "[已停用 · 点击启用]", "toggle-enabled")
                button(16, "[保存]", "save-account")
                button(16, "[取消]", "settings", 10)
                if self.account_id != self.provider:
                    button(18, "[删除账号]", "delete")
                text(20, self.message, "error")
            elif self.mode == "delete-confirm":
                text(3, "删除此账号配置？", "warning")
                text(5, account_label(self.provider, self.draft))
                button(8, "[删除]", "confirm-delete")
                button(8, "[取消]", "keep-account", 10)
            else:
                label = {"api_key": "Key", "codex_home": "CODEX_HOME", "cli_auth_path": "CLI 登录路径", "database_path": "数据库路径", "name": "名称"}[self.edit_field]
                text(3, NAMES[self.provider] + " " + label, self.provider)
                value = "*" * min(len(self.secret), max(0, usable - 3)) if self.edit_field == "api_key" else self.secret
                text(5, "[" + value + "]", "field")
                text(6, "已选中，输入可替换" if self.replace else "Ctrl+U 清空 · 方向键移动", "hint")
                button(8, "[确定]" if self.return_mode == "account" else "[保存]", "save")
                button(8, "[取消]", "cancel", 10)
                text(10, self.message or "Enter 确定 · Esc 取消", "error" if self.message else "hint")
                if self.edit_field == "codex_home":
                    text(12, "例：~/.codex2 或 $HOME/.codex2", "hint")
        rendered = []
        for row, value in sorted(rows.items()):
            colored = paint_spans(value, spans[row]) if row in spans else paint_usage(value) if styles.get(row) == "usage" else paint(value, styles.get(row, "body"))
            rendered.append(f"\x1b[{row};1H{colored}")
        return "\x1b[?25l\x1b[0m\x1b[2J" + "".join(rendered)
