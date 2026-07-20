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
    # 连接组策略只控制智能熔断，不改变模型手动启停或其他路由策略。
    smart_breaker_enabled: bool = True
    upstream_models: List[Dict[str, Any]] = field(default_factory=list)
    upstream_models_fetched_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionGroup":
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
            smart_breaker_enabled=bool(data.get("smart_breaker_enabled", True)),
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
    health_state: str = "normal"
    consecutive_failures: int = 0
    last_failure_at: int = 0
    breaker_until: int = 0
    breaker_reason: str = ""

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
            last_failure_at=max(0, int(data.get("last_failure_at") or 0)),
            breaker_until=max(0, int(data.get("breaker_until") or 0)),
            breaker_reason=str(data.get("breaker_reason", "")),
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
    # 聚合开关只控制当前聚合的成员级熔断，不覆盖成员底层连接组策略。
    smart_breaker_enabled: bool = True
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
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=str(data.get("name") or "").strip(),
            display_name=str(data.get("display_name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            route_key=str(data.get("route_key") or "").strip(),
            client_model_aliases=cls._normalize_client_model_aliases(data.get("client_model_aliases")),
            enabled=bool(data.get("enabled", True)),
            smart_breaker_enabled=bool(data.get("smart_breaker_enabled", True)),
            strategy=cls.normalize_strategy(data.get("strategy")),
            cooldown_minutes=int(data.get("cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES),
            created_at=str(data.get("created_at") or now),
            updated_at=str(data.get("updated_at") or now),
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
    last_failure_at: int = 0
    breaker_until: int = 0
    breaker_reason: str = ""

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
            last_failure_at=max(0, int(data.get("last_failure_at") or 0)),
            breaker_until=max(0, int(data.get("breaker_until") or 0)),
            breaker_reason=str(data.get("breaker_reason", "")),
        )
