from __future__ import annotations

import json
import time

from app import ArkProxyRouter, ConfigStore
from settings_store import SettingsStore
from linrouter_core.runtime import CandidateHealthService


def _router(tmp_path):
    config = {
        "groups": [{"id": "g1", "name": "relay", "provider_type": "relay", "base_url": "https://relay.example/v1", "route_key": "key", "waf_compatible": True}],
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
    return ArkProxyRouter(ConfigStore(path), None, tmp_path / "logs.jsonl"), path


def test_candidate_health_service_is_the_single_runtime_owner(tmp_path) -> None:
    router, _ = _router(tmp_path)

    assert isinstance(router.candidate_health, CandidateHealthService)
    assert router.runtime.candidate_health is router.candidate_health
    assert [candidate.label for candidate in router._iter_upstream_candidates(None, "g1")] == ["first", "second"]


def test_candidate_health_preserves_manual_member_disable_and_reload(tmp_path) -> None:
    router, path = _router(tmp_path)
    aggregate = router.store.find_aggregate("a1")
    assert aggregate is not None
    assert [candidate.label for candidate in router._iter_aggregate_candidates(aggregate)] == ["second"]

    router._set_aggregate_member_cooldown("am1", "network failure", 60, "network")
    router._mark_aggregate_member_success("am2")
    reloaded = ConfigStore(path)
    cooling = reloaded.find_aggregate_member("am1")
    disabled = reloaded.find_aggregate_member("am2")
    assert cooling is not None and cooling.health_state == "observing"
    assert cooling.consecutive_failures == 1
    assert cooling.cooldown_until == 0
    assert disabled is not None and disabled.enabled is False


def test_candidate_health_writes_model_states_without_second_copy(tmp_path) -> None:
    router, path = _router(tmp_path)

    router._set_cooldown(0, "network failure", 60, "network")
    router._set_success(1)
    router._set_unusable(1, "quota exhausted")
    reloaded = ConfigStore(path)
    assert reloaded.models[0].usable is True
    assert reloaded.models[0].health_state == "observing"
    assert reloaded.models[0].consecutive_failures == 1
    assert reloaded.models[0].cooldown_until == 0
    assert reloaded.models[1].usable is False
    assert reloaded.models[1].cooldown_until == 0


def test_breaker_is_enabled_by_default_and_opens_after_three_of_five_failures(tmp_path) -> None:
    router, path = _router(tmp_path)
    router._set_cooldown(0, "upstream unavailable", 0, "network")
    router._set_success(0)
    router._set_cooldown(0, "upstream unavailable", 0, "network")
    router._set_success(0)
    router._set_cooldown(0, "upstream unavailable", 0, "network")
    reloaded = ConfigStore(path)
    assert reloaded.models[0].health_state == "breaker_open"
    assert reloaded.models[0].consecutive_failures == 3
    assert reloaded.models[0].breaker_level == 1
    assert reloaded.models[0].breaker_until > 0

    router._set_success(0)
    recovered = ConfigStore(path).models[0]
    assert recovered.health_state == "observing"
    assert recovered.consecutive_failures == 3
    assert recovered.breaker_level == 1
    assert recovered.breaker_until == 0


def test_expired_breaker_auto_returns_to_observing_for_automatic_selection(tmp_path) -> None:
    _router_instance, path = _router(tmp_path)
    settings = SettingsStore(path)
    settings.update({"smart_breaker_enabled": True})
    router = ArkProxyRouter(ConfigStore(path), settings, tmp_path / "logs-breaker.jsonl")
    model = router.store.models[0]
    model.usable = True
    model.health_state = "breaker_open"
    model.breaker_until = int(time.time()) - 1

    assert list(router._iter_upstream_candidates(None, "g1"))[0].label == "first"
    assert router.candidate_health.runtime_health_state(model) == "observing"


def test_disabled_breaker_does_not_filter_persisted_open_state(tmp_path) -> None:
    router, _ = _router(tmp_path)
    model = router.store.models[0]
    model.usable = False
    model.health_state = "breaker_open"
    model.breaker_until = int(time.time()) + 60
    model.cooldown_until = int(time.time()) + 60

    router.candidate_health.clear_system_health_states()
    assert [candidate.label for candidate in router._iter_upstream_candidates("first", "g1")] == ["first"]
    assert model.health_state == "normal"
    assert model.breaker_until == 0
    assert model.cooldown_until == 0
    assert model.usable is True


def test_aggregate_ignores_open_underlying_automatic_breaker(tmp_path) -> None:
    _router_instance, path = _router(tmp_path)
    settings = SettingsStore(path)
    settings.update({"smart_breaker_enabled": True})
    router = ArkProxyRouter(ConfigStore(path), settings, tmp_path / "logs-aggregate-breaker.jsonl")
    member = router.store.find_aggregate_member("am1")
    model = router.store.find_model("m2")
    assert member is not None and model is not None
    model.usable = False
    model.health_state = "breaker_open"
    model.breaker_until = int(time.time()) + 60
    model.cooldown_until = int(time.time()) + 60

    reason, _message, _group, _model = router.candidate_health.aggregate_member_skip_reason(member)
    assert reason == ""

    settings.update({"smart_breaker_enabled": False})
    router.candidate_health.clear_system_health_states()
    reason, _message, _group, _model = router.candidate_health.aggregate_member_skip_reason(member)
    assert reason == ""
    assert model.health_state == "normal"
    assert model.usable is True


def test_manual_probe_rejections_and_busy_restore_prior_health_without_counting(tmp_path) -> None:
    router, path = _router(tmp_path)
    settings = SettingsStore(path)
    settings.update({"smart_breaker_enabled": True})
    router = ArkProxyRouter(ConfigStore(path), settings, tmp_path / "logs-manual-probe.jsonl")
    model = router.store.find_model("m1")
    member = router.store.find_aggregate_member("am1")
    assert model is not None and member is not None

    router._set_cooldown(0, "network failure", 60, "network")
    for reason in ("waf_blocked", "request_level", "auth_error", "missing_upstream_api_key", "serial_protection_wait_timeout"):
        router._manual_probe_candidate = lambda _candidate, current_reason=reason: (False, current_reason, "probe rejected")
        result = router.recover_model(model.id)
        assert result["ok"] is False
        assert model.health_state == "observing"
        assert model.consecutive_failures == 1
        assert model.breaker_until == 0
        assert [candidate.label for candidate in router._iter_upstream_candidates("first", "g1")] == ["first"]

    router._set_aggregate_member_cooldown(member.id, "network failure", 60, "network")
    router._manual_probe_candidate = lambda _candidate: (False, "waf_blocked", "probe rejected")
    result = router.recover_aggregate_member(member.id)
    assert result["ok"] is False
    assert member.health_state == "observing"
    assert member.consecutive_failures == 1
    assert member.breaker_until == 0


def test_aggregate_member_breaker_isolated_and_success_keeps_observation_window(tmp_path) -> None:
    router, path = _router(tmp_path)
    settings = SettingsStore(path)
    settings.update({"smart_breaker_enabled": True})
    router = ArkProxyRouter(ConfigStore(path), settings, tmp_path / "logs-2.jsonl")
    member = router.store.find_aggregate_member("am1")
    assert member is not None

    for _ in range(3):
        router._set_aggregate_member_cooldown(member.id, "upstream unavailable", 0, "network")

    broken = ConfigStore(path).find_aggregate_member(member.id)
    assert broken is not None
    assert broken.health_state == "breaker_open"
    assert broken.consecutive_failures == 3
    assert broken.breaker_until > 0
    assert list(router._iter_aggregate_candidates(router.store.find_aggregate("a1"))) == []

    router._mark_aggregate_member_success(member.id)
    recovered = ConfigStore(path).find_aggregate_member(member.id)
    assert recovered is not None
    assert recovered.health_state == "observing"
    assert recovered.consecutive_failures == 3
    assert recovered.breaker_level == 1
    assert recovered.breaker_until == 0
