from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from snapshot_pipeline.engine import run_pipeline
from snapshot_pipeline.guard import guard_public_tree
from snapshot_pipeline.packing import deterministic_tar


ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
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
                        "provider": {},
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


if __name__ == "__main__":
    unittest.main()

