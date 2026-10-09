"""A persistent terminal dock using only public Herdr 0.9 pane APIs."""
from .herdr import PLUGIN_ID, call, session_dir
from .model import UsageError
from .storage import atomic_json, lock, read_json


def snapshot():
    return call(["api", "snapshot"]).get("snapshot", {})


def owned_ids(data, saved):
    # Public pane IDs are opaque and may be restored by a new server. Check the
    # label too before accepting a saved handle or closing an owned pane.
    live = {p["pane_id"]: p for p in data.get("panes", [])}
    return {tab: pane for tab, pane in saved.items()
            if pane in live and live[pane].get("label") in ("订阅用量", "Subscription Usage")}


def is_visible():
    """Only a visible usage pane in the active tab needs subscription polling."""
    session = session_dir()
    data = snapshot()
    tab = data.get("focused_tab_id")
    if read_json(session / "hidden.json", {}).get(tab):
        return False
    pane = owned_ids(data, read_json(session / "docks.json", {})).get(tab)
    if not pane:
        return False
    layout = next((item for item in data.get("layouts", []) if item.get("tab_id") == tab), {})
    return not layout.get("zoomed") or data.get("focused_pane_id") == pane


def ensure(reveal=False):
    session = session_dir()
    if (session / "stop").exists():
        return None
    with lock(session / "dock.lock", blocking=reveal) as acquired:
        if not acquired:
            return None
        data = snapshot()
        saved = owned_ids(data, read_json(session / "docks.json", {}))
        tab = data.get("focused_tab_id")
        if not tab:
            return None
        hidden = read_json(session / "hidden.json", {})
        if reveal and hidden.get(tab):
            hidden.pop(tab, None)
            atomic_json(session / "hidden.json", hidden)
        elif hidden.get(tab):
            return None
        if tab in saved:
            if reveal:
                (session / "wake").touch(mode=0o600)
            return saved[tab]
        layout = next((l for l in data.get("layouts", []) if l.get("tab_id") == tab), None)
        if not layout or layout.get("zoomed"):
            # Respect the user's zoom mode; don't change it to reveal a dock.
            return None
        rectangles = layout.get("panes", [])
        if not rectangles:
            return None
        total = layout["area"]["width"]
        # Prefer the top of an existing narrow left column. Otherwise split
        # the leftmost pane and swap the new pane into the left position.
        left_x = min(p["rect"]["x"] for p in rectangles)
        left = [p for p in rectangles if p["rect"]["x"] == left_x]
        target = min(left, key=lambda p: p["rect"]["y"])
        below = target["rect"]["width"] <= min(56, total * 0.4) and target["rect"]["height"] >= 30
        direction = "down" if below else "right"
        opened = call(["plugin", "pane", "open", "--plugin", PLUGIN_ID,
                       "--entrypoint", "usage", "--placement", "split",
                       "--target-pane", target["pane_id"], "--direction", direction, "--no-focus"])
        pane = opened.get("plugin_pane", {}).get("pane", {}).get("pane_id")
        if not pane:
            raise UsageError("Herdr 没有返回用量窗格标识")
        # Save immediately: retries must never open duplicates after partial failure.
        saved[tab] = pane
        atomic_json(session / "docks.json", saved)
        (session / "wake").touch(mode=0o600)
        if below:
            call(["pane", "swap", "--source-pane", target["pane_id"], "--target-pane", pane])
            return pane
        call(["pane", "swap", "--source-pane", target["pane_id"], "--target-pane", pane])
        # Herdr's resize amount is a fraction of the containing split. Give the
        # dock a quarter of the previous pane width, capped at 42 cells initially.
        width = target["rect"]["width"]
        fraction = max(0.2, min(0.3, 42 / max(1, width)))
        call(["pane", "resize", "--pane", pane, "--direction", "left", "--amount", str(0.5 - fraction)])
        return pane


def hide(pane):
    """Close this tab's owned pane and keep background ensure from reopening it."""
    session = session_dir()
    with lock(session / "dock.lock", blocking=True) as acquired:
        if not acquired:
            raise UsageError("窗格正在调整，请稍后重试")
        saved = owned_ids(snapshot(), read_json(session / "docks.json", {}))
        tab = next((tab for tab, value in saved.items() if value == pane), None)
        if not tab:
            raise UsageError("用量窗格不存在")
        old_hidden = read_json(session / "hidden.json", {})
        atomic_json(session / "hidden.json", {**old_hidden, tab: True})
        # A pane can terminate its own process before close returns; persist all
        # state first so tab focus events and the worker cannot resurrect it.
        atomic_json(session / "docks.json", {key: value for key, value in saved.items() if key != tab})
        (session / "wake").touch(mode=0o600)
        try:
            call(["plugin", "pane", "close", pane])
        except UsageError:
            atomic_json(session / "docks.json", saved)
            atomic_json(session / "hidden.json", old_hidden)
            raise


def close_owned():
    session = session_dir()
    with lock(session / "dock.lock", blocking=True) as acquired:
        if not acquired:
            return
        saved = owned_ids(snapshot(), read_json(session / "docks.json", {}))
        for pane in saved.values():
            try:
                call(["plugin", "pane", "close", pane])
            except UsageError:
                pass
        atomic_json(session / "docks.json", {})


def pane_preferences(pane):
    return read_json(session_dir() / "view.json", {}).get(pane, {})


def set_collapsed(pane, collapsed):
    """Shrink the containing column to Herdr's 10% minimum and restore its ratio."""
    session = session_dir()
    with lock(session / "dock.lock", blocking=True) as acquired:
        if not acquired:
            raise UsageError("窗格正在调整，请稍后重试")
        layout = call(["pane", "layout", "--pane", pane]).get("layout", {})
        if layout.get("zoomed"):
            raise UsageError("请先退出窗格缩放模式")
        rect = next((p["rect"] for p in layout.get("panes", []) if p["pane_id"] == pane), None)
        if not rect:
            raise UsageError("用量窗格不存在")
        splits = [s for s in layout.get("splits", []) if s.get("direction") == "right"
                  and s["rect"]["x"] <= rect["x"]
                  and s["rect"]["x"] + s["rect"]["width"] >= rect["x"] + rect["width"]
                  and s["rect"]["y"] <= rect["y"]
                  and s["rect"]["y"] + s["rect"]["height"] >= rect["y"] + rect["height"]]
        if not splits:
            raise UsageError("当前布局没有可收起的左侧分隔线")
        split = min(splits, key=lambda s: s["rect"]["width"])
        left = rect["x"] == split["rect"]["x"]
        fraction = split["ratio"] if left else 1 - split["ratio"]
        preferences = read_json(session / "view.json", {})
        old = preferences.get(pane, {})
        if bool(old.get("collapsed")) == collapsed:
            return old
        target = 0.1 if collapsed else old.get("expanded_fraction", 0.28)
        target = max(0.1, min(0.9, target))
        if abs(target - fraction) > 0.001:
            smaller = target < fraction
            direction = ("left" if smaller else "right") if left else ("right" if smaller else "left")
            remaining = abs(target - fraction)
            while remaining > 0.001:
                amount = min(0.5, remaining)
                call(["pane", "resize", "--pane", pane, "--direction", direction,
                      "--amount", str(amount)])
                remaining -= amount
        updated = {"collapsed": collapsed, "expanded_fraction": fraction if collapsed else target}
        preferences[pane] = updated
        atomic_json(session / "view.json", preferences)
        return updated
