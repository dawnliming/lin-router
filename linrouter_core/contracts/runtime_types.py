"""Stable execution DTOs and errors shared without importing the legacy facade."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from linrouter_core.config.models import AggregateModel, ConnectionGroup, ModelConfig


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
    passthrough: bool = True
    is_global: bool = False
    aggregate: Optional[AggregateModel] = None
    is_deprecated_global: bool = False


class AllModelsFailedError(RuntimeError):
    """All candidate models failed; HTTP transport maps this to the stable response."""

    def __init__(
        self,
        message: str,
        attempted: int = 0,
        stream_timeout: bool = False,
        error_code: str = "",
        fallback_chain: Optional[List[Dict[str, Any]]] = None,
        aggregate_name: str = "",
    ) -> None:
        super().__init__(message)
        self.attempted = attempted
        self.stream_timeout = stream_timeout
        self.error_code = error_code
        self.fallback_chain = fallback_chain or []
        self.aggregate_name = aggregate_name


class StreamIdleTimeoutError(TimeoutError):
    """Raised only before a stream has produced its first valid chunk."""
