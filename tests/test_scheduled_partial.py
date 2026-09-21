"""Exercise the real runner with synthetic collection and offline persistence doubles."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEADER = b"age-encryption.org/v1\n"
AGE_DOUBLE = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path
args = sys.argv[1:]
output = Path(args[args.index('-o') + 1])
payload = Path(args[-1]).read_bytes()
header = b'age-encryption.org/v1\\n'
if '-d' in args:
    output.write_bytes(payload.removeprefix(header))
else:
    if os.environ.get('TEST_FAIL') == 'archive' and output.name.endswith('.tar.age') and 'vault' in output.parts:
        raise SystemExit(9)
    output.write_bytes(header + payload)
'''
GIT_DOUBLE = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path
args = sys.argv[3:]
command = args[0]
with Path(os.environ['TEST_EVENTS']).open('a') as stream:
    stream.write(command + '\\n')
if command == 'diff':
    raise SystemExit(1)
if command == 'push' and os.environ.get('TEST_FAIL') == 'push':
    raise SystemExit(9)
'''
PROVIDER = '''def collect(*, state, output, **kwargs):
    output.mkdir(parents=True)
    (output / 'complete.txt').write_text('complete article')
    (output / 'deferred.json').write_text('{"candidate": "pending-b"}')
    state['known'].append('complete-a')
    return {'state': state, 'new_count': 1, 'discovered_count': 2, 'deferred_count': 1}
'''


class ScheduledPartialTests(unittest.TestCase):
    def run_scheduled(self, root, failure=""):
        repo = root / "repo"
        scripts = repo / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(ROOT / "scripts/run_scheduled.sh", scripts)
        shutil.copytree(ROOT / "src/snapshot_pipeline", repo / "src/snapshot_pipeline", ignore=shutil.ignore_patterns('__pycache__'))
        # This double only copies synthetic fixture bytes; it neither downloads
        # tools nor claims to test encryption. Production still uses real age.
        (scripts / "install_age.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "directory=Path(sys.argv[sys.argv.index('--bin-dir')+1])\n"
            "directory.mkdir(parents=True)\n"
            f"target=directory/'age'\ntarget.write_text({AGE_DOUBLE!r})\ntarget.chmod(0o700)\n"
        )
        sealed = repo / "sealed"
        sealed.mkdir()
        for name, body in {
            "runtime-config.json.age": '{}',
            "checkpoint.json.age": '{"known": []}',
            "provider-overlay.py.age": PROVIDER,
        }.items():
            (sealed / name).write_bytes(HEADER + body.encode())
        recipients = repo / "recipients"
        recipients.mkdir()
        for name in ("runtime-recipient.txt", "export-recipient.txt"):
            (recipients / name).write_text("synthetic\n")
        binaries = root / "bin"
        binaries.mkdir()
        (binaries / "git").write_text(GIT_DOUBLE)
        (binaries / "git").chmod(0o700)
        runtime = root / "runtime"
        runtime.mkdir()
        env = {
            "PATH": str(binaries) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "RUNNER_TEMP": str(runtime), "RUNTIME_AGE_IDENTITY": "synthetic",
            "GITHUB_STEP_SUMMARY": str(root / "summary.txt"),
            "TEST_EVENTS": str(root / "events.txt"), "TEST_FAIL": failure,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(["bash", str(scripts / "run_scheduled.sh")],
                                env=env, capture_output=True, text=True, timeout=30)
        return repo, result

    def test_partial_completed_items_are_persisted_before_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, result = self.run_scheduled(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads((repo / "sealed/checkpoint.json.age").read_bytes().removeprefix(HEADER))
            self.assertEqual(state["known"], ["complete-a"])
            self.assertEqual(len(list((repo / "vault").rglob("*.tar.age"))), 1)
            events = (root / "events.txt").read_text().splitlines()
            self.assertEqual(events[-2:], ["commit", "push"])
            self.assertIn("stage=pack status=ok", result.stdout)
            self.assertNotIn("stage=run status=ok", result.stdout)
            self.assertLess(result.stdout.index("stage=state status=ok"), result.stdout.index("::warning::"))
            summary = (root / "summary.txt").read_text()
            self.assertIn("**partial**", summary)
            self.assertIn("1 candidate(s) deferred (captcha)", summary)
            self.assertEqual(list((root / "runtime").glob("snapshot-pipeline.*")), [])

    def test_failed_archive_or_push_never_claims_partial_saved(self):
        for failure in ("archive", "push"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _repo, result = self.run_scheduled(root, failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("::warning::Snapshot saved", result.stdout)
                self.assertFalse((root / "summary.txt").exists())
                self.assertTrue((root / "runtime/snapshot-diagnostic.tar.age").is_file())
                if failure == "archive":
                    self.assertFalse((root / "events.txt").exists())
                self.assertEqual(list((root / "runtime").glob("snapshot-pipeline.*")), [])


if __name__ == "__main__":
    unittest.main()
