from __future__ import annotations

import contextlib
import io
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snapshot_pipeline.cli import main
from snapshot_pipeline.engine import _validate_result
from intelligence_sources.contract import envelopes_from_batch


class PartialPipelineTests(unittest.TestCase):
    def test_counts_are_true_nonnegative_integers(self):
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            valid = {"state": {}, "new_count": 1, "discovered_count": 2, "deferred_count": 1}
            for field in ("new_count", "discovered_count", "deferred_count"):
                for value in (True, False, -1, 1.0, "1", None):
                    with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                        _validate_result({**valid, field: value}, batch)
            with self.assertRaisesRegex(RuntimeError, "without completed new items"):
                _validate_result({**valid, "new_count": 0}, batch)
            with self.assertRaisesRegex(RuntimeError, "discovery count"):
                _validate_result({**valid, "discovered_count": 0}, batch)
            self.assertEqual(_validate_result({"state": {}, "new_count": 0, "discovered_count": 0}, batch)["deferred_count"], 0)

    def run_cli(self, root, provider_source, state):
        (root / "provider.py").write_text(provider_source, encoding="utf-8")
        (root / "config.json").write_text('{}', encoding="utf-8")
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(["run", "--config", str(root / "config.json"),
                           "--provider", str(root / "provider.py"), "--state", str(root / "state.json"),
                           "--state-out", str(root / "state.next.json"), "--work", str(root / "work"),
                           "--result", str(root / "result.json")])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_partial_cli_preserves_completed_state_and_retries_only_pending_item(self):
        provider = '''\
import json
def collect(*, state, output, **kwargs):
    # Deliberately mutate input state to exercise durable change detection.
    first_run = not state['known']
    item = 'complete-a' if first_run else 'pending-b'
    state['known'].append(item)
    output.mkdir(parents=True)
    (output / 'metadata.json').write_text(json.dumps({'id': item}))
    print('private candidate https://example.invalid/blocked')
    return {'state': state, 'new_count': 1, 'discovered_count': 2,
            'deferred_count': 1 if first_run else 0}
'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status, stdout, stderr = self.run_cli(root, provider, {"known": []})
            self.assertEqual(status, 0, stderr)
            result = json.loads((root / "result.json").read_text())
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["deferred_count"], 1)
            self.assertEqual(result["failure_class"], "captcha")
            self.assertTrue(result["state_changed"])
            self.assertIn("status=partial count=1 deferred_count=1 failure_class=captcha", stdout)
            self.assertNotIn("stage=run status=ok", stdout)
            self.assertNotIn("example.invalid", stdout + stderr)
            state = json.loads((root / "state.next.json").read_text())
            self.assertEqual(state["known"], ["complete-a"])
            resumed = root / "resumed"
            resumed.mkdir()
            status, stdout, stderr = self.run_cli(resumed, provider, state)
            self.assertEqual(status, 0, stderr)
            self.assertEqual(json.loads((resumed / "state.next.json").read_text())["known"], ["complete-a", "pending-b"])
            self.assertEqual(json.loads((resumed / "result.json").read_text())["status"], "ok")

    def test_unknown_errors_and_zero_progress_captcha_are_not_salvaged(self):
        for failure_class in ("captcha", "network", "parse", "private-detail"):
            provider = f'''\
class Failure(RuntimeError):
    status = {failure_class!r}
def collect(*, state, output, **kwargs):
    state['known'].append('uncommitted')
    output.mkdir(parents=True)
    (output / 'incomplete.txt').write_text('unfinished')
    raise Failure('private candidate https://example.invalid/blocked')
'''
            with self.subTest(failure_class=failure_class), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status, stdout, stderr = self.run_cli(root, provider, {"known": []})
                self.assertEqual(status, 1)
                self.assertFalse((root / "state.next.json").exists())
                self.assertFalse((root / "result.json").exists())
                self.assertNotIn("example.invalid", stdout + stderr)
                public_class = failure_class if failure_class != "private-detail" else "failed"
                self.assertIn("failure_class=" + public_class, stderr)
                self.assertTrue((root / "work/private-error.json").exists())

    def test_downstream_consumers_ignore_deferred_directories_outside_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = root / "articles/complete"
            complete.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({"articles": [{"article_directory": "articles/complete"}]}))
            metadata = {
                "source": "Synthetic Publisher", "title": "Complete article",
                "url": "https://mp.weixin.qq.com/s?__biz=synthetic-biz&mid=100&idx=1&sn=synthetic-sn",
                "published_at": "2026-09-21T01:02:03Z",
            }
            (complete / "metadata.json").write_text(json.dumps(metadata))
            (complete / "content.txt").write_text("Complete preserved source text.")
            deferred = root / "deferred/articles/pending"
            deferred.mkdir(parents=True)
            # These would fail either consumer if it scanned unlisted records.
            (deferred / "metadata.json").write_text("incomplete metadata")
            (deferred / "content.txt").write_text("incomplete body")
            exported = envelopes_from_batch(batch_root=root, intake_config={
                "channel_key": "synthetic-channel", "publisher": "Synthetic Publisher",
                "author": "Synthetic Author",
            })
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]["privateDocument"]["content"], "Complete preserved source text.")
            path = Path(__file__).resolve().parents[1] / "scripts/technology_frontiers_daily.py"
            spec = importlib.util.spec_from_file_location("synthetic_daily_partial", path)
            daily = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(daily)
            with patch.dict(daily.os.environ, {"GATEX_TECHNOLOGY_SOURCE_BIZ_SHA256": ""}), \
                 patch.object(daily, "api", return_value={"ok": True}) as enqueue:
                self.assertEqual(daily.enqueue_batch(root), 1)
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.args[1]["lines"], ["Complete preserved source text."])


if __name__ == "__main__":
    unittest.main()
