#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import os
import platform
import stat
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path


VERSION = "1.3.1"
DOWNLOAD_RETRY_DELAYS = (5, 15)
RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
RELEASES = {
    ("Linux", "x86_64"): (
        "https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-linux-amd64.tar.gz",
        "bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377",
    ),
    ("Darwin", "arm64"): (
        "https://github.com/FiloSottile/age/releases/download/v1.3.1/age-v1.3.1-darwin-arm64.tar.gz",
        "01120ea2cbf0463d4c6bd767f99f3271bbed1cdc8a9aa718a76ba1fe4f01998b",
    ),
}


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "snapshot-pipeline-installer/1"})
    for attempt in range(len(DOWNLOAD_RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read(20 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in RETRYABLE_HTTP_STATUSES:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            pass
        if attempt == len(DOWNLOAD_RETRY_DELAYS):
            raise SystemExit("tool download failed after bounded retries")
        print(f"stage=tool status=retry count={attempt + 1}", flush=True)
        time.sleep(DOWNLOAD_RETRY_DELAYS[attempt])
    raise AssertionError("download retry loop did not return")


def install(bin_dir: Path) -> None:
    key = (platform.system(), platform.machine())
    if key not in RELEASES:
        raise SystemExit("unsupported platform")
    url, expected = RELEASES[key]
    payload = download(url)
    if hashlib.sha256(payload).hexdigest() != expected:
        raise SystemExit("tool checksum mismatch")
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = {Path(member.name).name: member for member in archive.getmembers()}
        for name in ("age", "age-keygen"):
            member = members.get(name)
            if member is None or not member.isfile() or member.size > 20 * 1024 * 1024:
                raise SystemExit("tool archive is invalid")
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit("tool archive is unreadable")
            destination = bin_dir / name
            destination.write_bytes(source.read())
            destination.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    print("stage=tool status=ready count=2")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin-dir", type=Path, required=True)
    args = parser.parse_args()
    install(args.bin_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
