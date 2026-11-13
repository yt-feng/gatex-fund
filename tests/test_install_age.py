from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib.util
import io
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("age_installer", ROOT / "scripts/install_age.py")
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
URL = "https://example.invalid/age.tar.gz"


def tool_archive():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name in ("age", "age-keygen"):
            body = ("synthetic " + name).encode()
            member = tarfile.TarInfo("age/" + name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return stream.getvalue()


class InstallerTests(unittest.TestCase):
    def test_transient_http_and_transport_errors_retry_then_verify_and_install(self):
        payload = tool_archive()
        expected = hashlib.sha256(payload).hexdigest()
        failure = urllib.error.HTTPError(URL, 504, "temporary", {}, io.BytesIO())
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(installer.platform, "system", return_value="Linux"), \
                patch.object(installer.platform, "machine", return_value="x86_64"), \
                patch.dict(installer.RELEASES, {("Linux", "x86_64"): (URL, expected)}), \
                patch.object(installer.urllib.request, "urlopen", side_effect=[failure, TimeoutError(), io.BytesIO(payload)]) as opener, \
                patch.object(installer.time, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            directory = Path(temporary) / "bin"
            installer.install(directory)
            self.assertEqual(opener.call_count, 3)
            self.assertEqual(sleep.call_args_list, [call(5), call(15)])
            for name in ("age", "age-keygen"):
                self.assertEqual((directory / name).read_bytes(), ("synthetic " + name).encode())
                self.assertEqual((directory / name).stat().st_mode & 0o777, 0o700)
            self.assertIn("stage=tool status=ready count=2", output.getvalue())

    def test_body_read_failures_are_retried_with_a_fresh_response(self):
        class BrokenResponse(io.BytesIO):
            def read(self, _size):
                raise http.client.IncompleteRead(b"short", 10)

        broken = BrokenResponse()
        with patch.object(installer.urllib.request, "urlopen", side_effect=[broken, io.BytesIO(b"complete")]) as opener, \
                patch.object(installer.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.download(URL), b"complete")
            self.assertTrue(broken.closed)
            self.assertEqual(opener.call_count, 2)
            sleep.assert_called_once_with(5)

    def test_exhausted_downloads_fail_after_three_attempts_without_private_details(self):
        output = io.StringIO()
        with patch.object(installer.urllib.request, "urlopen", side_effect=urllib.error.URLError("private transport detail")) as opener, \
                patch.object(installer.time, "sleep") as sleep, contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "tool download failed after bounded retries"):
                installer.download(URL)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(5), call(15)])
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn(URL, output.getvalue())

    def test_permanent_http_errors_are_not_retried(self):
        for code in (401, 403, 404):
            with self.subTest(code=code), \
                    patch.object(installer.urllib.request, "urlopen", side_effect=urllib.error.HTTPError(URL, code, "permanent", {}, io.BytesIO())) as opener, \
                    patch.object(installer.time, "sleep") as sleep:
                with self.assertRaises(urllib.error.HTTPError):
                    installer.download(URL)
                self.assertEqual(opener.call_count, 1)
                sleep.assert_not_called()

    def test_checksum_mismatch_never_retries_or_installs_a_tool(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(installer.platform, "system", return_value="Linux"), \
                patch.object(installer.platform, "machine", return_value="x86_64"), \
                patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong archive")) as opener, \
                patch.object(installer.time, "sleep") as sleep:
            directory = Path(temporary) / "bin"
            with self.assertRaisesRegex(SystemExit, "tool checksum mismatch"):
                installer.install(directory)
            self.assertFalse(directory.exists())
            self.assertEqual(opener.call_count, 1)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
