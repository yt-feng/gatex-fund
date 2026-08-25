from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .io import load_json


_CJK = re.compile(r"[\u3400-\u9fff]")
_ABSOLUTE_LOCAL = re.compile(r"/(?:Users|home)/[^/\s]+/")
_CREDENTIAL = re.compile(
    r"(?i)(?:authorization\s*[:=]|bearer\s+[a-z0-9_./+\-=]{20,}|api[_-]?key\s*[:=]|"
    r"age-secret-key-(?:1|pq-1)[a-z0-9]+|-----begin [^-]*private key-----|"
    r"ss://[a-z0-9_+\-/=]{24,}(?:@[a-z0-9.-]+:\d{1,5})?)"
)
_IGNORED_ROOTS = {".git", ".local", ".venv", "work"}


def _public_files(root: Path) -> Iterable[tuple[Path, Path]]:
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] in _IGNORED_ROOTS or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            yield path, relative
            continue
        if not path.is_file():
            continue
        yield path, relative


def _has_public_violation(text: str, markers: list[str]) -> bool:
    return bool(
        _CJK.search(text)
        or _ABSOLUTE_LOCAL.search(text)
        or _CREDENTIAL.search(text)
        or any(marker.casefold() in text.casefold() for marker in markers)
    )


def guard_public_tree(root: Path, config_path: Path | None = None) -> int:
    markers: list[str] = []
    if config_path:
        config = load_json(config_path)
        raw_markers = config.get("private_markers", []) if isinstance(config, dict) else []
        if not isinstance(raw_markers, list) or not all(isinstance(item, str) for item in raw_markers):
            raise RuntimeError("private marker list is invalid")
        markers = [item for item in raw_markers if item]

    hits = 0
    for path, relative in _public_files(root.resolve()):
        if path.is_symlink():
            hits += 1
            continue
        if _has_public_violation(relative.as_posix(), markers):
            hits += 1
            continue
        if path.suffix == ".age":
            try:
                with path.open("rb") as handle:
                    header = handle.read(len(b"age-encryption.org/v1\n"))
                if header != b"age-encryption.org/v1\n":
                    hits += 1
            except OSError:
                hits += 1
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hits += 1
            continue
        if _has_public_violation(text, markers):
            hits += 1
    if hits:
        print(f"stage=guard status=failed count={hits}")
        return 1
    print("stage=guard status=ok count=0")
    return 0
