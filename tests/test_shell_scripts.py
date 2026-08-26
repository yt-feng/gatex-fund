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

    def test_browser_runtime_is_pinned_and_runs_under_xvfb(self):
        workflow = (ROOT / ".github/workflows/scheduled-snapshot.yml").read_text(encoding="utf-8")
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"playwright==1.62.0"', project)
        self.assertIn("python3 -m playwright install --with-deps chromium", workflow)
        self.assertIn("xvfb-run -a bash scripts/run_scheduled.sh", workflow)


if __name__ == "__main__":
    unittest.main()
