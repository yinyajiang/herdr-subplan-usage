import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from subplan.network import MARKER, inherit_shell_proxy


class NetworkTests(unittest.TestCase):
    def test_existing_proxy_is_preserved_without_starting_shell(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://existing:8080"}, clear=True), patch("subplan.network._checked", False), patch("subplan.network.sys.platform", "linux"), patch("subplan.network.subprocess.run") as run:
            inherit_shell_proxy()
            run.assert_not_called()
            self.assertEqual(os.environ["HTTPS_PROXY"], "http://existing:8080")

    @unittest.skipIf(os.name == "nt", "Linux passwd database required")
    def test_only_proxy_environment_imported_and_shell_checked_once(self):
        data = {"HTTPS_PROXY": "http://127.0.0.1:29758", "NO_PROXY": "new", "CODEX_HOME": "/untrusted", "SECRET": "private"}
        process = SimpleNamespace(returncode=0, stdout="shell output\n" + MARKER + json.dumps(data) + "\n")
        with patch.dict(os.environ, {"NO_PROXY": "original"}, clear=True), patch("subplan.network._checked", False), patch("subplan.network.sys.platform", "linux"), patch("pwd.getpwuid", return_value=SimpleNamespace(pw_shell="/bin/zsh")), patch("subplan.network.subprocess.run", return_value=process) as run:
            inherit_shell_proxy()
            inherit_shell_proxy()
            run.assert_called_once()
            self.assertEqual(os.environ["HTTPS_PROXY"], data["HTTPS_PROXY"])
            self.assertEqual(os.environ["NO_PROXY"], "original")
            self.assertNotIn("CODEX_HOME", os.environ)
            self.assertNotIn("SECRET", os.environ)

    @unittest.skipIf(os.name == "nt", "Linux passwd database required")
    def test_shell_failures_do_not_block_refresh(self):
        for failure in (subprocess.TimeoutExpired("shell", 4), OSError("unavailable")):
            with patch.dict(os.environ, {}, clear=True), patch("subplan.network._checked", False), patch("subplan.network.sys.platform", "linux"), patch("pwd.getpwuid", return_value=SimpleNamespace(pw_shell="/bin/zsh")), patch("subplan.network.subprocess.run", side_effect=failure):
                inherit_shell_proxy()
                self.assertEqual(dict(os.environ), {})


if __name__ == "__main__":
    unittest.main()
