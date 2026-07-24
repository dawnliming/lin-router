from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .constants import (
    DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES,
    DEFAULT_AUTO_MODEL_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    MAX_STREAM_IDLE_TIMEOUT_SECONDS,
    PROVIDER_ARK,
)


ROUTING_POLICY_SMART_BREAKER = "smart_breaker"
ROUTING_POLICY_FIXED_COOLDOWN = "fixed_cooldown"
ROUTING_POLICY_STICKY_ROUTE = "sticky_route"
ROUTING_POLICY_COOLDOWN_OFF = "cooldown_off"
ROUTING_POLICIES = frozenset({
    ROUTING_POLICY_SMART_BREAKER,
    ROUTING_POLICY_FIXED_COOLDOWN,
    ROUTING_POLICY_STICKY_ROUTE,
    ROUTING_POLICY_COOLDOWN_OFF,
})
MIN_FIXED_COOLDOWN_MINUTES = 1
MAX_FIXED_COOLDOWN_MINUTES = 1440


def _failure_timestamps(value: Any) -> List[int]:
    """只接受匿名 epoch 秒值；保留同秒多次失败以免丢失阈值证据。"""
    if not isinstance(value, list):
        return []
    timestamps: List[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            timestamp = int(item)
        except (TypeError, ValueError):
            continue
        if timestamp > 0:
            timestamps.append(timestamp)
    return sorted(timestamps)[-5:]


class RoutingPolicyValidationError(ValueError):
    """路由策略配置非法时提供稳定错误码，供 HTTP 与导入入口统一返回。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_routing_policy_payload(
    data: Dict[str, Any],
    *,
    fallback_fixed_cooldown_minutes: int = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES,
    fixed_cooldown_field: str = "fixed_cooldown_minutes",
) -> str:
    """校验 canonical 策略和旧布尔字段，返回可持久化的 canonical 值。

    旧字段仅用于读取和迁移，不能与 ``routing_policy`` 表达相反含义；否则
    调用方必须在写入前拒绝请求，避免一次保存产生语义不确定的配置。
    """
    has_policy = "routing_policy" in data
    has_legacy = "smart_breaker_enabled" in data
    legacy_policy = ROUTING_POLICY_SMART_BREAKER
    if has_legacy:
        legacy_enabled = data["smart_breaker_enabled"]
        if not isinstance(legacy_enabled, bool):
            raise RoutingPolicyValidationError(
                "invalid_routing_policy",
                "路由策略无效：旧智能熔断字段必须为布尔值。",
            )
        legacy_policy = (
            ROUTING_POLICY_SMART_BREAKER
            if legacy_enabled
            else ROUTING_POLICY_COOLDOWN_OFF
        )

    if has_policy:
        policy = data["routing_policy"]
        if not isinstance(policy, str) or policy not in ROUTING_POLICIES:
            raise RoutingPolicyValidationError(
                "invalid_routing_policy",
                "路由策略无效：仅支持 smart_breaker、fixed_cooldown、sticky_route 或 cooldown_off。",
            )
        if has_legacy and policy != legacy_policy:
            raise RoutingPolicyValidationError(
                "conflicting_routing_policy",
                "路由策略冲突：routing_policy 与旧智能熔断字段表达的策略不一致。",
            )
    else:
        policy = legacy_policy if has_legacy else ROUTING_POLICY_SMART_BREAKER

    raw_minutes = data.get(fixed_cooldown_field, fallback_fixed_cooldown_minutes)
    if isinstance(raw_minutes, bool):
        raise RoutingPolicyValidationError(
            "invalid_fixed_cooldown_minutes",
            "固定冷却分钟数无效：必须是 1 到 1440 的整数。",
        )
    try:
        fixed_cooldown_minutes = int(raw_minutes)
    except (TypeError, ValueError):
        raise RoutingPolicyValidationError(
            "invalid_fixed_cooldown_minutes",
            "固定冷却分钟数无效：必须是 1 到 1440 的整数。",
        ) from None
    if policy == ROUTING_POLICY_FIXED_COOLDOWN and not (
        MIN_FIXED_COOLDOWN_MINUTES <= fixed_cooldown_minutes <= MAX_FIXED_COOLDOWN_MINUTES
    ):
        raise RoutingPolicyValidationError(
            "invalid_fixed_cooldown_minutes",
            "固定冷却分钟数无效：必须在 1 到 1440 分钟之间。",
        )
    return policy


def routing_policy_from_dict(
    data: Dict[str, Any],
    *,
    fallback_fixed_cooldown_minutes: int = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES,
) -> str:
    """容错读取落盘历史配置；HTTP/导入入口仍使用严格校验函数。"""
    candidate = dict(data)
    # 旧调用方常基于 asdict() 覆盖 smart_breaker_enabled，此时旧字段代表
    # 明确兼容意图。HTTP 写路径会在进入此函数前执行严格冲突校验。
    if "smart_breaker_enabled" in candidate:
        candidate.pop("routing_policy", None)
    try:
        return validate_routing_policy_payload(
            candidate,
            fallback_fixed_cooldown_minutes=fallback_fixed_cooldown_minutes,
        )
    except RoutingPolicyValidationError:
        return ROUTING_POLICY_SMART_BREAKER


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
    # Header 兼容与请求并发是两项独立策略。
    serial_protection: bool = False
    waf_accept_policy: str = "default"
    waf_client_mode: str = "always"
    reasoning_support: str = "unknown"
    # 新配置只持久化 canonical 策略；旧 smart_breaker_enabled 通过兼容属性读取。
    routing_policy: str = ROUTING_POLICY_SMART_BREAKER
    upstream_models: List[Dict[str, Any]] = field(default_factory=list)
    upstream_models_fetched_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionGroup":
        routing_policy = routing_policy_from_dict(
            data,
            fallback_fixed_cooldown_minutes=int(
                data.get("auto_model_cooldown_minutes")
                or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
            ),
        )
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            provider_type=str(data.get("provider_type") or PROVIDER_ARK),
            base_url="" if "base_url" in data and data.get("base_url") is None else (str(data.get("base_url")) if "base_url" in data else DEFAULT_BASE_URL),
            ark_api_key=data.get("ark_api_key") or "",
            api_key=str(data.get("api_key") or ""),
            route_key=str(data.get("route_key") or ""),
            auto_model_name=str(data.get("auto_model_name") or "").strip() or DEFAULT_AUTO_MODEL_NAME,
            auto_model_cooldown_minutes=int(data.get("auto_model_cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES),
            stream_idle_timeout=max(0, min(MAX_STREAM_IDLE_TIMEOUT_SECONDS, int(data.get("stream_idle_timeout", DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS) or 0))),
            waf_compatible=bool(data.get("waf_compatible", False)),
            serial_protection=bool(data.get("serial_protection", False)),
            waf_accept_policy=str(data.get("waf_accept_policy") or "default"),
            waf_client_mode=str(data.get("waf_client_mode") or "always").lower(),
            reasoning_support=str(data.get("reasoning_support") or "unknown").lower(),
            routing_policy=routing_policy,
            upstream_models=[item for item in data.get("upstream_models", []) if isinstance(item, dict)] if isinstance(data.get("upstream_models", []), list) else [],
            upstream_models_fetched_at=str(data.get("upstream_models_fetched_at") or ""),
        )

    @property
    def smart_breaker_enabled(self) -> bool:
        """旧运行时调用兼容层；该属性不会进入 dataclass 持久化输出。"""
        return self.routing_policy == ROUTING_POLICY_SMART_BREAKER

    @smart_breaker_enabled.setter
    def smart_breaker_enabled(self, enabled: bool) -> None:
        self.routing_policy = (
            ROUTING_POLICY_SMART_BREAKER
            if bool(enabled)
            else ROUTING_POLICY_COOLDOWN_OFF
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
    health_state: str = "normal"
    consecutive_failures: int = 0
    consecutive_network_failures: int = 0
    last_failure_at: int = 0
    breaker_until: int = 0
    breaker_reason: str = ""
    # 只保存匿名成功/合格失败结果，不能保存上游响应或请求内容。
    attempt_window: List[str] = field(default_factory=list)
    # v0.6.4 之后以匿名时间戳作为滚动窗口唯一依据，旧 attempt_window 仅兼容读取。
    qualified_failure_timestamps: List[int] = field(default_factory=list)
    network_failure_timestamps: List[int] = field(default_factory=list)
    breaker_level: int = 0

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
            health_state=str(data.get("health_state") or "normal"),
            consecutive_failures=max(0, int(data.get("consecutive_failures") or 0)),
            consecutive_network_failures=max(0, int(data.get("consecutive_network_failures") or 0)),
            last_failure_at=max(0, int(data.get("last_failure_at") or 0)),
            breaker_until=max(0, int(data.get("breaker_until") or 0)),
            breaker_reason=str(data.get("breaker_reason", "")),
            attempt_window=[item for item in data.get("attempt_window", []) if item in {"success", "qualified_failure"}][-5:] if isinstance(data.get("attempt_window"), list) else [],
            qualified_failure_timestamps=_failure_timestamps(data.get("qualified_failure_timestamps")),
            network_failure_timestamps=_failure_timestamps(data.get("network_failure_timestamps")),
            breaker_level=max(0, int(data.get("breaker_level") or 0)),
        )


@dataclass
class AggregateModel:
    id: str
    name: str
    display_name: str = ""
    description: str = ""
    route_key: str = ""
    client_model_aliases: List[str] = field(default_factory=list)
    enabled: bool = True
    # 聚合策略只作用于当前聚合成员，绝不覆盖底层连接组策略。
    routing_policy: str = ROUTING_POLICY_SMART_BREAKER
    strategy: str = "priority"
    cooldown_minutes: int = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        # 当前产品只保留手动优先级；历史值仍可读取，但不能继续参与价格排序。
        self.strategy = self.normalize_strategy(self.strategy)

    @staticmethod
    def normalize_strategy(_value: Any) -> str:
        """将历史或未知调度策略归一为当前唯一支持的 priority。"""
        return "priority"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AggregateModel":
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        routing_policy = routing_policy_from_dict(
            data,
            fallback_fixed_cooldown_minutes=int(
                data.get("cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
            ),
        )
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            route_key=str(data.get("route_key") or "").strip(),
            client_model_aliases=cls._normalize_client_model_aliases(data.get("client_model_aliases")),
            enabled=bool(data.get("enabled", True)),
            routing_policy=routing_policy,
            strategy=cls.normalize_strategy(data.get("strategy")),
            cooldown_minutes=int(data.get("cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES),
            created_at=str(data.get("created_at") or now),
            updated_at=str(data.get("updated_at") or now),
        )

    @property
    def smart_breaker_enabled(self) -> bool:
        """兼容旧健康层读取，持久化与导出始终只保留 routing_policy。"""
        return self.routing_policy == ROUTING_POLICY_SMART_BREAKER

    @smart_breaker_enabled.setter
    def smart_breaker_enabled(self, enabled: bool) -> None:
        self.routing_policy = (
            ROUTING_POLICY_SMART_BREAKER
            if bool(enabled)
            else ROUTING_POLICY_COOLDOWN_OFF
        )

    @staticmethod
    def _normalize_client_model_aliases(value: Any) -> List[str]:
        if isinstance(value, str):
            items = re.split(r"[\r\n,]+", value)
        elif isinstance(value, list):
            items = value
        else:
            items = []
        aliases: List[str] = []
        seen: set[str] = set()
        for item in items:
            for raw_alias in re.split(r"[\r\n,]+", str(item or "")):
                alias = raw_alias.strip()
                if alias and alias not in seen:
                    aliases.append(alias)
                    seen.add(alias)
        return aliases


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
    health_state: str = "normal"
    consecutive_failures: int = 0
    consecutive_network_failures: int = 0
    last_failure_at: int = 0
    breaker_until: int = 0
    breaker_reason: str = ""
    # 聚合成员独立维护匿名稳定性窗口，绝不改变底层真实模型的状态归属。
    attempt_window: List[str] = field(default_factory=list)
    qualified_failure_timestamps: List[int] = field(default_factory=list)
    network_failure_timestamps: List[int] = field(default_factory=list)
    breaker_level: int = 0

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
            health_state=str(data.get("health_state") or "normal"),
            consecutive_failures=max(0, int(data.get("consecutive_failures") or 0)),
            consecutive_network_failures=max(0, int(data.get("consecutive_network_failures") or 0)),
            last_failure_at=max(0, int(data.get("last_failure_at") or 0)),
            breaker_until=max(0, int(data.get("breaker_until") or 0)),
            breaker_reason=str(data.get("breaker_reason", "")),
            attempt_window=[item for item in data.get("attempt_window", []) if item in {"success", "qualified_failure"}][-5:] if isinstance(data.get("attempt_window"), list) else [],
            qualified_failure_timestamps=_failure_timestamps(data.get("qualified_failure_timestamps")),
            network_failure_timestamps=_failure_timestamps(data.get("network_failure_timestamps")),
            breaker_level=max(0, int(data.get("breaker_level") or 0)),
        )
