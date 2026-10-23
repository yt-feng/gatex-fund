from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from snapshot_pipeline.cli import main


ORIGINAL_STATE = {
    "known": ["anchor"],
    "cursor": {"offset": 9, "pending": ["pending"]},
}
PRIVATE_MARKER = "private retry detail https://example.invalid/private-source"

PROVIDER_PREAMBLE = '''\
import json
import sys
from pathlib import Path

module_calls = 0

def record_attempt():
    global module_calls
    module_calls += 1
    assert module_calls == 1, 'provider module was reused'
    path = Path(__file__).with_name('attempts.txt')
    attempt = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(attempt))
    print('private retry detail https://example.invalid/private-source attempt=' + str(attempt))
    print('private stderr attempt=' + str(attempt), file=sys.stderr)
    return attempt

class Failure(RuntimeError):
    status = 'captcha'
'''


class DiscoveryRetryTests(unittest.TestCase):
    def run_cli(self, root: Path, provider: str):
        (root / "provider.py").write_text(provider, encoding="utf-8")
        (root / "config.json").write_text("{}", encoding="utf-8")
        (root / "state.json").write_text(json.dumps(ORIGINAL_STATE), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("snapshot_pipeline.engine.time.sleep") as sleep, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main([
                "run", "--config", str(root / "config.json"),
                "--provider", str(root / "provider.py"),
                "--state", str(root / "state.json"),
                "--state-out", str(root / "state.next.json"),
                "--work", str(root / "work"),
                "--result", str(root / "result.json"),
            ])
        return status, stdout.getvalue(), stderr.getvalue(), sleep.call_args_list

    def assert_private_output(self, root: Path, stdout: str, stderr: str, attempts: int):
        self.assertNotIn(PRIVATE_MARKER, stdout + stderr)
        self.assertNotIn("example.invalid", stdout + stderr)
        self.assertNotIn("private stderr", stdout + stderr)
        private_log = root / "work/provider-output.log"
        content = private_log.read_text(encoding="utf-8")
        for attempt in range(1, attempts + 1):
            self.assertIn(PRIVATE_MARKER + " attempt=" + str(attempt), content)
            self.assertIn("private stderr attempt=" + str(attempt), content)
        self.assertEqual(private_log.stat().st_mode & 0o777, 0o600)

    def test_two_discovery_blocks_retry_from_clean_state_and_batch_then_succeed(self):
        provider = PROVIDER_PREAMBLE + '''\
def collect(*, state, output, emit, **kwargs):
    attempt = record_attempt()
    assert state == {'known': ['anchor'], 'cursor': {'offset': 9, 'pending': ['pending']}}, 'state leaked from failed attempt'
    assert not output.exists(), 'batch leaked from failed attempt'
    emit(stage='discover', status='start')
    state['known'].append('complete' if attempt == 3 else 'uncommitted')
    state['cursor']['pending'].clear()
    output.mkdir(parents=True)
    if attempt < 3:
        (output / 'incomplete.txt').write_text('unfinished')
        emit(stage='run', status='failed')
        raise Failure('private retry detail https://example.invalid/private-source')
    emit(stage='detail', status='ok', count=1)
    (output / 'complete.txt').write_text('complete body')
    return {'state': state, 'new_count': 1, 'discovered_count': 1}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status, stdout, stderr, sleeps = self.run_cli(root, provider)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(sleeps, [call(30), call(90)])
            self.assertEqual((root / "attempts.txt").read_text(), "3")
            self.assertEqual(json.loads((root / "state.json").read_text()), ORIGINAL_STATE)
            saved = json.loads((root / "state.next.json").read_text())
            self.assertEqual(saved["known"], ["anchor", "complete"])
            self.assertEqual(saved["cursor"]["pending"], [])
            self.assertTrue((root / "work/batch/complete.txt").is_file())
            self.assertFalse((root / "work/batch/incomplete.txt").exists())
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["new_count"], 1)
            self.assertTrue(result["state_changed"])
            self.assertEqual(stdout.count("stage=discover status=retry"), 2)
            self.assert_private_output(root, stdout, stderr, 3)

    def test_exhausted_discovery_blocks_still_fail_without_persisting_progress(self):
        provider = PROVIDER_PREAMBLE + '''\
def collect(*, state, output, emit, **kwargs):
    record_attempt()
    emit(stage='discover', status='start')
    state['known'].append('uncommitted')
    output.mkdir(parents=True)
    (output / 'incomplete.txt').write_text('unfinished')
    emit(stage='run', status='failed')
    raise Failure('private retry detail https://example.invalid/private-source')
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status, stdout, stderr, sleeps = self.run_cli(root, provider)
            self.assertEqual(status, 1)
            self.assertEqual(sleeps, [call(30), call(90)])
            self.assertEqual((root / "attempts.txt").read_text(), "3")
            self.assertFalse((root / "state.next.json").exists())
            self.assertFalse((root / "result.json").exists())
            self.assertEqual(json.loads((root / "state.json").read_text()), ORIGINAL_STATE)
            self.assertIn("failure_class=captcha", stderr)
            self.assertNotIn("stage=run status=ok", stdout)
            self.assertNotIn("stage=run status=partial", stdout)
            self.assertTrue((root / "work/private-error.json").is_file())
            self.assert_private_output(root, stdout, stderr, 3)

    def test_discovery_retry_requires_typed_captcha_error(self):
        for status_value in ("network", "parse", "config", "private-detail", None):
            provider = PROVIDER_PREAMBLE + f'''\
Failure.status = {status_value!r}
def collect(*, state, output, emit, **kwargs):
    record_attempt()
    emit(stage='discover', status='start')
    raise Failure('captcha private retry detail https://example.invalid/private-source')
'''
            with self.subTest(status=status_value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status, stdout, stderr, sleeps = self.run_cli(root, provider)
                self.assertEqual(status, 1)
                self.assertEqual(sleeps, [])
                self.assertEqual((root / "attempts.txt").read_text(), "1")
                self.assertFalse((root / "state.next.json").exists())
                self.assertFalse((root / "result.json").exists())
                expected_class = status_value if status_value in {"network", "parse", "config"} else "failed"
                self.assertIn("failure_class=" + expected_class, stderr)
                self.assert_private_output(root, stdout, stderr, 1)

    def test_captcha_does_not_retry_after_non_discovery_stages(self):
        for other_stage in ("detail", "assets", "state"):
            provider = PROVIDER_PREAMBLE + f'''\
def collect(*, state, output, emit, **kwargs):
    record_attempt()
    emit(stage='discover', status='start')
    emit(stage={other_stage!r}, status='start')
    # The most recent stage is insufficient to prove discovery-only work.
    emit(stage='discover', status='start')
    emit(stage='run', status='failed')
    raise Failure('private retry detail https://example.invalid/private-source')
'''
            with self.subTest(stage=other_stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status, stdout, stderr, sleeps = self.run_cli(root, provider)
                self.assertEqual(status, 1)
                self.assertEqual(sleeps, [])
                self.assertEqual((root / "attempts.txt").read_text(), "1")
                self.assertFalse((root / "state.next.json").exists())
                self.assertFalse((root / "result.json").exists())
                self.assertIn("failure_class=captcha", stderr)
                self.assert_private_output(root, stdout, stderr, 1)

    def test_captcha_without_discovery_evidence_does_not_retry(self):
        for events in ("", "    emit(stage='run', status='failed')\n"):
            provider = PROVIDER_PREAMBLE + '''\
def collect(*, state, output, emit, **kwargs):
    record_attempt()
''' + events + '''\
    raise Failure('private retry detail https://example.invalid/private-source')
'''
            with self.subTest(events=events), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status, stdout, stderr, sleeps = self.run_cli(root, provider)
                self.assertEqual(status, 1)
                self.assertEqual(sleeps, [])
                self.assertEqual((root / "attempts.txt").read_text(), "1")
                self.assertFalse((root / "state.next.json").exists())
                self.assertFalse((root / "result.json").exists())
                self.assert_private_output(root, stdout, stderr, 1)

    def test_captcha_after_completed_discovery_does_not_retry(self):
        for discovery_status in ("ok", "empty"):
            provider = PROVIDER_PREAMBLE + f'''\
def collect(*, state, output, emit, **kwargs):
    record_attempt()
    emit(stage='discover', status='start')
    emit(stage='discover', status={discovery_status!r}, count=0)
    state['known'].append('uncommitted')
    emit(stage='run', status='failed')
    raise Failure('private retry detail https://example.invalid/private-source')
'''
            with self.subTest(status=discovery_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status, stdout, stderr, sleeps = self.run_cli(root, provider)
                self.assertEqual(status, 1)
                self.assertEqual(sleeps, [])
                self.assertEqual((root / "attempts.txt").read_text(), "1")
                self.assertFalse((root / "state.next.json").exists())
                self.assertFalse((root / "result.json").exists())
                self.assertEqual(json.loads((root / "state.json").read_text()), ORIGINAL_STATE)
                self.assertIn("failure_class=captcha", stderr)
                self.assertNotIn("stage=discover status=retry", stdout)
                self.assert_private_output(root, stdout, stderr, 1)

    def test_valid_partial_batch_is_not_retried(self):
        provider = PROVIDER_PREAMBLE + '''\
def collect(*, state, output, emit, **kwargs):
    record_attempt()
    emit(stage='discover', status='start')
    output.mkdir(parents=True)
    (output / 'complete.txt').write_text('complete body')
    state['known'].append('complete')
    return {'state': state, 'new_count': 1, 'discovered_count': 2, 'deferred_count': 1}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status, stdout, stderr, sleeps = self.run_cli(root, provider)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(sleeps, [])
            self.assertEqual((root / "attempts.txt").read_text(), "1")
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["new_count"], 1)
            self.assertEqual(result["deferred_count"], 1)
            self.assertEqual(result["failure_class"], "captcha")
            self.assertEqual(json.loads((root / "state.next.json").read_text())["known"], ["anchor", "complete"])
            self.assertNotIn("stage=discover status=retry", stdout)
            self.assert_private_output(root, stdout, stderr, 1)


if __name__ == "__main__":
    unittest.main()
