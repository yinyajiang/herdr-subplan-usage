"""Own the background refresh worker and clear legacy sidebar metadata."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .display import TOKENS
from .model import UsageError
from .service import cached, refresh
from .storage import atomic_json, load_config, lock, paths, read_json

PLUGIN_ID = "herdr-subplan-usage"


def call(arguments):
    if not os.environ.get("HERDR_SOCKET_PATH"):
        raise UsageError("请从 Herdr 插件动作中运行此命令")
    try:
        response = subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), *arguments],
                                  capture_output=True, text=True, encoding="utf-8", timeout=8)
        if response.returncode:
            raise UsageError("Herdr 服务不可用")
        parsed = json.loads(response.stdout)
        if parsed.get("error"):
            raise UsageError("Herdr 服务拒绝了用量显示请求")
        return parsed.get("result", parsed)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise UsageError("Herdr 服务不可用") from None


def session_dir():
    socket = os.environ.get("HERDR_SOCKET_PATH")
    if not socket:
        raise UsageError("请从 Herdr 插件动作中启动自动更新")
    identity = hashlib.sha256(socket.encode()).hexdigest()[:16]
    return paths()[1] / "sessions" / identity


def report(pane_id, tokens, ttl=180_000):
    arguments = ["pane", "report-metadata", pane_id, "--source", PLUGIN_ID,
                 "--ttl-ms", str(ttl)]
    for key, value in tokens.items():
        arguments.extend(["--token", f"{key}={value}"] if value else ["--clear-token", key])
    call(arguments)


def clear():
    session = session_dir()
    anchor = read_json(session / "anchor.json", {}).get("pane_id")
    if anchor:
        try:
            report(anchor, {key: "" for key in TOKENS})
        except UsageError:
            pass
    atomic_json(session / "anchor.json", {})


def enabled():
    # Unlink, uninstall or disable stops this plugin's unsupervised worker.
    plugins = call(["plugin", "list", "--json"]).get("plugins", [])
    return any(p.get("id", p.get("plugin_id")) == PLUGIN_ID and p.get("enabled", False)
               for p in plugins)


def start():
    from .network import inherit_shell_proxy
    inherit_shell_proxy()
    session = session_dir()
    session.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop_file = session / "stop"
    stop_file.unlink(missing_ok=True)
    # The startup entrypoint exits immediately. Worker ownership uses an OS lock,
    # so repeated starts/attach actions never produce duplicate pollers.
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    main = Path(__file__).resolve().parents[1] / "main.py"
    try:
        subprocess.Popen([sys.executable, str(main), "watch"], cwd=main.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **options)
    except OSError:
        raise UsageError("无法启动用量更新") from None


def stop():
    session = session_dir()
    session.mkdir(parents=True, exist_ok=True, mode=0o700)
    (session / "stop").touch(mode=0o600)
    clear()
    from .dock import close_owned
    close_owned()


def watch():
    session = session_dir()
    with lock(session / "worker.lock") as acquired:
        if not acquired:
            return
        clear()
        failures = 0
        while not (session / "stop").exists():
            try:
                if not enabled():
                    break
                config = load_config()
                snapshot = cached()
                if time.time() - snapshot.get("queried_at", 0) >= config["refresh_seconds"]:
                    snapshot = refresh()
                if (session / "stop").exists():
                    break
                from .dock import ensure
                ensure()
                atomic_json(session / "health.json", {"updated_at": time.time(), "status": "running"})
                failures = 0
                interval = min(config["refresh_seconds"], 60)
            except UsageError as error:
                failures += 1
                atomic_json(session / "health.json", {"updated_at": time.time(), "status": "error", "message": str(error)})
                if failures >= 3:
                    break
                interval = 10
            # Check stop once each second without sending extra API requests.
            until = time.monotonic() + interval
            while time.monotonic() < until and not (session / "stop").exists():
                time.sleep(min(1, max(0, until - time.monotonic())))
        clear()
        try:
            from .dock import close_owned
            close_owned()
        except UsageError:
            pass
        atomic_json(session / "health.json", {"updated_at": time.time(), "status": "stopped"})
