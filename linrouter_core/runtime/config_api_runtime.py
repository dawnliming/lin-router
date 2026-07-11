"""Configuration and backup API business logic behind ``RouterHandler`` I/O facades."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List

from linrouter_core.config.constants import PROVIDER_ARK, new_route_key
from linrouter_core.config.models import AggregateMember, AggregateModel, ConnectionGroup, ModelConfig


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


def export_config_payload(store: Any) -> Dict[str, Any]:
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
    groups_raw, models_raw, aggregates_raw, members_raw = _config_lists(payload)
    with store._lock:
        for item in groups_raw:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            group = ConnectionGroup.from_dict(item)
            if not group.route_key:
                group.route_key = new_route_key()
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
            _replace_or_append(store.groups, group)
        for item in models_raw:
            if not isinstance(item, dict) or not item.get("name") or not item.get("ep_id"):
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
            if not isinstance(item, dict) or not item.get("name"):
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
            if not isinstance(item, dict):
                continue
            member = AggregateMember.from_dict(item)
            if member.aggregate_id not in imported_aggregate_ids and not store.find_aggregate(member.aggregate_id):
                continue
            if not store.find_group(member.group_id) or not store.find_model(member.model_id):
                continue
            duplicate = next(
                (existing for existing in store.aggregate_members
                 if existing.aggregate_id == member.aggregate_id
                 and existing.group_id == member.group_id
                 and existing.model_id == member.model_id
                 and existing.id != member.id),
                None,
            )
            if duplicate:
                continue
            _replace_or_append(store.aggregate_members, member)
        store._cleanup_orphan_members()
        store.save()
    return _import_response(store, aggregate_skip_reasons)


def import_backup_payload(store: Any, payload: Any) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ConfigApiError("备份文件无效：必须是一个 JSON 对象", "invalid_backup_file")
    groups_raw, models_raw, aggregates_raw, members_raw = _config_lists(payload)
    settings_raw = payload.get("settings") or {}
    new_groups: List[ConnectionGroup] = []
    for item in groups_raw:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        group = ConnectionGroup.from_dict(item)
        if not group.route_key:
            group.route_key = new_route_key()
        if not group.provider_type:
            group.provider_type = PROVIDER_ARK
        new_groups.append(group)
    new_models: List[ModelConfig] = []
    for item in models_raw:
        if not isinstance(item, dict) or not item.get("name") or not item.get("ep_id"):
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
        if not isinstance(item, dict) or not item.get("name"):
            continue
        aggregate = AggregateModel.from_dict(item)
        new_aggregates.append(aggregate)
        new_aggregate_ids.add(aggregate.id)
    new_members: List[AggregateMember] = []
    for item in members_raw:
        if not isinstance(item, dict):
            continue
        member = AggregateMember.from_dict(item)
        if member.aggregate_id not in new_aggregate_ids:
            continue
        if not any(group.id == member.group_id for group in new_groups) or not any(model.id == member.model_id for model in new_models):
            continue
        new_members.append(member)
    with store._lock:
        store.groups = new_groups
        store.models = new_models
        store.aggregate_models = new_aggregates
        store.aggregate_members = new_members
        store.save()
    allowed = {
        "auto_start", "start_minimized", "theme", "auto_refresh_logs",
        "upstream_http_client", "upstream_http2", "upstream_keepalive",
        "debug_mode", "debug_capture_enabled", "debug_capture_last_body",
        "normalize_tools_order",
    }
    new_settings = {key: value for key, value in settings_raw.items() if key in allowed}
    return _backup_import_response(store), new_settings


def _config_lists(payload: Dict[str, Any]) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    groups_raw = payload.get("groups") or []
    models_raw = payload.get("models") or []
    aggregates_raw = payload.get("aggregate_models") or []
    members_raw = payload.get("aggregate_members") or []
    if not isinstance(groups_raw, list) or not isinstance(models_raw, list):
        raise ConfigApiError("请求参数无效：groups 和 models 必须是数组", "invalid_payload")
    return (
        groups_raw,
        models_raw,
        aggregates_raw if isinstance(aggregates_raw, list) else [],
        members_raw if isinstance(members_raw, list) else [],
    )


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
