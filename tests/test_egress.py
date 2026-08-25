from __future__ import annotations

import base64
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "scripts/start_socks5_egress.py"


class EgressTests(unittest.TestCase):
    def test_synthetic_client_starts_without_secret_output_or_inheritance(self):
        spec = importlib.util.spec_from_file_location("synthetic_egress_starter", STARTER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        starter = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(starter)

        captured = {}

        class FakeProcess:
            pid = 4321

            def poll(self):
                return None

            def terminate(self):
                return None

            def kill(self):
                return None

            def wait(self, timeout=None):
                return 0

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_popen(arguments, **kwargs):
            captured["arguments"] = arguments
            captured["environment"] = kwargs["env"]
            config_path = Path(arguments[arguments.index("-c") + 1])
            captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
            return FakeProcess()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = base64.urlsafe_b64encode(
                b"chacha20-ietf-poly1305:synthetic-password"
            ).decode("ascii").rstrip("=")
            uri = "ss" + f"://{credentials}@relay.example.invalid:8388"
            with mock.patch.object(starter, "_find_client", return_value="/synthetic/client"), mock.patch.object(
                starter, "_reserve_loopback_port", return_value=19080
            ), mock.patch.object(starter.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
                starter.socket, "create_connection", return_value=FakeConnection()
            ):
                pid, address = starter.start_client(
                    uri=uri,
                    work_dir=root / "client-work",
                    address_file=root / "address",
                    pid_file=root / "pid",
                )
            self.assertEqual(pid, 4321)
            self.assertEqual(address, "socks5h://127.0.0.1:19080")
            self.assertRegex((root / "address").read_text(), r"^socks5h://127\.0\.0\.1:\d+\n$")
            self.assertNotIn("EGRESS_PROXY_URI", captured["environment"])
            self.assertNotIn("RUNTIME_AGE_IDENTITY", captured["environment"])
            self.assertEqual("synthetic-password", captured["config"]["password"])
            self.assertEqual("relay.example.invalid", captured["config"]["server"])

    def test_fully_encoded_authority_is_supported(self):
        parser = runpy.run_path(str(STARTER))["parse_relay_uri"]
        authority = base64.urlsafe_b64encode(
            b"aes-256-gcm:synthetic-password@relay.example.invalid:8388"
        ).decode("ascii").rstrip("=")
        self.assertEqual(
            parser("ss" + f"://{authority}"),
            ("relay.example.invalid", 8388, "aes-256-gcm", "synthetic-password"),
        )

    def test_invalid_uri_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_value = "invalid-private-value"
            environment = os.environ.copy()
            environment["EGRESS_PROXY_URI"] = private_value
            result = subprocess.run(
                [
                    sys.executable,
                    str(STARTER),
                    "--work-dir",
                    str(root / "client-work"),
                    "--address-file",
                    str(root / "address"),
                    "--pid-file",
                    str(root / "pid"),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(private_value, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
