"""冻结 PRD v1.0：候选稳定性窗口与递进熔断契约。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app import ArkProxyRouter, ConfigStore
from settings_store import SettingsStore


def _router(tmp_path: Path):
    config = {
        "groups": [{"id": "g1", "name": "relay", "provider_type": "relay", "base_url": "https://example.test/v1", "route_key": "r1"}],
        "models": [{"id": "m1", "name": "m1", "ep_id": "m1", "group_id": "g1", "api_key": "key", "usable": True}],
        "aggregate_models": [{"id": "a1", "name": "a1", "route_key": "a-key"}],
        "aggregate_members": [{"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "enabled": True}],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return ArkProxyRouter(ConfigStore(path), SettingsStore(path), tmp_path / "logs.jsonl")


def test_five_attempt_window_opens_after_three_failures_without_success_reset(tmp_path: Path) -> None:
    router = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None

    for reason in ("network", "success", "server_error_503", "success", "stream_incomplete"):
        if reason == "success":
            router.candidate_health.set_success(0)
        else:
            router.candidate_health.record_qualified_failure(0, reason, 0, reason)

    assert model.attempt_window == []
    assert len(model.qualified_failure_timestamps) == 3
    assert len(model.network_failure_timestamps) == 1
    assert model.health_state == "breaker_open"
    assert model.breaker_level == 1
    assert model.breaker_until > 0


def test_attempt_window_evicts_oldest_and_breaker_cooldown_escalates_to_ten_minutes(tmp_path: Path) -> None:
    router = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None

    for level in range(1, 5):
        reasons = ("network", "server_error_503", "stream_incomplete") if level == 1 else ("network",)
        for reason in reasons:
            router.candidate_health.record_qualified_failure(0, reason, 0, reason)
        assert model.health_state == "breaker_open"
        model.breaker_until = 0
        router.candidate_health.set_success(0)

    assert model.breaker_level == 4
    assert model.breaker_until == 0
    assert len(model.qualified_failure_timestamps) <= 5
    assert len(model.network_failure_timestamps) <= 5
    assert model.consecutive_failures == len(model.qualified_failure_timestamps)


def test_failure_window_is_capped_during_the_failure_write(tmp_path: Path) -> None:
    router = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None
    now = int(time.time())
    model.qualified_failure_timestamps = [now - offset for offset in range(1, 6)]
    model.network_failure_timestamps = [now - offset for offset in range(1, 6)]

    router.candidate_health.record_qualified_failure(0, "network", 0, "network")

    assert len(model.qualified_failure_timestamps) == 5
    assert len(model.network_failure_timestamps) == 5
    assert model.consecutive_failures == 5
    assert model.consecutive_network_failures == 5


def test_successes_after_breaker_recovery_keep_breaker_level_ladder(tmp_path: Path) -> None:
    router = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model is not None
    model.breaker_level = 3
    model.health_state = "breaker_open"
    model.breaker_until = 0

    for _ in range(5):
        router.candidate_health.set_success(0)

    assert model.health_state == "normal"
    assert model.breaker_level == 3
    assert model.attempt_window == []
