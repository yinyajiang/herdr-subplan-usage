"""Read-only provider integrations. Only fixed provider hosts receive credentials."""
import base64
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import UsageError, complete, number, timestamp, window
from .storage import expand_account_path

ZHIPU_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
KIMI_URL = "https://api.kimi.com/coding/v1/usages"
CURSOR_URL = "https://cursor.com/api/usage-summary"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Session/key material must never follow a redirect to a different host.
        return None


def get_json(url, headers, timeout):
    request = Request(url, headers={"Accept": "application/json",
                                   "User-Agent": "herdr-subplan-usage/0.1", **headers})
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(1_048_577)
        if len(data) > 1_048_576:
            raise UsageError("用量响应过大")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise UsageError("用量响应格式已变化")
        return value
    except HTTPError as error:
        if error.code in (401, 403):
            raise UsageError("凭据失效，请重新登录或检查 Key") from None
        if error.code == 429:
            raise UsageError("查询限流，稍后重试") from None
        raise UsageError(f"查询失败 HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise UsageError("网络不可用或查询超时") from None
    except (ValueError, UnicodeError):
        raise UsageError("用量响应格式已变化") from None


def parse_zhipu(data):
    if data.get("success") is False or data.get("code", 200) not in (200, "200"):
        raise UsageError("Zhipu 查询失败，请检查 Coding Plan Key")
    limits = data.get("data", {}).get("limits")
    if not isinstance(limits, list):
        raise UsageError("Zhipu 用量响应格式已变化")
    windows = {}
    units = {1: 1440, 3: 60, 5: 1, 6: 10080}
    for item in limits:
        if not isinstance(item, dict) or item.get("type") not in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            continue
        duration = (number(item.get("number")) or 0) * units.get(item.get("unit"), 0)
        name = {300: "5h", 10080: "7d"}.get(duration)
        if name:
            windows[name] = window(item.get("percentage"), item.get("nextResetTime"),
                                   limit=item.get("usage"), used=item.get("currentValue"),
                                   remaining=item.get("remaining"))
    return complete("zhipu", windows)


def parse_kimi(data):
    windows = {}
    pools = data.get("usages")
    if isinstance(pools, dict):
        for name, key in (("5h", "limit_5h"), ("7d", "limit_7d")):
            pool = pools.get(key)
            if isinstance(pool, dict):
                ratio = number(pool.get("used_ratio"))
                windows[name] = window(None if ratio is None else ratio * 100, pool.get("reset_time"))
    for item in data.get("limits", []) or []:
        if not isinstance(item, dict):
            continue
        period, detail = item.get("window", {}), item.get("detail", {})
        minutes = (number(period.get("duration")) or 0) * {
            "TIME_UNIT_MINUTE": 1, "TIME_UNIT_HOUR": 60, "TIME_UNIT_DAY": 1440
        }.get(period.get("timeUnit"), 0)
        name = {300: "5h", 10080: "7d"}.get(minutes)
        # New ratio pools are authoritative; don't mistake the monthly usage pool for a week.
        if name and (name not in windows or windows[name]["remaining_percent"] is None):
            windows[name] = kimi_detail(detail)
    if "7d" not in windows and not isinstance(pools, dict) and isinstance(data.get("usage"), dict):
        # The legacy API's top-level usage is its weekly quota. In the new format
        # this top-level field may be monthly, so never use it when ratio pools exist.
        windows["7d"] = kimi_detail(data["usage"])
    return complete("kimi", windows)


def kimi_detail(detail):
    reset = next((detail[k] for k in ("resetTime", "resetAt", "reset_time", "reset_at")
                  if detail.get(k) is not None), None)
    return window(reset=reset, remaining=detail.get("remaining"), used=detail.get("used"),
                  limit=detail.get("limit"))


def parse_codex(data):
    buckets = data.get("rateLimitsByLimitId")
    # Never select a model-specific bucket such as Spark as the whole subscription.
    if isinstance(buckets, dict) and isinstance(buckets.get("codex"), dict):
        limits = buckets["codex"]
    else:
        limits = data.get("rateLimits", {}) or {}
        if limits.get("limitId") not in (None, "codex"):
            return complete("codex", {})
    windows = {}
    for name in ("primary", "secondary"):
        item = limits.get(name)
        if isinstance(item, dict):
            target = {300: "5h", 10080: "7d"}.get(number(item.get("windowDurationMins")))
            if target:
                windows[target] = window(item.get("usedPercent"), item.get("resetsAt"))
    return complete("codex", windows)


def codex_usage(command, timeout, codex_home=None):
    """Delegate login, keychain and token renewal to the user's actual Codex CLI."""
    options = {}
    if codex_home:
        home = expand_account_path(codex_home)
        if not home.is_dir():
            raise UsageError("CODEX_HOME 目录不存在")
        # Each child gets an independent environment, including parallel queries.
        options["env"] = {**os.environ, "CODEX_HOME": str(home)}
    try:
        proc = subprocess.Popen([command, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", **options)
    except OSError:
        raise UsageError("未找到 Codex CLI，请配置 command") from None
    messages = queue.Queue()

    def read():
        try:
            for line in proc.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    def receive(request_id):
        while True:
            try:
                message = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise UsageError("Codex 用量查询超时") from None
            if message is None:
                raise UsageError("Codex 查询进程已退出")
            if isinstance(message, dict) and message.get("id") == request_id:
                if message.get("error"):
                    raise UsageError(codex_error_message(message["error"]))
                return message.get("result", {})
            if time.monotonic() >= deadline:
                raise UsageError("Codex 用量查询超时")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "herdr_subplan_usage", "version": "0.1.0"}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return parse_codex(receive(2))
    except (OSError, ValueError):
        raise UsageError("Codex 查询失败，请检查 CLI 登录") from None
    finally:
        proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        reader.join(timeout=1)
        proc.stdout.close()


def codex_error_message(error):
    # Classify the CLI error without exposing its HTTP body, tokens or account.
    message = str(error.get("message", "")) if isinstance(error, dict) else ""
    if re.search(r"\b403\b", message):
        return "Codex HTTP 403 · 检查网络/代理"
    if re.search(r"\b401\b", message):
        return "Codex 登录失效，请重新登录"
    if re.search(r"\b429\b", message):
        return "Codex 查询限流，稍后重试"
    return "Codex 用量查询失败，请检查 CLI 登录或网络"


def cursor_db_path():
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return base / "Cursor/User/globalStorage/state.vscdb"


def cursor_cli_auth_path():
    home = Path.home()
    if sys.platform == "darwin":
        return home / ".cursor/auth.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "Cursor/auth.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "cursor/auth.json"


def cursor_token_cookie(token, login_hint="请打开 Cursor 刷新登录"):
    try:
        if not isinstance(token, str) or not token:
            raise UsageError("Cursor 登录配置中没有访问令牌")
        parts = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        subject = payload.get("sub", "").split("|")[-1]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", subject):
            raise UsageError("Cursor 登录配置格式已变化")
        expiration = number(payload.get("exp"))
        if expiration is not None and expiration <= time.time():
            raise UsageError("Cursor 登录已过期，" + login_hint)
        return "WorkosCursorSessionToken=" + quote(subject + "::" + token, safe="")
    except (ValueError, IndexError, TypeError, AttributeError):
        raise UsageError("Cursor 登录配置格式已变化") from None


def cursor_cli_cookie(path):
    try:
        with expand_account_path(str(path)).open("r", encoding="utf-8") as source:
            text = source.read(1_048_577)
        if len(text) > 1_048_576:
            raise UsageError("Cursor CLI 登录配置过大")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise UsageError("Cursor CLI 登录配置格式已变化")
        return cursor_token_cookie(data.get("accessToken"), "请运行 agent login")
    except OSError:
        raise UsageError("无法读取 Cursor CLI 登录配置") from None
    except (ValueError, UnicodeError):
        raise UsageError("Cursor CLI 登录配置格式已变化") from None


def cursor_login_cookie(settings):
    # Explicit paths select one account; never silently switch to another login.
    if settings.get("database_path"):
        return cursor_cookie(settings["database_path"])
    if settings.get("cli_auth_path"):
        return cursor_cli_cookie(settings["cli_auth_path"])
    error = None
    database = cursor_db_path()
    if database.is_file():
        try:
            return cursor_cookie(database)
        except UsageError as failed:
            error = failed
    cli_auth = cursor_cli_auth_path()
    if cli_auth.is_file():
        return cursor_cli_cookie(cli_auth)
    if error:
        raise error
    raise UsageError("未找到 Cursor 登录，请登录客户端或 Agent CLI")


def cursor_cookie(path):
    path = expand_account_path(str(path)).resolve()
    if not path.is_file():
        raise UsageError("未找到 Cursor 登录配置")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as database:
            row = database.execute("SELECT value FROM ItemTable WHERE key = ?",
                                   ("cursorAuth/accessToken",)).fetchone()
        if not row or not row[0]:
            raise UsageError("请先在 Cursor 中登录")
        return cursor_token_cookie(row[0])
    except (sqlite3.Error, OSError):
        raise UsageError("无法读取 Cursor 登录数据库") from None
    except (ValueError, IndexError, TypeError, AttributeError):
        raise UsageError("Cursor 登录配置格式已变化") from None


def parse_cursor(data):
    individual = data.get("individualUsage", {}) or {}
    quota = individual.get("plan") or individual.get("overall") or {}
    # A shared team pool is not this user's personal subscription quota.
    pct = number(quota.get("totalPercentUsed"))
    if pct is not None and pct < 0:
        pct = None
    used, limit = number(quota.get("used")), number(quota.get("limit"))
    # Cursor's Included percentage accounts for the current subscription pools.
    # The capped base dollar counters may already be full while the included
    # allocation remains available; they must never override that percentage.
    monthly = window(pct, data.get("billingCycleEnd")) if pct is not None else window(
        reset=data.get("billingCycleEnd"), used=used, limit=limit)
    monthly["quota_source"] = "percentage" if pct is not None else "amount"
    monthly["used_percent"] = None if monthly["remaining_percent"] is None else 100 - monthly["remaining_percent"]
    monthly["api_remaining_percent"] = window(quota.get("apiPercentUsed"))["remaining_percent"]
    monthly["used_amount"] = None if pct is not None or used is None else used / 100
    monthly["limit_amount"] = None if pct is not None or limit is None else limit / 100
    monthly["amount_currency"] = "USD"
    return complete("cursor", {"month": monthly})


def fetch(provider, settings, timeout):
    if provider == "codex":
        return codex_usage(settings.get("command") or "codex", timeout, settings.get("codex_home"))
    if provider == "cursor":
        cookie = cursor_login_cookie(settings)
        return parse_cursor(get_json(CURSOR_URL, {"Cookie": cookie}, timeout))
    key = settings.get("api_key") or os.environ.get({"zhipu": "ZHIPU_API_KEY", "kimi": "KIMI_API_KEY"}[provider])
    if not isinstance(key, str) or not key.strip():
        raise UsageError("请配置 Coding Plan Key")
    url, parser = (ZHIPU_URL, parse_zhipu) if provider == "zhipu" else (KIMI_URL, parse_kimi)
    return parser(get_json(url, {"Authorization": "Bearer " + key.strip()}, timeout))
