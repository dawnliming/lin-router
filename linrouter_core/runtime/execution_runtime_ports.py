"""Concrete, narrow adapters used by the execution coordinator.

They are assembled in the composition root from existing owners.  The runtime receives
these explicit ports individually; it never receives the compatibility router.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Tuple


@dataclass(frozen=True)
class ExecutionFaults:
    all_models_failed: type[Exception]
    stream_idle_timeout: type[Exception]
    route_context: type


class CandidateStatePort:
    def __init__(self, *, refresh: Callable[[], None], find_group: Callable[[str], Any], route_group_id: Callable[[Any], str | None], resolve_aggregate: Callable[[str | None, Any], Any], supports_requested_model: Callable[..., bool], iter_candidates: Callable[..., Iterator[Any]], iter_aggregate: Callable[..., Iterator[Any]], aggregate_cooldown_seconds: Callable[[Any], int], set_aggregate_cooldown: Callable[..., None], set_cooldown: Callable[..., None], record_qualified_failure: Callable[..., bool], set_unusable: Callable[..., None], mark_success: Callable[[Any], None], mark_aggregate_success: Callable[[str], None], mark_unusable: Callable[..., None], release_probe: Callable[[Any], None]) -> None:
        self._refresh = refresh; self._find_group = find_group; self._route_group_id = route_group_id
        self._resolve_aggregate = resolve_aggregate; self._supports_requested_model = supports_requested_model; self._iter_candidates = iter_candidates; self._iter_aggregate = iter_aggregate
        self._aggregate_cooldown_seconds = aggregate_cooldown_seconds; self._set_aggregate_cooldown = set_aggregate_cooldown
        self._set_cooldown = set_cooldown; self._record_qualified_failure = record_qualified_failure; self._set_unusable = set_unusable; self._mark_success = mark_success
        self._mark_aggregate_success = mark_aggregate_success; self._mark_unusable = mark_unusable; self._release_probe = release_probe
    def refresh_expired_cooldowns(self) -> None: self._refresh()
    def find_group(self, group_id: str) -> Any: return self._find_group(group_id)
    def route_group_id(self, route: Any) -> str | None: return self._route_group_id(route)
    def resolve_aggregate(self, model: str | None, route: Any) -> Any: return self._resolve_aggregate(model, route)
    def supports_requested_model(self, model: str | None, group: Any) -> bool: return self._supports_requested_model(model, group)
    def iter_upstream_candidates(self, *args: Any, **kwargs: Any) -> Iterator[Any]: return self._iter_candidates(*args, **kwargs)
    def iter_aggregate_candidates(self, *args: Any, **kwargs: Any) -> Iterator[Any]: return self._iter_aggregate(*args, **kwargs)
    def aggregate_cooldown_seconds(self, aggregate: Any) -> int: return self._aggregate_cooldown_seconds(aggregate)
    def set_aggregate_member_cooldown(self, *args: Any) -> None: self._set_aggregate_cooldown(*args)
    def set_cooldown(self, *args: Any) -> None: self._set_cooldown(*args)
    def record_qualified_failure(self, *args: Any) -> bool: return bool(self._record_qualified_failure(*args))
    def set_unusable(self, *args: Any) -> None: self._set_unusable(*args)
    def mark_success(self, candidate: Any) -> None: self._mark_success(candidate)
    def mark_aggregate_member_success(self, member_id: str) -> None: self._mark_aggregate_success(member_id)
    def mark_unusable(self, candidate: Any, error: str) -> None: self._mark_unusable(candidate, error)
    def release_probe(self, candidate: Any) -> None: self._release_probe(candidate)


class RequestPreparationPort:
    def __init__(self, *, resolve_url: Callable[..., str], tools_enabled: Callable[[], bool], normalize_tools: Callable[..., Tuple[Dict[str, Any], bool]], body_for: Callable[..., Tuple[bytes, str]], headers_for: Callable[..., Dict[str, str]], aggregate_log_suffix: Callable[..., str], debug_detail: Callable[..., str], short_error: Callable[[str], str], fingerprint: Callable[..., str]) -> None:
        self._resolve_url = resolve_url; self._tools_enabled = tools_enabled; self._normalize_tools = normalize_tools
        self._body_for = body_for; self._headers_for = headers_for; self._aggregate_log_suffix = aggregate_log_suffix
        self._debug_detail = debug_detail; self._short_error = short_error; self._fingerprint = fingerprint
    def resolve_url(self, *args: Any) -> str: return self._resolve_url(*args)
    def tools_order_enabled(self) -> bool: return self._tools_enabled()
    def normalize_tools_order(self, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]: return self._normalize_tools(payload)
    def body_for_upstream(self, *args: Any) -> Tuple[bytes, str]: return self._body_for(*args)
    def headers_for(self, *args: Any, **kwargs: Any) -> Dict[str, str]: return self._headers_for(*args, **kwargs)
    def aggregate_log_suffix(self, **kwargs: Any) -> str: return self._aggregate_log_suffix(**kwargs)
    def debug_detail(self, *args: Any, **kwargs: Any) -> str: return self._debug_detail(*args, **kwargs)
    def short_error(self, error: str) -> str: return self._short_error(error)
    def payload_fingerprint(self, *args: Any, **kwargs: Any) -> str: return self._fingerprint(*args, **kwargs)


class ConcurrencyPort:
    def __init__(self, *, candidate_lock: Callable[..., Any], acquire: Callable[[Any], Tuple[bool, int]], release: Callable[[Any], bool | None], busy_detail: Callable[..., str], mark_stream_active: Callable[[Any, int], None]) -> None:
        self._candidate_lock = candidate_lock; self._acquire = acquire; self._release = release; self._busy_detail = busy_detail; self._mark_stream_active = mark_stream_active
    def candidate_lock(self, *args: Any) -> Any: return self._candidate_lock(*args)
    def acquire(self, lock: Any, request_id: str = "") -> Tuple[bool, int]:
        try:
            return self._acquire(lock, request_id=request_id)
        except TypeError as exc:
            if "request_id" not in str(exc):
                raise
            return self._acquire(lock)
    def release(self, lock: Any) -> bool:
        released = self._release(lock)
        return bool(lock) if released is None else bool(released)
    def busy_detail(self, *args: Any) -> str: return self._busy_detail(*args)
    def mark_stream_active(self, candidate: Any, delta: int) -> None: self._mark_stream_active(candidate, delta)


class StreamLifecyclePort:
    def __init__(self, *, idle_timeout: Callable[[Any], int], readline: Callable[..., bytes], response_usage: Callable[[bytes], tuple[int, int, int, int, int]], chunk_usage: Callable[[bytes], tuple[int, int, int, int, int]], completion_signal: Callable[[bytes], str], mark_timeout: Callable[..., int], chunk_usage_with_presence: Callable[[bytes], Tuple[tuple[int, int, int, int, int], bool]] | None = None) -> None:
        self._idle_timeout = idle_timeout; self._readline = readline; self._response_usage = response_usage; self._chunk_usage = chunk_usage; self._chunk_usage_with_presence = chunk_usage_with_presence; self._completion_signal = completion_signal; self._mark_timeout = mark_timeout
    def idle_timeout_seconds(self, group: Any) -> int: return self._idle_timeout(group)
    def readline_with_idle_timeout(self, response: Any, timeout: int) -> bytes: return self._readline(response, timeout)
    def usage_from_response(self, data: bytes) -> tuple[int, int, int, int, int]: return self._response_usage(data)
    def usage_from_stream_chunk(self, chunk: bytes) -> tuple[int, int, int, int, int]: return self._chunk_usage(chunk)
    def usage_from_stream_chunk_with_presence(self, chunk: bytes) -> Tuple[tuple[int, int, int, int, int], bool]:
        if self._chunk_usage_with_presence is not None:
            return self._chunk_usage_with_presence(chunk)
        # Numeric values alone cannot establish whether an SSE payload carried
        # usage: an explicit all-zero usage is valid.  Legacy value-only
        # callbacks therefore retain their parsed values but are conservatively
        # treated as lacking an explicit presence signal.
        return self._chunk_usage(chunk), False
    def completion_signal(self, chunk: bytes) -> str: return self._completion_signal(chunk)
    def mark_stream_timeout(self, candidate: Any, error: str) -> int: return self._mark_timeout(candidate, error)


class ObservabilityPort:
    def __init__(self, *, start: Callable[..., None], update: Callable[..., None], finish: Callable[..., None], add_log: Callable[..., None], patch_stream: Callable[..., bool], cancellation_requested: Callable[[str], bool], downstream_write_failed: Callable[[str], bool], set_response: Callable[[str, Any], None], close_response: Callable[..., bool]) -> None:
        self._start = start; self._update = update; self._finish = finish; self._add_log = add_log; self._patch_stream = patch_stream
        self._cancellation_requested = cancellation_requested; self._downstream_write_failed = downstream_write_failed; self._set_response = set_response; self._close_response = close_response
    def start_live_request(self, *args: Any, **kwargs: Any) -> None: self._start(*args, **kwargs)

    def update_live_request(self, *args: Any, **kwargs: Any) -> None: self._update(*args, **kwargs)
    def finish_live_request(self, *args: Any, **kwargs: Any) -> None: self._finish(*args, **kwargs)
    def add_log(self, *args: Any, **kwargs: Any) -> None: self._add_log(*args, **kwargs)
    def patch_stream_lifecycle(self, *args: Any, **kwargs: Any) -> bool: return self._patch_stream(*args, **kwargs)
    def cancellation_requested(self, request_id: str) -> bool: return self._cancellation_requested(request_id)
    def downstream_write_failed(self, request_id: str) -> bool: return self._downstream_write_failed(request_id)
    def set_live_response(self, request_id: str, response: Any) -> None: self._set_response(request_id, response)
    def close_live_response(self, request_id: str, response: Any = None) -> bool: return self._close_response(request_id, response)


class DebugCapturePort:
    def __init__(self, capture: Callable[..., None]) -> None: self._capture = capture
    def capture(self, **kwargs: Any) -> None: self._capture(**kwargs)
