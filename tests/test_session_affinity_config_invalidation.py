"""会话粘性在配置成功写入后的精确失效回归。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from linrouter_core.config.models import (
    AggregateMember,
    AggregateModel,
    ConnectionGroup,
    ModelConfig,
)
from linrouter_core.config.store import ConfigStore
from linrouter_core.runtime.config_api_runtime import (
    ConfigApiError,
    normalize_aggregate_routing_policy_item,
    normalize_group_routing_policy_item,
)
from linrouter_core.runtime.http_api_runtime import _invalidate_configuration_affinity
from linrouter_core.runtime.session_affinity import SessionAffinityService


def _candidate(model_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        aggregate_member_id="",
        model=SimpleNamespace(id=model_id),
        target_model=model_id,
    )


def _handler(tmp_path) -> tuple[SimpleNamespace, SessionAffinityService]:
    store = ConfigStore(tmp_path / "config.json")
    store.groups = [
        ConnectionGroup(id="g1", name="组一"),
        ConnectionGroup(id="g2", name="组二"),
    ]
    store.models = [
        ModelConfig(id="m1", name="模型一", ep_id="up-m1", group_id="g1"),
        ModelConfig(id="m2", name="模型二", ep_id="up-m2", group_id="g2"),
    ]
    store.aggregate_models = [
        AggregateModel(id="a1", name="聚合一"),
        AggregateModel(id="a2", name="聚合二"),
    ]
    store.aggregate_members = [
        AggregateMember(id="am1", aggregate_id="a1", group_id="g1", model_id="m1"),
        AggregateMember(id="am2", aggregate_id="a2", group_id="g2", model_id="m2"),
    ]
    affinity = SessionAffinityService()
    return SimpleNamespace(
        store=store,
        router=SimpleNamespace(runtime=SimpleNamespace(session_affinity=affinity)),
    ), affinity


def _bind(affinity: SessionAffinityService, scope: str, candidate: SimpleNamespace) -> None:
    affinity.bind(affinity.context(scope, "requested-model", f"session-{scope}"), candidate)


def _status(affinity: SessionAffinityService, scope: str, candidate: SimpleNamespace) -> str:
    context = affinity.context(scope, "requested-model", f"session-{scope}")
    _ordered, status = affinity.prioritize(context, [candidate])
    return status


def test_group_or_model_write_invalidates_direct_and_dependent_aggregate_scopes(tmp_path) -> None:
    handler, affinity = _handler(tmp_path)
    first = _candidate("m1")
    second = _candidate("m2")
    _bind(affinity, "group:g1", first)
    _bind(affinity, "aggregate:a1", first)
    _bind(affinity, "group:g2", second)
    _bind(affinity, "aggregate:a2", second)

    _invalidate_configuration_affinity(handler, group_ids=("g1",), model_ids=("m1",))

    assert _status(affinity, "group:g1", first) == "sticky_miss"
    assert _status(affinity, "aggregate:a1", first) == "sticky_miss"
    assert _status(affinity, "group:g2", second) == "sticky_hit"
    assert _status(affinity, "aggregate:a2", second) == "sticky_hit"


def test_explicit_aggregate_scope_does_not_clear_unrelated_group_scope(tmp_path) -> None:
    handler, affinity = _handler(tmp_path)
    first = _candidate("m1")
    _bind(affinity, "group:g1", first)
    _bind(affinity, "aggregate:a1", first)

    _invalidate_configuration_affinity(handler, aggregate_ids=("a1",))

    assert _status(affinity, "aggregate:a1", first) == "sticky_miss"
    assert _status(affinity, "group:g1", first) == "sticky_hit"


@pytest.mark.parametrize(
    ("normalizer", "payload", "code", "message_part"),
    [
        (
            normalize_group_routing_policy_item,
            {"routing_policy": "not-a-policy"},
            "invalid_routing_policy",
            "路由策略无效",
        ),
        (
            normalize_group_routing_policy_item,
            {"routing_policy": "fixed_cooldown", "auto_model_cooldown_minutes": 0},
            "invalid_fixed_cooldown_minutes",
            "固定冷却分钟数无效",
        ),
        (
            normalize_aggregate_routing_policy_item,
            {"routing_policy": "sticky_route", "smart_breaker_enabled": True},
            "conflicting_routing_policy",
            "路由策略冲突",
        ),
    ],
)
def test_group_and_aggregate_policy_normalizers_keep_chinese_message_and_stable_code(
    normalizer,
    payload,
    code,
    message_part,
) -> None:
    with pytest.raises(ConfigApiError) as raised:
        normalizer(payload)

    response = raised.value.response()["error"]
    assert response["message"].startswith(message_part)
    assert response["type"] == "invalid_request_error"
    assert response["code"] == code
