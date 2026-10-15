from contextlib import contextmanager
import json
import os
import re
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
        validate_settings(settings)
    accounts = config.get("accounts", [])
    if not isinstance(accounts, list) or len(accounts) > 32:
        raise UsageError("accounts 必须为列表，最多添加 32 个账号")
    used_ids = set(PROVIDERS)
    for account in accounts:
        if not isinstance(account, dict) or account.get("provider") not in PROVIDERS:
            raise UsageError("账号必须指定有效的 provider")
        identity = account.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity) or identity in used_ids:
            raise UsageError("账号 id 必须唯一，由小写字母、数字、下划线或连字符组成")
        used_ids.add(identity)
        validate_settings(account)
    return config


def validate_settings(settings):
    if not isinstance(settings.get("enabled", True), bool):
        raise UsageError("enabled 必须为布尔值")
    for key in ("api_key", "command", "database_path", "cli_auth_path", "codex_home", "name"):
        if key in settings and not isinstance(settings[key], str):
            raise UsageError(f"{key} 必须为字符串")
    name = settings.get("name", "")
    if len(name) > 48 or any(not char.isprintable() for char in name):
        raise UsageError("账号名称最多 48 字，不能包含控制字符")
    for key in ("codex_home", "database_path", "cli_auth_path", "command"):
        if any(not char.isprintable() for char in settings.get(key, "")):
            raise UsageError("路径不能包含控制字符")


def account_configs(config):
    return [(provider, provider, config[provider]) for provider in PROVIDERS] + [
        (account["id"], account["provider"], account) for account in config.get("accounts", [])]


def expand_account_path(value):
    home = str(Path.home())
    value = value.replace("${HOME}", home).replace("$HOME", home)
    path = Path(home) / value[2:] if value.startswith(("~/", "~\\")) else Path(home) if value == "~" else Path(value).expanduser()
    return path if path.is_absolute() else Path.home() / path


def save_account(identity, provider, settings):
    if provider not in PROVIDERS or not isinstance(settings, dict):
        raise UsageError("无效的账号类型")
    settings = dict(settings)
    settings.pop("id", None)
    settings.pop("provider", None)
    validate_settings(settings)
    if "api_key" in settings:
        value = settings["api_key"].strip()
        if len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise UsageError("Key 不能包含空格、换行或非英文字符")
        settings["api_key"] = value
    path = config_path()
    with lock(path.parent / "config.lock", blocking=True) as acquired:
        if not acquired:
            raise UsageError("配置正在使用，请稍后保存")
        config = load_config()
        if identity in PROVIDERS:
            if identity != provider:
                raise UsageError("不能更改默认账号的提供方")
            config[provider] = {**config[provider], **settings}
        else:
            if not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity):
                raise UsageError("无效的账号 id")
            accounts = config.setdefault("accounts", [])
            old = next((a for a in accounts if a["id"] == identity), None)
            if old:
                if old["provider"] != provider:
                    raise UsageError("不能更改账号的提供方")
                old.update(settings)
                settings = old
            else:
                if len(accounts) >= 32:
                    raise UsageError("最多添加 32 个账号")
                accounts.append({"id": identity, "provider": provider, **settings})
        if identity != provider and settings.get("enabled", True):
            if provider in ("zhipu", "kimi") and not settings.get("api_key"):
                raise UsageError("请填写此账号的 Coding Plan Key")
            if provider == "codex" and not settings.get("codex_home", "").strip():
                raise UsageError("请填写此账号的 CODEX_HOME")
            if provider == "cursor" and not (settings.get("cli_auth_path") or settings.get("database_path")):
                raise UsageError("请填写此账号的 Cursor 登录路径")
        atomic_json(path, config)


def remove_account(identity):
    if identity in PROVIDERS:
        raise UsageError("默认账号可以停用，不能删除")
    path = config_path()
    with lock(path.parent / "config.lock", blocking=True) as acquired:
        if not acquired:
            raise UsageError("配置正在使用，请稍后保存")
        config = load_config()
        config["accounts"] = [account for account in config.get("accounts", []) if account["id"] != identity]
        atomic_json(path, config)


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
