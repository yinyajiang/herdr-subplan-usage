from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

from .model import UsageError, result
from .providers import fetch
from .storage import account_configs, atomic_json, load_config, lock, paths, read_json


def failed(provider, message, previous, now):
    # Expired quota windows must not keep showing the pre-reset percentage.
    old = previous.get(provider)
    if isinstance(old, dict) and old.get("status") in ("ok", "stale", "unavailable"):
        age = now - old.get("updated_at", 0)
        if 0 <= age <= 900 and old.get("windows"):
            saved = {**old, "status": "stale", "message": message}
            saved["windows"] = {
                name: ({**value, "remaining_percent": None, "used_percent": None, "api_remaining_percent": None}
                       if value.get("resets_at") is not None and value["resets_at"] <= now else value)
                for name, value in old["windows"].items()
            }
            return saved
    return result(provider, {}, status="error", message=message)


def collect(config, previous=None, fetcher=fetch, now=None):
    previous = previous or {}
    now = time.time() if now is None else now

    def one(account):
        identity, provider, settings = account
        signature = hashlib.sha256(json.dumps({key: value for key, value in settings.items()
                                              if key not in ("name", "enabled")}, sort_keys=True).encode()).hexdigest()
        old = previous.get(identity, {})
        saved = {identity: old} if old.get("config_signature") == signature else {}
        try:
            if not settings.get("enabled", True):
                entry = result(provider, {}, status="disabled")
            else:
                if identity != provider:
                    if provider in ("zhipu", "kimi") and not settings.get("api_key"):
                        raise UsageError("请配置 Coding Plan Key")
                    if provider == "codex" and not settings.get("codex_home"):
                        raise UsageError("请配置 CODEX_HOME")
                    if provider == "cursor" and not (settings.get("cli_auth_path") or settings.get("database_path")):
                        raise UsageError("请配置 Cursor 登录路径")
                entry = fetcher(provider, settings, config["timeout_seconds"])
        except UsageError as error:
            entry = failed(identity, str(error), saved, now)
        except Exception:
            # Unexpected provider schema drift must not take down the other cards,
            # and third-party error messages must never leak credentials.
            entry = failed(identity, "用量响应格式已变化或查询失败", saved, now)
        return {**entry, "provider": provider, "account_id": identity,
                "name": settings.get("name", ""), "config_signature": signature}

    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(one, account_configs(config)))
    return {"schema_version": 2, "queried_at": now,
            "providers": {entry["account_id"]: entry for entry in entries}}


def refresh(blocking=False, visible_only=False):
    state = paths()[1]
    with lock(state / "refresh.lock", blocking=blocking) as acquired:
        if not acquired:
            return read_json(state / "usage.json", {"providers": {}})
        if visible_only:
            from .dock import is_visible
            # Recheck after acquiring the shared lock: the pane may have closed
            # while this request was waiting for another session's refresh.
            if not is_visible():
                return read_json(state / "usage.json", {"providers": {}})
        config = load_config()
        if any(settings.get("enabled", True) for _, _, settings in account_configs(config)):
            from .network import inherit_shell_proxy
            inherit_shell_proxy()
        previous = read_json(state / "usage.json", {})
        snapshot = collect(config, previous.get("providers", {}))
        atomic_json(state / "usage.json", snapshot)
        return snapshot


def cached():
    return read_json(paths()[1] / "usage.json", {"providers": {}})
