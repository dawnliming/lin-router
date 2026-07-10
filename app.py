from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import ssl

try:
    import certifi
    _ssl_context = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi 未安装时回退到系统默认，Windows 不受影响
    _ssl_context = ssl.create_default_context()

from linrouter_platform import get_platform
from settings_store import SettingsStore
from debug_capture import DebugCapture


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


DEFAULT_CONFIG_FILE = "lin-router-config.json"
DEFAULT_START_PORT = 18400
DEFAULT_AUTO_MODEL_NAME = "lin-router-auto"
DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES = 5
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 120
MAX_STREAM_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_PUBLIC_API_KEY = "lin-router"
GLOBAL_ROUTE_GROUP_ID = "__global__"
PROVIDER_ARK = "ark"
PROVIDER_RELAY = "relay"
PROVIDER_PROXY = "proxy"
MAX_PORT_SCAN = 1

BLOCKED_FORWARD_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "transfer-encoding",
    "host",
    "openai-organization",
    "openai-project",
    "x-request-id",
}

WAF_STRIP_PREFIXES = (
    "x-stainless-",
)

WAF_STRIP_EXACT = {
    "host",
    "connection",
    "content-length",
    "user-agent",
    "cache-control",
    "pragma",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "authorization",
    "openai-organization",
    "openai-project",
    "x-request-id",
}

PASSTHROUGH_STRIP_EXACT = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "authorization",
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def render_index_page() -> str:
    page_path = get_platform().get_resource_path("static", "index.html")
    html = page_path.read_text(encoding="utf-8")
    return html.replace("__AUTO_MODEL_NAME__", DEFAULT_AUTO_MODEL_NAME)


def new_route_key() -> str:
    return f"lr-{uuid.uuid4().hex[:16]}"


def new_aggregate_route_key() -> str:
    return f"lr-ag-{uuid.uuid4().hex[:16]}"


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def build_upstream_headers(api_key: str, *, stream: bool) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def build_waf_compatible_headers(incoming_headers: Dict[str, str], upstream_host: str, *, stream: bool) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in incoming_headers.items():
        lower = name.strip().lower()
        if not lower or lower in WAF_STRIP_EXACT or any(lower.startswith(prefix) for prefix in WAF_STRIP_PREFIXES):
            continue
        headers[name] = value
    headers["host"] = upstream_host
    headers["user-agent"] = BROWSER_UA
    if not any(k.lower() == "accept" for k in headers):
        headers["accept"] = "application/json, text/event-stream, */*"
    if not any(k.lower() == "accept-language" for k in headers):
        headers["accept-language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    return headers


def build_passthrough_headers(api_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in incoming_headers.items():
        lower = name.strip().lower()
        if not lower or lower in PASSTHROUGH_STRIP_EXACT:
            continue
        headers[name] = value
    headers["Authorization"] = f"Bearer {api_key}"
    headers["Content-Type"] = headers.get("Content-Type") or headers.get("content-type") or "application/json"
    if stream and not any(key.lower() == "accept" for key in headers):
        headers["Accept"] = "text/event-stream"
    elif not stream and not any(key.lower() == "accept" for key in headers):
        headers["Accept"] = "application/json"
    return headers


def build_model_fetch_headers(auth_key: str) -> Dict[str, str]:
    return {
        "authorization": f"Bearer {auth_key}",
        "user-agent": BROWSER_UA,
        "accept": "application/json, text/event-stream, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
    }


def can_forward_header(name: str) -> bool:
    normalized = name.strip().lower()
    return bool(normalized) and normalized not in BLOCKED_FORWARD_HEADERS and not normalized.startswith("x-stainless-")


def parse_bearer_key(auth_header: str) -> str:
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


REQUEST_LEVEL_ERROR_TYPES = {
    "invalid_request_error",
    "unsupported_parameter_error",
    "unsupported_parameter",
    "content_policy",
    "content_policy_violation",
    "request_format_error",
    "invalid_message_format",
    "authentication_error",
    "permission_error",
}


@dataclass
class ConnectionGroup:
    id: str
    name: str
    provider_type: str = PROVIDER_ARK
    base_url: str = DEFAULT_BASE_URL
    ark_api_key: str = ""
    api_key: str = ""
    route_key: str = ""
    auto_model_name: str = ""
    auto_model_cooldown_minutes: int = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
    stream_idle_timeout: int = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    waf_compatible: bool = False
    waf_accept_policy: str = "default"
    upstream_models: List[Dict[str, Any]] = field(default_factory=list)
    upstream_models_fetched_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionGroup":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            provider_type=str(data.get("provider_type") or PROVIDER_ARK),
            base_url=data.get("base_url") or DEFAULT_BASE_URL,
            ark_api_key=data.get("ark_api_key") or "",
            api_key=str(data.get("api_key") or ""),
            route_key=str(data.get("route_key") or ""),
            auto_model_name=str(data.get("auto_model_name") or "").strip() or DEFAULT_AUTO_MODEL_NAME,
            auto_model_cooldown_minutes=int(data.get("auto_model_cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES),
            stream_idle_timeout=max(0, min(MAX_STREAM_IDLE_TIMEOUT_SECONDS, int(data.get("stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS) or 0))),
            waf_compatible=bool(data.get("waf_compatible", False)),
            waf_accept_policy=str(data.get("waf_accept_policy") or "default"),
            upstream_models=[item for item in data.get("upstream_models", []) if isinstance(item, dict)] if isinstance(data.get("upstream_models", []), list) else [],
            upstream_models_fetched_at=str(data.get("upstream_models_fetched_at") or ""),
        )


@dataclass
class ModelConfig:
    id: str
    name: str
    ep_id: str
    group_id: str
    upstream_model: str = ""
    api_key: str = ""
    price_group: str = ""
    price_input: float = 0.0
    price_output: float = 0.0
    usable: bool = True
    disabled_by_user: bool = False
    last_error: str = ""
    last_success_at: str = ""
    last_checked_at: str = ""
    cooldown_until: int = 0
    cooldown_reason: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            ep_id=data["ep_id"],
            group_id=str(data.get("group_id") or ""),
            upstream_model=str(data.get("upstream_model") or ""),
            api_key=str(data.get("api_key") or ""),
            price_group=str(data.get("price_group") or ""),
            price_input=float(data.get("price_input") or 0),
            price_output=float(data.get("price_output") or 0),
            usable=bool(data.get("usable", True)),
            disabled_by_user=bool(data.get("disabled_by_user", False)),
            last_error=str(data.get("last_error", "")),
            last_success_at=str(data.get("last_success_at", "")),
            last_checked_at=str(data.get("last_checked_at", "")),
            cooldown_until=int(data.get("cooldown_until") or 0),
            cooldown_reason=str(data.get("cooldown_reason", "")),
        )


@dataclass
class AggregateModel:
    id: str
    name: str
    display_name: str = ""
    description: str = ""
    route_key: str = ""
    enabled: bool = True
    strategy: str = "priority"
    cooldown_minutes: int = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AggregateModel":
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            route_key=str(data.get("route_key") or "").strip(),
            enabled=bool(data.get("enabled", True)),
            strategy=str(data.get("strategy") or "priority").strip() or "priority",
            cooldown_minutes=int(data.get("cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES),
            created_at=str(data.get("created_at") or now),
            updated_at=str(data.get("updated_at") or now),
        )


@dataclass
class AggregateMember:
    id: str
    aggregate_id: str
    group_id: str
    model_id: str
    priority: int = 0
    manual_price: float | None = None
    weight: int = 100
    enabled: bool = True
    cooldown_until: int = 0
    cooldown_reason: str = ""
    last_error: str = ""
    last_success_at: str = ""
    last_checked_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AggregateMember":
        manual_price = data.get("manual_price")
        if manual_price is None or manual_price == "":
            manual_price = None
        else:
            try:
                manual_price = float(manual_price)
            except Exception:
                manual_price = None
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            aggregate_id=str(data.get("aggregate_id") or ""),
            group_id=str(data.get("group_id") or ""),
            model_id=str(data.get("model_id") or ""),
            priority=int(data.get("priority") or 0),
            manual_price=manual_price,
            weight=int(data.get("weight") or 100),
            enabled=bool(data.get("enabled", True)),
            cooldown_until=int(data.get("cooldown_until") or 0),
            cooldown_reason=str(data.get("cooldown_reason", "")),
            last_error=str(data.get("last_error", "")),
            last_success_at=str(data.get("last_success_at", "")),
            last_checked_at=str(data.get("last_checked_at", "")),
        )


@dataclass
class RequestLog:
    time: str
    path: str
    model: str
    status: str
    detail: str = ""
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    group_id: str = ""
    group_name: str = ""
    provider_type: str = ""
    event: str = ""
    request_id: str = ""
    attempt: int = 0
    usage_source: str = ""
    # 聚合调度链路字段
    requested_model: str = ""
    resolved_as: str = ""
    aggregate_model: str = ""
    aggregate_id: str = ""
    aggregate_member_id: str = ""
    selected_group: str = ""
    selected_model: str = ""
    selected_upstream_model: str = ""
    selection_reason: str = ""
    fallback_index: int = 0
    fallback_chain: str = ""
    member_cooled_down: bool = False
    cooldown_applied: bool = False
    failure_scope: str = ""


@dataclass
class UpstreamCandidate:
    idx: Optional[int]
    group: ConnectionGroup
    model: Optional[ModelConfig]
    label: str
    target_model: str
    auth_key: str
    channel: str = ""
    aggregate_id: str = ""
    aggregate_name: str = ""
    aggregate_member_id: str = ""
    manual_price: float | None = None


@dataclass
class RouteContext:
    client_key: str
    group: Optional[ConnectionGroup]
    group_id: str
    provider_type: str
    base_url: str
    display_name: str
    # passthrough 标识：proxy/ark 模式为 True，请求体与头部尽量原样转发；
    # relay 模式为 False，允许 WAF 兼容、冷却、自动调度等专属逻辑介入。
    passthrough: bool = True
    # 全局 Key 标识：使用 DEFAULT_PUBLIC_API_KEY 时跨所有组调度（已退役）
    is_global: bool = False
    # 聚合模型入口：使用 AggregateModel.route_key 时命中
    aggregate: Optional[AggregateModel] = None
    # 旧全局 Key 已退役，需要明确提示用户
    is_deprecated_global: bool = False


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.groups: List[ConnectionGroup] = []
        self.models: List[ModelConfig] = []
        self.aggregate_models: List[AggregateModel] = []
        self.aggregate_members: List[AggregateMember] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.groups = []
            self.models = []
            self.aggregate_models = []
            self.aggregate_members = []
            self.save()
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            self.groups = []
            self.models = []
            self.aggregate_models = []
            self.aggregate_members = []
            return

        if not isinstance(raw, dict):
            self.groups = []
            self.models = []
            self.aggregate_models = []
            self.aggregate_members = []
            return

        groups_raw = raw.get("groups", [])
        models_raw = raw.get("models", [])
        aggregates_raw = raw.get("aggregate_models", [])
        members_raw = raw.get("aggregate_members", [])
        if isinstance(groups_raw, list):
            self.groups = [ConnectionGroup.from_dict(x) for x in groups_raw if isinstance(x, dict)]
        else:
            self.groups = []
        changed = False
        for group in self.groups:
            if not group.route_key:
                group.route_key = new_route_key()
                changed = True
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
                changed = True

        if not isinstance(models_raw, list):
            if changed:
                self.save()
            self.models = []
            return

        legacy_models = [ModelConfig.from_dict(x) for x in models_raw if isinstance(x, dict)]
        if not self.groups and legacy_models:
            group_map: Dict[tuple[str, str], ConnectionGroup] = {}
            migrated_models: List[ModelConfig] = []
            for model in legacy_models:
                legacy = next((x for x in models_raw if isinstance(x, dict) and x.get("id") == model.id), {})
                base_url = str(legacy.get("base_url") or DEFAULT_BASE_URL)
                api_key = str(legacy.get("ark_api_key") or "")
                key = (base_url, api_key)
                group = group_map.get(key)
                if group is None:
                    group = ConnectionGroup(
                        id=uuid.uuid4().hex,
                        name=f"{base_url} · {len(group_map) + 1}",
                        base_url=base_url,
                        provider_type=PROVIDER_ARK,
                        ark_api_key=api_key,
                        route_key=new_route_key(),
                    )
                    group_map[key] = group
                model.group_id = group.id
                migrated_models.append(model)
            self.groups = list(group_map.values())
            self.models = migrated_models
            self.save()
            return

        self.models = legacy_models
        if self.groups:
            group_ids = {g.id for g in self.groups}
            for model in self.models:
                if not model.group_id or model.group_id not in group_ids:
                    model.group_id = self.groups[0].id
                    changed = True

        self.aggregate_models = [AggregateModel.from_dict(x) for x in aggregates_raw if isinstance(x, dict)] if isinstance(aggregates_raw, list) else []
        self.aggregate_members = [AggregateMember.from_dict(x) for x in members_raw if isinstance(x, dict)] if isinstance(members_raw, list) else []
        # 旧配置升级：为没有 route_key 的聚合模型自动生成
        for agg in self.aggregate_models:
            if not str(agg.route_key or "").strip():
                agg.route_key = new_aggregate_route_key()
                changed = True
        # 清理 orphan 成员
        if self._cleanup_orphan_members():
            changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            payload = {
                "groups": [asdict(g) for g in self.groups],
                "models": [asdict(m) for m in self.models],
                "aggregate_models": [asdict(m) for m in self.aggregate_models],
                "aggregate_members": [asdict(m) for m in self.aggregate_members],
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def _cleanup_orphan_members(self) -> bool:
        """删除引用不存在 group/model 的聚合成员。"""
        with self._lock:
            group_ids = {g.id for g in self.groups}
            model_ids = {m.id for m in self.models}
            before = len(self.aggregate_members)
            self.aggregate_members = [
                member for member in self.aggregate_members
                if member.aggregate_id in {a.id for a in self.aggregate_models}
                and member.group_id in group_ids
                and member.model_id in model_ids
            ]
            return len(self.aggregate_members) != before

    def find_aggregate(self, aggregate_id: str) -> Optional[AggregateModel]:
        return next((a for a in self.aggregate_models if a.id == aggregate_id), None)

    def find_aggregate_by_name(self, name: str) -> Optional[AggregateModel]:
        return next((a for a in self.aggregate_models if a.name == name), None)

    def find_aggregate_by_route_key(self, route_key: str) -> Optional[AggregateModel]:
        return next((a for a in self.aggregate_models if a.route_key == route_key), None)

    def get_aggregate_members(self, aggregate_id: str) -> List[AggregateMember]:
        return [m for m in self.aggregate_members if m.aggregate_id == aggregate_id]

    def find_aggregate_member(self, member_id: str) -> Optional[AggregateMember]:
        return next((m for m in self.aggregate_members if m.id == member_id), None)

    def _validate_aggregate_name(self, name: str, exclude_id: Optional[str] = None) -> Tuple[bool, str]:
        name = str(name or "").strip()
        if not name:
            return False, "聚合模型名不能为空"
        if name in {DEFAULT_AUTO_MODEL_NAME, "all-router-auto"}:
            return False, f"聚合模型名不能为保留名 {name}"
        for group in self.groups:
            if name == self._group_auto_model_name_static(group):
                return False, f"聚合模型名与连接组自动路由模型名冲突: {name}"
        for model in self.models:
            if name in {model.id, model.name, model.ep_id}:
                return False, f"聚合模型名与真实模型冲突: {name}"
        for agg in self.aggregate_models:
            if agg.id != exclude_id and agg.name == name:
                return False, f"聚合模型名已存在: {name}"
        return True, ""

    def _validate_aggregate_route_key(
        self, route_key: str, exclude_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        route_key = str(route_key or "").strip()
        if not route_key:
            return False, "聚合模型 Key 不能为空"
        if route_key == DEFAULT_PUBLIC_API_KEY:
            return False, f"聚合模型 Key 不能为保留 Key {DEFAULT_PUBLIC_API_KEY}"
        for group in self.groups:
            if group.route_key == route_key:
                return False, "聚合模型 Key 与连接组 Key 冲突"
        for agg in self.aggregate_models:
            if agg.id != exclude_id and agg.route_key == route_key:
                return False, "聚合模型 Key 已存在"
        return True, ""

    @staticmethod
    def _group_auto_model_name_static(group: ConnectionGroup) -> str:
        if group and group.auto_model_name and group.auto_model_name.strip():
            return group.auto_model_name.strip()
        return DEFAULT_AUTO_MODEL_NAME

    def upsert_aggregate(self, aggregate: AggregateModel) -> Tuple[bool, str]:
        with self._lock:
            ok, msg = self._validate_aggregate_name(aggregate.name, aggregate.id)
            if not ok:
                return False, msg
            # 新建或更新时若 route_key 为空，自动生成 lr-ag- 前缀 Key
            if not str(aggregate.route_key or "").strip():
                for _ in range(100):
                    aggregate.route_key = new_aggregate_route_key()
                    ok, _ = self._validate_aggregate_route_key(aggregate.route_key, aggregate.id)
                    if ok:
                        break
            ok, msg = self._validate_aggregate_route_key(aggregate.route_key, aggregate.id)
            if not ok:
                return False, msg
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            aggregate.updated_at = now
            existing = self.find_aggregate(aggregate.id)
            if existing:
                aggregate.created_at = existing.created_at or now
                for idx, item in enumerate(self.aggregate_models):
                    if item.id == aggregate.id:
                        self.aggregate_models[idx] = aggregate
                        break
            else:
                aggregate.created_at = now
                self.aggregate_models.append(aggregate)
            self.save()
            return True, ""

    def remove_aggregate(self, aggregate_id: str) -> Tuple[bool, int]:
        with self._lock:
            before = len(self.aggregate_models)
            self.aggregate_models = [a for a in self.aggregate_models if a.id != aggregate_id]
            removed_models = len(self.aggregate_models) != before
            before_members = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.aggregate_id != aggregate_id]
            removed_members_count = before_members - len(self.aggregate_members)
            if removed_models or removed_members_count:
                self.save()
            return removed_models, removed_members_count

    def upsert_aggregate_member(self, member: AggregateMember) -> Tuple[bool, str]:
        with self._lock:
            if not self.find_aggregate(member.aggregate_id):
                return False, "聚合模型不存在"
            group = self.find_group(member.group_id)
            if not group:
                return False, "连接组不存在"
            if group.provider_type != PROVIDER_RELAY:
                return False, "聚合成员只能来自 relay 连接组"
            if not self.find_model(member.model_id):
                return False, "模型不存在"
            existing = next((m for m in self.aggregate_members if m.id == member.id), None)
            # 手动启用聚合成员时自动清冷却：避免 checkbox 启用后仍被 _aggregate_member_usable 跳过
            if existing and member.enabled:
                now_ts = int(time.time())
                if not existing.enabled or (existing.cooldown_until and existing.cooldown_until > now_ts):
                    member.cooldown_until = 0
                    member.cooldown_reason = ""
                    member.last_error = ""
                    member.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            # 同一聚合模型内 (group_id, model_id) 不重复
            duplicate = next(
                (m for m in self.aggregate_members
                 if m.aggregate_id == member.aggregate_id
                 and m.group_id == member.group_id
                 and m.model_id == member.model_id
                 and m.id != member.id),
                None,
            )
            if duplicate:
                return False, "该连接组/模型组合已存在于当前聚合模型"
            if existing:
                for idx, item in enumerate(self.aggregate_members):
                    if item.id == member.id:
                        self.aggregate_members[idx] = member
                        break
            else:
                # 新成员默认放在末尾
                if member.priority == 0:
                    siblings = self.get_aggregate_members(member.aggregate_id)
                    max_priority = max((m.priority for m in siblings), default=0)
                    member.priority = max_priority + 1
                self.aggregate_members.append(member)
            self.save()
            return True, ""

    def remove_aggregate_member(self, member_id: str) -> bool:
        with self._lock:
            before = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.id != member_id]
            changed = len(self.aggregate_members) != before
            if changed:
                self.save()
            return changed

    def move_aggregate_member(self, member_id: str, direction: str) -> bool:
        with self._lock:
            member = next((m for m in self.aggregate_members if m.id == member_id), None)
            if not member:
                return False
            siblings = sorted(
                [m for m in self.aggregate_members if m.aggregate_id == member.aggregate_id],
                key=lambda m: m.priority,
            )
            idx = next((i for i, m in enumerate(siblings) if m.id == member_id), -1)
            if idx < 0:
                return False
            if direction == "up":
                new_idx = idx - 1
            elif direction == "down":
                new_idx = idx + 1
            elif direction == "top":
                new_idx = 0
            elif direction == "bottom":
                new_idx = len(siblings) - 1
            else:
                return False
            if new_idx < 0 or new_idx >= len(siblings) or new_idx == idx:
                return True
            siblings[idx], siblings[new_idx] = siblings[new_idx], siblings[idx]
            for i, m in enumerate(siblings):
                m.priority = i + 1
            self.save()
            return True

    def clear_aggregate_member_cooldown(self, member_id: str, now_str: Optional[str] = None) -> bool:
        with self._lock:
            member = next((m for m in self.aggregate_members if m.id == member_id), None)
            if not member:
                return False
            member.enabled = True
            member.cooldown_until = 0
            member.cooldown_reason = ""
            member.last_error = ""
            member.last_checked_at = now_str or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            self.save()
            return True

    def remove_members_for_group(self, group_id: str) -> int:
        with self._lock:
            before = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.group_id != group_id]
            removed = before - len(self.aggregate_members)
            if removed:
                self.save()
            return removed

    def remove_members_for_model(self, model_id: str) -> int:
        with self._lock:
            before = len(self.aggregate_members)
            self.aggregate_members = [m for m in self.aggregate_members if m.model_id != model_id]
            removed = before - len(self.aggregate_members)
            if removed:
                self.save()
            return removed

    def refresh_expired_cooldowns(self) -> bool:
        with self._lock:
            now = int(time.time())
            changed = False
            for model in self.models:
                if model.cooldown_until and model.cooldown_until <= now:
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    if not model.disabled_by_user:
                        model.usable = True
                    model.last_error = ""
                    model.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                    changed = True
            for member in self.aggregate_members:
                if member.cooldown_until and member.cooldown_until <= now:
                    member.cooldown_until = 0
                    member.cooldown_reason = ""
                    member.last_error = ""
                    member.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                    changed = True
            if changed:
                self.save()
            return changed

    def upsert_group(self, group: ConnectionGroup) -> None:
        with self._lock:
            if not group.route_key:
                existing = self.find_group(group.id)
                group.route_key = existing.route_key if existing and existing.route_key else new_route_key()
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
            for idx, item in enumerate(self.groups):
                if item.id == group.id:
                    self.groups[idx] = group
                    self.save()
                    return
            self.groups.append(group)
            self.save()

    def upsert_model(self, model: ModelConfig) -> None:
        with self._lock:
            for idx, item in enumerate(self.models):
                if item.id == model.id:
                    self.models[idx] = model
                    self.save()
                    return
            self.models.append(model)
            self.save()

    def remove_model(self, model_id: str) -> bool:
        with self._lock:
            before = len(self.models)
            self.models = [m for m in self.models if m.id != model_id]
            changed = len(self.models) != before
            if self.remove_members_for_model(model_id):
                changed = True
            if changed:
                self.save()
            return changed

    def move_model(self, model_id: str, direction: str) -> bool:
        with self._lock:
            idx = next((i for i, m in enumerate(self.models) if m.id == model_id), -1)
            if idx < 0:
                return False
            group_id = self.models[idx].group_id
            group_positions = [i for i, model in enumerate(self.models) if model.group_id == group_id]
            local_idx = next((i for i, pos in enumerate(group_positions) if pos == idx), -1)
            if local_idx < 0:
                return False
            group_models = [self.models[pos] for pos in group_positions]
            if direction == "up":
                new_local_idx = local_idx - 1
            elif direction == "down":
                new_local_idx = local_idx + 1
            elif direction == "bottom":
                new_local_idx = len(group_models) - 1
            elif direction == "top":
                new_local_idx = 0
            else:
                return False
            if new_local_idx < 0 or new_local_idx >= len(group_positions):
                return False
            if new_local_idx == local_idx:
                return True
            model = group_models.pop(local_idx)
            group_models.insert(new_local_idx, model)
            for pos, model in zip(group_positions, group_models):
                self.models[pos] = model
            self.save()
            return True

    def reset_usable(self) -> None:
        with self._lock:
            changed = False
            for model in self.models:
                if not model.usable or model.last_error or model.disabled_by_user:
                    model.usable = True
                    model.disabled_by_user = False
                    model.last_error = ""
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    changed = True
            if changed:
                self.save()

    def toggle_group(self, group_id: str) -> bool:
        """切换指定连接组下所有模型的可用状态（组内全可用则全部禁用，否则全部启用）。"""
        with self._lock:
            group_models = [m for m in self.models if m.group_id == group_id]
            if not group_models:
                return False
            all_usable = all(m.usable and not m.disabled_by_user for m in group_models)
            changed = False
            for model in group_models:
                target = not all_usable
                if model.usable != target or model.disabled_by_user == target:
                    model.usable = target
                    model.disabled_by_user = not target
                    if target:
                        model.cooldown_until = 0
                        model.cooldown_reason = ""
                        model.last_error = ""
                    changed = True
            if changed:
                self.save()
            return changed

    def find_group(self, group_id: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.id == group_id), None)

    def find_group_by_route_key(self, route_key: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.route_key == route_key), None)

    def find_model(self, model_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.id == model_id), None)

    def find_model_by_group_ep(self, group_id: str, ep_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.group_id == group_id and m.ep_id == ep_id), None)

    def remove_group(self, group_id: str) -> Tuple[bool, int, int]:
        """删除连接组，级联删除组下模型和聚合成员。"""
        with self._lock:
            before_groups = len(self.groups)
            before_models = len(self.models)
            self.models = [m for m in self.models if m.group_id != group_id]
            self.groups = [g for g in self.groups if g.id != group_id]
            removed_models = before_models - len(self.models)
            removed_members = self.remove_members_for_group(group_id)
            group_removed = len(self.groups) != before_groups
            if group_removed or removed_models or removed_members:
                self.save()
            return group_removed, removed_models, removed_members


class AllModelsFailedError(RuntimeError):
    """所有候选模型均不可用时抛出，便于 HTTP 层返回 503。"""

    def __init__(self, message: str, attempted: int = 0, stream_timeout: bool = False, error_code: str = "", fallback_chain: Optional[List[Dict[str, Any]]] = None, aggregate_name: str = "") -> None:
        super().__init__(message)
        self.attempted = attempted
        self.stream_timeout = stream_timeout
        self.error_code = error_code
        self.fallback_chain = fallback_chain or []
        self.aggregate_name = aggregate_name


class StreamIdleTimeoutError(TimeoutError):
    pass


class ArkProxyRouter:
    def __init__(self, store: ConfigStore, settings_store: Optional[SettingsStore] = None) -> None:
        self.store = store
        self.settings_store = settings_store
        self.logs: List[RequestLog] = []
        self.log_file = self._resolve_log_file()
        self.log_write_error = ""
        self.upstream_locks: Dict[str, threading.Lock] = {}
        self.upstream_active_streams: Dict[str, int] = {}
        self.upstream_locks_guard = threading.Lock()
        self.live_requests: Dict[str, Dict[str, Any]] = {}
        self.live_requests_lock = threading.Lock()
        self._upstream_client = self._create_upstream_client()
        self.debug_capture = DebugCapture(self, settings_store)
        self._load_log_file()

    def _create_upstream_client(self) -> "UpstreamClient":
        from upstream_client import UpstreamClient

        if self.settings_store is None:
            return UpstreamClient(client_type="urllib", ssl_context=_ssl_context)
        client_type = str(self.settings_store.get("upstream_http_client", "urllib")).lower()
        http2 = bool(self.settings_store.get("upstream_http2", False))
        keepalive = bool(self.settings_store.get("upstream_keepalive", False))
        return UpstreamClient(client_type=client_type, http2=http2, keepalive=keepalive, ssl_context=_ssl_context)

    def _refresh_upstream_client(self) -> None:
        if self.settings_store is None:
            return
        client_type = str(self.settings_store.get("upstream_http_client", "urllib")).lower()
        http2 = bool(self.settings_store.get("upstream_http2", False))
        keepalive = bool(self.settings_store.get("upstream_keepalive", False))
        current = self._upstream_client
        if (
            current.client_type != client_type
            or current.http2 != http2
            or current.keepalive != keepalive
        ):
            current.close()
            self._upstream_client = UpstreamClient(client_type=client_type, http2=http2, keepalive=keepalive, ssl_context=_ssl_context)

    def _resolve_log_file(self) -> Path:
        candidates = [
            get_platform().get_log_dir() / "lin-router-logs.jsonl",
            Path.home() / ".lin-router" / "logs" / "lin-router-logs.jsonl",
            Path(tempfile.gettempdir()) / "lin-router-logs.jsonl",
        ]
        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                with candidate.open("a", encoding="utf-8"):
                    pass
                return candidate
            except Exception:
                continue
        return candidates[-1]

    def add_log(
        self,
        path: str,
        model: str,
        status: str,
        detail: str = "",
        duration_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        group: Optional[ConnectionGroup] = None,
        event: str = "",
        request_id: str = "",
        attempt: int = 0,
        usage_source: str = "",
        cooldown_applied: bool = False,
        failure_scope: str = "",
    ) -> None:
        detail = self._sanitize_detail(detail)
        group_id = group.id if group else self._detail_value(detail, "group_id")
        group_name = group.name if group else self._detail_value(detail, "group_name")
        provider_type = group.provider_type if group else self._detail_value(detail, "provider")
        item = RequestLog(
            self._now(),
            path,
            model,
            status,
            detail[:5000],
            duration_ms,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_tokens,
            reasoning_tokens,
            group_id,
            group_name,
            provider_type,
            event or self._infer_event(status, detail),
            request_id,
            attempt,
            usage_source,
            requested_model=self._detail_value(detail, "requested"),
            resolved_as=self._detail_value(detail, "resolved_as"),
            aggregate_model=self._detail_value(detail, "aggregate_model"),
            aggregate_id=self._detail_value(detail, "aggregate_id"),
            aggregate_member_id=self._detail_value(detail, "aggregate_member_id"),
            selected_group=self._detail_value(detail, "selected_group"),
            selected_model=self._detail_value(detail, "selected_model"),
            selected_upstream_model=self._detail_value(detail, "selected_upstream_model"),
            selection_reason=self._detail_value(detail, "selection_reason"),
            fallback_index=int(self._detail_value(detail, "fallback_index") or 0),
            fallback_chain=self._detail_value(detail, "fallback_chain"),
            member_cooled_down=self._detail_value(detail, "member_cooled_down") == "true",
            cooldown_applied=cooldown_applied or self._detail_value(detail, "cooldown_applied") == "true",
            failure_scope=failure_scope or self._detail_value(detail, "failure_scope") or "",
        )
        self.logs.insert(0, item)
        self._append_log_file(item)
        del self.logs[80:]
        self._trim_log_file()

    def _trim_log_file(self, max_lines: int = 1000) -> None:
        # 日志文件滚动策略：保留最近 1000 条，避免磁盘无限增长
        try:
            if not self.log_file.exists():
                return
            with self.log_file.open("r", encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
            if len(lines) <= max_lines:
                return
            with self.log_file.open("w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
        except Exception as exc:
            self.log_write_error = f"日志滚动失败: {exc}"

    @staticmethod
    def _detail_value(detail: str, key: str) -> str:
        match = re.search(rf"(?:^|; ){re.escape(key)}=([^;]*)", detail or "")
        return match.group(1).strip() if match else ""

    @staticmethod
    def _infer_event(status: str, detail: str) -> str:
        text = f"{status} {detail}".lower()
        if "cooldown" in text:
            return "cooldown"
        if "retry ok" in text:
            return "retry_ok"
        if "try next" in text:
            return "fallback"
        if "stream ok" in text:
            return "stream_ok"
        if "skip" in text or "missing upstream api key" in text:
            return "skip"
        if "network" in text:
            return "network"
        if str(status).startswith("2"):
            return "ok"
        return "error"

    def _sanitize_detail(self, detail: str) -> str:
        if not detail:
            return ""
        safe = str(detail)
        secrets: List[str] = []
        for group in self.store.groups:
            secrets.extend([group.ark_api_key, group.api_key])
        for model in self.store.models:
            secrets.append(model.api_key)
        for secret in secrets:
            if secret and secret in safe:
                safe = safe.replace(secret, mask_secret(secret))
        return safe

    @staticmethod
    def _log_from_row(row: Dict[str, Any]) -> RequestLog:
        return RequestLog(
            time=str(row.get("time") or ""),
            path=str(row.get("path") or ""),
            model=str(row.get("model") or ""),
            status=str(row.get("status") or ""),
            detail=str(row.get("detail") or ""),
            duration_ms=int(row.get("duration_ms") or 0),
            prompt_tokens=int(row.get("prompt_tokens") or 0),
            completion_tokens=int(row.get("completion_tokens") or 0),
            total_tokens=int(row.get("total_tokens") or 0),
            cached_tokens=int(row.get("cached_tokens") or 0),
            reasoning_tokens=int(row.get("reasoning_tokens") or 0),
            group_id=str(row.get("group_id") or ""),
            group_name=str(row.get("group_name") or ""),
            provider_type=str(row.get("provider_type") or ""),
            event=str(row.get("event") or ""),
            request_id=str(row.get("request_id") or ""),
            attempt=int(row.get("attempt") or 0),
            usage_source=str(row.get("usage_source") or ""),
            requested_model=str(row.get("requested_model") or ""),
            resolved_as=str(row.get("resolved_as") or ""),
            aggregate_model=str(row.get("aggregate_model") or ""),
            aggregate_id=str(row.get("aggregate_id") or ""),
            aggregate_member_id=str(row.get("aggregate_member_id") or ""),
            selected_group=str(row.get("selected_group") or ""),
            selected_model=str(row.get("selected_model") or ""),
            selected_upstream_model=str(row.get("selected_upstream_model") or ""),
            selection_reason=str(row.get("selection_reason") or ""),
            fallback_index=int(row.get("fallback_index") or 0),
            fallback_chain=str(row.get("fallback_chain") or ""),
            member_cooled_down=bool(row.get("member_cooled_down")),
            cooldown_applied=bool(row.get("cooldown_applied")),
            failure_scope=str(row.get("failure_scope") or ""),
        )

    def _load_log_file(self) -> None:
        try:
            if not self.log_file.exists():
                return
            with self.log_file.open("r", encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            items: List[RequestLog] = []
            for row in rows[-80:]:
                if isinstance(row, dict):
                    row.setdefault("time", row.get("time") or self._now())
                    items.append(self._log_from_row(row))
            # 文件中按时间顺序追加（旧在前），内存中 logs[0] 需要是最新的，因此要反转
            if items:
                self.logs = list(reversed(items))
        except Exception as exc:
            self.log_write_error = f"日志加载失败: {exc}"
            return

    def _append_log_file(self, item: RequestLog) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
        except Exception as exc:
            self.log_write_error = f"日志写入失败: {exc}"
            # 在内存日志中插入一条系统级告警，方便用户从日志列表也能发现
            self.logs.insert(0, RequestLog(
                time=self._now(),
                path="/system/log",
                model="-",
                status="warn",
                detail=f"日志写入失败: {exc}; file={self.log_file}",
                duration_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cached_tokens=0,
                reasoning_tokens=0,
                group_id="",
                group_name="系统",
                provider_type="system",
                event="system",
                request_id="",
                attempt=0,
            ))
            del self.logs[80:]

    def recent_logs(self) -> List[Dict[str, str]]:
        return [asdict(item) for item in self.logs[:30]]

    def all_logs(self) -> List[RequestLog]:
        items: List[RequestLog] = []
        try:
            if self.log_file.exists():
                with self.log_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                        items.append(self._log_from_row(row))
        except Exception:
            items = []
        return items or list(reversed(self.logs))

    def clear_logs(self) -> None:
        self.logs.clear()
        try:
            if self.log_file.exists():
                self.log_file.unlink()
        except Exception:
            return

    def update_latest_stream_usage(
        self,
        request_id: str,
        usage: Tuple[int, int, int, int, int],
        usage_source: str,
        *,
        lock_wait_ms: Optional[int] = None,
        lock_release_reason: str = "",
    ) -> None:
        if not request_id:
            return
        prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = usage
        for item in self.logs:
            if item.request_id == request_id and item.event == "stream_ok":
                item.prompt_tokens = prompt_tokens
                item.completion_tokens = completion_tokens
                item.total_tokens = total_tokens
                item.cached_tokens = cached_tokens
                item.reasoning_tokens = reasoning_tokens
                item.usage_source = usage_source
                suffix_parts = ["lock_released=true"]
                if lock_wait_ms is not None:
                    suffix_parts.append(f"lock_wait_ms={lock_wait_ms}")
                if lock_release_reason:
                    suffix_parts.append(f"lock_release_reason={lock_release_reason}")
                if not usage_source:
                    suffix_parts.append("stream_not_finalized=true")
                item.detail = self._append_detail(item.detail, "; ".join(suffix_parts))
                break
        # 同步回写日志文件，避免重启后流式请求的 token 使用量丢失
        self._rewrite_log_file()

    def finalize_stream_if_needed(self, request_id: str) -> None:
        """客户端断连或 handler 异常退出后，若 stream_ok 日志仍未回填 usage_source，则标记异常样本。"""
        if not request_id:
            return
        for item in self.logs:
            if item.request_id == request_id and item.event == "stream_ok":
                if not item.usage_source:
                    item.detail = self._append_detail(item.detail, "stream_not_finalized=true")
                    self._rewrite_log_file()
                break

    def _rewrite_log_file(self) -> None:
        """将内存中的日志按时间顺序（旧在前，新在后）重新写入文件。"""
        try:
            if not self.log_file.exists():
                return
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            # self.logs 是新的在前，文件需要旧在前，因此反转写入
            lines = [json.dumps(asdict(item), ensure_ascii=False) + "\n" for item in reversed(self.logs)]
            with self.log_file.open("w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as exc:
            self.log_write_error = f"日志回写失败: {exc}"

    def export_logs_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time", "path", "request_id", "attempt", "group_name", "provider_type", "model", "status", "event", "duration_ms", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "usage_source", "cooldown_applied", "failure_scope", "detail"])
        for item in self.all_logs():
            writer.writerow([
                item.time,
                item.path,
                item.request_id,
                item.attempt,
                item.group_name,
                item.provider_type,
                item.model,
                item.status,
                item.event,
                item.duration_ms,
                item.prompt_tokens,
                item.completion_tokens,
                item.total_tokens,
                item.cached_tokens,
                item.reasoning_tokens,
                item.usage_source,
                item.cooldown_applied,
                item.failure_scope,
                item.detail,
            ])
        return output.getvalue()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    @staticmethod
    def _is_quota_exhausted(status_code: Optional[int], body: str) -> bool:
        if status_code != 429 or ArkProxyRouter._is_rate_limited(status_code, body):
            return False
        body_lower = body.lower()
        quota_markers = (
            "quotaexceeded",
            "setlimitexceeded",
            "insufficientquota",
            "insufficient_quota",
            "free trial quota exhausted",
            "quota exhausted",
            "reached the set inference limit",
            "model service has been paused",
            "余额不足",
            "额度不足",
            "额度已用完",
            "配额不足",
            "配额已用完",
        )
        return any(marker in body_lower for marker in quota_markers) or status_code == 429

    @staticmethod
    def _is_rate_limited(status_code: Optional[int], body: str) -> bool:
        return status_code == 429 and "RateLimitExceeded" in body

    @staticmethod
    def _is_server_error(status_code: Optional[int]) -> bool:
        return status_code is not None and status_code >= 500

    @staticmethod
    def _upstream_error_type(raw: str) -> Optional[str]:
        """从上游 JSON 错误体中提取 error.type。"""
        try:
            data = json.loads(raw)
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("type") or "").strip() or None
        except Exception:
            pass
        return None

    @staticmethod
    def _is_request_level_error(status_code: Optional[int], raw: str) -> bool:
        """判断是否为不应触发 cooldown 的请求级/鉴权错误。"""
        if status_code in (401, 403):
            return True
        if status_code == 400:
            err_type = ArkProxyRouter._upstream_error_type(raw)
            if err_type in REQUEST_LEVEL_ERROR_TYPES:
                return True
            lower = raw.lower()
            if any(k in lower for k in ("invalid request", "unsupported parameter", "content policy", "bad request")):
                return True
        if status_code is not None and 400 <= status_code < 500 and status_code != 429:
            return True
        return False

    @staticmethod
    def _is_waf_blocked_error(status_code: Optional[int], raw: str) -> bool:
        """识别上游中转站 WAF/风控拦截类 403 错误。"""
        if status_code != 403:
            return False
        lower = raw.lower()
        markers = (
            "your request was blocked",
            "request was blocked",
            "blocked by waf",
            "waf blocked",
            "access denied",
            "blocked",
            "waf",
            "风控",
        )
        return any(marker in lower for marker in markers)

    @classmethod
    def _classify_candidate_error(cls, status_code: Optional[int], raw: str, error_kind: str = "http") -> Dict[str, Any]:
        """
        error_kind: 'http' | 'network' | 'stream_timeout'
        返回 {
            should_cooldown: bool,
            is_request_level: bool,
            category: str,
            log_reason: str,
            failure_scope: str,  # request | candidate | upstream
        }
        """
        if error_kind in ("network", "stream_timeout"):
            return {"should_cooldown": True, "is_request_level": False, "category": error_kind, "log_reason": error_kind, "failure_scope": "upstream"}
        if status_code is None:
            return {"should_cooldown": True, "is_request_level": False, "category": "network", "log_reason": "network", "failure_scope": "upstream"}
        if status_code >= 500:
            return {"should_cooldown": True, "is_request_level": False, "category": "server_error", "log_reason": f"server_error_{status_code}", "failure_scope": "upstream"}
        if status_code == 429:
            if cls._is_rate_limited(status_code, raw):
                return {"should_cooldown": True, "is_request_level": False, "category": "rate_limit", "log_reason": "rate_limit", "failure_scope": "upstream"}
            if cls._is_quota_exhausted(status_code, raw):
                return {"should_cooldown": True, "is_request_level": False, "category": "quota_exhausted", "log_reason": "quota_exhausted", "failure_scope": "upstream"}
            return {"should_cooldown": True, "is_request_level": False, "category": "rate_limit", "log_reason": "rate_limit_429", "failure_scope": "upstream"}
        if cls._is_waf_blocked_error(status_code, raw):
            return {"should_cooldown": False, "is_request_level": True, "category": "waf_blocked", "log_reason": "waf_blocked", "failure_scope": "candidate"}
        if cls._is_request_level_error(status_code, raw):
            if status_code in (401, 403):
                return {"should_cooldown": False, "is_request_level": True, "category": "auth_error", "log_reason": "auth_error", "failure_scope": "candidate"}
            return {"should_cooldown": False, "is_request_level": True, "category": "request_level", "log_reason": "request_level", "failure_scope": "request"}
        return {"should_cooldown": False, "is_request_level": False, "category": "unknown", "log_reason": f"http_{status_code}", "failure_scope": "upstream"}

    @staticmethod
    def _waf_blocked_suffix(classification: Dict[str, Any], group: ConnectionGroup) -> str:
        """为 WAF 拦截类 403 错误生成中文提示后缀，供日志 detail 使用。"""
        if classification.get("category") != "waf_blocked":
            return ""
        if group.provider_type == PROVIDER_RELAY and group.waf_compatible:
            return "; waf_blocked=true; message=上游中转站拦截了请求，可能是中转站账号、渠道权限、频率限制或服务商风控导致; suggestion=该连接组已开启 WAF，仍被拦截，请检查中转站后台"
        return "; waf_blocked=true; message=上游中转站拦截了请求，通常需要开启 WAF 兼容模式或调整中转站风控配置; suggestion=请在该连接组设置中开启「仅中转站 WAF 兼容」后重试"

    @staticmethod
    def _waf_blocked_hint(fallback_chain: List[Dict[str, Any]]) -> str:
        """根据 fallback_chain 判断是否存在 WAF 拦截错误，并返回中文提示。"""
        if not fallback_chain:
            return ""
        waf_items = [item for item in fallback_chain if str(item.get("category")) == "waf_blocked"]
        if not waf_items:
            return ""
        # 如果任一失败成员所属连接组已开启 WAF，则提示检查中转站后台
        if any(item.get("waf_compatible") for item in waf_items):
            return " 上游中转站返回 403：Your request was blocked。该连接组已开启 WAF，可能是中转站账号、渠道权限、频率限制或服务商风控导致，请检查中转站后台。"
        return " 上游中转站返回 403：Your request was blocked。该连接组未开启 WAF 兼容，建议开启「仅中转站 WAF 兼容」后重试。"

    @staticmethod
    def _resolve_url(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        suffix = path.lstrip("/")
        if suffix.startswith("v1/"):
            suffix = suffix[3:]
        return f"{base}/{suffix}"

    @staticmethod
    def _empty_usage() -> Tuple[int, int, int, int, int]:
        return 0, 0, 0, 0, 0

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    @staticmethod
    def _usage_from_payload(payload: Any) -> Tuple[int, int, int, int, int]:
        if not isinstance(payload, dict):
            return ArkProxyRouter._empty_usage()
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            response = payload.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return ArkProxyRouter._empty_usage()

        prompt_tokens = ArkProxyRouter._int_value(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion_tokens = ArkProxyRouter._int_value(usage.get("completion_tokens") or usage.get("output_tokens"))
        total_tokens = ArkProxyRouter._int_value(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)

        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
        cached_tokens = ArkProxyRouter._int_value(prompt_details.get("cached_tokens") or input_details.get("cached_tokens"))
        reasoning_tokens = ArkProxyRouter._int_value(output_details.get("reasoning_tokens"))
        return prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens

    @staticmethod
    def _usage_from_response(data: bytes) -> Tuple[int, int, int, int, int]:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return ArkProxyRouter._empty_usage()
        return ArkProxyRouter._usage_from_payload(payload)

    @staticmethod
    def _usage_from_stream_chunk(chunk: bytes) -> Tuple[int, int, int, int, int]:
        text = chunk.decode("utf-8", "ignore").strip()
        if not text.startswith("data:"):
            return ArkProxyRouter._empty_usage()
        data = text[5:].strip()
        if not data or data == "[DONE]":
            return ArkProxyRouter._empty_usage()
        try:
            payload = json.loads(data)
        except Exception:
            return ArkProxyRouter._empty_usage()
        return ArkProxyRouter._usage_from_payload(payload)

    def default_model(self) -> Optional[ModelConfig]:
        return next((m for m in self.store.models if m.usable), None)

    @staticmethod
    def group_auto_model_name(group: ConnectionGroup | None) -> str:
        if group and group.auto_model_name and group.auto_model_name.strip():
            return group.auto_model_name.strip()
        return DEFAULT_AUTO_MODEL_NAME

    @staticmethod
    def _is_auto_model(requested_model: str | None, group: ConnectionGroup | None = None) -> bool:
        if not requested_model:
            return True
        if requested_model in {DEFAULT_AUTO_MODEL_NAME, "all-router-auto"}:
            return True
        if group and requested_model == ArkProxyRouter.group_auto_model_name(group):
            return True
        return False

    def _iter_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Tuple[int, ModelConfig]]:
        group = self.store.find_group(group_id) if group_id else None
        if self._is_auto_model(requested_model, group):
            requested_model = None
        for idx, model in enumerate(self.store.models):
            if model.cooldown_until and model.cooldown_until <= int(time.time()):
                model.cooldown_until = 0
                model.cooldown_reason = ""
                if not model.disabled_by_user:
                    model.usable = True
                model.last_error = ""
                model.last_checked_at = self._now()
                self.store.save()
            if model.disabled_by_user or not model.usable:
                continue
            if group_id and model.group_id != group_id:
                continue
            if requested_model and requested_model not in {model.id, model.name, model.ep_id}:
                continue
            yield idx, model

    def _group_for(self, model: ModelConfig) -> Optional[ConnectionGroup]:
        return self.store.find_group(model.group_id)

    @staticmethod
    def _mode_for(group: Optional[ConnectionGroup]) -> str:
        return group.provider_type if group and group.provider_type else PROVIDER_ARK

    def _hit_detail(self, group: ConnectionGroup, model: ModelConfig, requested_label: str, suffix: str) -> str:
        mode = self._mode_for(group)
        channel = f"; channel={model.price_group}" if mode == PROVIDER_RELAY and model.price_group else ""
        return f"mode={mode}; hit={model.ep_id}{channel}; requested={requested_label}; {suffix}"

    def _candidate_hit_detail(self, candidate: UpstreamCandidate, requested_label: str, suffix: str) -> str:
        mode = self._mode_for(candidate.group)
        channel = f"; channel={candidate.channel}" if candidate.channel else ""
        waf = "; waf=on" if candidate.group.provider_type == PROVIDER_RELAY and candidate.group.waf_compatible else ""
        aggregate = ""
        if candidate.aggregate_id:
            aggregate = f"; aggregate={candidate.aggregate_name}; aggregate_id={candidate.aggregate_id}; aggregate_member_id={candidate.aggregate_member_id}"
        return f"mode={mode}{waf}; hit={candidate.target_model}{channel}; requested={requested_label}{aggregate}; {suffix}"

    @staticmethod
    def _aggregate_log_suffix(
        resolved_as: str,
        aggregate_model: str,
        aggregate_id: str,
        selected_group: str,
        selected_model: str,
        selected_upstream_model: str,
        selection_reason: str,
        fallback_index: int,
        fallback_chain: List[Dict[str, Any]],
        strategy: str = "priority",
        manual_price: float | None = None,
    ) -> str:
        chain_str = ""
        if fallback_chain:
            chain_str = "; fallback_chain=" + json.dumps(fallback_chain, ensure_ascii=False, separators=(",", ":"))
        price_str = f"; manual_price={manual_price}" if manual_price is not None else ""
        return (
            f"resolved_as={resolved_as}"
            f"; aggregate_model={aggregate_model}"
            f"; aggregate_id={aggregate_id}"
            f"; selected_group={selected_group}"
            f"; selected_model={selected_model}"
            f"; selected_upstream_model={selected_upstream_model}"
            f"; selection_reason={selection_reason}"
            f"; fallback_index={fallback_index}"
            f"; strategy={strategy}"
            f"{price_str}"
            f"{chain_str}"
        )

    @staticmethod
    def _body_sha256(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()[:16]

    @staticmethod
    def _normalize_for_cache(value: Any) -> Any:
        volatile_keys = {
            "id",
            "created",
            "object",
            "request_id",
            "x-request-id",
            "response_id",
            "previous_response_id",
            "trace_id",
            "tool_call_id",
            "run_id",
            "session_id",
        }
        if isinstance(value, dict):
            items: Dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in volatile_keys:
                    continue
                items[str(key)] = ArkProxyRouter._normalize_for_cache(item)
            return items
        if isinstance(value, list):
            return [ArkProxyRouter._normalize_for_cache(item) for item in value]
        return value

    @staticmethod
    def _normalized_body_sha256(payload: Dict[str, Any]) -> str:
        normalized = ArkProxyRouter._normalize_for_cache(payload)
        text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _hash_json(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _json_bytes(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return len(str(value).encode("utf-8"))

    @staticmethod
    def _cache_prefix_fingerprint(payload: Dict[str, Any], body: bytes) -> str:
        parts = [
            f"body_len={len(body)}",
            f"body_4k={hashlib.sha256(body[:4096]).hexdigest()[:16]}",
            f"body_16k={hashlib.sha256(body[:16384]).hexdigest()[:16]}",
            f"body_64k={hashlib.sha256(body[:65536]).hexdigest()[:16]}",
            f"body_128k={hashlib.sha256(body[:131072]).hexdigest()[:16]}",
            f"body_256k={hashlib.sha256(body[:262144]).hexdigest()[:16]}",
            f"body_all={hashlib.sha256(body).hexdigest()[:16]}",
        ]
        messages = payload.get("messages")
        system_bytes = 0
        developer_bytes = 0
        user_assistant_bytes = 0
        tool_result_bytes = 0
        if isinstance(messages, list):
            normalized_messages = ArkProxyRouter._normalize_for_cache(messages)
            for count in (1, 4, 16, 32, 64):
                if len(normalized_messages) >= count:
                    parts.append(f"msg_{count}={ArkProxyRouter._hash_json(normalized_messages[:count])}")
            parts.append(f"msg_all={ArkProxyRouter._hash_json(normalized_messages)}")
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                content = message.get("content")
                size = ArkProxyRouter._json_bytes(content)
                if role == "system":
                    system_bytes += size
                elif role == "developer":
                    developer_bytes += size
                elif role in ("user", "assistant"):
                    user_assistant_bytes += size
                elif role == "tool":
                    tool_result_bytes += size
        parts.append(f"system_bytes={system_bytes}")
        parts.append(f"developer_bytes={developer_bytes}")
        parts.append(f"user_assistant_bytes={user_assistant_bytes}")
        parts.append(f"tool_result_bytes={tool_result_bytes}")
        tools = payload.get("tools")
        tools_bytes = 0
        tools_count = 0
        if isinstance(tools, list):
            tools_count = len(tools)
            tools_bytes = ArkProxyRouter._json_bytes(tools)
            normalized_tools = ArkProxyRouter._normalize_for_cache(tools)
            parts.append(f"tools_count={tools_count}")
            parts.append(f"tools_bytes={tools_bytes}")
            parts.append(f"tools_hash={ArkProxyRouter._hash_json(normalized_tools)}")
        else:
            parts.append("tools_count=0")
            parts.append("tools_bytes=0")
        return "; ".join(parts)

    @staticmethod
    def _safe_header_view(headers: Dict[str, str]) -> str:
        interesting = {
            "accept",
            "accept-language",
            "cache-control",
            "content-length",
            "content-type",
            "origin",
            "pragma",
            "referer",
            "user-agent",
        }
        items: List[str] = []
        x_headers: List[str] = []
        seen: set[str] = set()
        for name, value in headers.items():
            lower = name.strip().lower()
            if lower in seen:
                continue
            seen.add(lower)
            if lower in interesting:
                text = " ".join(str(value).split())
                if lower == "user-agent" and len(text) > 72:
                    text = text[:72] + "..."
                items.append(f"{lower}={text}")
            elif lower.startswith("x-"):
                x_headers.append(lower)
        if x_headers:
            items.append("x-headers=" + ",".join(sorted(set(x_headers))))
        return "; ".join(items) if items else "headers=none"

    @staticmethod
    def _payload_fingerprint(payload: Dict[str, Any], body: bytes, path: str = "", tools_normalized: bool = False) -> str:
        keys = [
            "model",
            "stream",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "reasoning_effort",
            "service_tier",
            "tool_choice",
            "parallel_tool_calls",
            "store",
        ]
        parts: List[str] = []
        for key in keys:
            if key in payload:
                parts.append(f"{key}={payload.get(key)!r}")
        messages = payload.get("messages")
        messages_count = 0
        tool_result_bytes = 0
        if isinstance(messages, list):
            messages_count = len(messages)
            roles: List[str] = []
            content_chars = 0
            for message in messages:
                if isinstance(message, dict):
                    roles.append(str(message.get("role") or "?"))
                    try:
                        content_chars += len(json.dumps(message.get("content"), ensure_ascii=False, separators=(",", ":")))
                    except Exception:
                        content_chars += len(str(message.get("content")))
                    if message.get("role") == "tool":
                        tool_result_bytes += ArkProxyRouter._json_bytes(message.get("content"))
            parts.append(f"messages={messages_count}")
            parts.append("roles=" + ",".join(roles[:12]))
            parts.append(f"content_chars={content_chars}")
        # Responses API 结构化统计
        is_responses = path == "/v1/responses" or "input" in payload
        if is_responses:
            input_value = payload.get("input")
            input_type = "none"
            input_items = 0
            input_bytes = 0
            if isinstance(input_value, str):
                input_type = "str"
                input_bytes = ArkProxyRouter._json_bytes(input_value)
            elif isinstance(input_value, list):
                input_type = "list"
                input_items = len(input_value)
                input_bytes = ArkProxyRouter._json_bytes(input_value)
            elif isinstance(input_value, dict):
                input_type = "dict"
                input_bytes = ArkProxyRouter._json_bytes(input_value)
            parts.append(f"responses_input_type={input_type}")
            parts.append(f"responses_input_items={input_items}")
            parts.append(f"responses_input_bytes={input_bytes}")
            parts.append(f"instructions_bytes={ArkProxyRouter._json_bytes(payload.get('instructions'))}")
            parts.append(f"previous_response_id_present={'true' if payload.get('previous_response_id') else 'false'}")
            response_tools = payload.get("tools")
            if isinstance(response_tools, list):
                parts.append(f"response_tools_count={len(response_tools)}")
                parts.append(f"response_tools_bytes={ArkProxyRouter._json_bytes(response_tools)}")
            else:
                parts.append("response_tools_count=0")
                parts.append("response_tools_bytes=0")
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                parts.append(f"response_metadata_keys={len(list(metadata.keys()))}")
            else:
                parts.append("response_metadata_keys=0")
        tools = payload.get("tools")
        tools_bytes = 0
        if isinstance(tools, list):
            tools_bytes = ArkProxyRouter._json_bytes(tools)
            names: List[str] = []
            for item in tools[:12]:
                if not isinstance(item, dict):
                    continue
                fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                names.append(str(fn.get("name") or item.get("name") or item.get("type") or "?"))
            parts.append(f"tools={len(tools)}:{','.join(names)}")
        for key in ("functions",):
            value = payload.get(key)
            if isinstance(value, list):
                names = []
                for item in value[:12]:
                    if not isinstance(item, dict):
                        continue
                    fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                    names.append(str(fn.get("name") or item.get("name") or item.get("type") or "?"))
                parts.append(f"{key}={len(value)}:{','.join(names)}")
        stream_options = payload.get("stream_options")
        if isinstance(stream_options, dict):
            parts.append("stream_options=" + ",".join(f"{k}={stream_options[k]!r}" for k in sorted(stream_options)))
        if tools_normalized:
            parts.append("tools_normalized=true")
        # Payload 减重预警标记
        body_len = len(body)
        if body_len > 262144:
            parts.append("payload_very_large=true")
        elif body_len > 131072:
            parts.append("payload_large=true")
        if tools_bytes > 65536:
            parts.append("tools_large=true")
        if tool_result_bytes > 65536:
            parts.append("tool_results_large=true")
        if messages_count > 60:
            parts.append("messages_many=true")
        parts.append(f"body_len={body_len}")
        parts.append(f"body_sha256={ArkProxyRouter._body_sha256(body)}")
        parts.append(f"normalized_sha256={ArkProxyRouter._normalized_body_sha256(payload)}")
        parts.append(f"prefix=({ArkProxyRouter._cache_prefix_fingerprint(payload, body)})")
        return "; ".join(parts)

    def _debug_detail(
        self,
        candidate: UpstreamCandidate,
        requested_label: str,
        target_url: str,
        body_mode: str,
        body: bytes,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        suffix: str,
        resp: Optional[Any] = None,
        tools_normalized: bool = False,
        lock_wait_ms: Optional[int] = None,
        lock_release_reason: str = "",
        aggregate_suffix: str = "",
    ) -> str:
        if aggregate_suffix:
            suffix = f"{suffix}; {aggregate_suffix}" if suffix else aggregate_suffix
        base = self._candidate_hit_detail(candidate, requested_label, suffix)
        group_name = str(candidate.group.name).replace(";", ",")
        # mode=passthrough 表示该请求走透传路径（ark/proxy），relay 专属逻辑不介入
        mode_tag = "passthrough" if candidate.group.provider_type != PROVIDER_RELAY else "relay"
        path = urlparse(target_url).path
        header_policy = "waf_browser" if candidate.group.provider_type == PROVIDER_RELAY and candidate.group.waf_compatible else "passthrough"
        lower_headers = {k.lower(): v for k, v in headers.items()}
        accept = lower_headers.get("accept", "")
        content_type = lower_headers.get("content-type", "")
        user_agent = lower_headers.get("user-agent", "")
        user_agent_family = self._user_agent_family(user_agent)
        waf_lock_enabled = self._candidate_lock_enabled(candidate)
        http_client = getattr(self, "_upstream_client", None) and getattr(self._upstream_client, "client_type", "urllib") or "urllib"
        http_version = getattr(resp, "http_version", "") if resp else ""
        extra = (
            f"; header_policy={header_policy}"
            f"; accept={accept}"
            f"; content_type={content_type}"
            f"; user_agent_family={user_agent_family}"
            f"; waf_compatible={'true' if candidate.group.waf_compatible else 'false'}"
            f"; waf_lock_enabled={'true' if waf_lock_enabled else 'false'}"
            f"; http_client={http_client}"
            f"; upstream_http_version={http_version or '-'}"
        )
        if lock_wait_ms is not None:
            extra += f"; lock_wait_ms={lock_wait_ms}"
        if lock_release_reason:
            extra += f"; lock_release_reason={lock_release_reason}"
        return (
            f"{base}; group_id={candidate.group.id}; group_name={group_name}; provider={candidate.group.provider_type}; mode={mode_tag}; "
            f"upstream={target_url}; body={body_mode}; "
            f"fingerprint=({self._payload_fingerprint(payload, body, path, tools_normalized=tools_normalized)}); "
            f"out_headers=({self._safe_header_view(headers)})"
            f"{extra}"
        )

    @staticmethod
    def _user_agent_family(user_agent: str) -> str:
        ua = str(user_agent).lower()
        if "codex" in ua:
            return "codex"
        if any(k in ua for k in ("chrome", "safari", "firefox", "edge", "mozilla")):
            return "browser"
        return "other"

    @staticmethod
    def _short_error(raw: str, limit: int = 900) -> str:
        text = " ".join(str(raw or "").split())
        return text[:limit]

    def _tools_order_enabled(self) -> bool:
        if self.settings_store is None:
            return False
        return bool(self.settings_store.get("normalize_tools_order", False))

    @staticmethod
    def _normalize_tools_order(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        tools = payload.get("tools")
        if not isinstance(tools, list) or len(tools) <= 1:
            return payload, False
        try:
            def _tool_key(item: Any) -> str:
                if not isinstance(item, dict):
                    return ""
                fn = item.get("function")
                if isinstance(fn, dict):
                    return str(fn.get("name") or "")
                return str(item.get("name") or item.get("type") or "")

            sorted_tools = sorted(tools, key=_tool_key)
            if sorted_tools == tools:
                return payload, False
            new_payload = dict(payload)
            new_payload["tools"] = sorted_tools
            return new_payload, True
        except Exception:
            return payload, False

    def _auth_for(self, group: ConnectionGroup, model: Optional[ModelConfig]) -> str:
        mode = self._mode_for(group)
        if mode == PROVIDER_RELAY:
            return model.api_key if model else ""
        if mode == PROVIDER_PROXY:
            return group.api_key or group.ark_api_key
        return group.ark_api_key

    def _headers_for(self, group: ConnectionGroup, auth_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
        if group.provider_type == PROVIDER_RELAY and group.waf_compatible:
            upstream_host = urlparse(group.base_url).netloc
            headers = build_waf_compatible_headers(incoming_headers, upstream_host, stream=stream)
            headers["authorization"] = f"Bearer {auth_key}"
            if not any(key.lower() == "content-type" for key in headers):
                headers["content-type"] = "application/json"
            policy = str(group.waf_accept_policy or "default")
            if policy == "text_event_stream":
                headers["accept"] = "text/event-stream" if stream else "application/json"
            elif policy == "passthrough":
                incoming_accept = None
                for name, value in incoming_headers.items():
                    if name.strip().lower() == "accept":
                        incoming_accept = value
                        break
                if incoming_accept:
                    headers["accept"] = incoming_accept
            return headers
        if incoming_headers:
            return build_passthrough_headers(auth_key, incoming_headers, stream=stream)
        return build_upstream_headers(auth_key, stream=stream)

    def _candidate_from_model(self, idx: int, model: ModelConfig, group: ConnectionGroup) -> UpstreamCandidate:
        mode = self._mode_for(group)
        channel = ""
        if mode == PROVIDER_RELAY and model.price_group:
            channel = model.price_group
        elif mode == PROVIDER_PROXY:
            channel = "proxy"
        return UpstreamCandidate(
            idx=idx,
            group=group,
            model=model,
            label=model.name,
            target_model=model.ep_id,
            auth_key=self._auth_for(group, model),
            channel=channel,
        )

    def _candidate_lock(self, candidate: UpstreamCandidate) -> Optional[threading.Lock]:
        if not self._candidate_lock_enabled(candidate):
            return None
        key = f"{candidate.group.id}:{candidate.target_model}:{candidate.channel}"
        with self.upstream_locks_guard:
            lock = self.upstream_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self.upstream_locks[key] = lock
            return lock

    def _candidate_lock_key(self, candidate: UpstreamCandidate) -> str:
        return f"{candidate.group.id}:{candidate.target_model}:{candidate.channel}"

    def _active_stream_count(self, candidate: UpstreamCandidate) -> int:
        key = self._candidate_lock_key(candidate)
        with self.upstream_locks_guard:
            return int(self.upstream_active_streams.get(key, 0))

    def _mark_stream_active(self, candidate: UpstreamCandidate, delta: int) -> None:
        key = self._candidate_lock_key(candidate)
        with self.upstream_locks_guard:
            next_value = max(0, int(self.upstream_active_streams.get(key, 0)) + delta)
            if next_value:
                self.upstream_active_streams[key] = next_value
            else:
                self.upstream_active_streams.pop(key, None)

    def _waf_lock_busy_detail(self, candidate: UpstreamCandidate, body: bytes, lock_wait_ms: int) -> str:
        active_streams = self._active_stream_count(candidate)
        fallback_reason = "large_task_in_progress" if active_streams or len(body) > 131072 else "candidate_busy"
        return (
            f"waf_lock_wait_timeout; fallback_reason={fallback_reason}; "
            f"failure_scope=busy; cooldown_applied=false; active_streams={active_streams}; "
            f"lock_wait_ms={lock_wait_ms}; busy_hint=candidate_busy"
        )

    @staticmethod
    def _candidate_lock_enabled(candidate: UpstreamCandidate) -> bool:
        return candidate.group.provider_type == PROVIDER_RELAY and candidate.group.waf_compatible

    @staticmethod
    def _release_lock(lock: Optional[threading.Lock]) -> None:
        if lock:
            lock.release()

    def _acquire_upstream_lock(self, lock: Optional[threading.Lock], timeout: float = 10.0) -> Tuple[bool, int]:
        """尝试获取 WAF lock，返回 (是否成功, 等待毫秒数)。"""
        if not lock:
            return True, 0
        started = time.perf_counter()
        acquired = lock.acquire(timeout=timeout)
        wait_ms = int((time.perf_counter() - started) * 1000)
        return acquired, wait_ms

    @staticmethod
    def _append_detail(detail: str, suffix: str) -> str:
        if not detail:
            return suffix
        return f"{detail}; {suffix}"

    def _live_request_start(self, request_id: str, path: str, requested_model: str, *, stream: bool) -> None:
        if not request_id:
            return
        now = time.time()
        with self.live_requests_lock:
            self.live_requests[request_id] = {
                "request_id": request_id,
                "path": path,
                "requested_model": requested_model,
                "model": requested_model,
                "group": "",
                "candidate": "",
                "aggregate_model": "",
                "stage": "selecting_candidate",
                "stage_label": "选择候选",
                "started_at": now,
                "updated_at": now,
                "elapsed_ms": 0,
                "stream": bool(stream),
                "attempt": 0,
                "status": "running",
                "slow": False,
                "possible_reason": "",
            }

    def _live_request_update(self, request_id: str, **patch: Any) -> None:
        if not request_id:
            return
        with self.live_requests_lock:
            item = self.live_requests.get(request_id)
            if not item:
                return
            item.update({k: v for k, v in patch.items() if v is not None})
            item["updated_at"] = time.time()

    def _live_request_finish(self, request_id: str, status: str = "done") -> None:
        if not request_id:
            return
        with self.live_requests_lock:
            item = self.live_requests.get(request_id)
            if item:
                item["status"] = status
                item["stage"] = status
                item["stage_label"] = "已完成" if status == "done" else "已结束"
                item["updated_at"] = time.time()
            self.live_requests.pop(request_id, None)

    def live_requests_payload(self) -> Dict[str, Any]:
        now = time.time()
        items: List[Dict[str, Any]] = []
        with self.live_requests_lock:
            for item in self.live_requests.values():
                row = dict(item)
                elapsed_ms = int((now - float(row.get("started_at") or now)) * 1000)
                row["elapsed_ms"] = elapsed_ms
                row["slow"] = elapsed_ms >= 10000 or str(row.get("stage") or "") in {"waiting_waf_lock", "waiting_first_byte"} and elapsed_ms >= 5000
                if row["slow"] and not row.get("possible_reason"):
                    stage = str(row.get("stage") or "")
                    if stage == "waiting_waf_lock":
                        row["possible_reason"] = "候选可能正在处理大上下文请求，正在等待 WAF 锁"
                    elif stage == "waiting_first_byte":
                        row["possible_reason"] = "上游首包较慢，可能是模型正在执行复杂任务"
                    else:
                        row["possible_reason"] = "请求耗时较长，请关注上游状态"
                row["request_id_short"] = str(row.get("request_id") or "")[:8]
                items.append(row)
        items.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
        return {"ok": True, "requests": items, "count": len(items), "server_time": int(now)}

    def diagnose_request(self, request_id: str) -> Dict[str, Any]:
        related = [item for item in self.logs if item.request_id == request_id]
        if not related:
            for item in self.all_logs():
                if item.request_id == request_id:
                    related.append(item)
        if not related:
            return {"ok": False, "message": "未找到该请求记录", "code": "request_not_found"}
        related_chrono = list(reversed(related))
        return {"ok": True, "diagnosis": self._diagnose_logs(related_chrono)}

    def _diagnose_logs(self, logs: List[RequestLog]) -> Dict[str, Any]:
        text = "\n".join(f"{l.status} {l.event} {l.failure_scope} {l.detail}" for l in logs).lower()
        final = next((l for l in reversed(logs) if str(l.status).startswith("2") or l.event in {"error", "network", "stream_idle_timeout", "waf_lock_timeout"}), logs[-1])
        title = "请求已完成"
        severity = "success"
        root_cause = "request_completed"
        scope = final.failure_scope or "request"
        suggestion = "无需处理。"
        actions: List[Dict[str, str]] = []
        cooldown_applied = any(bool(l.cooldown_applied) for l in logs)
        if "waf_lock_wait_timeout" in text or "candidate_busy" in text or "large_task_in_progress" in text:
            title = "候选忙 / 等待 WAF 锁超时"
            severity = "warning"
            root_cause = "candidate_busy"
            scope = "local_lock"
            suggestion = "候选正在处理大上下文请求，系统已尝试切换到下一个候选；通常无需清冷却。"
        elif "stream_idle_timeout" in text:
            title = "上游流式响应空闲超时"
            severity = "error"
            root_cause = "stream_idle_timeout"
            scope = "upstream"
            suggestion = "建议稍后重试，或对冷却中的单个模型/成员执行“重试恢复”。"
            actions.append({"type": "recover", "label": "重试恢复冷却对象"})
        elif "read_timeout" in text or "timed out" in text or "timeout" in text and "waf_lock" not in text:
            title = "上游请求超时"
            severity = "error"
            root_cause = "upstream_timeout"
            scope = "upstream"
            suggestion = "如果该候选已进入冷却，可单点重试恢复；如果频繁出现，建议降低优先级或检查中转站。"
            actions.append({"type": "recover", "label": "重试恢复冷却对象"})
        elif "waf_blocked" in text or "request_level" in text or "upstream_request_rejected" in text:
            title = "请求级错误 / 上游拒绝请求"
            severity = "warning"
            root_cause = "request_level_error"
            scope = "request"
            cooldown_applied = False
            suggestion = "请检查请求参数、内容策略或 WAF 兼容设置；这类错误不会判定为模型健康失败。"
        elif "auth_error" in text or "401" in text or "403" in text:
            title = "鉴权失败"
            severity = "error"
            root_cause = "auth_error"
            scope = "candidate"
            suggestion = "请检查该连接组或模型的 API Key / Route Key 是否正确。"
        elif "rate_limit" in text or "429" in text:
            title = "上游限流"
            severity = "warning"
            root_cause = "rate_limit"
            scope = "upstream"
            suggestion = "建议稍后重试，或临时切换到其他候选。"
        elif "server_error" in text or " 5" in text or "network" in text:
            title = "上游健康失败"
            severity = "error"
            root_cause = "upstream_error"
            scope = "upstream"
            suggestion = "系统会对真实上游故障写入冷却；可在确认恢复后单点重试。"
            actions.append({"type": "recover", "label": "重试恢复冷却对象"})
        return {
            "title": title,
            "severity": severity,
            "root_cause": root_cause,
            "failure_scope": scope,
            "cooldown_applied": cooldown_applied,
            "suggestion": suggestion,
            "request_id": logs[0].request_id if logs else "",
            "related_events": len(logs),
            "actions": actions,
            "technical_summary": self._sanitize_detail(final.detail)[:500],
        }

    def _manual_probe_candidate(self, candidate: UpstreamCandidate) -> Tuple[bool, str, str]:
        """对单个候选执行最小非流式探测，不计入正式请求与收益统计。"""
        if not candidate.auth_key:
            return False, "missing_upstream_api_key", "缺少上游 API Key"
        target_url = self._resolve_url(candidate.group.base_url, "/v1/chat/completions")
        payload = {
            "model": candidate.target_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers_for(candidate.group, candidate.auth_key, {}, stream=False)
        upstream_lock = self._candidate_lock(candidate)
        acquired, _ = self._acquire_upstream_lock(upstream_lock, timeout=5.0)
        if not acquired:
            return False, "waf_lock_wait_timeout", "候选忙，等待 WAF 锁超时"
        try:
            with self._upstream_client.request("POST", target_url, headers, body, stream=False, timeout=20) as resp:
                resp.read()
                if 200 <= int(resp.status) < 300:
                    return True, "probe_ok", "最小探测成功"
                return False, f"http_{resp.status}", f"上游返回 HTTP {resp.status}"
        except HTTPError as err:
            raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
            classification = self._classify_candidate_error(err.code, raw, "http")
            return False, classification["log_reason"], self._short_error(raw)
        except (URLError, TimeoutError, OSError) as err:
            return False, "network", self._short_error(str(err))
        finally:
            self._release_lock(upstream_lock)

    def recover_model(self, model_id: str) -> Dict[str, Any]:
        model = self.store.find_model(model_id)
        if not model:
            return {"ok": False, "message": "模型不存在", "code": "model_not_found"}
        if model.disabled_by_user or (not model.usable and not model.cooldown_until):
            return {"ok": False, "message": "该模型为手动停用状态，请先手动启用；系统不会自动恢复手动停用模型。", "code": "manual_disabled"}
        group = self.store.find_group(model.group_id)
        if not group:
            return {"ok": False, "message": "模型所属连接组不存在，无法执行探测。", "code": "group_not_found"}
        before = asdict(model)
        candidate = self._candidate_from_model(self.store.models.index(model), model, group)
        ok, reason, detail = self._manual_probe_candidate(candidate)
        if not ok:
            self._set_cooldown(candidate.idx, detail, self._auto_cooldown_seconds(group), reason)
            self.add_log("/api/models/recover", model.name, "probe_failed", f"manual_probe=true; model_id={model.id}; probe_result=failed; reason={reason}; detail={detail}; cooldown_applied=true; failure_scope=upstream", event="manual_probe", usage_source="manual_probe", cooldown_applied=True, failure_scope="upstream")
            return {"ok": False, "message": "最小探测未通过，模型保持冷却，请稍后重试或检查上游服务。", "code": "probe_failed", "before": before, "model": asdict(model)}
        model.cooldown_until = 0
        model.cooldown_reason = ""
        model.last_error = ""
        model.usable = True
        model.disabled_by_user = False
        model.last_success_at = self._now()
        model.last_checked_at = model.last_success_at
        self.store.save()
        self.add_log("/api/models/recover", model.name, "probe_ok", f"manual_probe=true; model_id={model.id}; probe_result=success; cooldown_applied=false; failure_scope=manual", event="manual_probe", usage_source="manual_probe")
        return {"ok": True, "message": "最小探测成功，已恢复该模型参与调度。", "model": asdict(model), "before": before}

    def recover_aggregate_member(self, member_id: str) -> Dict[str, Any]:
        member = self.store.find_aggregate_member(member_id)
        if not member:
            return {"ok": False, "message": "成员不存在", "code": "aggregate_member_not_found"}
        if member.enabled is False:
            return {"ok": False, "message": "该聚合成员已手动停用，请先手动启用；系统不会自动恢复手动停用成员。", "code": "manual_disabled"}
        model = self.store.find_model(member.model_id)
        group = self.store.find_group(member.group_id)
        aggregate = self.store.find_aggregate(member.aggregate_id)
        if not model or not group:
            return {"ok": False, "message": "成员底层模型或连接组不存在，无法执行探测。", "code": "member_target_missing"}
        if model.disabled_by_user or not model.usable and not model.cooldown_until:
            return {"ok": False, "message": "底层模型为手动停用状态，不能自动恢复聚合成员。", "code": "underlying_manual_disabled"}
        before = asdict(member)
        candidate = self._candidate_from_model(self.store.models.index(model), model, group)
        ok, reason, detail = self._manual_probe_candidate(candidate)
        if not ok:
            self._set_aggregate_member_cooldown(member.id, detail, self._aggregate_cooldown_seconds(aggregate) if aggregate else self._auto_cooldown_seconds(group), reason)
            self.add_log("/api/aggregate-members/recover", model.name, "probe_failed", f"manual_probe=true; aggregate_member_id={member.id}; probe_result=failed; reason={reason}; detail={detail}; cooldown_applied=true; failure_scope=upstream", event="manual_probe", usage_source="manual_probe", cooldown_applied=True, failure_scope="upstream")
            return {"ok": False, "message": "最小探测未通过，成员保持冷却，请稍后重试或检查上游服务。", "code": "probe_failed", "before": before, "member": asdict(member)}
        member.cooldown_until = 0
        member.cooldown_reason = ""
        member.last_error = ""
        member.last_success_at = self._now()
        member.last_checked_at = member.last_success_at
        self.store.save()
        self.add_log("/api/aggregate-members/recover", model.name, "probe_ok", f"manual_probe=true; aggregate_member_id={member.id}; probe_result=success; cooldown_applied=false; failure_scope=manual", event="manual_probe", usage_source="manual_probe")
        return {"ok": True, "message": "最小探测成功，已恢复该聚合成员参与调度。", "member": asdict(member), "before": before}
    def _iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[UpstreamCandidate]:
        # 旧全局 Key 已退役，不再跨组调度真实模型
        if group_id == GLOBAL_ROUTE_GROUP_ID:
            return
        if group_id:
            group = self.store.find_group(group_id)
            if not group:
                return
            matched = False
            candidates = list(self._iter_candidates(requested_model, group.id))
            for idx, model in candidates:
                matched = True
                yield self._candidate_from_model(idx, model, group)
            if self._mode_for(group) == PROVIDER_PROXY and not matched and requested_model and not self._is_auto_model(requested_model, group):
                yield UpstreamCandidate(
                    idx=None,
                    group=group,
                    model=None,
                    label=requested_model,
                    target_model=requested_model,
                    auth_key=self._auth_for(group, None),
                    channel="pass-through",
                )
            return

        for idx, model in self._iter_candidates(requested_model, None):
            group = self._group_for(model)
            if group:
                yield self._candidate_from_model(idx, model, group)

    def _resolve_aggregate(
        self,
        requested_model: str | None,
        route: RouteContext | str | None,
    ) -> Optional[Tuple[AggregateModel, str]]:
        """解析聚合模型。返回 (AggregateModel, resolved_as)。

        resolved_as:
        - "aggregate": 直接命中 AggregateModel.name
        - all-router-auto 与旧全局默认聚合模型已退役，不再映射
        """
        if not requested_model:
            return None
        aggregate = getattr(route, "aggregate", None) if isinstance(route, RouteContext) else None
        if aggregate:
            if not aggregate.enabled:
                raise AllModelsFailedError(
                    f"聚合模型 {aggregate.name} 已禁用",
                    attempted=0,
                    error_code="aggregate_disabled",
                )
            if requested_model == aggregate.name:
                return aggregate, "aggregate"
            # 聚合模型 Key 只能请求自身聚合模型名
            raise AllModelsFailedError(
                f"聚合模型 Key 只能请求 {aggregate.name}",
                attempted=0,
                error_code="model_not_found",
            )
        # 连接组 Key / 旧全局 Key 不再通过 name 命中聚合模型
        if requested_model == "all-router-auto":
            raise AllModelsFailedError(
                "all-router-auto 已停用，请改用具体聚合模型名和聚合模型 Key",
                attempted=0,
                error_code="global_auto_deprecated",
            )
        return None

    def _aggregate_member_skip_reason(self, member: AggregateMember) -> Tuple[str, str, Optional[ConnectionGroup], Optional[ModelConfig]]:
        """返回聚合成员跳过原因；空 reason 表示可参与调度。"""
        group = self.store.find_group(member.group_id)
        model = self.store.find_model(member.model_id)
        now_ts = int(time.time())
        if not member.enabled:
            return "member_disabled", "该聚合成员已手动停用，不参与本次调度。", group, model
        if member.cooldown_until and member.cooldown_until > now_ts:
            return "member_cooling", "该聚合成员正在冷却中，本次直接跳过。", group, model
        if not group:
            return "underlying_group_missing", "底层连接组不存在，请检查聚合成员配置。", group, model
        if not model:
            return "underlying_model_missing", "底层真实模型不存在，请检查聚合成员配置。", group, model
        if not model.usable or getattr(model, "disabled_by_user", False):
            return "underlying_model_disabled", "底层真实模型已停用，请先启用真实模型。", group, model
        if model.cooldown_until and model.cooldown_until > now_ts:
            return "underlying_model_cooling", "底层真实模型冷却中，本次直接跳过。", group, model
        return "", "", group, model

    def _aggregate_member_usable(self, member: AggregateMember) -> bool:
        """检查聚合成员是否可用（存在、启用、未 cooldown、真实模型未被用户手动禁用）。"""
        reason, _, _, _ = self._aggregate_member_skip_reason(member)
        return not reason

    def _log_aggregate_member_skip(
        self,
        path: str,
        aggregate: AggregateModel,
        member: AggregateMember,
        reason: str,
        message: str,
        group: Optional[ConnectionGroup],
        model: Optional[ModelConfig],
        requested_label: str,
        request_id: str,
        resolved_as: str,
    ) -> None:
        selected_group = group.name if group else member.group_id
        selected_model = model.name if model else member.model_id
        selected_upstream_model = (model.upstream_model or model.ep_id) if model else ""
        suffix = self._aggregate_log_suffix(
            resolved_as=resolved_as,
            aggregate_model=aggregate.name,
            aggregate_id=aggregate.id,
            selected_group=selected_group,
            selected_model=selected_model,
            selected_upstream_model=selected_upstream_model,
            selection_reason=f"skip_{reason}",
            fallback_index=0,
            fallback_chain=[],
            strategy=aggregate.strategy or "priority",
            manual_price=member.manual_price,
        )
        detail = (
            f"requested={requested_label}; skip_reason={reason}; aggregate_member_id={member.id}; "
            f"cooldown_applied=false; failure_scope=skip; {message}; {suffix}"
        )
        label = selected_model or member.model_id or member.id
        self.add_log(path, label, "skip", detail, group=group, event="skip", request_id=request_id, attempt=0, cooldown_applied=False, failure_scope="skip")

    def _iter_aggregate_candidates(
        self,
        aggregate: AggregateModel,
        *,
        log_skips: bool = False,
        path: str = "",
        requested_label: str = "",
        request_id: str = "",
        resolved_as: str = "",
    ) -> Iterator[UpstreamCandidate]:
        """按聚合模型策略产出候选成员。"""
        self.store.refresh_expired_cooldowns()
        members = self.store.get_aggregate_members(aggregate.id)
        strategy = aggregate.strategy or "priority"
        if strategy == "price_first":
            members = sorted(
                members,
                key=lambda m: (
                    m.manual_price is None,
                    m.manual_price if m.manual_price is not None else 0,
                    m.priority,
                ),
            )
        else:
            members = sorted(members, key=lambda m: m.priority)
        for member in members:
            reason, message, group, model = self._aggregate_member_skip_reason(member)
            if reason:
                if log_skips:
                    self._log_aggregate_member_skip(path, aggregate, member, reason, message, group, model, requested_label, request_id, resolved_as)
                continue
            if not group or not model:
                continue
            candidate = self._candidate_from_model(self.store.models.index(model), model, group)
            candidate.aggregate_id = aggregate.id
            candidate.aggregate_name = aggregate.name
            candidate.aggregate_member_id = member.id
            candidate.manual_price = member.manual_price
            yield candidate

    def _aggregate_cooldown_seconds(self, aggregate: AggregateModel) -> int:
        try:
            minutes = int(aggregate.cooldown_minutes)
        except Exception:
            minutes = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
        return max(0, minutes) * 60

    def _set_aggregate_member_cooldown(self, member_id: str, error: str, cooldown_seconds: int, reason: str) -> None:
        member = next((m for m in self.store.aggregate_members if m.id == member_id), None)
        if not member:
            return
        now_ts = int(time.time())
        member.last_error = error[:500]
        member.last_checked_at = self._now()
        member.cooldown_until = now_ts + max(0, cooldown_seconds)
        member.cooldown_reason = reason[:120]
        self.store.save()

    def _mark_aggregate_member_success(self, member_id: str) -> None:
        member = next((m for m in self.store.aggregate_members if m.id == member_id), None)
        if not member:
            return
        member.last_error = ""
        member.last_success_at = self._now()
        member.last_checked_at = member.last_success_at
        self.store.save()

    def _set_unusable(self, idx: int, error: str) -> None:
        model = self.store.models[idx]
        model.usable = False
        model.last_error = error[:500]
        model.last_checked_at = self._now()
        model.cooldown_until = 0
        model.cooldown_reason = ""
        self.store.save()

    def _set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        model = self.store.models[idx]
        now_ts = int(time.time())
        model.usable = False
        model.last_error = error[:500]
        model.last_checked_at = self._now()
        model.cooldown_until = now_ts + max(0, cooldown_seconds)
        model.cooldown_reason = reason[:120]
        self.store.save()

    def _set_success(self, idx: int) -> None:
        model = self.store.models[idx]
        model.last_error = ""
        model.last_success_at = self._now()
        model.last_checked_at = model.last_success_at
        self.store.save()

    def _mark_unusable(self, candidate: UpstreamCandidate, error: str) -> None:
        if candidate.idx is not None:
            self._set_unusable(candidate.idx, error)

    def _mark_success(self, candidate: UpstreamCandidate) -> None:
        if candidate.idx is not None:
            self._set_success(candidate.idx)

    @staticmethod
    def _route_group_id(route: RouteContext | str | None) -> str | None:
        if isinstance(route, RouteContext):
            return route.group_id
        return route

    def _auto_cooldown_seconds(self, group: Optional[ConnectionGroup]) -> int:
        if not group:
            return DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES * 60
        try:
            minutes = int(group.auto_model_cooldown_minutes)
        except Exception:
            minutes = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
        return max(0, minutes) * 60

    @staticmethod
    def _stream_idle_timeout_seconds(group: Optional[ConnectionGroup]) -> int:
        if not group:
            return DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        try:
            seconds = int(group.stream_idle_timeout)
        except Exception:
            seconds = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        return max(0, min(MAX_STREAM_IDLE_TIMEOUT_SECONDS, seconds))

    def _mark_stream_timeout(self, candidate: UpstreamCandidate, error: str) -> int:
        if candidate.idx is None:
            return 0
        cooldown_seconds = self._auto_cooldown_seconds(candidate.group)
        self._set_cooldown(candidate.idx, error, cooldown_seconds, "stream_timeout")
        return cooldown_seconds

    @staticmethod
    def _readline_with_idle_timeout(resp: Any, timeout_seconds: int) -> bytes:
        # UpstreamResponse 等包装对象支持带超时的 readline
        if hasattr(resp, "readline") and callable(getattr(resp, "readline")):
            try:
                return resp.readline(timeout_seconds)
            except TypeError:
                pass
        if timeout_seconds <= 0:
            return resp.readline()
        result: queue.Queue[Any] = queue.Queue(maxsize=1)

        def read_once() -> None:
            try:
                result.put(resp.readline())
            except Exception as exc:
                result.put(exc)

        worker = threading.Thread(target=read_once, daemon=True)
        worker.start()
        try:
            item = result.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise StreamIdleTimeoutError("stream_idle_timeout") from exc
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def _body_for_upstream(payload: Dict[str, Any], raw_body: bytes | None, requested_model: str | None, target_model: str) -> Tuple[bytes, str]:
        if raw_body and requested_model:
            if requested_model == target_model:
                return raw_body, "raw"
            target = json.dumps(target_model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            source_variants = {
                json.dumps(requested_model, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                json.dumps(requested_model, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            }
            for source in source_variants:
                pattern = rb'("model"\s*:\s*)' + re.escape(source)
                patched, count = re.subn(pattern, rb"\1" + target, raw_body, count=1)
                if count:
                    return patched, "raw-model-patch"
        outbound_payload = dict(payload)
        outbound_payload["model"] = target_model
        return json.dumps(outbound_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "json-rebuild"

    def call(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None, incoming_headers: Optional[Dict[str, str]] = None, raw_body: bytes | None = None) -> Tuple[int, Dict[str, str], bytes]:
        self.store.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = self._route_group_id(route)
        route_group = route.group if isinstance(route, RouteContext) else self.store.find_group(group_id) if group_id else None
        is_deprecated_global = isinstance(route, RouteContext) and route.is_deprecated_global
        if is_deprecated_global:
            return 403, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key", "type": "global_key_deprecated", "code": "use_group_or_aggregate_key"}}, ensure_ascii=False).encode("utf-8")
        route_aggregate = route.aggregate if isinstance(route, RouteContext) else None
        is_global = isinstance(route, RouteContext) and route.is_global
        auto_mode = self._is_auto_model(str(requested_model) if requested_model else None, route_group)
        # auto_fallback：组级 auto 或聚合模型下，失败时尝试下一个候选（全局 Key 已退役）
        auto_fallback = auto_mode or bool(route_aggregate)
        request_id = uuid.uuid4().hex[:12]
        self._live_request_start(request_id, path, requested_label, stream=False)
        attempt = 0
        last_error: Optional[Exception] = None
        saw_cooldown = False
        saw_request_level = False

        # 聚合模型解析
        aggregate_info = self._resolve_aggregate(
            str(requested_model) if requested_model else None,
            route,
        )
        aggregate_model: Optional[AggregateModel] = None
        resolved_as = ""
        fallback_index = 0
        fallback_chain: List[Dict[str, Any]] = []
        if aggregate_info:
            aggregate_model, resolved_as = aggregate_info
            auto_fallback = True
            candidates_iter: Iterator[UpstreamCandidate] = self._iter_aggregate_candidates(aggregate_model, log_skips=True, path=path, requested_label=requested_label, request_id=request_id, resolved_as=resolved_as)
        else:
            candidates_iter = self._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id)

        for candidate in candidates_iter:
            attempt += 1
            group = candidate.group
            target_url = self._resolve_url(group.base_url, path)
            self._live_request_update(
                request_id,
                stage="preparing_upstream",
                stage_label="准备上游请求",
                group=group.name,
                candidate=candidate.label,
                model=candidate.label,
                aggregate_model=aggregate_model.name if aggregate_model else "",
                attempt=attempt,
            )
            is_aggregate_candidate = bool(candidate.aggregate_member_id)
            selection_reason = "priority_first" if fallback_index == 0 else "fallback_after_failure"
            aggregate_suffix = ""
            if is_aggregate_candidate and aggregate_model:
                model_name = candidate.model.name if candidate.model else ""
                aggregate_suffix = self._aggregate_log_suffix(
                    resolved_as=resolved_as,
                    aggregate_model=aggregate_model.name,
                    aggregate_id=aggregate_model.id,
                    selected_group=group.name,
                    selected_model=model_name,
                    selected_upstream_model=candidate.target_model,
                    selection_reason=selection_reason,
                    fallback_index=fallback_index,
                    fallback_chain=fallback_chain,
                    strategy=aggregate_model.strategy or "priority",
                    manual_price=candidate.manual_price,
                )
            if not candidate.auth_key:
                skip_detail = f"requested={requested_label}; missing upstream api key"
                if aggregate_suffix:
                    skip_detail += "; " + aggregate_suffix
                self.add_log(path, candidate.label, "skip", skip_detail, group=group, request_id=request_id, attempt=attempt, event="skip")
                continue
            payload_for_upstream = payload
            tools_normalized = False
            if self._tools_order_enabled():
                payload_for_upstream, tools_normalized = self._normalize_tools_order(payload)
            body, body_mode = self._body_for_upstream(payload_for_upstream, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = self._headers_for(group, candidate.auth_key, incoming_headers, stream=False)
            upstream_lock = self._candidate_lock(candidate)
            started_at = time.perf_counter()
            if upstream_lock:
                self._live_request_update(request_id, stage="waiting_waf_lock", stage_label="等待 WAF 锁")
            acquired, lock_wait_ms = self._acquire_upstream_lock(upstream_lock)
            if not acquired:
                self._live_request_update(request_id, stage="candidate_busy", stage_label="候选忙/等待锁超时", possible_reason="候选正在处理大上下文请求，已临时切换")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self.add_log(
                    path,
                    candidate.label,
                    "timeout",
                    self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, self._waf_lock_busy_detail(candidate, body, lock_wait_ms), lock_wait_ms=lock_wait_ms),
                    duration_ms,
                    group=group,
                    request_id=request_id,
                    attempt=attempt,
                    event="waf_lock_timeout",
                    cooldown_applied=False,
                    failure_scope="busy",
                )
                if auto_fallback:
                    continue
                self._live_request_finish(request_id, "error")
                return 503, {"Content-Type": "application/json; charset=utf-8"}, [json.dumps({"error": {"message": "候选正在处理大上下文请求，已临时切换到下一个候选", "type": "timeout", "code": "waf_lock_wait_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")]
            try:
                self._live_request_update(request_id, stage="connecting_upstream", stage_label="连接上游")
                resp = self._upstream_client.request("POST", target_url, outbound_headers, body, stream=False, timeout=120)
                with resp:
                    self._live_request_update(request_id, stage="receiving_response", stage_label="接收响应")
                    data = resp.read()
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = self._usage_from_response(data)
                    self._mark_success(candidate)
                    if candidate.aggregate_member_id:
                        self._mark_aggregate_member_success(candidate.aggregate_member_id)
                    self.add_log(
                        path,
                        candidate.label,
                        str(resp.status),
                        self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "ok", resp=resp, tools_normalized=tools_normalized, lock_wait_ms=lock_wait_ms, lock_release_reason="response_inline", aggregate_suffix=aggregate_suffix),
                        duration_ms,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cached_tokens,
                        reasoning_tokens,
                        group=group,
                        event="ok",
                        request_id=request_id,
                        usage_source="response_inline",
                    )
                    try:
                        self.debug_capture.capture(
                            path=path,
                            group=group,
                            model=candidate.label,
                            target_model=candidate.target_model,
                            body=body,
                            body_mode=body_mode,
                            headers=outbound_headers,
                            fingerprint=self._payload_fingerprint(payload_for_upstream, body, urlparse(target_url).path, tools_normalized=tools_normalized),
                            request_id=request_id,
                            usage_source="response_inline",
                        )
                    except Exception:
                        pass
                    self._live_request_finish(request_id, "done")
                    return resp.status, dict(resp.headers.items()), data
            except HTTPError as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                classification = self._classify_candidate_error(err.code, raw, "http")
                cooldown_applied = classification["should_cooldown"]
                is_request_level = classification["is_request_level"]
                if cooldown_applied:
                    saw_cooldown = True
                if is_request_level:
                    saw_request_level = True

                # 聚合成员失败：仅冷却类错误才写入 cooldown
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    if cooldown_applied:
                        cooldown_seconds = self._aggregate_cooldown_seconds(aggregate_model)
                        self._set_aggregate_member_cooldown(candidate.aggregate_member_id, raw or str(err), cooldown_seconds, classification["log_reason"])
                    failure_scope = classification["failure_scope"]
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": err.code,
                        "reason": self._short_error(raw),
                        "cooldown_applied": cooldown_applied,
                        "failure_scope": failure_scope,
                        "category": classification["category"],
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(raw)}{self._waf_blocked_suffix(classification, group)}"
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 429 立即重试一次（非聚合路径保持原有行为）
                if classification["category"] == "rate_limit" and not is_aggregate_candidate:
                    try:
                        retry_started_at = time.perf_counter()
                        with self._upstream_client.request("POST", target_url, outbound_headers, body, stream=False, timeout=120) as resp:
                            data = resp.read()
                            retry_duration_ms = int((time.perf_counter() - retry_started_at) * 1000)
                            prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = self._usage_from_response(data)
                            self._mark_success(candidate)
                            self.add_log(
                                path,
                                candidate.label,
                                str(resp.status),
                                self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "retry ok", resp=resp, lock_wait_ms=lock_wait_ms, lock_release_reason="retry_ok"),
                                retry_duration_ms,
                                prompt_tokens,
                                completion_tokens,
                                total_tokens,
                                cached_tokens,
                                reasoning_tokens,
                                group=group,
                                event="retry_ok",
                                cooldown_applied=False,
                            )
                            self._live_request_finish(request_id, "done")
                            return resp.status, dict(resp.headers.items()), data
                    except Exception as retry_err:
                        last_error = retry_err
                        retry_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        self.add_log(path, candidate.label, "retry failed", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, str(retry_err), lock_wait_ms=lock_wait_ms, lock_release_reason="retry_failed"), retry_duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=False)

                # 自动 fallback（组级 auto 或聚合模型）
                if auto_fallback:
                    if cooldown_applied:
                        if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                            self._set_cooldown(candidate.idx, raw or str(err), self._auto_cooldown_seconds(group), classification["log_reason"])
                        elif candidate.idx is not None:
                            self._set_unusable(candidate.idx, raw or str(err))
                        saw_cooldown = True
                    failure_scope = classification["failure_scope"]
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": err.code,
                            "reason": self._short_error(raw),
                            "cooldown_applied": cooldown_applied,
                            "failure_scope": failure_scope,
                            "category": classification["category"],
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(raw)}{self._waf_blocked_suffix(classification, group)}"
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 非自动 fallback：保留原有显式模型处理逻辑
                if self._is_quota_exhausted(err.code, raw):
                    self._mark_unusable(candidate, raw)
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "quota exhausted, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=False, failure_scope="upstream")
                    continue
                if self._is_server_error(err.code):
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "server error, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=False, failure_scope="upstream")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={self._short_error(raw)}"
                self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=False)
                self._live_request_finish(request_id, "error")
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                classification = self._classify_candidate_error(None, str(err), "network")
                saw_cooldown = True

                # 聚合成员网络失败：cooldown 聚合成员本身并记录 fallback 链路
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = self._aggregate_cooldown_seconds(aggregate_model)
                    self._set_aggregate_member_cooldown(candidate.aggregate_member_id, str(err), cooldown_seconds, classification["log_reason"])
                    failure_scope = classification["failure_scope"]
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": "network",
                        "reason": self._short_error(str(err)),
                        "cooldown_applied": True,
                        "failure_scope": failure_scope,
                        "category": classification["category"],
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(str(err))}"
                    self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                if auto_fallback:
                    if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                        self._set_cooldown(candidate.idx, str(err), self._auto_cooldown_seconds(group), classification["log_reason"])
                    elif candidate.idx is not None:
                        self._set_unusable(candidate.idx, str(err))
                    failure_scope = classification["failure_scope"]
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": "network",
                            "reason": self._short_error(str(err)),
                            "cooldown_applied": True,
                            "failure_scope": failure_scope,
                            "category": classification["category"],
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(str(err))}"
                    self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                failure_scope = classification["failure_scope"]
                detail = f"cooldown_applied=false; failure_scope={failure_scope}; {classification['log_reason']}; error={self._short_error(str(err))}"
                self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=False, failure_scope=failure_scope)
                continue
            finally:
                self._release_lock(upstream_lock)

        if aggregate_model:
            self._live_request_finish(request_id, "error")
            if not saw_cooldown and saw_request_level:
                raise AllModelsFailedError(
                    f"聚合模型 {aggregate_model.name} 的所有成员均因请求级错误被拒绝{self._waf_blocked_hint(fallback_chain)}",
                    attempted=attempt,
                    error_code="upstream_request_rejected",
                    fallback_chain=fallback_chain,
                    aggregate_name=aggregate_model.name,
                )
            raise AllModelsFailedError(
                f"聚合模型 {aggregate_model.name} 的所有成员均不可用",
                attempted=attempt,
                error_code="aggregate_members_unavailable",
                fallback_chain=fallback_chain,
                aggregate_name=aggregate_model.name,
            )
        self._live_request_finish(request_id, "error")
        if last_error is None:
            raise AllModelsFailedError("没有可用模型", attempted=attempt, error_code="no_usable_models")
        if not saw_cooldown and saw_request_level:
            raise AllModelsFailedError(
                f"所有候选均因请求级错误被拒绝{self._waf_blocked_hint(fallback_chain)}",
                attempted=attempt,
                error_code="upstream_request_rejected",
            )
        raise AllModelsFailedError(
            f"所有可用模型均请求失败，共尝试 {attempt} 个上游",
            attempted=attempt,
            error_code="all_models_failed",
        ) from last_error

    def stream(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None, incoming_headers: Optional[Dict[str, str]] = None, raw_body: bytes | None = None) -> Tuple[int, Dict[str, str], Iterable[bytes]]:
        """流式请求调度。聚合 fallback 只允许在向客户端写出首字节之前发生；
        一旦 iterator 被返回并 yield 第一个 chunk，后续只透传当前上游流，不再切换候选。"""
        self.store.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = self._route_group_id(route)
        route_group = route.group if isinstance(route, RouteContext) else self.store.find_group(group_id) if group_id else None
        is_deprecated_global = isinstance(route, RouteContext) and route.is_deprecated_global
        if is_deprecated_global:
            def deprecated_iter():
                yield json.dumps({"error": {"message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key", "type": "global_key_deprecated", "code": "use_group_or_aggregate_key"}}, ensure_ascii=False).encode("utf-8")
            return 403, {"Content-Type": "application/json; charset=utf-8"}, deprecated_iter()
        route_aggregate = route.aggregate if isinstance(route, RouteContext) else None
        is_global = isinstance(route, RouteContext) and route.is_global
        auto_mode = self._is_auto_model(str(requested_model) if requested_model else None, route_group)
        auto_fallback = auto_mode or bool(route_aggregate)
        request_id = uuid.uuid4().hex[:12]
        self._live_request_start(request_id, path, requested_label, stream=True)
        attempt = 0
        last_error: Optional[Exception] = None
        saw_stream_timeout = False
        saw_cooldown = False
        saw_request_level = False

        # 聚合模型解析
        aggregate_info = self._resolve_aggregate(
            str(requested_model) if requested_model else None,
            route,
        )
        aggregate_model: Optional[AggregateModel] = None
        resolved_as = ""
        fallback_index = 0
        fallback_chain: List[Dict[str, Any]] = []
        if aggregate_info:
            aggregate_model, resolved_as = aggregate_info
            auto_fallback = True
            candidates_iter = self._iter_aggregate_candidates(aggregate_model, log_skips=True, path=path, requested_label=requested_label, request_id=request_id, resolved_as=resolved_as)
        else:
            candidates_iter = self._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id)

        for candidate in candidates_iter:
            attempt += 1
            group = candidate.group
            target_url = self._resolve_url(group.base_url, path)
            self._live_request_update(
                request_id,
                stage="preparing_upstream",
                stage_label="准备上游流式请求",
                group=group.name,
                candidate=candidate.label,
                model=candidate.label,
                aggregate_model=aggregate_model.name if aggregate_model else "",
                attempt=attempt,
            )
            idle_timeout = self._stream_idle_timeout_seconds(group)
            is_aggregate_candidate = bool(candidate.aggregate_member_id)
            selection_reason = "priority_first" if fallback_index == 0 else "fallback_after_failure"
            aggregate_suffix = ""
            if is_aggregate_candidate and aggregate_model:
                model_name = candidate.model.name if candidate.model else ""
                aggregate_suffix = self._aggregate_log_suffix(
                    resolved_as=resolved_as,
                    aggregate_model=aggregate_model.name,
                    aggregate_id=aggregate_model.id,
                    selected_group=group.name,
                    selected_model=model_name,
                    selected_upstream_model=candidate.target_model,
                    selection_reason=selection_reason,
                    fallback_index=fallback_index,
                    fallback_chain=fallback_chain,
                    strategy=aggregate_model.strategy or "priority",
                    manual_price=candidate.manual_price,
                )
            if not candidate.auth_key:
                skip_detail = f"requested={requested_label}; missing upstream api key"
                if aggregate_suffix:
                    skip_detail += "; " + aggregate_suffix
                self.add_log(path, candidate.label, "skip", skip_detail, group=group, request_id=request_id, attempt=attempt, event="skip")
                continue
            payload_for_upstream = payload
            tools_normalized = False
            if self._tools_order_enabled():
                payload_for_upstream, tools_normalized = self._normalize_tools_order(payload)
            body, body_mode = self._body_for_upstream(payload_for_upstream, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = self._headers_for(group, candidate.auth_key, incoming_headers, stream=True)
            upstream_lock = self._candidate_lock(candidate)
            resp: Optional[Any] = None
            started_at = time.perf_counter()
            if upstream_lock:
                self._live_request_update(request_id, stage="waiting_waf_lock", stage_label="等待 WAF 锁")
            acquired, lock_wait_ms = self._acquire_upstream_lock(upstream_lock)
            if not acquired:
                self._live_request_update(request_id, stage="candidate_busy", stage_label="候选忙/等待锁超时", possible_reason="候选正在处理大上下文请求，已临时切换")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self.add_log(
                    path,
                    candidate.label,
                    "timeout",
                    self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, self._waf_lock_busy_detail(candidate, body, lock_wait_ms), lock_wait_ms=lock_wait_ms, aggregate_suffix=aggregate_suffix),
                    duration_ms,
                    group=group,
                    request_id=request_id,
                    attempt=attempt,
                    event="waf_lock_timeout",
                    cooldown_applied=False,
                    failure_scope="busy",
                )
                if auto_fallback:
                    continue
                error_body = json.dumps({"error": {"message": "候选正在处理大上下文请求，已临时切换到下一个候选", "type": "timeout", "code": "waf_lock_wait_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                self._live_request_finish(request_id, "error")
                return 503, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
            try:
                self._live_request_update(request_id, stage="connecting_upstream", stage_label="连接上游")
                resp = self._upstream_client.request("POST", target_url, outbound_headers, body, stream=True, timeout=120)
                self._live_request_update(request_id, stage="waiting_first_byte", stage_label="等待首包")
                first_chunk = self._readline_with_idle_timeout(resp, idle_timeout)
                if not first_chunk:
                    raise URLError("upstream stream closed before first chunk")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                latest_usage = self._usage_from_stream_chunk(first_chunk)
                self._mark_success(candidate)
                if candidate.aggregate_member_id:
                    self._mark_aggregate_member_success(candidate.aggregate_member_id)
                detail = self._debug_detail(
                    candidate,
                    requested_label,
                    target_url,
                    body_mode,
                    body,
                    payload_for_upstream,
                    outbound_headers,
                    f"stream ok; idle_timeout_seconds={idle_timeout}; chunks_received=1; bytes_received={len(first_chunk)}; final_result=streaming",
                    resp=resp,
                    tools_normalized=tools_normalized,
                    lock_wait_ms=lock_wait_ms,
                    aggregate_suffix=aggregate_suffix,
                )
                self.add_log(path, candidate.label, "200", detail, duration_ms, *latest_usage, group=group, request_id=request_id, attempt=attempt, event="stream_ok")
                self._live_request_update(request_id, stage="streaming", stage_label="接收流式响应")
                self._mark_stream_active(candidate, 1)
                try:
                    self.debug_capture.capture(
                        path=path,
                        group=group,
                        model=candidate.label,
                        target_model=candidate.target_model,
                        body=body,
                        body_mode=body_mode,
                        headers=outbound_headers,
                        fingerprint=self._payload_fingerprint(payload_for_upstream, body, urlparse(target_url).path, tools_normalized=tools_normalized),
                        request_id=request_id,
                        usage_source="",
                    )
                except Exception:
                    pass

                def iterator() -> Iterator[bytes]:
                    usage_total = latest_usage
                    chunks_received = 1
                    bytes_received = len(first_chunk)
                    stream_state = {"timeout": False, "completed_normally": False}
                    release_reason = "client_disconnect"
                    try:
                        yield first_chunk
                        while True:
                            try:
                                chunk = self._readline_with_idle_timeout(resp, idle_timeout)
                            except StreamIdleTimeoutError:
                                stream_state["timeout"] = True
                                release_reason = "stream_idle_timeout"
                                self.add_log(
                                    path,
                                    candidate.label,
                                    "timeout",
                                    self._debug_detail(
                                        candidate,
                                        requested_label,
                                        target_url,
                                        body_mode,
                                        body,
                                        payload,
                                        outbound_headers,
                                        f"reason=stream_idle_timeout; idle_timeout_seconds={idle_timeout}; chunks_received={chunks_received}; bytes_received={bytes_received}; final_result=client_stream_aborted",
                                        lock_wait_ms=lock_wait_ms,
                                        lock_release_reason=release_reason,
                                    ),
                                    int((time.perf_counter() - started_at) * 1000),
                                    *usage_total,
                                    group=group,
                                    request_id=request_id,
                                    attempt=attempt,
                                    event="stream_timeout",
                                    usage_source="stream_incomplete",
                                )
                                break
                            if not chunk:
                                stream_state["completed_normally"] = True
                                release_reason = "stream_final" if any(usage_total) else "missing"
                                break
                            chunks_received += 1
                            bytes_received += len(chunk)
                            usage = self._usage_from_stream_chunk(chunk)
                            if any(usage):
                                usage_total = usage
                            yield chunk
                    finally:
                        if resp:
                            resp.close()
                        if stream_state["timeout"]:
                            usage_source = "stream_incomplete"
                            lifecycle_event = "stream_idle_timeout"
                            lifecycle_status = "timeout"
                            lifecycle_result = "stream_idle_timeout"
                        elif stream_state["completed_normally"]:
                            usage_source = "stream_final" if any(usage_total) else "missing"
                            lifecycle_event = "stream_done"
                            lifecycle_status = "200"
                            lifecycle_result = "stream_done"
                        else:
                            usage_source = "stream_incomplete"
                            lifecycle_event = "client_disconnected"
                            lifecycle_status = "client_disconnected"
                            lifecycle_result = "client_disconnected"
                        self.update_latest_stream_usage(request_id, usage_total, usage_source, lock_wait_ms=lock_wait_ms, lock_release_reason=release_reason)
                        self._live_request_finish(request_id, "done" if stream_state["completed_normally"] else "ended")
                        lifecycle_detail = self._debug_detail(
                            candidate,
                            requested_label,
                            target_url,
                            body_mode,
                            body,
                            payload_for_upstream,
                            outbound_headers,
                            f"stream_finalized=true; final_result={lifecycle_result}; chunks_received={chunks_received}; bytes_received={bytes_received}",
                            lock_wait_ms=lock_wait_ms,
                            lock_release_reason=release_reason,
                            aggregate_suffix=aggregate_suffix,
                        )
                        self.add_log(path, candidate.label, lifecycle_status, lifecycle_detail, int((time.perf_counter() - started_at) * 1000), *usage_total, group=group, request_id=request_id, attempt=attempt, event=lifecycle_event, usage_source=usage_source)
                        self._mark_stream_active(candidate, -1)
                        self._release_lock(upstream_lock)

                return 200, dict(resp.headers.items()), iterator(), request_id
            except StreamIdleTimeoutError as err:
                saw_stream_timeout = True
                saw_cooldown = True
                last_error = err
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                if resp:
                    resp.close()
                self._release_lock(upstream_lock)
                # 聚合成员在首包前 stream 超时：cooldown 聚合成员并继续 fallback
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = self._aggregate_cooldown_seconds(aggregate_model)
                    self._set_aggregate_member_cooldown(candidate.aggregate_member_id, "stream_idle_timeout", cooldown_seconds, "stream_idle_timeout")
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": "stream_idle_timeout",
                        "reason": "stream_idle_timeout",
                        "cooldown_applied": True,
                        "failure_scope": "upstream",
                        "category": "stream_idle_timeout",
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope=upstream; reason=stream_idle_timeout; idle_timeout_seconds={idle_timeout}; chunks_received=0; bytes_received=0; cooldown_minutes={cooldown_seconds // 60}; fallback_next=true; final_result=timeout"
                    self.add_log(path, candidate.label, "timeout", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="stream_idle_timeout", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="stream_timeout", usage_source="stream_incomplete", cooldown_applied=True, failure_scope="upstream")
                    continue
                cooldown_seconds = self._mark_stream_timeout(candidate, "stream_idle_timeout")
                detail = self._debug_detail(
                    candidate,
                    requested_label,
                    target_url,
                    body_mode,
                    body,
                    payload,
                    outbound_headers,
                    f"cooldown_applied=true; reason=stream_idle_timeout; idle_timeout_seconds={idle_timeout}; chunks_received=0; bytes_received=0; cooldown_minutes={cooldown_seconds // 60}; fallback_next={str(auto_fallback).lower()}; final_result=timeout",
                    lock_wait_ms=lock_wait_ms,
                    lock_release_reason="stream_idle_timeout",
                )
                self.add_log(path, candidate.label, "timeout", detail, duration_ms, group=group, request_id=request_id, attempt=attempt, event="stream_timeout", usage_source="stream_incomplete", cooldown_applied=True, failure_scope="upstream")
                if auto_fallback:
                    self.add_log(path, candidate.label, "fallback", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "reason=stream_idle_timeout; fallback_next=true", lock_wait_ms=lock_wait_ms), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=True, failure_scope="upstream")
                    continue
                error_body = json.dumps({"error": {"message": "流式响应空闲超时，请稍后重试", "type": "timeout", "code": "stream_idle_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                return 504, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
            except HTTPError as err:
                if resp:
                    resp.close()
                self._release_lock(upstream_lock)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                classification = self._classify_candidate_error(err.code, raw, "http")
                cooldown_applied = classification["should_cooldown"]
                is_request_level = classification["is_request_level"]
                if cooldown_applied:
                    saw_cooldown = True
                if is_request_level:
                    saw_request_level = True

                # 聚合成员 HTTP 失败：仅冷却类错误才写入 cooldown
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    if cooldown_applied:
                        cooldown_seconds = self._aggregate_cooldown_seconds(aggregate_model)
                        self._set_aggregate_member_cooldown(candidate.aggregate_member_id, raw or str(err), cooldown_seconds, classification["log_reason"])
                    failure_scope = classification["failure_scope"]
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": err.code,
                        "reason": self._short_error(raw),
                        "cooldown_applied": cooldown_applied,
                        "failure_scope": failure_scope,
                        "category": classification["category"],
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(raw)}{self._waf_blocked_suffix(classification, group)}"
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 自动 fallback（组级 auto 或聚合模型）
                if auto_fallback:
                    if cooldown_applied:
                        if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                            self._set_cooldown(candidate.idx, raw or str(err), self._auto_cooldown_seconds(group), classification["log_reason"])
                        elif candidate.idx is not None:
                            self._set_unusable(candidate.idx, raw or str(err))
                        saw_cooldown = True
                    failure_scope = classification["failure_scope"]
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": err.code,
                            "reason": self._short_error(raw),
                            "cooldown_applied": cooldown_applied,
                            "failure_scope": failure_scope,
                            "category": classification["category"],
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(raw)}{self._waf_blocked_suffix(classification, group)}"
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 非自动 fallback：保留原有显式模型处理逻辑
                if self._is_quota_exhausted(err.code, raw):
                    self._mark_unusable(candidate, raw)
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "quota exhausted, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=False, failure_scope="upstream")
                    continue
                if self._is_server_error(err.code):
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "server error, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=False, failure_scope="upstream")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={self._short_error(raw)}"
                self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=False)
                return err.code, headers, [raw.encode("utf-8")], request_id
            except (URLError, TimeoutError, OSError) as err:
                if resp:
                    resp.close()
                self._release_lock(upstream_lock)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                classification = self._classify_candidate_error(None, str(err), "network")
                saw_cooldown = True

                # 聚合成员网络失败：cooldown 聚合成员本身并记录 fallback 链路
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = self._aggregate_cooldown_seconds(aggregate_model)
                    self._set_aggregate_member_cooldown(candidate.aggregate_member_id, str(err), cooldown_seconds, classification["log_reason"])
                    failure_scope = classification["failure_scope"]
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": "network",
                        "reason": self._short_error(str(err)),
                        "cooldown_applied": True,
                        "failure_scope": failure_scope,
                        "category": classification["category"],
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(str(err))}"
                    self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                if auto_fallback:
                    if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                        self._set_cooldown(candidate.idx, str(err), self._auto_cooldown_seconds(group), classification["log_reason"])
                    elif candidate.idx is not None:
                        self._set_unusable(candidate.idx, str(err))
                    failure_scope = classification["failure_scope"]
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": "network",
                            "reason": self._short_error(str(err)),
                            "cooldown_applied": True,
                            "failure_scope": failure_scope,
                            "category": classification["category"],
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={self._short_error(str(err))}"
                    self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                failure_scope = classification["failure_scope"]
                detail = f"cooldown_applied=false; failure_scope={failure_scope}; {classification['log_reason']}; error={self._short_error(str(err))}"
                self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=False, failure_scope=failure_scope)
                continue

        if aggregate_model:
            self._live_request_finish(request_id, "error")
            if not saw_cooldown and saw_request_level:
                raise AllModelsFailedError(
                    f"聚合模型 {aggregate_model.name} 的所有成员均因请求级错误被拒绝{self._waf_blocked_hint(fallback_chain)}",
                    attempted=attempt,
                    stream_timeout=saw_stream_timeout,
                    error_code="upstream_request_rejected",
                    fallback_chain=fallback_chain,
                    aggregate_name=aggregate_model.name,
                )
            raise AllModelsFailedError(
                f"聚合模型 {aggregate_model.name} 的所有成员均不可用",
                attempted=attempt,
                stream_timeout=saw_stream_timeout,
                error_code="aggregate_members_unavailable",
                fallback_chain=fallback_chain,
                aggregate_name=aggregate_model.name,
            )
        self._live_request_finish(request_id, "error")
        if last_error is None:
            raise AllModelsFailedError(
                "没有可用模型",
                attempted=attempt,
                stream_timeout=saw_stream_timeout,
                error_code="no_usable_models",
            )
        if not saw_cooldown and saw_request_level:
            raise AllModelsFailedError(
                f"所有候选均因请求级错误被拒绝{self._waf_blocked_hint(fallback_chain)}",
                attempted=attempt,
                stream_timeout=saw_stream_timeout,
                error_code="upstream_request_rejected",
            )
        raise AllModelsFailedError(
            f"所有可用模型均请求失败，共尝试 {attempt} 个上游",
            attempted=attempt,
            stream_timeout=saw_stream_timeout,
            error_code="all_models_failed",
        ) from last_error






class RouterHandler(BaseHTTPRequestHandler):
    server_version = "LinRouter/2.0"

    @property
    def store(self) -> ConfigStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def router(self) -> ArkProxyRouter:
        return self.server.router  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_all_models_failed_error(self, err: AllModelsFailedError) -> None:
        """统一 AllModelsFailedError 错误响应：message 中文，type/code 英文机器码。

        状态码按错误语义区分：
        - 客户端请求错误（模型不存在、已退役、已禁用）使用 4xx；
        - 仅「所有聚合成员都不可用」或「无可用模型」才返回 503；
        - 流式空闲超时返回 504。
        """
        err_code = getattr(err, "error_code", "") or ""
        if err_code == "model_not_found":
            status = 400
        elif err_code == "upstream_request_rejected":
            status = 400
        elif err_code == "global_auto_deprecated":
            status = 410
        elif err_code == "aggregate_disabled":
            status = 403
        elif getattr(err, "stream_timeout", False):
            status = 504
        else:
            status = 503
        err_type = "all_models_failed"
        code = "stream_idle_timeout" if status == 504 else "service_unavailable"
        message = str(err)
        details: Dict[str, Any] | None = None
        if err_code == "aggregate_disabled":
            err_type = "aggregate_disabled"
            code = "aggregate_disabled"
        elif err_code == "global_auto_deprecated":
            err_type = "global_auto_deprecated"
            code = "use_aggregate_model"
        elif err_code == "model_not_found":
            err_type = "model_not_found"
            code = "model_not_found"
        elif err_code == "upstream_request_rejected":
            err_type = "upstream_request_rejected"
            code = "upstream_request_rejected"
            details = {
                "attempted": err.attempted,
                "cooldown_applied": False,
                "fallback_chain": [],
            }
            for item in err.fallback_chain:
                details["fallback_chain"].append({
                    "member_id": item.get("member_id", ""),
                    "group": item.get("group", ""),
                    "model": item.get("model", ""),
                    "manual_price": item.get("manual_price"),
                    "status": item.get("status", ""),
                    "reason": item.get("reason", ""),
                    "cooldown_applied": item.get("cooldown_applied", False),
                })
        elif err_code == "aggregate_members_unavailable":
            err_type = "all_aggregate_members_failed"
            code = "aggregate_members_unavailable"
            aggregate = self.store.find_aggregate_by_name(getattr(err, "aggregate_name", "")) if getattr(err, "aggregate_name", "") else None
            details = {
                "aggregate_model": getattr(err, "aggregate_name", ""),
                "attempted": err.attempted,
                "strategy": aggregate.strategy if aggregate else "priority",
                "fallback_chain": [],
            }
            for item in err.fallback_chain:
                member_id = item.get("member_id") or ""
                member = self.store.find_aggregate_member(member_id) if member_id else None
                chain_entry = {
                    "member_id": member_id,
                    "group": item.get("group", ""),
                    "model": item.get("model", ""),
                    "manual_price": item.get("manual_price"),
                    "status": item.get("status", ""),
                    "reason": item.get("reason", ""),
                }
                if member and member.cooldown_until:
                    chain_entry["cooldown_until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(member.cooldown_until))
                details["fallback_chain"].append(chain_entry)
        elif err_code == "no_usable_models":
            code = "no_usable_models"
        elif err_code == "all_models_failed":
            code = "all_models_failed"
        payload: Dict[str, Any] = {
            "error": {
                "message": message,
                "type": err_type,
                "code": code,
            }
        }
        if details:
            payload["error"]["details"] = details
        self._send_json(payload, status=status)

    def _send_model_list(self, ctx: Any) -> None:
        # 旧全局 Key 已退役，/v1/models 不再返回所有真实模型
        if getattr(ctx, "is_deprecated_global", False):
            self._send_json({
                "error": {
                    "message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key",
                    "type": "global_key_deprecated",
                    "code": "use_group_or_aggregate_key",
                }
            }, status=403)
            return
        # 聚合模型 Key：只返回自身聚合模型
        if getattr(ctx, "aggregate", None):
            aggregate = ctx.aggregate
            if not aggregate.enabled:
                self._send_json({
                    "error": {
                        "message": f"聚合模型 {aggregate.name} 已禁用",
                        "type": "aggregate_disabled",
                        "code": "aggregate_disabled",
                    }
                }, status=403)
                return
            self._send_json({
                "object": "list",
                "data": [{
                    "id": aggregate.name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "lin-router",
                    "root": aggregate.name,
                    "parent": None,
                    "display_name": aggregate.display_name or aggregate.name,
                    "is_aggregate": True,
                    "aggregate_id": aggregate.id,
                    "usable": True,
                }]
            })
            return
        group = ctx.group
        visible_group = group
        # 先统计匹配模型数，便于排查“只能看到 auto”的问题
        matched_models = [
            model for model in self.store.models
            if not visible_group or model.group_id == visible_group.id
        ]
        # 连接组 Key 使用组自定义 auto_model_name（默认 lin-router-auto）
        auto_model_name = self.router.group_auto_model_name(group)
        # 成功的 /v1/models 请求不再记录到最近请求，避免客户端频繁拉取模型列表时刷屏
        data = [{
            "id": auto_model_name,
            "object": "model",
            "created": 0,
            "owned_by": "lin-router",
            "root": auto_model_name,
            "parent": None,
        }]
        # /v1/models 返回对应连接组下的全部已配置模型（包含禁用的），方便客户端查看完整列表
        for model in matched_models:
            model_group = self.store.find_group(model.group_id)
            data.append({
                "id": model.name,
                "object": "model",
                "created": 0,
                "owned_by": "lin-router",
                "root": model.name,
                "parent": None,
                "display_name": model.name,
                "ep_id": model.ep_id,
                "group_id": model.group_id,
                "provider_type": model_group.provider_type if model_group else "",
                "price_group": model.price_group,
                "usable": model.usable,
            })
        self._send_json({"object": "list", "data": data})

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path, content_type: str = "") -> None:
        import mimetypes
        if not file_path.exists():
            self._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)
            return
        data = file_path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_raw_body()
        return json.loads(raw.decode("utf-8"))

    def _read_raw_body(self) -> bytes:
        # do_PUT 转发到 do_POST 时会把 body 缓存在这里，避免重复读取 rfile
        cached = getattr(self, "_put_body", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b"{}"

    def _read_multipart_json(self) -> Optional[Dict[str, Any]]:
        """解析 multipart/form-data 上传的 JSON 配置文件，返回解析后的 dict。"""
        import email
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return None
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return None
        body = self.rfile.read(length)
        try:
            msg = email.message_from_bytes(
                b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
            )
            for part in msg.get_payload() or []:
                if not isinstance(part, email.message.Message):
                    continue
                name = part.get_param("name", header="Content-Disposition")
                if name == "file" or part.get_filename():
                    data = part.get_payload(decode=True)
                    if data:
                        return json.loads(data.decode("utf-8"))
        except Exception:
            return None
        return None

    @staticmethod
    def _json_from_raw(raw: bytes) -> Dict[str, Any]:
        return json.loads(raw.decode("utf-8"))

    def _client_base_url(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        return f"http://{host}/v1"

    def _effective_group_auth(self, group: ConnectionGroup, payload: Dict[str, Any] | None = None) -> str:
        payload = payload or {}
        api_key = str(payload.get("api_key") or "").strip()
        if api_key:
            return api_key
        if group.provider_type == PROVIDER_PROXY:
            return group.api_key or group.ark_api_key
        if group.provider_type == PROVIDER_RELAY:
            return group.ark_api_key or group.api_key
        return group.ark_api_key or group.api_key

    def _fetch_upstream_models(self, group: ConnectionGroup, auth_key: str) -> List[Dict[str, Any]]:
        target_url = self.router._resolve_url(group.base_url, "/v1/models")
        headers = build_model_fetch_headers(auth_key)
        request = Request(
            target_url,
            headers=headers,
            method="GET",
        )
        started_at = time.perf_counter()
        try:
            with urlopen(request, timeout=60, context=_ssl_context) as resp:
                raw = resp.read()
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self.router.add_log(
                    "/v1/models",
                    group.name,
                    str(resp.status),
                    f"fetch upstream models ok; upstream={target_url}; out_headers=({self.router._safe_header_view(headers)})",
                    duration_ms,
                    group=group,
                    event="fetch_models",
                )
        except HTTPError as err:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            body = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
            self.router.add_log(
                "/v1/models",
                group.name,
                str(err.code),
                f"fetch upstream models failed; upstream={target_url}; error={self.router._short_error(body)}; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models_failed",
            )
            raise RuntimeError(body or f"获取上游模型失败：HTTP {err.code}") from err
        except Exception as err:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.router.add_log(
                "/v1/models",
                group.name,
                "network",
                f"fetch upstream models failed; upstream={target_url}; error={self.router._short_error(str(err))}; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models_failed",
            )
            raise
        payload = json.loads(raw.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("上游模型列表格式无效")
        return [item for item in data if isinstance(item, dict)]

    def _clone_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        source = self.store.find_group(group_id)
        if not source:
            return None
        cloned = ConnectionGroup(
            id=uuid.uuid4().hex,
            name=f"{source.name} - 副本",
            provider_type=source.provider_type,
            base_url=source.base_url,
            ark_api_key=source.ark_api_key,
            api_key=source.api_key,
            route_key=new_route_key(),
            auto_model_cooldown_minutes=source.auto_model_cooldown_minutes if source.provider_type == PROVIDER_RELAY else 0,
            stream_idle_timeout=source.stream_idle_timeout,
            waf_compatible=source.waf_compatible,
            upstream_models=[dict(item) for item in source.upstream_models],
            upstream_models_fetched_at=source.upstream_models_fetched_at,
        )
        if cloned.provider_type == PROVIDER_PROXY and not cloned.api_key and cloned.ark_api_key:
            cloned.api_key = cloned.ark_api_key
        if cloned.provider_type in {PROVIDER_RELAY, PROVIDER_ARK}:
            cloned.api_key = ""
            cloned.ark_api_key = "" if cloned.provider_type == PROVIDER_RELAY else cloned.ark_api_key
        self.store.upsert_group(cloned)

        copied = 0
        source_models = [model for model in self.store.models if model.group_id == source.id]
        for model in source_models:
            self.store.upsert_model(ModelConfig(
                id=uuid.uuid4().hex,
                name=model.name,
                ep_id=model.ep_id,
                group_id=cloned.id,
                upstream_model=model.upstream_model,
                api_key=model.api_key,
                price_group=model.price_group,
                usable=model.usable,
                disabled_by_user=model.disabled_by_user,
                last_error=model.last_error,
                last_success_at=model.last_success_at,
                last_checked_at=model.last_checked_at,
                cooldown_until=model.cooldown_until,
                cooldown_reason=model.cooldown_reason,
            ))
            copied += 1
        return {"group": asdict(cloned), "copied_models": copied}

    def _route_context(self) -> Optional[RouteContext]:
        key = parse_bearer_key(self.headers.get("Authorization", ""))
        if not key:
            return None
        # 聚合模型 Key 优先识别
        aggregate = self.store.find_aggregate_by_route_key(key)
        if aggregate:
            return RouteContext(
                client_key=key,
                group=None,
                group_id=f"__aggregate__{aggregate.id}",
                provider_type="aggregate",
                base_url="",
                display_name=aggregate.display_name or aggregate.name,
                passthrough=False,
                is_global=False,
                aggregate=aggregate,
            )
        # 连接组 Key
        group = self.store.find_group_by_route_key(key)
        if group:
            return RouteContext(
                client_key=key,
                group=group,
                group_id=group.id,
                provider_type=group.provider_type,
                base_url=group.base_url,
                display_name=group.name,
                # 仅 relay 模式不走 passthrough；ark/proxy 均视为透传，避免中转站专属逻辑污染
                passthrough=group.provider_type != PROVIDER_RELAY,
            )
        # 旧全局 Key 已退役
        if key == DEFAULT_PUBLIC_API_KEY:
            return RouteContext(
                client_key=key,
                group=None,
                group_id=GLOBAL_ROUTE_GROUP_ID,
                provider_type="global",
                base_url=DEFAULT_BASE_URL,
                display_name="全局调度（已退役）",
                passthrough=False,
                is_global=True,
                is_deprecated_global=True,
            )
        return None

    def _route_group(self) -> Optional[ConnectionGroup]:
        ctx = self._route_context()
        return ctx.group if ctx else None

    def _require_route_context(self) -> Optional[RouteContext]:
        ctx = self._route_context()
        if ctx:
            return ctx
        self._send_json({
            "error": {
                "message": "缺少或无效的 Lin Router API Key，请使用连接组 Key 或聚合模型 Key",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }, status=401)
        return None

    def _require_route_group(self) -> Optional[ConnectionGroup]:
        ctx = self._require_route_context()
        return ctx.group if ctx else None

    def _visible_models(self, group: Optional[ConnectionGroup]) -> List[ModelConfig]:
        return [
            model
            for model in self.store.models
            if model.usable and (group is None or model.group_id == group.id)
        ]

    CONFIG_SKIP_REASONS = {
        "member_disabled",
        "member_cooling",
        "underlying_model_disabled",
        "underlying_model_cooling",
        "underlying_group_disabled",
    }

    def _log_detail_dict(self, detail: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if not detail:
            return result
        for part in str(detail).split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _log_value(self, log: Any, key: str, default: Any = "") -> Any:
        if isinstance(log, dict):
            return log.get(key, default)
        return getattr(log, key, default)

    def _is_config_skip_log(self, log: Any) -> bool:
        if self._log_value(log, "event") != "skip":
            return False
        detail = self._log_detail_dict(str(self._log_value(log, "detail", "") or ""))
        return detail.get("skip_reason") in self.CONFIG_SKIP_REASONS

    def _log_matches_aggregate(self, log: RequestLog, aggregate: AggregateModel) -> bool:
        return (
            log.aggregate_id == aggregate.id
            or log.aggregate_model == aggregate.name
            or log.requested_model == aggregate.name
            or log.model == aggregate.name
        )

    def _aggregate_stats_payload(self, aggregate_id: str, limit: int = 100) -> Dict[str, Any]:
        aggregate = self.store.find_aggregate(aggregate_id)
        if not aggregate:
            return {"ok": False, "message": "聚合模型不存在"}
        limit = max(1, min(int(limit or 100), 500))
        logs = [log for log in reversed(self.router.all_logs()) if self._log_matches_aggregate(log, aggregate)]
        by_request: Dict[str, List[RequestLog]] = {}
        synthetic_idx = 0
        for log in logs:
            key = log.request_id or f"__log_{synthetic_idx}"
            if not log.request_id:
                synthetic_idx += 1
            by_request.setdefault(key, []).append(log)

        request_groups: List[List[RequestLog]] = []
        for group_logs in by_request.values():
            if any(not self._is_config_skip_log(log) for log in group_logs):
                request_groups.append(group_logs)
        request_groups = request_groups[:limit]

        request_count = len(request_groups)
        success_count = 0
        fallback_success_count = 0
        first_choice_success = 0
        cooldown_skip_count = 0
        busy_switch_count = 0
        prompt_tokens = 0
        cached_tokens = 0
        first_chunk_durations: List[int] = []
        done_durations: List[int] = []
        member_risk: Dict[str, Dict[str, Any]] = {}

        for group_logs in request_groups:
            non_skip = [log for log in group_logs if not self._is_config_skip_log(log)]
            success_logs = [log for log in non_skip if str(log.status).startswith("2") and log.event in {"ok", "stream_ok", "stream_done", "retry_ok"}]
            final_success = next((log for log in non_skip if str(log.status).startswith("2") and log.event in {"stream_done", "ok", "retry_ok"}), None) or (success_logs[-1] if success_logs else None)
            if final_success:
                success_count += 1
                prompt_tokens += int(final_success.prompt_tokens or 0)
                cached_tokens += int(final_success.cached_tokens or 0)
                if int(final_success.attempt or 1) <= 1 and int(final_success.fallback_index or 0) <= 0:
                    first_choice_success += 1
            first_stream = next((log for log in non_skip if log.event == "stream_ok" and int(log.duration_ms or 0) > 0), None)
            if first_stream:
                first_chunk_durations.append(int(first_stream.duration_ms or 0))
            done = next((log for log in non_skip if log.event == "stream_done" and int(log.duration_ms or 0) > 0), None)
            if done:
                done_durations.append(int(done.duration_ms or 0))

            had_prior_runtime_issue = False
            for log in group_logs:
                detail = self._log_detail_dict(log.detail)
                reason = detail.get("skip_reason") or detail.get("fallback_reason") or detail.get("cooldown_reason") or ""
                event_blob = f"{log.event};{log.detail};{reason}"
                if reason in {"member_cooling", "underlying_model_cooling"}:
                    cooldown_skip_count += 1
                if "candidate_busy" in event_blob or "large_task_in_progress" in event_blob or log.event == "waf_lock_timeout":
                    busy_switch_count += 1
                    had_prior_runtime_issue = True
                if log.event in {"fallback", "cooldown", "network", "stream_timeout", "stream_idle_timeout"} or log.failure_scope in {"upstream", "candidate", "busy", "local_lock"}:
                    had_prior_runtime_issue = True
                if log.aggregate_member_id:
                    risk = member_risk.setdefault(log.aggregate_member_id, {
                        "member_id": log.aggregate_member_id,
                        "model": log.selected_model or log.model,
                        "timeout_count": 0,
                        "waf_blocked_count": 0,
                        "failure_count": 0,
                        "last_error": "",
                    })
                    if "timeout" in event_blob:
                        risk["timeout_count"] += 1
                    if "waf" in event_blob.lower():
                        risk["waf_blocked_count"] += 1
                    if not str(log.status).startswith("2") and not self._is_config_skip_log(log):
                        risk["failure_count"] += 1
                        risk["last_error"] = self.router._short_error(log.detail or log.status)
            if final_success and had_prior_runtime_issue:
                fallback_success_count += 1

        success_rate = (success_count / request_count) if request_count else None
        first_choice_success_rate = (first_choice_success / success_count) if success_count else None
        cache_hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens else None
        avg_first_chunk_ms = round(sum(first_chunk_durations) / len(first_chunk_durations)) if first_chunk_durations else None
        avg_done_ms = round(sum(done_durations) / len(done_durations)) if done_durations else None
        high_risk_members = [
            item for item in member_risk.values()
            if item.get("timeout_count") or item.get("waf_blocked_count") or item.get("failure_count", 0) >= 2
        ]
        high_risk_members.sort(key=lambda x: (x.get("timeout_count", 0) + x.get("waf_blocked_count", 0) + x.get("failure_count", 0)), reverse=True)

        return {
            "ok": True,
            "aggregate_id": aggregate.id,
            "aggregate_name": aggregate.name,
            "range": {"type": "last_n", "limit": limit},
            "request_count": request_count,
            "success_count": success_count,
            "success_rate": success_rate,
            "fallback_success_count": fallback_success_count,
            "first_choice_success_rate": first_choice_success_rate,
            "cooldown_skip_count": cooldown_skip_count,
            "busy_switch_count": busy_switch_count,
            "avg_first_chunk_ms": avg_first_chunk_ms,
            "avg_done_ms": avg_done_ms,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "cache_hit_rate": cache_hit_rate,
            "high_risk_members": high_risk_members[:5],
        }

    def _model_runtime_item(self, model: ModelConfig) -> Dict[str, Any]:
        now = int(time.time())
        if model.cooldown_until and model.cooldown_until > now:
            status = "cooling"
            reason = model.cooldown_reason or "模型正在健康冷却"
        elif model.usable is False and model.disabled_by_user:
            status = "manual_disabled"
            reason = "用户已停用该模型"
        elif model.usable is False:
            status = "unavailable"
            reason = model.last_error or "模型当前不可用"
        elif model.last_error:
            status = "warning"
            reason = model.last_error
        else:
            status = "healthy"
            reason = "模型可参与调度"
        return {
            "group_id": model.group_id,
            "model_id": model.id,
            "derived_status": status,
            "derived_reason": reason,
            "usable": model.usable,
            "disabled_by_user": model.disabled_by_user,
            "cooldown_until": model.cooldown_until,
            "cooldown_reason": model.cooldown_reason,
            "last_error": model.last_error,
            "last_success_at": getattr(model, "last_success_at", ""),
            "last_failure_at": getattr(model, "last_failure_at", ""),
        }

    def _member_runtime_item(self, member: AggregateMember) -> Dict[str, Any]:
        now = int(time.time())
        group = self.store.find_group(member.group_id)
        model = self.store.find_model(member.model_id)
        if member.enabled is False:
            status = "manual_disabled"
            reason = "用户已停用该聚合成员"
        elif member.cooldown_until and member.cooldown_until > now:
            status = "cooling"
            reason = member.cooldown_reason or "聚合成员因上游健康失败短期冷却"
        elif not group:
            status = "config_error"
            reason = "底层连接组缺失"
        elif not model:
            status = "config_error"
            reason = "底层真实模型缺失"
        elif model.usable is False and model.disabled_by_user:
            status = "underlying_model_disabled"
            reason = "底层真实模型已手动停用"
        elif model.cooldown_until and model.cooldown_until > now:
            status = "underlying_model_cooling"
            reason = model.cooldown_reason or "底层真实模型正在冷却"
        elif member.last_error:
            status = "warning"
            reason = member.last_error
        else:
            status = "healthy"
            reason = "成员可参与聚合调度"
        return {
            "aggregate_id": member.aggregate_id,
            "member_id": member.id,
            "derived_status": status,
            "derived_reason": reason,
            "enabled": member.enabled,
            "cooldown_until": member.cooldown_until,
            "cooldown_reason": member.cooldown_reason,
            "last_error": member.last_error,
            "last_success_at": getattr(member, "last_success_at", ""),
            "last_failure_at": getattr(member, "last_failure_at", ""),
            "underlying_model_status": self._model_runtime_item(model)["derived_status"] if model else "missing",
        }

    def _filtered_recent_logs(self, include_skip: bool = False) -> List[RequestLog]:
        logs = self.router.recent_logs()
        if include_skip:
            return logs
        return [log for log in logs if not self._is_config_skip_log(log)]

    def _runtime_state_payload(self, include_skip: bool = False) -> Dict[str, Any]:
        self.store.refresh_expired_cooldowns()
        return {
            "ok": True,
            "models": [self._model_runtime_item(model) for model in self.store.models],
            "aggregate_members": [self._member_runtime_item(member) for member in self.store.aggregate_members],
            "logs": self._filtered_recent_logs(include_skip=include_skip),
            "log_write_error": self.router.log_write_error,
        }

    def _aggregate_member_chain_item(self, member: AggregateMember) -> Dict[str, Any]:
        group = self.store.find_group(member.group_id)
        model = self.store.find_model(member.model_id)
        runtime = self._member_runtime_item(member)
        return {
            "member_id": member.id,
            "aggregate_id": member.aggregate_id,
            "priority": member.priority,
            "group_id": member.group_id,
            "group_name": group.name if group else "未知连接组",
            "model_id": member.model_id,
            "model_name": model.name if model else "未知模型",
            "enabled": member.enabled,
            "cooldown_until": member.cooldown_until,
            "cooldown_reason": member.cooldown_reason,
            "derived_status": runtime["derived_status"],
            "derived_reason": runtime["derived_reason"],
        }

    def _candidate_chain_for_members(self, members: List[AggregateMember]) -> List[Dict[str, Any]]:
        return [self._aggregate_member_chain_item(member) for member in sorted(members, key=lambda m: m.priority)]

    def _aggregate_member_sort_preview(self, member_id: str, direction: str) -> Dict[str, Any]:
        member = self.store.find_aggregate_member(member_id)
        if not member:
            return {"ok": False, "message": "成员不存在", "can_apply": False}
        direction = str(direction or "").strip()
        if direction not in {"up", "down", "top", "bottom"}:
            return {"ok": False, "message": "排序方向无效", "can_apply": False}
        siblings = sorted(self.store.get_aggregate_members(member.aggregate_id), key=lambda m: m.priority)
        idx = next((i for i, item in enumerate(siblings) if item.id == member_id), -1)
        if idx < 0:
            return {"ok": False, "message": "成员不存在", "can_apply": False}
        target_idx = {"up": idx - 1, "down": idx + 1, "top": 0, "bottom": len(siblings) - 1}[direction]
        target_idx = max(0, min(target_idx, len(siblings) - 1))
        after = list(siblings)
        changed = target_idx != idx
        if changed:
            moved = after.pop(idx)
            after.insert(target_idx, moved)
        after_chain = []
        for order, item in enumerate(after, start=1):
            chain_item = self._aggregate_member_chain_item(item)
            chain_item["priority"] = order
            after_chain.append(chain_item)
        aggregate = self.store.find_aggregate(member.aggregate_id)
        return {
            "ok": True,
            "can_apply": True,
            "changed": changed,
            "direction": direction,
            "aggregate_id": member.aggregate_id,
            "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
            "member_id": member.id,
            "candidate_chain_before": self._candidate_chain_for_members(siblings),
            "candidate_chain_after": after_chain,
        }

    def _aggregate_member_clear_cooldown_preview(self, member_id: str) -> Dict[str, Any]:
        member = self.store.find_aggregate_member(member_id)
        if not member:
            return {"ok": False, "message": "成员不存在", "can_apply": False}
        before_chain = self._candidate_chain_for_members(self.store.get_aggregate_members(member.aggregate_id))
        after_member = AggregateMember.from_dict(asdict(member))
        after_member.enabled = True
        after_member.cooldown_until = 0
        after_member.cooldown_reason = ""
        after_member.last_error = ""
        after_members = []
        for item in self.store.get_aggregate_members(member.aggregate_id):
            after_members.append(after_member if item.id == member_id else item)
        aggregate = self.store.find_aggregate(member.aggregate_id)
        return {
            "ok": True,
            "can_apply": True,
            "changed": bool((not member.enabled) or member.cooldown_until or member.cooldown_reason or member.last_error),
            "aggregate_id": member.aggregate_id,
            "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
            "member_id": member.id,
            "cooldown_before": {
                "enabled": member.enabled,
                "cooldown_until": member.cooldown_until,
                "cooldown_reason": member.cooldown_reason,
                "last_error": member.last_error,
            },
            "cooldown_after": {
                "enabled": True,
                "cooldown_until": 0,
                "cooldown_reason": "",
                "last_error": "",
            },
            "candidate_chain_before": before_chain,
            "candidate_chain_after": self._candidate_chain_for_members(after_members),
        }

    def _group_delete_preview(self, group_id: str) -> Dict[str, Any]:
        group = self.store.find_group(group_id)
        if not group:
            return {"ok": False, "message": "连接组不存在", "can_delete": False}
        models = [m for m in self.store.models if m.group_id == group_id]
        model_ids = {m.id for m in models}
        affected_members = []
        for member in self.store.aggregate_members:
            if member.group_id == group_id or member.model_id in model_ids:
                aggregate = self.store.find_aggregate(member.aggregate_id)
                model = self.store.find_model(member.model_id)
                affected_members.append({
                    "aggregate_id": member.aggregate_id,
                    "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
                    "member_id": member.id,
                    "model": model.name if model else member.model_id,
                })
        warnings = []
        aggregate_counts: Dict[str, int] = {}
        for item in affected_members:
            aggregate_counts[item["aggregate_name"]] = aggregate_counts.get(item["aggregate_name"], 0) + 1
        for name, count in aggregate_counts.items():
            warnings.append(f"删除后聚合模型 {name} 将失去 {count} 个成员")
        return {
            "ok": True,
            "group_id": group.id,
            "group_name": group.name,
            "can_delete": True,
            "affected_models": len(models),
            "affected_model_names": [m.name for m in models],
            "affected_aggregate_members": affected_members,
            "warnings": warnings,
            "reversible": False,
        }

    def _model_delete_preview(self, model_id: str) -> Dict[str, Any]:
        model = self.store.find_model(model_id)
        if not model:
            return {"ok": False, "message": "模型不存在", "can_delete": False}
        group = self.store.find_group(model.group_id)
        affected_members = []
        for member in self.store.aggregate_members:
            if member.model_id == model_id:
                aggregate = self.store.find_aggregate(member.aggregate_id)
                affected_members.append({
                    "aggregate_id": member.aggregate_id,
                    "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
                    "member_id": member.id,
                    "model": model.name,
                })
        warnings = [f"聚合模型 {item['aggregate_name']} 依赖该模型，删除后对应成员会失效" for item in affected_members]
        return {
            "ok": True,
            "model_id": model.id,
            "model_name": model.name,
            "group_id": model.group_id,
            "group_name": group.name if group else "未知连接组",
            "can_delete": True,
            "affected_aggregate_members": affected_members,
            "warnings": warnings,
            "reversible": False,
        }

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(render_index_page(), content_type="text/html; charset=utf-8")
            return
        if parsed.path.startswith("/") and not parsed.path.startswith("/api/") and not parsed.path.startswith("/v1/"):
            # 服务静态资源（css/js/html 等），统一映射到 static/ 目录
            rel = parsed.path.lstrip("/")
            if ".." in rel:
                self._send_json({"error": {"message": "禁止访问", "type": "invalid_request_error", "code": "forbidden"}}, status=403)
                return
            file_path = get_platform().get_resource_path("static", *rel.split("/"))
            self._send_file(file_path)
            return
        if parsed.path in {"/v1/models", "/models"}:
            ctx = self._require_route_context()
            if not ctx:
                return
            try:
                self._send_model_list(ctx)
            except Exception as err:
                router = getattr(self, 'router', None)
                error_msg = f"local model list failed; error={str(err)}"
                if router and hasattr(router, '_short_error'):
                    error_msg = f"local model list failed; error={router._short_error(str(err))}"
                if router and hasattr(router, 'add_log'):
                    router.add_log(
                        "/v1/models",
                        "lin-router",
                        "500",
                        error_msg,
                        0,
                        event="models_failed",
                    )
                self._send_json({
                    "object": "list",
                    "data": [{
                        "id": DEFAULT_AUTO_MODEL_NAME,
                        "object": "model",
                        "created": 0,
                        "owned_by": "lin-router",
                        "root": DEFAULT_AUTO_MODEL_NAME,
                        "parent": None,
                    }],
                })
            return
        if parsed.path == "/api/state":
            self.store.refresh_expired_cooldowns()
            settings = self.server.settings_store.to_dict()  # type: ignore[attr-defined]
            self._send_json({
                "config_file": str(self.store.path),
                "auto_model_name": DEFAULT_AUTO_MODEL_NAME,
                "settings": {
                    **settings,
                    # 开机自启以注册表真实状态为准
                    "auto_start": get_platform().is_autostart_enabled(),
                },
                "group_meta": {
                    group.id: {
                        "auto_model_name": self.router.group_auto_model_name(group),
                        "model_count": len([m for m in self.store.models if m.group_id == group.id]),
                        "usable_count": len([m for m in self.store.models if m.group_id == group.id and m.usable]),
                    }
                    for group in self.store.groups
                },
                "groups": [asdict(g) for g in self.store.groups],
                "models": [asdict(m) for m in self.store.models],
                "aggregate_models": [asdict(m) for m in self.store.aggregate_models],
                "aggregate_members": [asdict(m) for m in self.store.aggregate_members],
                "logs": self.router.recent_logs(),
                "log_file": str(self.router.log_file),
                "log_write_error": self.router.log_write_error,
            })
            return
        if parsed.path == "/api/runtime-state":
            params = parse_qs(parsed.query)
            include_skip = str((params.get("include_skip") or params.get("debug") or [""])[0] or "").lower() in {"1", "true", "yes", "on"}
            payload = self._runtime_state_payload(include_skip=include_skip)
            payload["live_requests"] = self.router.live_requests_payload().get("requests", [])
            self._send_json(payload)
            return
        if parsed.path == "/api/live-requests":
            self._send_json(self.router.live_requests_payload())
            return
        if parsed.path.startswith("/api/diagnose/"):
            request_id = parsed.path.split("/api/diagnose/", 1)[1].strip("/")
            payload = self.router.diagnose_request(request_id)
            self._send_json(payload, status=200 if payload.get("ok") else 404)
            return
        if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/stats"):
            parts = parsed.path.split("/")
            aggregate_id = parts[3] if len(parts) >= 4 else ""
            params = parse_qs(parsed.query)
            limit = int((params.get("limit") or [100])[0] or 100)
            payload = self._aggregate_stats_payload(aggregate_id, limit)
            if not payload.get("ok"):
                self._send_json(payload, status=404)
            else:
                self._send_json(payload)
            return
        if parsed.path.startswith("/api/client-config/"):
            group_id = parsed.path.split("/", 3)[3]
            group = self.store.find_group(group_id)
            if not group:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
                return
            self._send_json({
                "base_url": self._client_base_url(),
                "api_key": group.route_key,
                "model": DEFAULT_AUTO_MODEL_NAME,
                "group_id": group.id,
                "group_name": group.name,
            })
            return
        if parsed.path == "/api/settings":
            # 返回当前用户设置（开机自启、启动最小化等）
            self._send_json(self.server.settings_store.to_dict())
            return
        if parsed.path == "/api/debug/capture":
            capture = self.router.debug_capture.load_capture()
            if capture is None:
                self._send_json({"ok": True, "exists": False})
                return
            # 返回快照摘要，不暴露完整 body_base64，避免前端意外泄露长内容
            summary = {k: v for k, v in capture.items() if k != "body_base64"}
            summary["exists"] = True
            summary["has_body"] = bool(capture.get("body_base64"))
            self._send_json({"ok": True, "capture": summary})
            return
        if parsed.path == "/api/logs" or parsed.path == "/api/logs/":
            params = parse_qs(parsed.query)

            def _first(values, default=""):
                return values[0] if values else default

            logs = list(reversed(self.router.logs))
            limit = int(_first(params.get("limit"), "0") or 0)
            offset = int(_first(params.get("offset"), "0") or 0)
            group_filter = _first(params.get("group"))
            status_filter = _first(params.get("status"))
            event_filter = _first(params.get("event"))
            include_skip = str(_first(params.get("include_skip")) or _first(params.get("debug")) or "").lower() in {"1", "true", "yes", "on"}
            aggregate_filter = _first(params.get("aggregate"))
            start_str = _first(params.get("start"))
            end_str = _first(params.get("end"))

            def _ts(s):
                try:
                    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return 0

            start_ts = _ts(start_str) if start_str else 0
            end_ts = _ts(end_str) if end_str else 0

            def _keep(item):
                if not include_skip and self._is_config_skip_log(item):
                    return False
                if group_filter and getattr(item, "group_id", "") != group_filter:
                    return False
                if aggregate_filter:
                    if getattr(item, "aggregate_id", "") != aggregate_filter and getattr(item, "aggregate_model", "") != aggregate_filter:
                        return False
                if event_filter and getattr(item, "event", "") != event_filter:
                    return False
                if status_filter:
                    status = str(getattr(item, "status", "") or "")
                    if status_filter == "2xx":
                        if not status.startswith("2"):
                            return False
                    elif status_filter == "cooldown":
                        if getattr(item, "event", "") not in ("cooldown", "fallback", "retry_ok"):
                            return False
                    elif status_filter == "error":
                        event = getattr(item, "event", "")
                        if status.startswith("2") or event in ("cooldown", "fallback", "retry_ok"):
                            return False
                    elif status not in status_filter:
                        return False
                if start_ts or end_ts:
                    t = _ts(getattr(item, "time", ""))
                    if start_ts and t < start_ts:
                        return False
                    if end_ts and t > end_ts:
                        return False
                return True

            filtered = [item for item in logs if _keep(item)]
            total = len(filtered)
            if offset:
                filtered = filtered[offset:]
            if limit and limit > 0:
                filtered = filtered[:limit]
            self._send_json({"ok": True, "total": total, "offset": offset, "limit": limit, "logs": [asdict(item) for item in filtered]})
            return
        if parsed.path == "/api/logs/export":
            csv_text = self.router.export_logs_csv()
            self._send_text(csv_text, content_type="text/csv; charset=utf-8")
            return
        if parsed.path == "/api/aggregates":
            self._send_json({
                "ok": True,
                "aggregate_models": [asdict(m) for m in self.store.aggregate_models],
                "aggregate_members": [asdict(m) for m in self.store.aggregate_members],
            })
            return
        if parsed.path == "/api/logs/all":
            self._send_json([asdict(item) for item in self.router.all_logs()])
            return
        if parsed.path == "/api/config/export":
            # 导出当前配置（groups + models + aggregates），用于备份和迁移
            payload = {
                "groups": [asdict(g) for g in self.store.groups],
                "models": [asdict(m) for m in self.store.models],
                "aggregate_models": [asdict(m) for m in self.store.aggregate_models],
                "aggregate_members": [asdict(m) for m in self.store.aggregate_members],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="lin-router-config-export.json"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/backup/export":
            # 导出全部数据：配置 + 设置
            settings_store = self.server.settings_store  # type: ignore[attr-defined]
            payload = {
                "groups": [asdict(g) for g in self.store.groups],
                "models": [asdict(m) for m in self.store.models],
                "aggregate_models": [asdict(m) for m in self.store.aggregate_models],
                "aggregate_members": [asdict(m) for m in self.store.aggregate_members],
                "settings": settings_store.to_dict(),
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="lin-router-backup.json"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/health":
            self._send_json({
                "ok": True,
                "groups": len(self.store.groups),
                "models": len(self.store.models),
                "aggregate_models": len(self.store.aggregate_models),
                "aggregate_members": len(self.store.aggregate_members),
            })
            return
        self._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config/import":
            # 导入配置：合并模式，按 id 覆盖同名连接组/模型，其余保留
            # 优先尝试 multipart/form-data 文件上传，失败回退到 JSON body
            payload = self._read_multipart_json()
            if payload is None:
                try:
                    payload = self._read_json()
                except Exception as e:
                    self._send_json({"error": {"message": f"配置文件无效：{e}", "type": "invalid_request_error", "code": "invalid_config_file"}}, status=400)
                return
            if not isinstance(payload, dict):
                self._send_json({"error": {"message": "配置文件无效：必须是一个 JSON 对象", "type": "invalid_request_error", "code": "invalid_config_file"}}, status=400)
                return
            groups_raw = payload.get("groups") or []
            models_raw = payload.get("models") or []
            aggregates_raw = payload.get("aggregate_models") or []
            members_raw = payload.get("aggregate_members") or []
            if not isinstance(groups_raw, list) or not isinstance(models_raw, list):
                self._send_json({"error": {"message": "请求参数无效：groups 和 models 必须是数组", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
                return
            if not isinstance(aggregates_raw, list):
                aggregates_raw = []
            if not isinstance(members_raw, list):
                members_raw = []
            with self.store._lock:
                for item in groups_raw:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    group = ConnectionGroup.from_dict(item)
                    if not group.route_key:
                        group.route_key = new_route_key()
                    if not group.provider_type:
                        group.provider_type = PROVIDER_ARK
                    # 按 id 覆盖，否则追加
                    for idx, existing in enumerate(self.store.groups):
                        if existing.id == group.id:
                            self.store.groups[idx] = group
                            break
                    else:
                        self.store.groups.append(group)
                for item in models_raw:
                    if not isinstance(item, dict) or not item.get("name") or not item.get("ep_id"):
                        continue
                    model = ModelConfig.from_dict(item)
                    # 缺失 group_id 时挂到第一个组，避免悬空
                    if not model.group_id or not self.store.find_group(model.group_id):
                        if self.store.groups:
                            model.group_id = self.store.groups[0].id
                        else:
                            continue
                    for idx, existing in enumerate(self.store.models):
                        if existing.id == model.id:
                            self.store.models[idx] = model
                            break
                    else:
                        self.store.models.append(model)
                # 导入聚合模型：按 id 覆盖，name 校验不通过则跳过
                imported_aggregate_ids: set = set()
                for item in aggregates_raw:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    aggregate = AggregateModel.from_dict(item)
                    ok, _ = self.store._validate_aggregate_name(aggregate.name, aggregate.id)
                    if not ok:
                        continue
                    for idx, existing in enumerate(self.store.aggregate_models):
                        if existing.id == aggregate.id:
                            self.store.aggregate_models[idx] = aggregate
                            break
                    else:
                        self.store.aggregate_models.append(aggregate)
                    imported_aggregate_ids.add(aggregate.id)
                # 导入聚合成员：按 id 覆盖，orphan/重复则跳过
                imported_member_ids: set = set()
                for item in members_raw:
                    if not isinstance(item, dict):
                        continue
                    member = AggregateMember.from_dict(item)
                    if member.aggregate_id not in imported_aggregate_ids and not self.store.find_aggregate(member.aggregate_id):
                        continue
                    if not self.store.find_group(member.group_id) or not self.store.find_model(member.model_id):
                        continue
                    duplicate = next(
                        (m for m in self.store.aggregate_members
                         if m.aggregate_id == member.aggregate_id
                         and m.group_id == member.group_id
                         and m.model_id == member.model_id
                         and m.id != member.id),
                        None,
                    )
                    if duplicate:
                        continue
                    for idx, existing in enumerate(self.store.aggregate_members):
                        if existing.id == member.id:
                            self.store.aggregate_members[idx] = member
                            break
                    else:
                        self.store.aggregate_members.append(member)
                    imported_member_ids.add(member.id)
                self.store._cleanup_orphan_members()
                self.store.save()
            self._send_json({
                "ok": True,
                "groups": len(self.store.groups),
                "models": len(self.store.models),
                "aggregate_models": len(self.store.aggregate_models),
                "aggregate_members": len(self.store.aggregate_members),
            })
            return
        if parsed.path == "/api/backup/import":
            # 恢复全部数据：配置 + 设置，完全覆盖当前数据
            payload = self._read_multipart_json()
            if payload is None:
                try:
                    payload = self._read_json()
                except Exception as e:
                    self._send_json({"error": {"message": f"备份文件无效：{e}", "type": "invalid_request_error", "code": "invalid_backup_file"}}, status=400)
                return
            if not isinstance(payload, dict):
                self._send_json({"error": {"message": "备份文件无效：必须是一个 JSON 对象", "type": "invalid_request_error", "code": "invalid_backup_file"}}, status=400)
                return
            groups_raw = payload.get("groups") or []
            models_raw = payload.get("models") or []
            aggregates_raw = payload.get("aggregate_models") or []
            members_raw = payload.get("aggregate_members") or []
            settings_raw = payload.get("settings") or {}
            if not isinstance(groups_raw, list) or not isinstance(models_raw, list):
                self._send_json({"error": {"message": "请求参数无效：groups 和 models 必须是数组", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
                return
            if not isinstance(aggregates_raw, list):
                aggregates_raw = []
            if not isinstance(members_raw, list):
                members_raw = []
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
                # 缺失或无效 group_id 时挂到第一个组
                if not model.group_id or not any(g.id == model.group_id for g in new_groups):
                    if new_groups:
                        model.group_id = new_groups[0].id
                    else:
                        continue
                new_models.append(model)
            new_aggregates: List[AggregateModel] = []
            new_aggregate_ids = set()
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
                if not any(g.id == member.group_id for g in new_groups) or not any(m.id == member.model_id for m in new_models):
                    continue
                new_members.append(member)
            with self.store._lock:
                self.store.groups = new_groups
                self.store.models = new_models
                self.store.aggregate_models = new_aggregates
                self.store.aggregate_members = new_members
                self.store.save()
            # 恢复设置
            settings_store = self.server.settings_store  # type: ignore[attr-defined]
            allowed = {
                "auto_start", "start_minimized", "theme", "auto_refresh_logs",
                "upstream_http_client", "upstream_http2", "upstream_keepalive",
                "debug_mode", "debug_capture_enabled", "debug_capture_last_body",
                "normalize_tools_order",
            }
            new_settings = {k: v for k, v in settings_raw.items() if k in allowed}
            if "auto_start" in new_settings:
                get_platform().set_autostart(bool(new_settings["auto_start"]))
            updated = settings_store.update(new_settings)
            # 恢复设置后若影响上游客户端，立即刷新
            if any(k in new_settings for k in ("upstream_http_client", "upstream_http2", "upstream_keepalive")):
                self.router._refresh_upstream_client()
            self._send_json({
                "ok": True,
                "groups": len(self.store.groups),
                "models": len(self.store.models),
                "aggregate_models": len(self.store.aggregate_models),
                "aggregate_members": len(self.store.aggregate_members),
                "settings": {**updated, "auto_start": get_platform().is_autostart_enabled()},
            })
            return
        if parsed.path == "/api/groups":
            payload = self._read_json()
            if not payload.get("name"):
                self._send_json({"error": {"message": "缺少连接组名称", "type": "invalid_request_error", "code": "missing_group_name"}}, status=400)
                return
            existing = self.store.find_group(str(payload.get("id") or ""))
            if existing and not payload.get("route_key"):
                payload["route_key"] = existing.route_key
            if not payload.get("provider_type"):
                payload["provider_type"] = existing.provider_type if existing else PROVIDER_ARK
            if existing and "ark_api_key" not in payload:
                payload["ark_api_key"] = existing.ark_api_key
            if existing and "api_key" not in payload:
                payload["api_key"] = existing.api_key
            if existing and "auto_model_cooldown_minutes" not in payload:
                payload["auto_model_cooldown_minutes"] = existing.auto_model_cooldown_minutes
            if existing and "stream_idle_timeout" not in payload:
                payload["stream_idle_timeout"] = existing.stream_idle_timeout
            if existing and "waf_compatible" not in payload:
                payload["waf_compatible"] = existing.waf_compatible
            if existing and "waf_accept_policy" not in payload:
                payload["waf_accept_policy"] = existing.waf_accept_policy
            if existing and "auto_model_name" not in payload:
                payload["auto_model_name"] = existing.auto_model_name
            # 自动路由模型名空值按默认值处理
            auto_name = str(payload.get("auto_model_name") or "").strip() or DEFAULT_AUTO_MODEL_NAME
            # 不允许与同组模型 name/id/ep_id 冲突；仅 all-router-auto 为全局保留名
            group_id_for_check = str(payload.get("id") or existing.id if existing else "").strip()
            conflict_model = next((m for m in self.store.models if m.group_id == group_id_for_check and auto_name in {m.id, m.name, m.ep_id}), None)
            if conflict_model or auto_name == "all-router-auto":
                self._send_json({"ok": False, "message": f"自动路由模型名 '{auto_name}' 与已有模型或保留名称冲突"}, status=400)
                return
            group = ConnectionGroup.from_dict(payload)
            if group.provider_type == PROVIDER_PROXY and not group.api_key and group.ark_api_key:
                group.api_key = group.ark_api_key
            if group.provider_type == PROVIDER_RELAY:
                group.ark_api_key = ""
                group.api_key = ""
            if group.provider_type == PROVIDER_ARK:
                group.api_key = ""
            self.store.upsert_group(group)
            self._send_json({"ok": True, "group": asdict(group)})
            return
        if parsed.path.startswith("/api/groups/") and parsed.path.endswith("/clone"):
            group_id = parsed.path.split("/")[3]
            cloned = self._clone_group(group_id)
            if not cloned:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
                return
            self._send_json({"ok": True, **cloned})
            return
        if parsed.path == "/api/models":
            payload = self._read_json()
            if not payload.get("name") or not payload.get("ep_id") or not payload.get("group_id"):
                self._send_json({"error": {"message": "缺少必填字段", "type": "invalid_request_error", "code": "missing_required_fields"}}, status=400)
                return
            group = self.store.find_group(str(payload["group_id"]))
            if not group:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=400)
                return
            existing = self.store.find_model(str(payload.get("id") or ""))
            merged: Dict[str, Any] = asdict(existing) if existing else {}
            merged.update(payload)
            model = ModelConfig.from_dict(merged)
            if existing:
                model.usable = bool(merged.get("usable", existing.usable))
                model.last_error = str(merged.get("last_error", existing.last_error))
                model.last_success_at = str(merged.get("last_success_at", existing.last_success_at))
                model.last_checked_at = str(merged.get("last_checked_at", existing.last_checked_at))
            if group.provider_type != PROVIDER_RELAY:
                model.api_key = ""
                model.price_group = ""
            if group.provider_type in {PROVIDER_RELAY, PROVIDER_PROXY} and not model.upstream_model:
                model.upstream_model = model.ep_id
            if group.provider_type not in {PROVIDER_RELAY, PROVIDER_PROXY}:
                model.upstream_model = ""
            # 模型名/ep_id 不得与所属连接组的自动路由模型名冲突，避免路由歧义
            auto_name = self.router.group_auto_model_name(group)
            if auto_name in {model.name, model.ep_id}:
                self._send_json({"ok": False, "message": f"模型名/ep_id 与连接组自动路由模型名 '{auto_name}' 冲突"}, status=400)
                return
            self.store.upsert_model(model)
            if group.provider_type in {PROVIDER_RELAY, PROVIDER_PROXY}:
                group.upstream_models = []
                group.upstream_models_fetched_at = ""
                self.store.upsert_group(group)
            self._send_json({"ok": True, "model": asdict(model)})
            return
        if parsed.path == "/api/models/batch":
            payload = self._read_json()
            group_id = str(payload.get("group_id") or "")
            group = self.store.find_group(group_id)
            if not group_id or not group:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=400)
                return
            raw_text = str(payload.get("text") or "")
            fmt = str(payload.get("format") or "lines").strip().lower()
            defaults = payload.get("defaults") or {}
            preview = bool(payload.get("preview", False))

            def _parse_batch_items(text: str, fmt: str) -> List[Dict[str, Any]]:
                items: List[Dict[str, Any]] = []
                if fmt == "json":
                    arr = json.loads(text)
                    if not isinstance(arr, list):
                        raise ValueError("JSON 格式必须是数组")
                    for idx, entry in enumerate(arr, start=1):
                        if isinstance(entry, dict):
                            copied = dict(entry)
                            copied["line"] = idx
                            items.append(copied)
                        elif isinstance(entry, str):
                            items.append({"ep_id": entry.strip(), "line": idx})
                        else:
                            items.append({"ep_id": "", "line": idx, "parse_error": "JSON 数组项必须是对象或字符串"})
                elif fmt == "models_response":
                    obj = json.loads(text)
                    data = obj.get("data") if isinstance(obj, dict) else None
                    if not isinstance(data, list):
                        raise ValueError("/v1/models 响应必须包含 data 数组")
                    for idx, entry in enumerate(data, start=1):
                        if isinstance(entry, dict):
                            items.append({"ep_id": str(entry.get("id") or "").strip(), "line": idx})
                        elif isinstance(entry, str):
                            items.append({"ep_id": entry.strip(), "line": idx})
                        else:
                            items.append({"ep_id": "", "line": idx, "parse_error": "data 项必须是对象或字符串"})
                else:
                    # lines 格式：每行一个模型名，空行跳过但保留原始行号
                    for idx, line in enumerate(text.splitlines(), start=1):
                        ep = line.strip()
                        if ep:
                            items.append({"ep_id": ep, "line": idx})
                return items

            try:
                raw_items = _parse_batch_items(raw_text, fmt)
            except Exception as err:
                self._send_json({"ok": False, "message": f"解析失败：{err}"}, status=400)
                return

            existing_ep_ids = {m.ep_id for m in self.store.models if m.group_id == group_id}
            existing_names = {m.name for m in self.store.models if m.group_id == group_id}
            is_relay = group.provider_type == PROVIDER_RELAY
            is_proxy = group.provider_type == PROVIDER_PROXY
            need_upstream = is_relay or is_proxy

            processed: List[Dict[str, Any]] = []
            seen_ep_ids: set[str] = set()
            seen_names: set[str] = set()
            name_re = re.compile(r"^[^\s,;]+$")
            for item in raw_items:
                line_no = int(item.get("line") or 0)
                ep_id = str(item.get("ep_id") or item.get("upstream_model") or "").strip()
                name = str(item.get("name") or "").strip() or ep_id
                upstream_model = str(item.get("upstream_model") or "").strip() or ep_id
                # 单个模型字段 > 批量统一字段 > 默认值
                api_key = str(item.get("api_key") if item.get("api_key") is not None else defaults.get("api_key") or "").strip()
                price_group = str(item.get("price_group") if item.get("price_group") is not None else defaults.get("price_group") or "").strip()
                usable = item.get("usable") if isinstance(item.get("usable"), bool) else bool(defaults.get("usable", True))
                price_input = float(item.get("price_input") if item.get("price_input") is not None else defaults.get("price_input") or 0)
                price_output = float(item.get("price_output") if item.get("price_output") is not None else defaults.get("price_output") or 0)

                status = "new"
                reason = "将新增"
                if item.get("parse_error"):
                    status = "invalid"
                    reason = str(item.get("parse_error"))
                elif not ep_id:
                    status = "invalid"
                    reason = "模型名为空"
                elif not name_re.match(ep_id) or not name_re.match(name):
                    status = "invalid"
                    reason = "模型名不能包含空白、逗号或分号"
                elif ep_id in existing_ep_ids or name in existing_names:
                    status = "duplicate"
                    reason = "已存在同名模型，默认跳过"
                elif ep_id in seen_ep_ids or name in seen_names:
                    status = "duplicate"
                    reason = "本次导入列表中重复，默认跳过"
                elif need_upstream and not upstream_model:
                    status = "invalid"
                    reason = "缺少上游模型名"

                if ep_id:
                    seen_ep_ids.add(ep_id)
                if name:
                    seen_names.add(name)
                processed.append({
                    "line": line_no,
                    "name": name,
                    "ep_id": ep_id,
                    "upstream_model": upstream_model if need_upstream else "",
                    "api_key": api_key if is_relay else "",
                    "has_api_key": bool(api_key) if is_relay else False,
                    "price_group": price_group if is_relay else "",
                    "price_input": price_input,
                    "price_output": price_output,
                    "usable": usable,
                    "status": status,
                    "reason": reason,
                })

            total = len(processed)
            new_count = sum(1 for p in processed if p["status"] == "new")
            duplicate_count = sum(1 for p in processed if p["status"] == "duplicate")
            invalid_count = sum(1 for p in processed if p["status"] == "invalid")

            if preview:
                self._send_json({
                    "ok": True,
                    "preview": True,
                    "summary": {
                        "total": total,
                        "new": new_count,
                        "duplicate": duplicate_count,
                        "invalid": invalid_count,
                    },
                    "items": processed,
                })
                return

            if invalid_count > 0:
                self._send_json({"ok": False, "message": f"存在 {invalid_count} 条无效记录，请修正后再导入"}, status=400)
                return

            added = 0
            skipped = 0
            for p in processed:
                if p["status"] == "duplicate":
                    skipped += 1
                    continue
                self.store.upsert_model(ModelConfig(
                    id=uuid.uuid4().hex,
                    name=p["name"],
                    ep_id=p["ep_id"],
                    group_id=group_id,
                    upstream_model=p["upstream_model"],
                    api_key=p["api_key"],
                    price_group=p["price_group"],
                    price_input=p["price_input"],
                    price_output=p["price_output"],
                    usable=p["usable"],
                ))
                added += 1
            self._send_json({"ok": True, "added": added, "skipped": skipped})
            return
        if parsed.path == "/api/models/fetch-upstream":
            payload = self._read_json()
            group_id = str(payload.get("group_id") or "")
            group = self.store.find_group(group_id)
            if not group:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=400)
                return
            if group.provider_type not in {PROVIDER_RELAY, PROVIDER_PROXY}:
                self._send_json({"error": {"message": "仅 relay/proxy 连接组支持拉取上游模型", "type": "invalid_request_error", "code": "upstream_fetch_unsupported_provider"}}, status=400)
                return
            auth_key = self._effective_group_auth(group, payload)
            if not auth_key:
                self._send_json({"error": {"message": "缺少上游 API Key", "type": "invalid_request_error", "code": "missing_upstream_api_key"}}, status=400)
                return
            try:
                items = self._fetch_upstream_models(group, auth_key)
            except Exception as err:
                self._send_json({"error": {"message": f"拉取上游模型失败：{err}", "type": "api_error", "code": "upstream_fetch_failed"}}, status=500)
                return
            candidates: List[Dict[str, Any]] = []
            for item in items:
                ep_id = str(item.get("id") or "").strip()
                if not ep_id or ep_id == DEFAULT_AUTO_MODEL_NAME:
                    continue
                name = str(item.get("display_name") or item.get("name") or ep_id).strip()
                candidates.append({
                    "name": name or ep_id,
                    "ep_id": ep_id,
                    "root": str(item.get("root") or item.get("id") or ep_id).strip(),
                })
            group.upstream_models = candidates
            group.upstream_models_fetched_at = self.router._now()
            self.store.upsert_group(group)
            self._send_json({
                "ok": True,
                "count": len(candidates),
            })
            return
        if parsed.path.endswith("/toggle") and parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            model = self.store.find_model(model_id)
            if not model:
                self._send_json({"error": {"message": "模型不存在", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
                return
            if model.cooldown_until:
                # 恢复冷却视为用户手动启用
                model.usable = True
                model.disabled_by_user = False
                model.cooldown_until = 0
                model.cooldown_reason = ""
                model.last_error = ""
                model.last_checked_at = self.router._now()
            elif model.usable:
                # 当前可用 -> 用户手动禁用
                model.usable = False
                model.disabled_by_user = True
            else:
                # 当前不可用（用户禁用或冷却已过期） -> 用户手动启用
                model.usable = True
                model.disabled_by_user = False
                model.cooldown_until = 0
                model.cooldown_reason = ""
                model.last_error = ""
                model.last_checked_at = self.router._now()
            self.store.save()
            self._send_json({"ok": True, "usable": model.usable, "disabled_by_user": model.disabled_by_user})
            return
        if parsed.path.endswith("/usable") and parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            model = self.store.find_model(model_id)
            if not model:
                self._send_json({"error": {"message": "模型不存在", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
                return
            payload = self._read_json()
            usable = bool(payload.get("usable", True))
            model.usable = usable
            model.disabled_by_user = not usable
            if usable:
                model.cooldown_until = 0
                model.cooldown_reason = ""
                model.last_error = ""
            model.last_checked_at = self.router._now()
            self.store.save()
            self._send_json({"ok": True, "usable": model.usable, "disabled_by_user": model.disabled_by_user})
            return
        if parsed.path == "/api/models/usable/all":
            payload = self._read_json()
            usable = bool(payload.get("usable", True))
            changed = False
            with self.store._lock:
                for model in self.store.models:
                    if model.usable != usable:
                        model.usable = usable
                        changed = True
                    model.disabled_by_user = not usable
                    if usable:
                        model.cooldown_until = 0
                        model.cooldown_reason = ""
                        model.last_error = ""
                if changed:
                    self.store.save()
            self._send_json({"ok": True, "changed": changed})
            return
        if parsed.path.endswith("/toggle") and parsed.path.startswith("/api/groups/"):
            group_id = parsed.path.split("/")[3]
            changed = self.store.toggle_group(group_id)
            if not changed:
                self._send_json({"error": {"message": "连接组不存在或为空", "type": "invalid_request_error", "code": "group_not_found_or_empty"}}, status=400)
                return
            self._send_json({"ok": True})
            return
        if parsed.path.endswith("/usable") and parsed.path.startswith("/api/groups/"):
            group_id = parsed.path.split("/")[3]
            group = self.store.find_group(group_id)
            if not group:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
                return
            payload = self._read_json()
            usable = bool(payload.get("usable", True))
            changed = False
            with self.store._lock:
                for model in self.store.models:
                    if model.group_id != group_id:
                        continue
                    if model.usable != usable:
                        model.usable = usable
                        changed = True
                    model.disabled_by_user = not usable
                    if usable:
                        model.cooldown_until = 0
                        model.cooldown_reason = ""
                        model.last_error = ""
                if changed:
                    self.store.save()
            self._send_json({"ok": True, "changed": changed})
            return
        if parsed.path.endswith("/move") and parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            payload = self._read_json()
            moved = self.store.move_model(model_id, str(payload.get("direction", "")))
            if not moved:
                self._send_json({"error": {"message": "移动失败", "type": "invalid_request_error", "code": "move_failed"}}, status=400)
                return
            self._send_json({"ok": True})
            return
        if parsed.path.startswith("/api/groups/") and parsed.path.endswith("/delete-preview"):
            group_id = parsed.path.split("/")[3]
            payload = self._group_delete_preview(group_id)
            self._send_json(payload, status=200 if payload.get("ok") else 404)
            return
        if parsed.path.startswith("/api/models/") and parsed.path.endswith("/delete-preview"):
            model_id = parsed.path.split("/")[3]
            payload = self._model_delete_preview(model_id)
            self._send_json(payload, status=200 if payload.get("ok") else 404)
            return
        if parsed.path.startswith("/api/models/") and parsed.path.endswith("/recover"):
            model_id = parsed.path.split("/")[3]
            payload = self.router.recover_model(model_id)
            self._send_json(payload, status=200 if payload.get("ok") else 400)
            return
        if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/sort-preview"):
            member_id = parsed.path.split("/")[3]
            payload_in = self._read_json()
            payload = self._aggregate_member_sort_preview(member_id, str(payload_in.get("direction") or ""))
            self._send_json(payload, status=200 if payload.get("ok") else 404)
            return
        if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/clear-cooldown-preview"):
            member_id = parsed.path.split("/")[3]
            payload = self._aggregate_member_clear_cooldown_preview(member_id)
            self._send_json(payload, status=200 if payload.get("ok") else 404)
            return
        if parsed.path == "/api/reset":
            self.store.reset_usable()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/logs/clear":
            self.router.clear_logs()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/settings":
            # 更新用户设置，未知字段会被忽略
            raw = self._read_raw_body()
            payload = self._json_from_raw(raw)
            if not isinstance(payload, dict):
                self._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
                return
            allowed = {
                "auto_start", "start_minimized", "theme", "auto_refresh_logs",
                "upstream_http_client", "upstream_http2", "upstream_keepalive",
                "debug_mode", "debug_capture_enabled", "debug_capture_last_body",
                "normalize_tools_order",
            }
            new_settings = {k: v for k, v in payload.items() if k in allowed}
            # 开机自启需要同步到 Windows 注册表
            if "auto_start" in new_settings:
                get_platform().set_autostart(bool(new_settings["auto_start"]))
            settings_store = self.server.settings_store  # type: ignore[attr-defined]
            updated = settings_store.update(new_settings)
            # 上游客户端相关设置变更后，立即刷新客户端实例
            if any(k in new_settings for k in ("upstream_http_client", "upstream_http2", "upstream_keepalive")):
                self.router._refresh_upstream_client()
            self._send_json({
                **updated,
                "auto_start": get_platform().is_autostart_enabled(),
            })
            return
        # 聚合模型 CRUD（POST /api/aggregates、POST /api/aggregates/{id}/members）
        if parsed.path == "/api/aggregates":
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
                return
            name = str(payload.get("name") or "").strip()
            if not name:
                self._send_json({"ok": False, "message": "聚合模型名不能为空"}, status=400)
                return
            aggregate_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex
            existing = self.store.find_aggregate(aggregate_id)
            merged: Dict[str, Any] = asdict(existing) if existing else {}
            merged.update(payload)
            merged["id"] = aggregate_id
            merged["name"] = name
            aggregate = AggregateModel.from_dict(merged)
            ok, msg = self.store.upsert_aggregate(aggregate)
            if not ok:
                self._send_json({"ok": False, "message": msg}, status=400)
                return
            self._send_json({"ok": True, "aggregate_model": asdict(aggregate)})
            return
        if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/members"):
            parts = parsed.path.split("/")
            if len(parts) < 5:
                self._send_json({"error": {"message": "请求路径无效", "type": "invalid_request_error", "code": "invalid_path"}}, status=400)
                return
            aggregate_id = parts[3]
            payload = self._read_json()
            if not isinstance(payload, dict):
                self._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
                return
            if not self.store.find_aggregate(aggregate_id):
                self._send_json({"ok": False, "message": "聚合模型不存在"}, status=404)
                return
            member_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex
            existing_member = self.store.find_aggregate_member(member_id)
            # 更新时允许只传部分字段，group_id/model_id 从已有成员补全
            group_id = str(payload.get("group_id") or (existing_member.group_id if existing_member else "")).strip()
            model_id = str(payload.get("model_id") or (existing_member.model_id if existing_member else "")).strip()
            if not group_id or not model_id:
                self._send_json({"ok": False, "message": "连接组和模型不能为空"}, status=400)
                return
            member_merged: Dict[str, Any] = asdict(existing_member) if existing_member else {}
            member_merged.update(payload)
            member_merged["id"] = member_id
            member_merged["aggregate_id"] = aggregate_id
            member_merged["group_id"] = group_id
            member_merged["model_id"] = model_id
            member = AggregateMember.from_dict(member_merged)
            ok, msg = self.store.upsert_aggregate_member(member)
            if not ok:
                self._send_json({"ok": False, "message": msg}, status=400)
                return
            if bool(payload.get("clear_cooldown")):
                self.store.clear_aggregate_member_cooldown(member.id, self.router._now())
            # 支持在同一请求中调整排序（direction: up/down/top/bottom）
            direction = str(payload.get("direction") or "").strip()
            if direction:
                self.store.move_aggregate_member(member.id, direction)
            self._send_json({"ok": True, "member": asdict(self.store.find_aggregate_member(member.id) or member)})
            return
        if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/clear-cooldown"):
            parts = parsed.path.split("/")
            if len(parts) >= 5:
                member_id = parts[3]
                member = self.store.find_aggregate_member(member_id)
                if not member:
                    self._send_json({"error": {"message": "成员不存在", "type": "invalid_request_error", "code": "aggregate_member_not_found"}}, status=404)
                    return
                self.store.clear_aggregate_member_cooldown(member_id, self.router._now())
                self._send_json({"ok": True, "member": asdict(self.store.find_aggregate_member(member.id) or member)})
                return
        if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/recover"):
            member_id = parsed.path.split("/")[3]
            payload = self.router.recover_aggregate_member(member_id)
            self._send_json(payload, status=200 if payload.get("ok") else 400)
            return
        if parsed.path == "/api/test":
            ctx = self._require_route_context()
            if not ctx:
                return
            raw = self._read_raw_body()
            payload = self._json_from_raw(raw)
            path = str(payload.get("path", "/v1/chat/completions"))
            body = payload.get("body") or {"messages": [{"role": "user", "content": "ping"}]}
            try:
                status, headers, result = self.router.call(path, body, ctx, dict(self.headers.items()))
                self._send_json({"status": status, "headers": headers, "body": result.decode("utf-8", "ignore")})
            except AllModelsFailedError as err:
                self._send_all_models_failed_error(err)
            except Exception as err:
                self._send_json({
                    "error": {
                        "message": f"服务器内部错误: {err}",
                        "type": "internal_server_error",
                        "code": "internal_error",
                    }
                }, status=500)
            return
        if parsed.path == "/api/debug/replay":
            payload = self._read_json()
            count = int(payload.get("count", 10)) if isinstance(payload.get("count"), (int, float, str)) else 10
            client_type = str(payload.get("client", "")).lower() or None
            if client_type not in ("urllib", "httpx", None):
                client_type = None
            waf_off_variant = bool(payload.get("waf_off_variant", False))
            results = self.router.debug_capture.replay(count=count, client_type=client_type, waf_off_variant=waf_off_variant)
            self._send_json({"ok": True, "count": len(results), "results": results})
            return
        if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):
            ctx = self._require_route_context()
            if not ctx:
                return
            raw = self._read_raw_body()
            payload = self._json_from_raw(raw)
            stream = bool(payload.get("stream"))
            try:
                if stream:
                    status, headers, iterator, request_id = self.router.stream(parsed.path, payload, ctx, dict(self.headers.items()), raw)
                    self.send_response(status)
                    for key, value in headers.items():
                        if key.lower() in {"content-length", "connection", "transfer-encoding"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Type", headers.get("Content-Type", "text/event-stream; charset=utf-8"))
                    self.end_headers()
                    try:
                        for chunk in iterator:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    finally:
                        iterator.close()
                        self.router.finalize_stream_if_needed(request_id)
                    return
                status, headers, data = self.router.call(parsed.path, payload, ctx, dict(self.headers.items()), raw)
                self.send_response(status)
                for key, value in headers.items():
                    if key.lower() in {"content-length", "connection", "transfer-encoding"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except AllModelsFailedError as err:
                self._send_all_models_failed_error(err)
            except Exception as err:
                self._send_json({
                    "error": {
                        "message": f"服务器内部错误: {err}",
                        "type": "internal_server_error",
                        "code": "internal_error",
                    }
                }, status=500)
            return
        self._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)

    def do_PUT(self) -> None:
        """把 PUT /api/groups/{id}、PUT /api/models/{id} 和 PUT /api/settings 转发到对应的 POST 处理逻辑。"""
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            # 前端设置面板使用 PUT 保存设置，复用 do_POST 的处理逻辑
            self._put_body = self._read_raw_body()
            return self.do_POST()
        if parsed.path.startswith("/api/groups/"):
            group_id = parsed.path.split("/")[3]
            payload = self._read_json()
            payload["id"] = group_id
            self.path = "/api/groups"
            self._put_body = json.dumps(payload).encode("utf-8")
            return self.do_POST()
        if parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            payload = self._read_json()
            payload["id"] = model_id
            self.path = "/api/models"
            self._put_body = json.dumps(payload).encode("utf-8")
            return self.do_POST()
        if parsed.path.startswith("/api/aggregates/"):
            aggregate_id = parsed.path.split("/")[3]
            payload = self._read_json()
            payload["id"] = aggregate_id
            self.path = "/api/aggregates"
            self._put_body = json.dumps(payload).encode("utf-8")
            return self.do_POST()
        if parsed.path.startswith("/api/aggregate-members/"):
            member_id = parsed.path.split("/")[3]
            payload = self._read_json()
            payload["id"] = member_id
            # 从已有成员补全 aggregate_id，避免前端漏传
            existing = self.store.find_aggregate_member(member_id)
            if existing and not payload.get("aggregate_id"):
                payload["aggregate_id"] = existing.aggregate_id
            self.path = f"/api/aggregates/{payload.get('aggregate_id')}/members"
            self._put_body = json.dumps(payload).encode("utf-8")
            return self.do_POST()
        self._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/groups/"):
            group_id = parsed.path.split("/")[3]
            # 统一使用 store.remove_group()，确保级联删除组下模型及引用该组的聚合成员
            group_removed, removed_models, removed_members = self.store.remove_group(group_id)
            if not group_removed:
                self._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
                return
            self._send_json({"ok": True, "removed_models": removed_models, "removed_members": removed_members})
            return
        if parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            if self.store.remove_model(model_id):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": {"message": "模型不存在", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
            return
        if parsed.path.startswith("/api/aggregates/"):
            aggregate_id = parsed.path.split("/")[3]
            removed_model, removed_members = self.store.remove_aggregate(aggregate_id)
            if not removed_model:
                self._send_json({"error": {"message": "聚合模型不存在", "type": "invalid_request_error", "code": "aggregate_not_found"}}, status=404)
                return
            self._send_json({"ok": True, "removed_members": removed_members})
            return
        if parsed.path.startswith("/api/aggregate-members/"):
            member_id = parsed.path.split("/")[3]
            if self.store.remove_aggregate_member(member_id):
                self._send_json({"ok": True})
            else:
                self._send_json({"error": {"message": "聚合成员不存在", "type": "invalid_request_error", "code": "aggregate_member_not_found"}}, status=404)
            return
        self._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)


def ensure_sample_config(path: Path) -> None:
    if path.exists():
        return
    sample_group = ConnectionGroup(
        id=uuid.uuid4().hex,
        name="默认组",
        provider_type=PROVIDER_ARK,
        base_url=DEFAULT_BASE_URL,
        ark_api_key="sk-xxxx",
    )
    sample_model = ModelConfig(
        id=uuid.uuid4().hex,
        name="DeepSeek",
        ep_id="ep-xxxx",
        group_id=sample_group.id,
        usable=True,
    )
    with path.open("w", encoding="utf-8") as f:
        json.dump({"groups": [asdict(sample_group)], "models": [asdict(sample_model)]}, f, ensure_ascii=False, indent=2)


def pick_port(start_port: int, host: str) -> int:
    for port in range(start_port, start_port + MAX_PORT_SCAN):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port found in range {start_port}-{start_port + MAX_PORT_SCAN - 1}")


def create_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_START_PORT,
    config: str | Path = DEFAULT_CONFIG_FILE,
) -> Tuple[ThreadingHTTPServer, int, Path]:
    config_path = Path(config)
    ensure_sample_config(config_path)
    store = ConfigStore(config_path)
    store.refresh_expired_cooldowns()
    settings_store = SettingsStore(config_path)
    router = ArkProxyRouter(store, settings_store)
    selected_port = pick_port(port, host)

    server = ThreadingHTTPServer((host, selected_port), RouterHandler)
    server.store = store  # type: ignore[attr-defined]
    server.router = router  # type: ignore[attr-defined]
    server.settings_store = settings_store  # type: ignore[attr-defined]
    return server, selected_port, config_path.resolve()


def main() -> None:
    # 默认配置文件固定在项目根目录，不跟随命令行工作目录变化
    default_config = str(get_platform().get_config_path(DEFAULT_CONFIG_FILE))
    parser = argparse.ArgumentParser(description="Lin Router proxy UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=DEFAULT_START_PORT, type=int)
    parser.add_argument("--config", default=default_config)
    args = parser.parse_args()

    server, port, config_path = create_server(args.host, args.port, args.config)

    print(f"Lin Router running on http://{args.host}:{port}")
    print(f"Config file: {config_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
