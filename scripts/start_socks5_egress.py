#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


_METHOD = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_RELAY_SCHEME = "ss" + "://"


class EgressConfigurationError(RuntimeError):
    pass


def _decode_base64(value: str) -> str:
    try:
        encoded = unquote(value).encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        return decoded.decode("utf-8")
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise EgressConfigurationError("egress URI is invalid") from error


def _split_endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise EgressConfigurationError("egress URI is invalid") from error
    if (
        not host
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
    ):
        raise EgressConfigurationError("egress URI is invalid")
    return host, port


def parse_relay_uri(uri: str) -> tuple[str, int, str, str]:
    if not isinstance(uri, str) or not uri or uri != uri.strip() or any(c.isspace() for c in uri):
        raise EgressConfigurationError("egress URI is invalid")
    without_fragment = uri.split("#", 1)[0]
    without_query, separator, query = without_fragment.partition("?")
    if separator and query:
        raise EgressConfigurationError("egress URI parameters are unsupported")
    if not without_query.startswith(_RELAY_SCHEME):
        raise EgressConfigurationError("egress URI scheme is unsupported")
    authority = without_query[len(_RELAY_SCHEME):]
    if not authority:
        raise EgressConfigurationError("egress URI is invalid")

    if "@" in authority:
        encoded_credentials, endpoint = authority.rsplit("@", 1)
        credentials = _decode_base64(encoded_credentials)
    else:
        decoded = _decode_base64(authority)
        if "@" not in decoded:
            raise EgressConfigurationError("egress URI is invalid")
        credentials, endpoint = decoded.rsplit("@", 1)

    if ":" not in credentials:
        raise EgressConfigurationError("egress URI is invalid")
    method, password = credentials.split(":", 1)
    if not _METHOD.fullmatch(method) or not password or len(password) > 1024 or "\0" in password:
        raise EgressConfigurationError("egress URI is invalid")
    host, port = _split_endpoint(endpoint)
    return host, port, method, password


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _find_client(requested: str | None) -> str:
    candidates = (requested,) if requested else ("sslocal", "ss-local")
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise EgressConfigurationError("a compatible egress client is unavailable")


def start_client(
    *,
    uri: str,
    work_dir: Path,
    address_file: Path,
    pid_file: Path,
    client_bin: str | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    host, remote_port, method, password = parse_relay_uri(uri)
    executable = _find_client(client_bin)
    local_port = _reserve_loopback_port()
    work_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(work_dir, 0o700)
    config_path = work_dir / "client.json"
    config = {
        "server": host,
        "server_port": remote_port,
        "password": password,
        "method": method,
        "local_address": "127.0.0.1",
        "local_port": local_port,
    }
    _write_private(config_path, json.dumps(config, separators=(",", ":")))

    child_environment = {
        name: os.environ[name]
        for name in ("LANG", "LC_ALL", "PATH", "TZ")
        if name in os.environ
    }
    process = subprocess.Popen(
        [executable, "-c", str(config_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=child_environment,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise EgressConfigurationError("egress client did not start")
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise EgressConfigurationError("egress client did not become ready")
        proxy_address = f"socks5h://127.0.0.1:{local_port}"
        _write_private(address_file, proxy_address + "\n")
        _write_private(pid_file, f"{process.pid}\n")
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        raise
    return process.pid, proxy_address


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--address-file", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--client-bin")
    args = parser.parse_args()
    uri = os.environ.get("EGRESS_PROXY_URI", "")
    try:
        start_client(
            uri=uri,
            work_dir=args.work_dir,
            address_file=args.address_file,
            pid_file=args.pid_file,
            client_bin=args.client_bin,
        )
    except BaseException:
        print("stage=egress status=failed", file=sys.stderr)
        return 1
    print("stage=egress status=ok count=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
