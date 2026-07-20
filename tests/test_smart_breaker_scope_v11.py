"""智能熔断 v1.1：全局、连接组、聚合模型三层作用域契约。"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

import pytest

from app import ArkProxyRouter, ConfigStore, RouterHandler, create_server
from linrouter_core.config.models import AggregateModel, ConnectionGroup
from settings_store import SettingsStore



def _raw_config() -> dict:
    return {
        "groups": [
            {
                "id": "g-a",
                "name": "组 A",
                "provider_type": "relay",
                "base_url": "https://a.example/v1",
                "route_key": "route-a",
            },
            {
                "id": "g-b",
                "name": "组 B",
                "provider_type": "relay",
                "base_url": "https://b.example/v1",
                "route_key": "route-b",
            },
        ],
        "models": [
            {
                "id": "m-a",
                "name": "模型 A",
                "ep_id": "model-a",
                "group_id": "g-a",
                "api_key": "key-a",
                "usable": True,
            },
            {
                "id": "m-b",
                "name": "模型 B",
                "ep_id": "model-b",
                "group_id": "g-b",
                "api_key": "key-b",
                "usable": True,
            },
        ],
        "aggregate_models": [
            {"id": "agg-x", "name": "聚合 X", "route_key": "agg-x-key"},
            {"id": "agg-y", "name": "聚合 Y", "route_key": "agg-y-key"},
        ],
        "aggregate_members": [
            {
                "id": "member-x-a",
                "aggregate_id": "agg-x",
                "group_id": "g-a",
                "model_id": "m-a",
                "priority": 1,
                "enabled": True,
            },
            {
                "id": "member-y-a",
                "aggregate_id": "agg-y",
                "group_id": "g-a",
                "model_id": "m-a",
                "priority": 1,
                "enabled": True,
            },
            {
                "id": "member-x-b",
                "aggregate_id": "agg-x",
                "group_id": "g-b",
                "model_id": "m-b",
                "priority": 2,
                "enabled": True,
            },
        ],
    }



def _router(tmp_path: Path, *, global_enabled: bool = True) -> tuple[ArkProxyRouter, SettingsStore, Path]:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_raw_config(), ensure_ascii=False), encoding="utf-8")
    settings = SettingsStore(config_path)
    settings.update({"smart_breaker_enabled": global_enabled})
    router = ArkProxyRouter(
        ConfigStore(config_path),
        settings,
        tmp_path / "logs.jsonl",
    )
    return router, settings, config_path



def _mark_health(item, *, state: str = "breaker_open") -> None:
    item.health_state = state
    item.consecutive_failures = 7
    item.last_failure_at = int(time.time())
    item.cooldown_until = 0
    item.cooldown_reason = ""
    item.breaker_until = int(time.time()) + 300
    item.breaker_reason = "server_error_503"
    item.last_error = "redacted_sha256:health,bytes:1"



def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(port: int, path: str) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_scope_fields_default_true_and_explicit_false_round_trip(tmp_path: Path) -> None:
    router, settings, config_path = _router(tmp_path)

    assert router.store.find_group("g-a").smart_breaker_enabled is True
    assert router.store.find_aggregate("agg-x").smart_breaker_enabled is True

    router.store.find_group("g-a").smart_breaker_enabled = False
    router.store.find_aggregate("agg-x").smart_breaker_enabled = False
    router.store.save()

    reloaded = ConfigStore(config_path)
    assert reloaded.find_group("g-a").smart_breaker_enabled is False
    assert reloaded.find_group("g-b").smart_breaker_enabled is True
    assert reloaded.find_aggregate("agg-x").smart_breaker_enabled is False
    assert reloaded.find_aggregate("agg-y").smart_breaker_enabled is True
    assert settings.get("smart_breaker_enabled") is True

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["groups"][0]["smart_breaker_enabled"] is False
    assert payload["aggregate_models"][0]["smart_breaker_enabled"] is False



def test_effective_policy_matrix_respects_global_group_and_aggregate(tmp_path: Path) -> None:
    router, settings, _config_path = _router(tmp_path)
    model = router.store.find_model("m-a")
    member = router.store.find_aggregate_member("member-x-a")
    assert model is not None and member is not None

    assert router.candidate_health.policy_status(model) == {
        "smart_breaker_effective_enabled": True,
        "smart_breaker_disabled_by": "",
    }
    assert router.candidate_health.policy_status(member) == {
        "smart_breaker_effective_enabled": True,
        "smart_breaker_disabled_by": "",
    }

    router.store.find_aggregate("agg-x").smart_breaker_enabled = False
    assert router.candidate_health.policy_status(model)["smart_breaker_effective_enabled"] is True
    assert router.candidate_health.policy_status(member) == {
        "smart_breaker_effective_enabled": False,
        "smart_breaker_disabled_by": "aggregate",
    }

    router.store.find_group("g-a").smart_breaker_enabled = False
    assert router.candidate_health.policy_status(model) == {
        "smart_breaker_effective_enabled": False,
        "smart_breaker_disabled_by": "group",
    }
    assert router.candidate_health.policy_status(member) == {
        "smart_breaker_effective_enabled": False,
        "smart_breaker_disabled_by": "group",
    }

    settings.update({"smart_breaker_enabled": False})
    assert router.candidate_health.policy_status(model) == {
        "smart_breaker_effective_enabled": False,
        "smart_breaker_disabled_by": "global",
    }
    assert router.candidate_health.policy_status(member) == {
        "smart_breaker_effective_enabled": False,
        "smart_breaker_disabled_by": "global",
    }



def test_group_disable_clears_only_group_models_and_cross_aggregate_members(tmp_path: Path) -> None:
    router, _settings, config_path = _router(tmp_path)
    model_a = router.store.find_model("m-a")
    model_b = router.store.find_model("m-b")
    member_x_a = router.store.find_aggregate_member("member-x-a")
    member_y_a = router.store.find_aggregate_member("member-y-a")
    member_x_b = router.store.find_aggregate_member("member-x-b")
    assert all(item is not None for item in (model_a, model_b, member_x_a, member_y_a, member_x_b))

    for item in (model_a, model_b, member_x_a, member_y_a, member_x_b):
        _mark_health(item)
    model_b.disabled_by_user = True
    model_b.usable = False
    member_x_b.enabled = False
    router.store.save()

    group_a = router.store.find_group("g-a")
    updated = ConnectionGroup.from_dict({**asdict(group_a), "smart_breaker_enabled": False})
    router.store.upsert_group(updated)

    assert router.store.find_group("g-a").smart_breaker_enabled is False
    assert model_a.health_state == "normal"
    assert model_a.consecutive_failures == 0
    assert model_a.breaker_until == 0
    assert member_x_a.health_state == "normal"
    assert member_y_a.health_state == "normal"
    # 非目标连接组完全不参与本次清理，已有系统状态和手动停用均保持原样。
    assert model_b.health_state == "breaker_open"
    assert model_b.disabled_by_user is True
    assert model_b.usable is False
    assert member_x_b.health_state == "breaker_open"
    assert member_x_b.enabled is False

    reloaded = ConfigStore(config_path)
    assert reloaded.find_group("g-a").smart_breaker_enabled is False
    assert reloaded.find_model("m-a").health_state == "normal"
    assert reloaded.find_aggregate_member("member-y-a").health_state == "normal"
    assert reloaded.find_model("m-b").health_state == "breaker_open"



def test_aggregate_disable_clears_only_selected_aggregate_members(tmp_path: Path) -> None:
    router, _settings, _config_path = _router(tmp_path)
    member_x_a = router.store.find_aggregate_member("member-x-a")
    member_y_a = router.store.find_aggregate_member("member-y-a")
    model_a = router.store.find_model("m-a")
    assert member_x_a is not None and member_y_a is not None and model_a is not None

    for item in (member_x_a, member_y_a, model_a):
        _mark_health(item)
    router.store.save()

    aggregate_x = router.store.find_aggregate("agg-x")
    updated = AggregateModel.from_dict({**asdict(aggregate_x), "smart_breaker_enabled": False})
    router.store.upsert_aggregate(updated)

    assert router.store.find_aggregate("agg-x").smart_breaker_enabled is False
    assert member_x_a.health_state == "normal"
    assert member_x_a.consecutive_failures == 0
    assert member_y_a.health_state == "breaker_open"
    assert model_a.health_state == "breaker_open"



def test_scope_close_rolls_back_flag_and_health_when_save_fails(tmp_path: Path) -> None:
    router, _settings, _config_path = _router(tmp_path)
    group = router.store.find_group("g-a")
    model = router.store.find_model("m-a")
    assert group is not None and model is not None
    _mark_health(model)
    router.store.save()
    before = asdict(model)
    original_save = router.store.save

    def fail_save() -> None:
        raise OSError("disk full")

    router.store.save = fail_save  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        router.store.upsert_group(ConnectionGroup.from_dict({**asdict(group), "smart_breaker_enabled": False}))
    router.store.save = original_save  # type: ignore[method-assign]

    assert router.store.find_group("g-a").smart_breaker_enabled is True
    assert asdict(router.store.find_model("m-a")) == before



def test_disabled_scope_does_not_read_or_write_stale_health(tmp_path: Path) -> None:
    router, _settings, _config_path = _router(tmp_path)
    group = router.store.find_group("g-a")
    model = router.store.find_model("m-a")
    member = router.store.find_aggregate_member("member-x-a")
    assert group is not None and model is not None and member is not None
    group.smart_breaker_enabled = False
    model_health_before = asdict(model)
    member_health_before = asdict(member)

    router._set_cooldown(0, "server error", 60, "server_error_503")
    router._set_aggregate_member_cooldown(member.id, "server error", 60, "server_error_503")

    assert asdict(model) == model_health_before
    assert asdict(member) == member_health_before
    assert list(router._iter_upstream_candidates("模型 A", "g-a"))



def test_aggregate_policy_does_not_bypass_underlying_group_policy(tmp_path: Path) -> None:
    router, _settings, _config_path = _router(tmp_path)
    aggregate_x = router.store.find_aggregate("agg-x")
    model_a = router.store.find_model("m-a")
    member_x_a = router.store.find_aggregate_member("member-x-a")
    assert aggregate_x is not None and model_a is not None and member_x_a is not None

    _mark_health(member_x_a)
    aggregate_x.smart_breaker_enabled = False
    assert list(router._iter_aggregate_candidates(aggregate_x))

    _mark_health(model_a)
    # 聚合关闭只忽略成员级状态；底层模型 A 仍按连接组策略跳过，组 B 成员继续可用。
    assert [candidate.aggregate_member_id for candidate in router._iter_aggregate_candidates(aggregate_x)] == [
        "member-x-b"
    ]

    router.store.find_group("g-a").smart_breaker_enabled = False
    assert list(router._iter_aggregate_candidates(aggregate_x))



def test_disabled_aggregate_member_recover_api_rejects_without_manual_probe(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_raw_config(), ensure_ascii=False), encoding="utf-8")
    server, port, _ = create_server("127.0.0.1", _free_port(), config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        member = server.store.find_aggregate_member("member-x-a")
        aggregate = server.store.find_aggregate("agg-x")
        assert member is not None and aggregate is not None
        _mark_health(member)
        server.store.save()
        server.store.upsert_aggregate(
            AggregateModel.from_dict({**asdict(aggregate), "smart_breaker_enabled": False})
        )

        probe_calls = []
        server.router._manual_probe_candidate = (
            lambda candidate: probe_calls.append(candidate) or (True, "probe_ok", "ok")
        )
        log_count = len(server.router.logs)

        status, payload = _post_json(port, "/api/aggregate-members/member-x-a/recover")

        assert status == 400
        assert payload["ok"] is False
        assert payload["code"] == "smart_breaker_disabled"
        assert payload["smart_breaker_effective_enabled"] is False
        assert payload["smart_breaker_disabled_by"] == "aggregate"
        assert probe_calls == []
        assert len(server.router.logs) == log_count
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("disabled_by", ["global", "group"])
def test_disabled_model_recover_api_rejects_without_manual_probe(
    tmp_path: Path,
    disabled_by: str,
) -> None:
    """关闭全局或连接组策略后，旧页面/API 不能再触发真实模型探测。"""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_raw_config(), ensure_ascii=False), encoding="utf-8")
    server, port, _ = create_server("127.0.0.1", _free_port(), config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        model = server.store.find_model("m-a")
        group = server.store.find_group("g-a")
        assert model is not None and group is not None

        if disabled_by == "global":
            assert server.router.settings_store is not None
            server.router.settings_store.update({"smart_breaker_enabled": False})
        else:
            server.store.upsert_group(
                ConnectionGroup.from_dict({**asdict(group), "smart_breaker_enabled": False})
            )

        # 模拟关闭范围前遗留的健康状态；恢复请求不得读取或改写它。
        _mark_health(model)
        server.store.save()
        health_before = asdict(model)
        candidate_calls = []
        probe_calls = []
        original_candidate_from_model = server.router._candidate_from_model

        def record_candidate(*args):
            candidate_calls.append(args)
            return original_candidate_from_model(*args)

        server.router._candidate_from_model = record_candidate
        server.router._manual_probe_candidate = (
            lambda candidate: probe_calls.append(candidate) or (True, "probe_ok", "ok")
        )
        log_count = len(server.router.logs)

        status, payload = _post_json(port, "/api/models/m-a/recover")

        expected_label = "全局" if disabled_by == "global" else "连接组"
        assert status == 400
        assert payload == {
            "ok": False,
            "message": f"{expected_label}智能熔断已关闭，不能执行模型重试恢复。",
            "code": "smart_breaker_disabled",
            "smart_breaker_effective_enabled": False,
            "smart_breaker_disabled_by": disabled_by,
        }
        assert candidate_calls == []
        assert probe_calls == []
        assert len(server.router.logs) == log_count
        assert asdict(server.store.find_model("m-a")) == health_before
    finally:
        server.shutdown()
        server.server_close()


def test_runtime_projection_exposes_scope_disabled_source(tmp_path: Path) -> None:
    router, _settings, _config_path = _router(tmp_path)
    handler = object.__new__(RouterHandler)
    handler.server = type("Server", (), {"router": router, "store": router.store})()
    model = router.store.find_model("m-a")
    member = router.store.find_aggregate_member("member-x-a")
    assert model is not None and member is not None

    router.store.find_aggregate("agg-x").smart_breaker_enabled = False
    member_item = handler._member_runtime_item(member)
    assert member_item["derived_status"] == "breaker_policy_disabled"
    assert member_item["smart_breaker_effective_enabled"] is False
    assert member_item["smart_breaker_disabled_by"] == "aggregate"

    router.store.find_group("g-a").smart_breaker_enabled = False
    model_item = handler._model_runtime_item(model)
    assert model_item["derived_status"] == "breaker_policy_disabled"
    assert model_item["smart_breaker_effective_enabled"] is False
    assert model_item["smart_breaker_disabled_by"] == "group"



def test_scope_fields_are_included_in_frontend_forms_and_api_contract() -> None:
    root = Path(__file__).resolve().parent.parent
    config_js = (root / "static/js/config-tab.js").read_text(encoding="utf-8")
    form_js = (root / "static/js/config-tab-form.js").read_text(encoding="utf-8")
    actions_js = (root / "static/js/config-tab-actions.js").read_text(encoding="utf-8")
    runtime_js = (root / "static/js/config-tab-runtime.js").read_text(encoding="utf-8")
    config_css = (root / "static/css/config-tab.css").read_text(encoding="utf-8")
    settings_js = (root / "static/js/settings-panel.js").read_text(encoding="utf-8")
    http_api = (root / "linrouter_core/runtime/http_api_runtime.py").read_text(encoding="utf-8")
    app_py = (root / "app.py").read_text(encoding="utf-8")

    # 三个表单均处于明确的全局、连接组或聚合上下文中，开关名称统一保持简洁。
    assert config_js.count("<span>智能熔断</span>") >= 2
    assert "对此连接组启用智能熔断" not in config_js
    assert "对此聚合模型启用成员级智能熔断" not in config_js
    assert "group-smart-breaker-enabled" in config_js
    assert "aggregate-smart-breaker-enabled" in config_js
    assert config_js.count("checkbox smart-breaker-toggle") == 2
    assert (
        ".form-row > .checkbox.smart-breaker-toggle {\n"
        "  justify-content: flex-end;\n"
        "}"
    ) in config_css
    assert "smart_breaker_enabled" in form_js
    assert "smart_breaker_enabled" in actions_js
    assert "Modal.confirm" in actions_js
    assert "stillViewingSavedAggregate" in actions_js
    assert "同步移除已关闭策略下的恢复按钮" in actions_js
    assert "data-member-actions" in config_js
    assert "if (!canRecover && recover)" in runtime_js
    assert "controller.onRecoverAggregateMember(member.id)" in runtime_js
    assert "m?.smart_breaker_effective_enabled !== false" in config_js
    assert "m?.derived_status !== 'breaker_policy_disabled'" in config_js
    assert "m?.smart_breaker_effective_enabled !== false" in runtime_js
    assert "m?.derived_status !== 'breaker_policy_disabled'" in runtime_js
    assert "smart_breaker_enabled" in http_api
    assert '"code": "smart_breaker_disabled"' in app_py
    assert "不能执行模型重试恢复" in app_py
    assert "<span>智能熔断</span>" in settings_js
    assert "全局智能熔断总开关" not in settings_js
