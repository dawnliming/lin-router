from __future__ import annotations

import json
from pathlib import Path

import pytest

import linrouter_core.config.store as store_module
from linrouter_core.config.store import ConfigStore


def test_save_retries_transient_windows_access_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"groups": [], "models": []}), encoding="utf-8")
    store = ConfigStore(config_path)
    original_replace = type(config_path).replace
    attempts = 0
    sleeps: list[float] = []

    def flaky_replace(source: Path, target: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError(13, "access denied", str(source), str(target))
            error.winerror = 5  # type: ignore[attr-defined]
            raise error
        return original_replace(source, target)

    monkeypatch.setattr(type(config_path), "replace", flaky_replace)
    monkeypatch.setattr(store_module.os, "name", "nt")
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    store.save()

    assert attempts == 3
    assert sleeps == [0.05, 0.1]
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "groups": [],
        "models": [],
        "aggregate_models": [],
        "aggregate_members": [],
        "aggregate_member_revisions": {},
    }
