from __future__ import annotations

import json
import time
from types import SimpleNamespace

from app import ArkProxyRouter, ConfigStore, RouterHandler
from settings_store import SettingsStore


def _router(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "groups": [
                    {
                        "id": "g1",
                        "name": "relay",
                        "provider_type": "relay",
                        "base_url": "https://relay.example/v1",
                        "route_key": "route-key",
                    }
                ],
                "models": [
                    {
                        "id": "m1",
                        "name": "first",
                        "ep_id": "model-1",
                        "group_id": "g1",
                        "api_key": "key-1",
                        "usable": True,
                    },
                    {
                        "id": "m2",
                        "name": "second",
                        "ep_id": "model-2",
                        "group_id": "g1",
                        "api_key": "key-2",
                        "usable": True,
                    },
                ],
                "aggregate_models": [{"id": "a1", "name": "aggregate", "route_key": "aggregate-key"}],
                "aggregate_members": [
                    {
                        "id": "am1",
                        "aggregate_id": "a1",
                        "group_id": "g1",
                        "model_id": "m1",
                        "priority": 1,
                        "enabled": True,
                    },
                    {
                        "id": "am2",
                        "aggregate_id": "a1",
                        "group_id": "g1",
                        "model_id": "m2",
                        "priority": 2,
                        "enabled": True,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(config_path)
    return ArkProxyRouter(ConfigStore(config_path), settings, tmp_path / "logs.jsonl"), settings


def test_missing_setting_defaults_to_enabled_and_explicit_false_is_preserved(tmp_path) -> None:
    router, settings = _router(tmp_path)

    assert settings.get("smart_breaker_enabled") is True
    assert router.candidate_health.is_enabled() is True

    settings.update({"smart_breaker_enabled": False})
    assert SettingsStore(tmp_path / "config.json").get("smart_breaker_enabled") is False


def test_model_and_member_use_prd_failure_ladder(tmp_path) -> None:
    router, _settings = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    assert member is not None

    for expected_count in range(1, 4):
        router._set_cooldown(0, "upstream unavailable", 0, "server_error_503")
        router._set_aggregate_member_cooldown(member.id, "upstream unavailable", 0, "server_error_503")
        model = router.store.models[0]
        assert model.health_state == "observing"
        assert model.consecutive_failures == expected_count
        assert model.cooldown_until == 0
        assert member.health_state == "observing"
        assert member.consecutive_failures == expected_count
        assert member.cooldown_until == 0

    for expected_count, expected_state, expected_seconds in (
        (4, "cooling", 30),
        (5, "cooling", 60),
        (6, "cooling", 180),
        (7, "breaker_open", 300),
    ):
        before = int(time.time())
        router._set_cooldown(0, "upstream unavailable", 0, "server_error_503")
        router._set_aggregate_member_cooldown(member.id, "upstream unavailable", 0, "server_error_503")
        model = router.store.models[0]
        assert model.consecutive_failures == expected_count
        assert model.health_state == expected_state
        assert member.consecutive_failures == expected_count
        assert member.health_state == expected_state
        deadline = model.breaker_until if expected_state == "breaker_open" else model.cooldown_until
        member_deadline = member.breaker_until if expected_state == "breaker_open" else member.cooldown_until
        assert before + expected_seconds <= deadline <= before + expected_seconds + 1
        assert before + expected_seconds <= member_deadline <= before + expected_seconds + 1


def test_expired_cooling_observes_and_expired_breaker_requires_single_probe(tmp_path) -> None:
    router, _settings = _router(tmp_path)
    model = router.store.models[0]
    model.health_state = "cooling"
    model.consecutive_failures = 4
    model.cooldown_until = int(time.time()) - 1
    router.store.save()

    router.store.refresh_expired_cooldowns()
    assert model.health_state == "observing"
    assert model.consecutive_failures == 4
    assert model.cooldown_until == 0

    model.health_state = "breaker_open"
    model.consecutive_failures = 7
    model.breaker_until = int(time.time()) - 1
    router.store.save()

    first = list(router._iter_upstream_candidates("first", "g1"))
    assert [candidate.label for candidate in first] == ["first"]
    assert model.health_state == "breaker_open"
    assert router.candidate_health.runtime_health_state(model) == "half_open_probe"
    assert list(router._iter_upstream_candidates("first", "g1")) == []

    router._set_success(0)
    assert model.health_state == "normal"
    assert model.consecutive_failures == 0
    assert model.breaker_until == 0


def test_disabling_breaker_clears_system_health_but_keeps_manual_disable(tmp_path) -> None:
    router, settings = _router(tmp_path)
    model = router.store.models[0]
    member = router.store.find_aggregate_member("am1")
    assert member is not None

    model.disabled_by_user = True
    model.usable = False
    model.health_state = "breaker_open"
    model.consecutive_failures = 7
    model.breaker_until = int(time.time()) + 300
    model.last_error = "redacted_sha256:1234567890abcdef,bytes:8"
    member.health_state = "cooling"
    member.consecutive_failures = 4
    member.cooldown_until = int(time.time()) + 30
    member.last_error = "redacted_sha256:abcdef1234567890,bytes:8"
    router.store.save()

    settings.update({"smart_breaker_enabled": False})
    router.candidate_health.clear_system_health_states()

    assert model.disabled_by_user is True
    assert model.usable is False
    assert model.health_state == "manual_disabled"
    assert model.consecutive_failures == 0
    assert model.breaker_until == 0
    assert model.last_error == ""
    assert member.enabled is True
    assert member.health_state == "normal"
    assert member.consecutive_failures == 0
    assert member.cooldown_until == 0
    assert member.last_error == ""


def _open_breaker(router, *, model_index: int = 0, member_id: str | None = None) -> None:
    """通过真实失败累计进入 breaker，禁止测试直接伪造 usable 状态。"""
    for _ in range(7):
        router._set_cooldown(model_index, "upstream unavailable", 0, "server_error_503")
        if member_id:
            router._set_aggregate_member_cooldown(
                member_id,
                "upstream unavailable",
                0,
                "server_error_503",
            )


def test_expired_breaker_yields_one_real_probe_candidate_and_release_allows_retry(tmp_path) -> None:
    router, _settings = _router(tmp_path)
    model = router.store.models[0]
    _open_breaker(router)
    assert model.health_state == "breaker_open"
    assert model.usable is False

    model.breaker_until = int(time.time()) - 1
    first = list(router._iter_upstream_candidates(None, "g1"))
    assert [candidate.label for candidate in first] == ["first", "second"]
    assert router.candidate_health.runtime_health_state(model) == "half_open_probe"
    assert [candidate.label for candidate in router._iter_upstream_candidates(None, "g1")] == ["second"]

    router.candidate_health.release_probe(first[0])
    assert [candidate.label for candidate in router._iter_upstream_candidates(None, "g1")] == ["first", "second"]


def test_member_and_underlying_breaker_projection_never_reports_healthy(tmp_path) -> None:
    router, _settings = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    assert member is not None
    _open_breaker(router, member_id=member.id)
    model = router.store.models[0]
    assert member.health_state == "breaker_open"
    assert model.health_state == "breaker_open"
    assert model.usable is False

    handler = object.__new__(RouterHandler)
    handler.server = SimpleNamespace(router=router, store=router.store)
    member_item = handler._member_runtime_item(member)
    model_item = handler._model_runtime_item(model)

    assert member_item["derived_status"] == "breaker_open"
    assert member_item["derived_status"] != "healthy"
    assert member_item["consecutive_failures"] == 7
    assert member_item["breaker_until"] == member.breaker_until
    assert model_item["derived_status"] == "breaker_open"
    assert model_item["usable"] is False


def test_aggregate_probe_releases_member_and_underlying_model_leases(tmp_path) -> None:
    router, _settings = _router(tmp_path)
    member = router.store.find_aggregate_member("am1")
    aggregate = router.store.find_aggregate("a1")
    assert member is not None
    assert aggregate is not None

    _open_breaker(router, member_id=member.id)
    model = router.store.models[0]
    member.breaker_until = int(time.time()) - 1
    model.breaker_until = int(time.time()) - 1

    candidates = list(router._iter_aggregate_candidates(aggregate))
    assert candidates
    first = candidates[0]
    assert set(first.health_probe_keys) == {f"member:{member.id}", f"model:{model.id}"}
    assert router.candidate_health.runtime_health_state(member) == "half_open_probe"
    assert router.candidate_health.runtime_health_state(model) == "half_open_probe"

    router.candidate_health.release_probe(first)
    assert router.candidate_health.runtime_health_state(member) == "breaker_open"
    assert router.candidate_health.runtime_health_state(model) == "breaker_open"


def test_expired_member_breaker_releases_lease_when_candidate_cannot_be_built(tmp_path) -> None:
    scenarios = (
        ("disabled_model", "underlying_model_disabled"),
        ("missing_group", "underlying_group_missing"),
        ("missing_model", "underlying_model_missing"),
    )
    for scenario, expected_reason in scenarios:
        scenario_dir = tmp_path / scenario
        scenario_dir.mkdir()
        router, _settings = _router(scenario_dir)
        member = router.store.find_aggregate_member("am1")
        assert member is not None
        _open_breaker(router, member_id=member.id)
        member.breaker_until = int(time.time()) - 1

        if scenario == "disabled_model":
            model = router.store.find_model(member.model_id)
            assert model is not None
            model.disabled_by_user = True
            model.usable = False
        elif scenario == "missing_group":
            member.group_id = "missing-group"
        else:
            member.model_id = "missing-model"

        for _ in range(2):
            reason, _message, _group, _model = router._aggregate_member_skip_reason(member)
            assert reason == expected_reason
            assert router.candidate_health.runtime_health_state(member) == "breaker_open"
