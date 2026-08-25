from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .io import load_json


_CJK = re.compile(r"[\u3400-\u9fff]")
_ABSOLUTE_LOCAL = re.compile(r"/(?:Users|home)/[^/\s]+/")
_CREDENTIAL = re.compile(
    r"(?i)(?:authorization\s*[:=]|bearer\s+[a-z0-9_./+\-=]{20,}|api[_-]?key\s*[:=])"
)
_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _public_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.suffix == ".age" or path.suffix not in _TEXT_SUFFIXES:
            continue
        yield path


def guard_public_tree(root: Path, config_path: Path | None = None) -> int:
    markers: list[str] = []
    if config_path:
        config = load_json(config_path)
        raw_markers = config.get("private_markers", []) if isinstance(config, dict) else []
        if not isinstance(raw_markers, list) or not all(isinstance(item, str) for item in raw_markers):
            raise RuntimeError("private marker list is invalid")
        markers = [item for item in raw_markers if item]

    hits = 0
    for path in _public_files(root.resolve()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hits += 1
            continue
        if _CJK.search(text) or _ABSOLUTE_LOCAL.search(text) or _CREDENTIAL.search(text):
            hits += 1
            continue
        if any(marker.casefold() in text.casefold() for marker in markers):
            hits += 1
    if hits:
        print(f"stage=guard status=failed count={hits}")
        return 1
    print("stage=guard status=ok count=0")
    return 0
