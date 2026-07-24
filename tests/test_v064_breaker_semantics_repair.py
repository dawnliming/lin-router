from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from app import ArkProxyRouter, ConfigStore, RouterHandler
from linrouter_core.runtime.http_api_runtime import handle_post
from settings_store import SettingsStore


def _router(tmp_path: Path) -> ArkProxyRouter:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "groups": [{"id": "g1", "name": "group", "provider_type": "relay", "base_url": "https://relay.example/v1", "route_key": "group-key"}],
                "models": [{"id": "m1", "name": "model", "ep_id": "upstream", "group_id": "g1", "api_key": "secret", "usable": True}],
                "aggregate_models": [
                    {"id": "a1", "name": "aggregate-one", "route_key": "a1-key"},
                    {"id": "a2", "name": "aggregate-two", "route_key": "a2-key"},
                ],
                "aggregate_members": [
                    {"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "priority": 1, "enabled": True},
                    {"id": "am2", "aggregate_id": "a2", "group_id": "g1", "model_id": "m1", "priority": 1, "enabled": True},
                    {"id": "am3", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "priority": 2, "enabled": True},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(path)
    settings.update({"smart_breaker_enabled": True})
    return ArkProxyRouter(ConfigStore(path), settings, tmp_path / "logs.jsonl")


def _open_member_breaker(router: ArkProxyRouter, member_id: str) -> None:
    for _ in range(3):
        router._set_aggregate_member_cooldown(member_id, "upstream unavailable", 0, "server_error_503")


def test_aggregate_failure_only_breaks_member_and_group_model_remains_routable(tmp_path: Path) -> None:
    router = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    aggregate = router.store.find_aggregate("a1")
    model = router.store.find_model("m1")
    assert member and aggregate and model

    _open_member_breaker(router, member.id)

    assert member.health_state == "breaker_open"
    assert model.health_state == "normal"
    assert list(router._iter_upstream_candidates("model", "g1"))
    assert "am1" not in [candidate.aggregate_member_id for candidate in router._iter_aggregate_candidates(aggregate)]


def test_underlying_automatic_breaker_does_not_block_aggregate_but_manual_disable_does(tmp_path: Path) -> None:
    router = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    aggregate = router.store.find_aggregate("a1")
    model = router.store.find_model("m1")
    assert member and aggregate and model

    model.health_state = "breaker_open"
    model.breaker_until = int(time.time()) + 300
    model.usable = False
    assert router.candidate_health.aggregate_member_skip_reason(member)[0] == ""
    assert list(router._iter_aggregate_candidates(aggregate))

    model.disabled_by_user = True
    reason, _message, _group, _model = router.candidate_health.aggregate_member_skip_reason(member)
    assert reason == "underlying_model_disabled"


def test_same_model_in_two_aggregates_keeps_member_health_scopes_independent(tmp_path: Path) -> None:
    router = _router(tmp_path)
    member_a = router.store.find_aggregate_member("am1")
    member_b = router.store.find_aggregate_member("am2")
    aggregate_b = router.store.find_aggregate("a2")
    model = router.store.find_model("m1")
    assert member_a and member_b and aggregate_b and model

    _open_member_breaker(router, member_a.id)

    assert member_a.health_state == "breaker_open"
    assert member_b.health_state == "normal"
    assert list(router._iter_aggregate_candidates(aggregate_b))
    assert model.health_state == "normal"


def test_aggregate_recovery_is_one_save_no_probe_and_preserves_manual_boundaries(tmp_path: Path, monkeypatch) -> None:
    router = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    manual = router.store.find_aggregate_member("am3")
    model = router.store.find_model("m1")
    assert member and manual and model
    manual.enabled = False
    member.health_state = "breaker_open"
    member.breaker_until = int(time.time()) + 60
    member.breaker_level = 2
    member.qualified_failure_timestamps = [int(time.time())]
    model.health_state = "breaker_open"
    model.breaker_until = int(time.time()) + 60
    model.usable = False

    probe_called = False
    monkeypatch.setattr(router, "_manual_probe_candidate", lambda *_args: (_ for _ in ()).throw(AssertionError("不应发起上游探测")))
    original_save = router.store.save
    save_calls = {"count": 0}

    def counted_save() -> None:
        save_calls["count"] += 1
        original_save()

    monkeypatch.setattr(router.store, "save", counted_save)
    first = router.recover_aggregate_members("a1")
    assert first["recovered_count"] == 1
    assert first["manual_disabled_count"] == 1
    assert member.health_state == "observing"
    assert member.breaker_level == 2
    assert model.health_state == "breaker_open"
    assert model.usable is False
    assert save_calls["count"] == 1

    second = router.recover_aggregate_members("a1")
    assert second["recovered_count"] == 0
    assert second["already_normal_count"] == 1
    assert second["manual_disabled_count"] == 1
    assert save_calls["count"] == 1
    assert probe_called is False


def test_success_keeps_recent_failure_window_and_old_failures_expire(tmp_path: Path) -> None:
    router = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model
    now = int(time.time())
    model.qualified_failure_timestamps = [now - 301, now - 100]
    model.network_failure_timestamps = [now - 301]
    router.candidate_health.record_qualified_failure(0, "server error", 0, "server_error_500", "server_error")
    assert model.consecutive_failures == 2
    assert model.health_state == "observing"

    router.candidate_health.set_success(0)
    assert model.consecutive_failures == 2
    assert model.health_state == "observing"


def test_breaker_expiry_auto_observing_and_next_cycle_keeps_level(tmp_path: Path) -> None:
    router = _router(tmp_path)
    model = router.store.find_model("m1")
    assert model
    model.health_state = "breaker_open"
    model.usable = False
    model.breaker_level = 1
    model.breaker_until = int(time.time()) - 1
    model.qualified_failure_timestamps = [int(time.time()) - 1]
    router.store.save()

    router.store.refresh_expired_cooldowns()
    assert model.health_state == "observing"
    assert model.usable is True
    assert model.breaker_level == 1
    assert model.qualified_failure_timestamps == []
    assert not getattr(next(iter(router._iter_upstream_candidates("model", "g1"))), "health_probe_keys", ())

    for _ in range(3):
        router.candidate_health.record_qualified_failure(0, "server error", 0, "server_error_503", "server_error")
    assert model.health_state == "breaker_open"
    assert model.breaker_level == 2


def test_recover_members_http_contract_returns_counts(tmp_path: Path) -> None:
    router = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    assert member
    member.health_state = "observing"
    member.qualified_failure_timestamps = [int(time.time())]
    router.store.save()

    responses: list[tuple[dict, int]] = []
    handler = SimpleNamespace(
        path="/api/aggregates/a1/recover-members",
        router=router,
        store=router.store,
        _send_json=lambda payload, status=200: responses.append((payload, status)),
    )
    handle_post(handler)
    assert responses and responses[0][1] == 200
    assert responses[0][0]["recovered_count"] == 1
    assert responses[0][0]["already_normal_count"] == 1
