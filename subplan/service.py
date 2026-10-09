from concurrent.futures import ThreadPoolExecutor
import time

from .model import UsageError, result
from .providers import fetch
from .storage import PROVIDERS, atomic_json, load_config, lock, paths, read_json


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

    def one(provider):
        if not config[provider].get("enabled", True):
            return result(provider, {}, status="disabled")
        try:
            return fetcher(provider, config[provider], config["timeout_seconds"])
        except UsageError as error:
            return failed(provider, str(error), previous, now)
        except Exception:
            # Unexpected provider schema drift must not take down the other cards,
            # and third-party error messages must never leak credentials.
            return failed(provider, "用量响应格式已变化或查询失败", previous, now)

    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(one, PROVIDERS))
    return {"schema_version": 1, "queried_at": now,
            "providers": {entry["provider"]: entry for entry in entries}}


def refresh(blocking=False):
    state = paths()[1]
    with lock(state / "refresh.lock", blocking=blocking) as acquired:
        if not acquired:
            return read_json(state / "usage.json", {"providers": {}})
        config = load_config()
        if any(config[name].get("enabled", True) for name in PROVIDERS):
            from .network import inherit_shell_proxy
            inherit_shell_proxy()
        previous = read_json(state / "usage.json", {})
        snapshot = collect(config, previous.get("providers", {}))
        atomic_json(state / "usage.json", snapshot)
        return snapshot


def cached():
    return read_json(paths()[1] / "usage.json", {"providers": {}})
