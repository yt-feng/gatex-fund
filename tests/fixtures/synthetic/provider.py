from __future__ import annotations

import json
from pathlib import Path


def collect(*, config, state, token, output: Path, emit):
    assert token == config.get("provider", {}).get("expected_token")
    emit(stage="discover", status="ok", count=1)
    known = set(state.get("known", []))
    item_id = "source-alpha-item-1"
    if item_id in known:
        return {"state": state, "new_count": 0, "discovered_count": 1}
    output.mkdir(parents=True, exist_ok=True)
    (output / "metadata.json").write_text(
        json.dumps({"id": item_id, "url": "https://example.invalid/item/1"}) + "\n",
        encoding="utf-8",
    )
    known.add(item_id)
    emit(stage="detail", status="ok", count=1)
    return {
        "state": {"version": 1, "known": sorted(known)},
        "new_count": 1,
        "discovered_count": 1,
    }
