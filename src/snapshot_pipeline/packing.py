from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

from .io import atomic_write_bytes


def _files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise RuntimeError("batch contains a symbolic link")
        if path.is_file():
            result.append(path)
    return result


def deterministic_tar(source: Path, output: Path) -> str:
    if not source.is_dir():
        raise RuntimeError("batch directory is missing")
    files = _files(source)
    if not files:
        raise RuntimeError("batch directory is empty")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            payload = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    payload = buffer.getvalue()
    atomic_write_bytes(output, payload)
    os.chmod(output, 0o600)
    return hashlib.sha256(payload).hexdigest()
