from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ShellScriptTests(unittest.TestCase):
    def test_runner_has_valid_bash_syntax(self):
        for name in (
            "run_scheduled.sh",
            "run_intelligence_backfill.sh",
            "run_intelligence_profile_preflight.sh",
        ):
            with self.subTest(name=name):
                result = subprocess.run(
                    ["bash", "-n", str(ROOT / "scripts" / name)],
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

    def test_intelligence_workflows_default_to_dry_run_and_backfill_is_manual(self):
        incremental = (
            ROOT / ".github/workflows/intelligence-source-incremental.yml"
        ).read_text(encoding="utf-8")
        backfill = (
            ROOT / ".github/workflows/intelligence-source-backfill.yml"
        ).read_text(encoding="utf-8")
        identity = (
            ROOT / ".github/workflows/intelligence-source-identity-preflight.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("default: dry-run", incremental)
        self.assertIn("ENABLE_INTELLIGENCE_SOURCE_INGEST", incremental)
        self.assertIn("github.event_name == 'workflow_dispatch'", incremental)
        self.assertIn("github.event_name == 'schedule' && 'source-a'", incremental)
        self.assertIn("github.event_name == 'schedule' && 'post'", incremental)
        self.assertIn("workflow_dispatch:", backfill)
        self.assertNotIn("schedule:", backfill)
        self.assertIn("TIKHUB_WECHAT_TOKEN", backfill)
        self.assertIn("workflow_dispatch:", identity)
        self.assertNotIn("schedule:", identity)
        self.assertIn("TIKHUB_WECHAT_TOKEN", identity)
        self.assertIn("permissions:\n  contents: read", identity)
        existing = (
            ROOT / ".github/workflows/scheduled-snapshot.yml"
        ).read_text(encoding="utf-8")
        for workflow in (existing, incremental, backfill):
            self.assertIn("group: scheduled-source-state-main", workflow)

    def test_intelligence_intake_does_not_persist_full_source_bundles(self):
        runner = (ROOT / "scripts/run_scheduled.sh").read_text(encoding="utf-8")
        self.assertIn(
            'if [[ "$new_count" -gt 0 && "$intake_mode" == "off" ]]; then',
            runner,
        )
        self.assertIn('diagnostic_input="diagnostic"', runner)
        self.assertNotIn('tar -C "$work_dir" -cf "$diagnostic_tar" run', runner)

    def test_provider_and_backfill_do_not_inherit_the_delivery_secret(self):
        incremental = (ROOT / "scripts/run_scheduled.sh").read_text(encoding="utf-8")
        backfill = (ROOT / "scripts/run_intelligence_backfill.sh").read_text(encoding="utf-8")
        for runner in (incremental, backfill):
            self.assertLess(
                runner.index("unset GATEX_INTELLIGENCE_INTAKE_SECRET"),
                runner.index("intelligence_sources.cli deliver"),
            )
            self.assertIn('GATEX_INTELLIGENCE_INTAKE_SECRET="$intake_secret"', runner)
        self.assertLess(
            backfill.index("unset TIKHUB_WECHAT_TOKEN"),
            backfill.index("intelligence_sources.cli deliver"),
        )

    def test_post_configuration_is_checked_before_a_zero_new_collection(self):
        runner = (ROOT / "scripts/run_scheduled.sh").read_text(encoding="utf-8")
        backfill = (ROOT / "scripts/run_intelligence_backfill.sh").read_text(encoding="utf-8")
        self.assertLess(
            runner.index("intelligence_sources.cli check-delivery"),
            runner.index("snapshot_pipeline.cli run"),
        )
        self.assertLess(
            runner.index("intelligence_sources.cli check-delivery"),
            runner.index('new_count="$('),
        )
        self.assertLess(
            backfill.index("intelligence_sources.cli check-delivery"),
            backfill.index("intelligence_sources.cli backfill-page"),
        )


if __name__ == "__main__":
    unittest.main()
