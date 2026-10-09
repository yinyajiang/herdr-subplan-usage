"""Inherit the user's Linux shell proxies when a background Herdr lacks them."""
import json
import os
import shlex
import subprocess
import sys

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
ENV_KEYS = PROXY_KEYS + ("NO_PROXY", "no_proxy")
MARKER = "HERDR_SUBPLAN_PROXY_ENV="
_checked = False


def inherit_shell_proxy():
    global _checked
    if _checked or not sys.platform.startswith("linux") or any(os.environ.get(key) for key in PROXY_KEYS):
        return
    _checked = True
    try:
        import pwd
        shell = pwd.getpwuid(os.getuid()).pw_shell
        if not shell or not os.path.isabs(shell):
            return
        # Only proxy variables cross this boundary; never import credentials,
        # PATH, CODEX_HOME, or terminal control output from shell startup files.
        code = "import os,json;print(" + repr(MARKER) + "+json.dumps({k:os.environ[k] for k in " + repr(ENV_KEYS) + " if os.environ.get(k)}))"
        command = shlex.quote(sys.executable) + " -c " + shlex.quote(code)
        process = subprocess.run([shell, "-ic", command], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, encoding="utf-8", timeout=4)
        if process.returncode or MARKER not in process.stdout:
            return
        data = json.loads(process.stdout.rsplit(MARKER, 1)[1].splitlines()[0])
        if not isinstance(data, dict):
            return
        for key in ENV_KEYS:
            value = data.get(key)
            if not os.environ.get(key) and isinstance(value, str) and len(value) <= 8192 and not any(ord(c) < 32 for c in value):
                os.environ[key] = value
    except (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired):
        # Missing or slow shell configuration must not prevent plugin startup.
        return
