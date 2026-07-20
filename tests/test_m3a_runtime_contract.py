from __future__ import annotations

import json
from pathlib import Path

from app import ArkProxyRouter, ConfigStore
from linrouter_core.runtime import SerialProtectionState


def _router(tmp_path: Path) -> ArkProxyRouter:
    config = {
        "groups": [{"id": "g1", "name": "relay", "provider_type": "relay", "base_url": "https://relay.example/v1", "route_key": "key", "waf_compatible": True, "serial_protection": True}],
        "models": [
            {"id": "m1", "name": "first", "ep_id": "model-1", "group_id": "g1", "api_key": "key-1", "usable": True},
            {"id": "m2", "name": "second", "ep_id": "model-2", "group_id": "g1", "api_key": "key-2", "usable": True},
        ],
        "aggregate_models": [{"id": "a1", "name": "aggregate", "route_key": "agg-key", "strategy": "priority"}],
        "aggregate_members": [
            {"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m2", "priority": 2, "enabled": True},
            {"id": "am2", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "priority": 1, "enabled": False},
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return ArkProxyRouter(ConfigStore(path), None, tmp_path / "logs.jsonl")


def test_m3a_candidate_facades_preserve_order_and_skip_logging(tmp_path: Path) -> None:
    router = _router(tmp_path)

    assert [candidate.label for candidate in router._iter_upstream_candidates(None, "g1")] == ["first", "second"]
    aggregate = router.store.find_aggregate("a1")
    assert aggregate is not None
    candidates = list(router._iter_aggregate_candidates(aggregate, log_skips=True, path="/v1/chat/completions", requested_label="aggregate", request_id="request", resolved_as="aggregate"))

    assert [candidate.label for candidate in candidates] == ["second"]
    assert candidates[0].aggregate_member_id == "am1"
    assert any("skip_reason=member_disabled" in log.detail for log in router.logs)


def test_m3a_error_classifier_and_health_facades_preserve_contract(tmp_path: Path) -> None:
    router = _router(tmp_path)

    assert router._classify_candidate_error(403, "Your request was blocked").category == "waf_blocked"
    assert router._classify_candidate_error(400, '{"error":{"type":"invalid_request_error"}}').failure_scope == "request"
    router._set_cooldown(0, "network failure", 60, "network")
    assert router.store.models[0].usable is True
    assert router.store.models[0].health_state == "observing"
    assert router.store.models[0].consecutive_failures == 1
    router._set_success(1)
    assert router.store.models[1].last_error == ""


def test_m3a_serial_protection_facades_preserve_shared_lock_and_busy_state(tmp_path: Path) -> None:
    router = _router(tmp_path)
    candidate = next(router._iter_upstream_candidates(None, "g1"))

    lock = router._candidate_lock(candidate, {})
    assert lock is not None
    assert lock is router._candidate_lock(candidate, {})
    router._mark_stream_active(candidate, 1)
    assert router._active_stream_count(candidate) == 1
    assert "fallback_reason=large_task_in_progress" in router._serial_protection_busy_detail(candidate, b"small", 12)
    router._mark_stream_active(candidate, -1)
    assert router._active_stream_count(candidate) == 0
    assert isinstance(router._runtime_locks, SerialProtectionState)
