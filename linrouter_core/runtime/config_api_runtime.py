"""Configuration and backup API business logic behind ``RouterHandler`` I/O facades."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from linrouter_core.config.constants import PROVIDER_ARK, new_route_key
from linrouter_core.config.models import (
    AggregateMember,
    AggregateModel,
    ConnectionGroup,
    ModelConfig,
    RoutingPolicyValidationError,
    validate_routing_policy_payload,
)


class ConfigApiError(Exception):
    """Stable API error response for configuration import endpoints."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def response(self) -> Dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error",
                "code": self.code,
            }
        }


def normalize_routing_policy_item(
    item: Dict[str, Any],
    *,
    fallback_fixed_cooldown_minutes: int,
    fixed_cooldown_field: str,
) -> Dict[str, Any]:
    """将 API/导入输入规范化为 canonical 策略字段，不保留旧布尔字段。"""
    normalized = dict(item)
    try:
        policy = validate_routing_policy_payload(
            normalized,
            fallback_fixed_cooldown_minutes=fallback_fixed_cooldown_minutes,
            fixed_cooldown_field=fixed_cooldown_field,
        )
    except RoutingPolicyValidationError as error:
        raise ConfigApiError(error.message, error.code) from error
    normalized["routing_policy"] = policy
    # 固定冷却沿用既有分钟字段，拒绝继续写入错误的第二套字段。
    normalized.pop("fixed_cooldown_minutes", None)
    normalized.pop("smart_breaker_enabled", None)
    return normalized


def normalize_group_routing_policy_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """按连接组旧冷却字段补齐固定冷却策略的默认分钟数。"""
    return normalize_routing_policy_item(
        item,
        fallback_fixed_cooldown_minutes=int(item.get("auto_model_cooldown_minutes") or 5),
        fixed_cooldown_field="auto_model_cooldown_minutes",
    )


def normalize_aggregate_routing_policy_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """按聚合旧冷却字段补齐固定冷却策略的默认分钟数。"""
    return normalize_routing_policy_item(
        item,
        fallback_fixed_cooldown_minutes=int(item.get("cooldown_minutes") or 5),
        fixed_cooldown_field="cooldown_minutes",
    )


def export_config_payload(store: Any) -> Dict[str, Any]:
    # dataclass 中不再包含旧 smart_breaker_enabled，因此新导出天然只含 canonical 字段。
    return {
        "groups": [asdict(group) for group in store.groups],
        "models": [asdict(model) for model in store.models],
        "aggregate_models": [asdict(model) for model in store.aggregate_models],
        "aggregate_members": [asdict(member) for member in store.aggregate_members],
    }


def export_backup_payload(store: Any, settings_store: Any) -> Dict[str, Any]:
    return {**export_config_payload(store), "settings": settings_store.to_dict()}


def import_config_payload(store: Any, payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ConfigApiError("配置文件无效：必须是一个 JSON 对象", "invalid_config_file")
    groups_raw, models_raw, aggregates_raw, members_raw = _normalized_config_lists(payload)
    with store._lock:
        snapshot = _store_snapshot(store)
        try:
            for item in groups_raw:
                if not item.get("name"):
                    continue
                group = ConnectionGroup.from_dict(item)
                if not group.route_key:
                    group.route_key = new_route_key()
                if not group.provider_type:
                    group.provider_type = PROVIDER_ARK
                _replace_or_append(store.groups, group)
            for item in models_raw:
                if not item.get("name") or not item.get("ep_id"):
                    continue
                model = ModelConfig.from_dict(item)
                if not model.group_id or not store.find_group(model.group_id):
                    if store.groups:
                        model.group_id = store.groups[0].id
                    else:
                        continue
                _replace_or_append(store.models, model)
            imported_aggregate_ids: set[str] = set()
            aggregate_skip_reasons: List[str] = []
            for item in aggregates_raw:
                if not item.get("name"):
                    aggregate_skip_reasons.append("聚合模型缺少名称")
                    continue
                aggregate = AggregateModel.from_dict(item)
                ok, message = store._validate_aggregate_name(aggregate.name, aggregate.id)
                if ok:
                    ok, message = store._validate_aggregate_client_aliases(aggregate.client_model_aliases)
                if not ok:
                    aggregate_skip_reasons.append(f"聚合模型 {aggregate.name}：{message}")
                    continue
                _replace_or_append(store.aggregate_models, aggregate)
                imported_aggregate_ids.add(aggregate.id)
            for item in members_raw:
                member = AggregateMember.from_dict(item)
                if member.aggregate_id not in imported_aggregate_ids and not store.find_aggregate(member.aggregate_id):
                    continue
                if not store.find_group(member.group_id) or not store.find_model(member.model_id):
                    continue
                duplicate = next(
                    (
                        existing
                        for existing in store.aggregate_members
                        if existing.aggregate_id == member.aggregate_id
                        and existing.group_id == member.group_id
                        and existing.model_id == member.model_id
                        and existing.id != member.id
                    ),
                    None,
                )
                if duplicate:
                    continue
                _replace_or_append(store.aggregate_members, member)
            store._cleanup_orphan_members()
            # 与配置写入共用一次落盘：失败时由下方快照完整恢复，不留下半截导入。
            store._clear_health_states_locked(_is_policy_disabled_item(store))
            store.save()
        except Exception:
            _restore_store_snapshot(store, snapshot)
            raise
    return _import_response(store, aggregate_skip_reasons)


def import_backup_payload(store: Any, payload: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ConfigApiError("备份文件无效：必须是一个 JSON 对象", "invalid_backup_file")
    groups_raw, models_raw, aggregates_raw, members_raw = _normalized_config_lists(payload)
    settings_raw = payload.get("settings") or {}
    new_groups: List[ConnectionGroup] = []
    for item in groups_raw:
        if not item.get("name"):
            continue
        group = ConnectionGroup.from_dict(item)
        if not group.route_key:
            group.route_key = new_route_key()
        if not group.provider_type:
            group.provider_type = PROVIDER_ARK
        new_groups.append(group)
    new_models: List[ModelConfig] = []
    for item in models_raw:
        if not item.get("name") or not item.get("ep_id"):
            continue
        model = ModelConfig.from_dict(item)
        if not model.group_id or not any(group.id == model.group_id for group in new_groups):
            if new_groups:
                model.group_id = new_groups[0].id
            else:
                continue
        new_models.append(model)
    new_aggregates: List[AggregateModel] = []
    new_aggregate_ids: set[str] = set()
    for item in aggregates_raw:
        if not item.get("name"):
            continue
        aggregate = AggregateModel.from_dict(item)
        new_aggregates.append(aggregate)
        new_aggregate_ids.add(aggregate.id)
    new_members: List[AggregateMember] = []
    for item in members_raw:
        member = AggregateMember.from_dict(item)
        if member.aggregate_id not in new_aggregate_ids:
            continue
        if not any(group.id == member.group_id for group in new_groups) or not any(model.id == member.model_id for model in new_models):
            continue
        new_members.append(member)
    with store._lock:
        snapshot = _store_snapshot(store)
        try:
            store.groups = new_groups
            store.models = new_models
            store.aggregate_models = new_aggregates
            store.aggregate_members = new_members
            store.aggregate_member_revisions = {aggregate.id: 0 for aggregate in new_aggregates}
            store._clear_health_states_locked(_is_policy_disabled_item(store))
            store.save()
        except Exception:
            _restore_store_snapshot(store, snapshot)
            raise
    allowed = {
        "auto_start", "start_minimized", "theme", "auto_refresh_logs",
        "upstream_http_client", "upstream_http2", "upstream_keepalive",
        "debug_mode", "debug_capture_enabled", "debug_capture_last_body",
        "normalize_tools_order", "smart_breaker_enabled",
    }
    new_settings = {key: value for key, value in settings_raw.items() if key in allowed}
    return _backup_import_response(store), new_settings


def _normalized_config_lists(payload: Dict[str, Any]) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]], list[Dict[str, Any]], list[Dict[str, Any]]]:
    groups_raw = payload.get("groups") or []
    models_raw = payload.get("models") or []
    aggregates_raw = payload.get("aggregate_models") or []
    members_raw = payload.get("aggregate_members") or []
    if not isinstance(groups_raw, list) or not isinstance(models_raw, list):
        raise ConfigApiError("请求参数无效：groups 和 models 必须是数组", "invalid_payload")
    if not isinstance(aggregates_raw, list) or not isinstance(members_raw, list):
        raise ConfigApiError("请求参数无效：aggregate_models 和 aggregate_members 必须是数组", "invalid_payload")
    groups = [
        normalize_group_routing_policy_item(item)
        for item in groups_raw
        if isinstance(item, dict)
    ]
    aggregates = [
        normalize_aggregate_routing_policy_item(item)
        for item in aggregates_raw
        if isinstance(item, dict)
    ]
    return (
        groups,
        [dict(item) for item in models_raw if isinstance(item, dict)],
        aggregates,
        [dict(item) for item in members_raw if isinstance(item, dict)],
    )


def _is_policy_disabled_item(store: Any) -> Any:
    return lambda item: (
        isinstance(item, ModelConfig)
        and store._policy_disables_smart_breaker(store.find_group(item.group_id))
    ) or (
        isinstance(item, AggregateMember)
        and (
            store._policy_disables_smart_breaker(store.find_group(item.group_id))
            or store._policy_disables_smart_breaker(store.find_aggregate(item.aggregate_id))
        )
    )


def _store_snapshot(store: Any) -> Dict[str, Any]:
    """导入失败回滚内存状态；配置文件只在所有校验和清理完成后写入一次。"""
    return {
        "groups": list(store.groups),
        "models": list(store.models),
        "aggregate_models": list(store.aggregate_models),
        "aggregate_members": list(store.aggregate_members),
        "aggregate_member_revisions": dict(store.aggregate_member_revisions),
        "health": [(item, asdict(item)) for item in [*store.models, *store.aggregate_members]],
    }


def _restore_store_snapshot(store: Any, snapshot: Dict[str, Any]) -> None:
    store.groups = snapshot["groups"]
    store.models = snapshot["models"]
    store.aggregate_models = snapshot["aggregate_models"]
    store.aggregate_members = snapshot["aggregate_members"]
    store.aggregate_member_revisions = snapshot["aggregate_member_revisions"]
    for item, values in snapshot["health"]:
        store._restore_health_item(item, values)


def _replace_or_append(items: List[Any], replacement: Any) -> None:
    for index, existing in enumerate(items):
        if existing.id == replacement.id:
            items[index] = replacement
            return
    items.append(replacement)


def _backup_import_response(store: Any) -> Dict[str, Any]:
    return {
        "ok": True,
        "groups": len(store.groups),
        "models": len(store.models),
        "aggregate_models": len(store.aggregate_models),
        "aggregate_members": len(store.aggregate_members),
    }


def _import_response(store: Any, skipped_aggregates: List[str]) -> Dict[str, Any]:
    return {
        "ok": True,
        "groups": len(store.groups),
        "models": len(store.models),
        "aggregate_models": len(store.aggregate_models),
        "aggregate_members": len(store.aggregate_members),
        "skipped_aggregates": skipped_aggregates,
    }
