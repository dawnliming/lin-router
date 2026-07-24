"""Dependency ports for the v0.6 execution plane.

Runtime services depend on these contracts, never on ``app.py`` or HTTP handler
classes.  Concrete adapters keep the existing externally visible behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Protocol, Sequence, Tuple, runtime_checkable


@dataclass(frozen=True)
class CandidateErrorClassification:
    should_cooldown: bool
    is_request_level: bool
    category: str
    log_reason: str
    failure_scope: str


@runtime_checkable
class ExecutionPolicyPort(Protocol):
    def classify_candidate_error(
        self,
        status_code: Optional[int],
        raw: str,
        error_kind: str = "http",
    ) -> CandidateErrorClassification: ...
    def is_auto_model(self, requested_model: str | None, group: Any) -> bool: ...
    def auto_cooldown_seconds(self, group: Any) -> int: ...
    def waf_blocked_suffix(self, classification: CandidateErrorClassification, group: Any) -> str: ...
    def waf_blocked_hint(self, fallback_chain: Sequence[Dict[str, Any]]) -> str: ...


class CandidateQueryPort(Protocol):
    def iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Any]: ...
    def iter_aggregate_candidates(self, aggregate: Any, **kwargs: Any) -> Iterator[Any]: ...
    def resolve_aggregate(self, requested_model: str | None, route: Any) -> Optional[Tuple[Any, str]]: ...


class HealthStatePort(Protocol):
    def refresh_expired_cooldowns(self) -> None: ...
    def mark_success(self, candidate: Any) -> None: ...
    def mark_unusable(self, candidate: Any, error: str) -> None: ...
    def set_cooldown(
        self,
        idx: int,
        error: str,
        cooldown_seconds: int,
        reason: str,
        category: str | None = None,
    ) -> bool: ...
    def set_aggregate_member_cooldown(
        self,
        member_id: str,
        error: str,
        cooldown_seconds: int,
        reason: str,
        category: str | None = None,
    ) -> bool: ...


class UpstreamRequestPort(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: bytes,
        *,
        stream: bool,
        timeout: int,
        stream_idle_timeout: int | None = None,
    ) -> Any: ...
    def close(self) -> None: ...


class ConcurrencyPort(Protocol):
    def lock_for(self, candidate: Any, enabled: bool) -> Any: ...
    def active_count(self, candidate: Any) -> int: ...
    def mark_stream_active(self, candidate: Any, delta: int) -> None: ...


class RequestObservabilityPort(Protocol):
    def start_live_request(self, request_id: str, path: str, requested_model: str, *, stream: bool) -> None: ...
    def update_live_request(self, request_id: str, **patch: Any) -> None: ...
    def finish_live_request(self, request_id: str, status: str = "done") -> None: ...
    def add_log(self, path: str, model: str, status: str, detail: str = "", **kwargs: Any) -> None: ...
    def patch_stream_lifecycle(self, request_id: str, attempt: int, candidate_label: str, usage: tuple[int, int, int, int, int], usage_source: str, **kwargs: Any) -> bool: ...
    def downstream_write_failed(self, request_id: str) -> bool: ...
    def downstream_failure_category(self, request_id: str) -> str: ...


class ProtocolAdapterPort(Protocol):
    def resolve_endpoint(self, base_url: str, path: str) -> str: ...
    def build_request(self, *, base_url: str, auth_key: str, incoming_headers: Dict[str, str], stream: bool, waf_compatible: bool, waf_accept_policy: str) -> Dict[str, str]: ...
    def fetch_models(self, base_url: str, auth_key: str) -> tuple[str, Dict[str, str], int, list[Dict[str, Any]]]: ...
