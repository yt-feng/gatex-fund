from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from snapshot_pipeline.cli import _public_failure_class
from snapshot_pipeline.engine import run_pipeline
from snapshot_pipeline.guard import guard_public_tree
from snapshot_pipeline.packing import deterministic_tar


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def test_public_failure_class_is_allowlisted(self):
        class SyntheticNetworkError(RuntimeError):
            status = "network"

        class SyntheticPrivateError(RuntimeError):
            status = "private-detail"

        self.assertEqual(_public_failure_class(SyntheticNetworkError()), "network")
        self.assertEqual(_public_failure_class(SyntheticPrivateError()), "failed")

    def test_synthetic_run_and_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            state = root / "state.json"
            config.write_text(
                json.dumps(
                    {
                        "token_env": "TEST_SOURCE_TOKEN",
                        "private_markers": ["private-marker"],
                        "provider": {"expected_token": "synthetic-token"},
                    }
                ),
                encoding="utf-8",
            )
            state.write_text('{"version": 1, "known": []}\n', encoding="utf-8")
            os.environ["TEST_SOURCE_TOKEN"] = "synthetic-token"
            try:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    count = run_pipeline(
                        config_path=config,
                        provider_path=ROOT / "tests/fixtures/synthetic/provider.py",
                        state_path=state,
                        state_out=root / "state.next.json",
                        work_dir=root / "work",
                        result_path=root / "result.json",
                    )
                self.assertEqual(count, 1)
                self.assertNotIn("example.invalid", output.getvalue())
                self.assertNotIn("source-alpha", output.getvalue())

                state.write_bytes((root / "state.next.json").read_bytes())
                os.environ["TEST_SOURCE_TOKEN"] = "synthetic-token"
                second = run_pipeline(
                    config_path=config,
                    provider_path=ROOT / "tests/fixtures/synthetic/provider.py",
                    state_path=state,
                    state_out=root / "state.second.json",
                    work_dir=root / "second-work",
                    result_path=root / "second-result.json",
                )
                self.assertEqual(second, 0)
            finally:
                os.environ.pop("TEST_SOURCE_TOKEN", None)

    def test_credentialless_synthetic_run(self):
        for token_binding in ("missing", None):
            with self.subTest(token_binding=token_binding), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "config.json"
                state = root / "state.json"
                payload = {
                    "private_markers": [],
                    "provider": {"expected_token": None},
                }
                if token_binding is None:
                    payload["token_env"] = None
                config.write_text(json.dumps(payload), encoding="utf-8")
                state.write_text('{"version": 1, "known": []}\n', encoding="utf-8")
                self.assertEqual(
                    run_pipeline(
                        config_path=config,
                        provider_path=ROOT / "tests/fixtures/synthetic/provider.py",
                        state_path=state,
                        state_out=root / "state.next.json",
                        work_dir=root / "work",
                        result_path=root / "result.json",
                    ),
                    1,
                )

    def test_declared_token_is_strictly_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            state = root / "state.json"
            config.write_text(
                json.dumps(
                    {
                        "token_env": "TEST_MISSING_TOKEN",
                        "private_markers": [],
                        "provider": {"expected_token": "unused"},
                    }
                ),
                encoding="utf-8",
            )
            state.write_text('{"version": 1, "known": []}\n', encoding="utf-8")
            os.environ.pop("TEST_MISSING_TOKEN", None)
            with self.assertRaisesRegex(RuntimeError, "credential is unavailable"):
                run_pipeline(
                    config_path=config,
                    provider_path=ROOT / "tests/fixtures/synthetic/provider.py",
                    state_path=state,
                    state_out=root / "state.next.json",
                    work_dir=root / "work",
                    result_path=root / "result.json",
                )

    def test_invalid_token_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "token_env": "invalid-name",
                        "private_markers": [],
                        "provider": {"expected_token": None},
                    }
                ),
                encoding="utf-8",
            )
            (root / "state.json").write_text(
                '{"version": 1, "known": []}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "token binding is invalid"):
                run_pipeline(
                    config_path=root / "config.json",
                    provider_path=ROOT / "tests/fixtures/synthetic/provider.py",
                    state_path=root / "state.json",
                    state_out=root / "state.next.json",
                    work_dir=root / "work",
                    result_path=root / "result.json",
                )

    def test_deterministic_tar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "b.txt").write_text("b\n", encoding="utf-8")
            (source / "a.txt").write_text("a\n", encoding="utf-8")
            first = root / "first.tar"
            second = root / "second.tar"
            self.assertEqual(deterministic_tar(source, first), deterministic_tar(source, second))
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_public_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.md").write_text("Public synthetic text.\n", encoding="utf-8")
            self.assertEqual(guard_public_tree(root), 0)
            (root / "unsafe.md").write_text("private-marker\n", encoding="utf-8")
            config = root / "config.plaintext"
            config.write_text(
                json.dumps({"private_markers": ["private-marker"]}), encoding="utf-8"
            )
            self.assertEqual(guard_public_tree(root, config), 1)

    def test_public_guard_scans_relative_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marked = root / "private-marker" / "safe.md"
            marked.parent.mkdir()
            marked.write_text("Public synthetic text.\n", encoding="utf-8")
            config = root / "config.plaintext"
            config.write_text(
                json.dumps({"private_markers": ["private-marker"]}), encoding="utf-8"
            )
            self.assertEqual(guard_public_tree(root, config), 1)

    def test_public_guard_rejects_secret_shapes_symlinks_and_fake_ciphertext(self):
        samples = (
            "AGE" + "-SECRET-KEY-1SYNTHETICVALUE",
            "-----BEGIN " + "PRI" + "VATE KEY-----\nsynthetic\n",
            "ss" + "://YWVzLTI1Ni1nY206c3ludGhldGljQHJlbGF5OjgzODg=",
        )
        for index, sample in enumerate(samples):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "candidate.txt").write_text(sample, encoding="utf-8")
                self.assertEqual(guard_public_tree(root), 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "plain.age").write_text("not ciphertext", encoding="utf-8")
            self.assertEqual(guard_public_tree(root), 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "target.txt").write_text("safe", encoding="utf-8")
            (root / "link.txt").symlink_to(root / "target.txt")
            self.assertEqual(guard_public_tree(root), 1)

    def test_provider_output_is_private_and_token_environment_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = root / "provider.py"
            provider.write_text(
                """\
import os
print('provider-import-output')
def collect(*, config, state, token, output, emit):
    print('provider-collect-output')
    assert token == 'synthetic-token'
    assert 'TEST_CAPTURE_TOKEN' not in os.environ
    emit(stage='discover', status='empty', count=0)
    return {'state': state, 'new_count': 0, 'discovered_count': 0}
""",
                encoding="utf-8",
            )
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "token_env": "TEST_CAPTURE_TOKEN",
                        "private_markers": [],
                        "provider": {},
                    }
                ),
                encoding="utf-8",
            )
            (root / "state.json").write_text('{"version":1}\n', encoding="utf-8")
            os.environ["TEST_CAPTURE_TOKEN"] = "synthetic-token"
            output = io.StringIO()
            try:
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        run_pipeline(
                            config_path=root / "config.json",
                            provider_path=provider,
                            state_path=root / "state.json",
                            state_out=root / "state.next.json",
                            work_dir=root / "work",
                            result_path=root / "result.json",
                        ),
                        0,
                    )
            finally:
                os.environ.pop("TEST_CAPTURE_TOKEN", None)
            self.assertNotIn("provider-import-output", output.getvalue())
            self.assertNotIn("provider-collect-output", output.getvalue())
            private_output = (root / "work" / "provider-output.log").read_text(encoding="utf-8")
            self.assertIn("provider-import-output", private_output)
            self.assertIn("provider-collect-output", private_output)


if __name__ == "__main__":
    unittest.main()
