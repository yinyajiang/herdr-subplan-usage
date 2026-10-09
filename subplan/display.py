from datetime import datetime
import time
import unicodedata

NAMES = {"zhipu": "Zhipu CN", "kimi": "Kimi CN", "codex": "Codex", "cursor": "Cursor"}
TOKENS = ["sp_title"] + [f"sp_{provider}_{part}" for provider in NAMES
                          for part in (("name", "month") if provider == "cursor" else ("name", "5h", "7d"))]


def percent(value):
    if value is None:
        return "—"
    # Don't round 0.4% to 0%, or 99.6% to 100%.
    if 0 < value < 1 or 99 < value < 100:
        return f"{value:.1f}%"
    return f"{value:.0f}%"


def reset_text(value, full=False):
    if value is None:
        return "重置未知"
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M" if full else "%m/%d %H:%M")


def effective(entry, now=None):
    now = time.time() if now is None else now
    if entry.get("status") in ("ok", "unavailable", "stale") and now - entry.get("updated_at", 0) > 180:
        entry = {**entry, "status": "stale"}
    if entry.get("status") == "stale":
        entry = {**entry, "windows": {
            key: ({**value, "remaining_percent": None, "used_percent": None, "api_remaining_percent": None}
                  if value.get("resets_at") is not None and value["resets_at"] <= now else value)
            for key, value in entry.get("windows", {}).items()
        }}
    return entry


def quota_line(name, value, stale=False, full=False):
    marker = "~" if stale else ""
    return f"{name} {marker}{percent(value.get('remaining_percent'))} · {reset_text(value.get('resets_at'), full)}"


def text_view(snapshot):
    lines = ["订阅用量", "剩余额度 · 本地重置时间", ""]
    for provider, label in NAMES.items():
        entry = effective(snapshot.get("providers", {}).get(provider, {"status": "error", "message": "待刷新"}))
        if entry["status"] == "disabled":
            continue
        lines.append(label + ("  [缓存]" if entry["status"] == "stale" else ""))
        if entry["status"] == "error":
            lines.append("  " + (entry.get("message") or "查询失败"))
        else:
            for key, value in entry.get("windows", {}).items():
                lines.append("  " + quota_line("30d" if key == "month" else key, value,
                                               entry["status"] == "stale", full=True))
                if key == "month":
                    marker = "~" if entry["status"] == "stale" else ""
                    lines.append("  api " + marker + percent(value.get("api_remaining_percent")))
                    if value.get("used_amount") is not None and value.get("limit_amount") is not None:
                        lines.append(f"  本月已用 ${value['used_amount']:.2f} / ${value['limit_amount']:.2f}")
            if entry.get("message"):
                lines.append("  " + entry["message"])
        lines.append("")
    lines.append("q / Esc 关闭 · ~ 表示上次查询缓存")
    return "\n".join(lines)


def cell_width(text):
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


def clip_cells(text, width):
    if cell_width(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        size = cell_width(char)
        if used + size > max(0, width - 1):
            break
        out.append(char)
        used += size
    return "".join(out) + ("…" if width > 0 else "")


def dock_lines(snapshot, width, height):
    """Compact quotas fit a narrow column; optional rows yield on short screens."""
    cards = []
    for provider, label in NAMES.items():
        entry = effective(snapshot.get("providers", {}).get(provider, {"status": "error", "message": "待刷新"}))
        if entry["status"] == "disabled":
            continue
        stale = entry["status"] == "stale"
        card = [label + (" · 缓存" if stale else "")]
        if entry["status"] == "error":
            message = entry.get("message") or "查询失败"
            card.append("请配置 Key" if "配置 Coding Plan Key" in message else message)
        else:
            for key, value in entry.get("windows", {}).items():
                card.append(quota_line("30d" if key == "month" else key, value, stale, full=width >= 40))
            if provider == "cursor":
                monthly = entry.get("windows", {}).get("month", {})
                card.append("api " + ("~" if stale else "") + percent(monthly.get("api_remaining_percent")))
                if monthly.get("used_amount") is not None and monthly.get("limit_amount") is not None:
                    card.append(f"已用 ${monthly['used_amount']:.2f}/${monthly['limit_amount']:.2f}")
        cards.append(card)
    lines = []
    for card in cards:
        lines.extend(card + [""])
    if len(lines) > height:
        lines = [line for card in cards for line in card]
    if len(lines) > height:
        # Keep all provider/window labels in very short terminals; amounts and
        # extra amounts are lower priority. Exact reset points stay visible.
        lines = [line for line in lines if not line.startswith("已用 $")]
    if len(lines) > height:
        lines = lines[:max(0, height - 1)] + ["窗口过矮，请增加高度"]
    return [clip_cells(line, max(0, width - 1)) for line in lines[:height]]


def dock_frame(snapshot, width, height):
    lines = dock_lines(snapshot, width, height)
    # Explicit cursor coordinates avoid ONLCR's double carriage returns and
    # keep the last row from scrolling the content away on small terminals.
    first = 1
    return "\x1b[?25l\x1b[0m\x1b[2J" + "".join(
        f"\x1b[{first + i};1H{text}" for i, text in enumerate(lines))
