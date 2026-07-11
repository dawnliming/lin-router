from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict


RuntimeSnapshotProvider = Callable[[], Dict[str, Any]]


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
