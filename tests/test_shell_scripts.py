from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ShellScriptTests(unittest.TestCase):
    def test_runner_has_valid_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "scripts/run_scheduled.sh")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_egress_is_bound_before_dynamic_token_validation(self):
        runner = (ROOT / "scripts/run_scheduled.sh").read_text(encoding="utf-8")
        self.assertLess(runner.index("start_socks5_egress.py"), runner.index('token_env="$('))
        self.assertIn('export SNAPSHOT_EGRESS_PROXY="$proxy_address"', runner)
        self.assertIn('kill -TERM -- "-$egress_pid"', runner)
        self.assertIn('kill -KILL -- "-$egress_pid"', runner)


if __name__ == "__main__":
    unittest.main()
