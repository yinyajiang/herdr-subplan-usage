"""Terminal palette colors, applied after measuring plain-text layout."""
import re

from .display import NAMES

STYLES = {
    "body": "39", "title": "1;36", "hint": "39", "time": "1;39", "warning": "33",
    "error": "1;31", "success": "32", "button": "1;36", "cancel": "39",
    "zhipu": "1;34", "kimi": "1;36", "codex": "1;32", "cursor": "1;35",
    "field": "35", "label": "1;39",
}


def paint(text, role):
    return f"\x1b[{STYLES.get(role, STYLES['body'])}m{text}\x1b[0m" if text else ""


def paint_usage(text):
    if text.startswith("订阅用量"):
        return paint(text, "title")
    for provider, name in NAMES.items():
        if text.startswith(name):
            return paint(text[:len(name)], provider) + paint(text[len(name):], "warning")
    quota = re.match(r"^(5h|7d|30d|月|api)(\s+)(~?(?:\d+(?:\.\d+)?%|—))(.*)$", text)
    if quota:
        label, space, value, reset = quota.groups()
        if value == "—":
            role = "hint"
        elif value.startswith("~"):
            role = "warning"
        else:
            remaining = float(value.rstrip("%"))
            role = "error" if remaining < 10 else "warning" if remaining < 30 else "success"
        return paint(label, "label") + space + paint(value, role) + paint(reset, "time")
    if text.startswith("已用 $"):
        return paint("已用 ", "hint") + paint(text[3:], "body")
    if text.startswith("自动更新"):
        return paint(text, "hint")
    if "配置" in text or "限流" in text or "过矮" in text:
        return paint(text, "warning")
    return paint(text, "error") if text else ""


def paint_spans(text, spans):
    """Spans index ordinary characters; ANSI bytes never enter hit geometry."""
    result, position = [], 0
    for start, end, role in sorted(spans):
        result.append(text[position:start])
        result.append(paint(text[start:end], role))
        position = end
    result.append(text[position:])
    return "".join(result)
