from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .model import UsageError

PROVIDERS = ("zhipu", "kimi", "codex", "cursor")


def paths():
    # Herdr supplies these paths on every plugin invocation. Standalone commands
    # use an explicit, portable fallback, never the managed source checkout.
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", os.environ.get("APPDATA", home / ".config")
                                     if os.name == "nt" else home / ".config"))
    state_home = Path(os.environ.get("XDG_STATE_HOME", os.environ.get("LOCALAPPDATA", home / "AppData/Local")
                                    if os.name == "nt" else home / ".local/state"))
    config = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR", config_home / "herdr/plugins/config/herdr-subplan-usage"))
    state = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR", state_home / "herdr/plugins/herdr-subplan-usage"))
    return config, state


def config_path():
    return Path(os.environ.get("SUBPLAN_CONFIG", paths()[0] / "config.json")).expanduser()


def load_config():
    path = config_path()
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise UsageError("无法读取 config.json，请检查 JSON 格式") from None
        if not isinstance(config, dict):
            raise UsageError("config.json 必须为 JSON 对象")
    else:
        config = {}
    for key, default, lower, upper in (("refresh_seconds", 60, 30, 3600),
                                        ("timeout_seconds", 15, 2, 60)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
            raise UsageError(f"{key} 必须在 {lower}–{upper} 之间")
        config[key] = value
    for provider in PROVIDERS:
        settings = config.setdefault(provider, {})
        if not isinstance(settings, dict):
            raise UsageError(f"{provider} 配置必须为对象")
        if not isinstance(settings.get("enabled", True), bool):
            raise UsageError(f"{provider}.enabled 必须为布尔值")
        for key in ("api_key", "command", "database_path"):
            if key in settings and not isinstance(settings[key], str):
                raise UsageError(f"{provider}.{key} 必须为字符串")
    return config


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".subplan-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_api_key(provider, value):
    if provider not in ("zhipu", "kimi"):
        raise UsageError("不支持编辑该提供方的 Key")
    value = value.strip()
    if len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise UsageError("Key 不能包含空格、换行或非英文字符")
    path = config_path()
    with lock(path.parent / "config.lock", blocking=True) as acquired:
        if not acquired:
            raise UsageError("配置正在使用，请稍后保存")
        config = load_config()
        config[provider]["api_key"] = value
        config[provider]["enabled"] = True
        atomic_json(path, config)


@contextmanager
def lock(path, blocking=False):
    """OS-owned lock is automatically released on process exit, including crashes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except (OSError, BlockingIOError):
            pass
        try:
            yield acquired
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
