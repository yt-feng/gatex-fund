from __future__ import annotations

import copy
import re
from pathlib import Path

from .io import atomic_write_json, load_json


class RuntimeConfigurationError(RuntimeError):
    status = "config"


def configure_tikhub_runtime(config_path: Path, profile_path: Path, maximum_items: int) -> None:
    config, profile = load_json(config_path), load_json(profile_path)
    if not isinstance(config, dict) or not isinstance(profile, dict):
        raise RuntimeConfigurationError("runtime profile is invalid")
    provider, approved = config.get("provider"), profile.get("provider")
    intake = profile.get("intelligence_intake")
    if not isinstance(provider, dict) or not isinstance(approved, dict) or not isinstance(intake, dict):
        raise RuntimeConfigurationError("API source profile is unavailable")
    if intake.get("enabled") is not True or intake.get("verification_status") != "verified":
        raise RuntimeConfigurationError("API source profile is not approved")
    if not re.fullmatch(r"gh_[A-Za-z0-9_]{3,61}", str(intake.get("tikhub_username") or "")):
        raise RuntimeConfigurationError("API source username is invalid")
    publisher = str(intake.get("publisher") or "").strip().casefold()
    expected = str(provider.get("source_name") or "").strip().casefold()
    biz = str(provider.get("expected_biz") or "")
    if not publisher or publisher != expected or not biz or approved.get("expected_biz") != biz:
        raise RuntimeConfigurationError("API source does not match the archive profile")
    if type(maximum_items) is not int or not 1 <= maximum_items <= 20:
        raise RuntimeConfigurationError("API article limit is invalid")
    updated = copy.deepcopy(config)
    updated["token_env"] = "TIKHUB_WECHAT_TOKEN"
    updated["intelligence_intake"] = copy.deepcopy(intake)
    updated["provider"].update(
        collection_method="tikhub",
        max_new_articles_per_run=maximum_items,
        max_api_pages_per_run=2,
        max_api_calls_per_run=maximum_items * 4 + 6,
    )
    atomic_write_json(config_path, updated)
