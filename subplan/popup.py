"""Native Herdr popup lifecycle without splitting or resizing working panes."""
import os
import re
import time
import uuid

from .herdr import PLUGIN_ID, call, session_dir
from .model import UsageError
from .storage import atomic_json, lock, read_json


def process_alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) is not a safe existence check on Windows.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, OverflowError):
        return False


def alive(state):
    pid, updated = state.get("pid"), state.get("updated_at")
    return (type(pid) is int and pid > 0 and isinstance(updated, (int, float))
            and 0 <= time.time() - updated <= 3 and process_alive(pid))


def is_visible():
    session = session_dir()
    state = read_json(session / "popup.json", {})
    instance = state.get("instance", "")
    return bool(re.fullmatch(r"[0-9a-f]{32}", instance) and not (session / ("popup-close-" + instance)).exists() and alive(state))


def ensure(reveal=False):
    # Kept as the startup/focus hook: only an explicit show may open a popup.
    if not reveal:
        return None
    session = session_dir()
    if (session / "stop").exists():
        return None
    with lock(session / "popup.lock", blocking=True) as acquired:
        if not acquired:
            raise UsageError("弹窗正在打开，请稍后重试")
        current = read_json(session / "popup.json", {})
        closing = current.get("instance", "")
        if re.fullmatch(r"[0-9a-f]{32}", closing) and (session / ("popup-close-" + closing)).exists():
            until = time.monotonic() + 2
            while alive(current) and time.monotonic() < until:
                time.sleep(0.05)
                current = read_json(session / "popup.json", {})
        if is_visible() or (current.get("instance") and not current.get("pid") and time.time() - current.get("requested_at", 0) < 8):
            (session / "wake").touch(mode=0o600)
            return current["instance"]
        instance = uuid.uuid4().hex
        atomic_json(session / "popup.json", {"instance": instance, "requested_at": time.time()})
        try:
            # The manifest supplies placement=popup and its geometry. The 0.9
            # CLI does not accept --placement popup, despite supporting it here.
            call(["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "usage",
                  "--env", "SUBPLAN_POPUP_ID=" + instance, "--focus"])
        except UsageError:
            if not is_visible():
                if read_json(session / "popup.json", {}).get("instance") == instance:
                    atomic_json(session / "popup.json", {})
                raise
        return instance


def begin():
    instance = os.environ.get("SUBPLAN_POPUP_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", instance):
        raise UsageError("请通过 prefix+u 打开用量弹窗")
    if not heartbeat(instance):
        raise UsageError("用量弹窗已关闭")
    (session_dir() / "wake").touch(mode=0o600)
    return instance


def heartbeat(instance, size=None):
    session = session_dir()
    state = read_json(session / "popup.json", {})
    if state.get("instance") != instance or (session / ("popup-close-" + instance)).exists():
        return False
    state.update(pid=os.getpid(), updated_at=time.time())
    if size:
        state.update(columns=size.columns, rows=size.lines, rendered=True)
    atomic_json(session / "popup.json", state)
    return True


def finish(instance):
    session = session_dir()
    if read_json(session / "popup.json", {}).get("instance") == instance:
        atomic_json(session / "popup.json", {})
    (session / ("popup-close-" + instance)).unlink(missing_ok=True)
    (session / "wake").touch(mode=0o600)


def close_owned():
    # Ask our own viewer to exit. Never use the global popup.close API, which
    # could close another plugin's popup after our viewer has already exited.
    session = session_dir()
    state = read_json(session / "popup.json", {})
    instance = state.get("instance", "")
    if not re.fullmatch(r"[0-9a-f]{32}", instance):
        return
    (session / ("popup-close-" + instance)).touch(mode=0o600)
    (session / "wake").touch(mode=0o600)
