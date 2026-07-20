"""Candidate execution coordinator behind the compatibility facade.

The coordinator receives explicit owner ports from the composition root and never depends
on the legacy router or HTTP transport.  Candidate ordering and request semantics remain
unchanged while dependencies are kept narrow and auditable.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from linrouter_core.config.constants import DEFAULT_AUTO_MODEL_NAME, GLOBAL_ROUTE_GROUP_ID, PROVIDER_PROXY, PROVIDER_RELAY
from linrouter_core.contracts.execution_ports import (
    CandidateErrorClassification,
    ExecutionPolicyPort,
)
from linrouter_core.runtime.candidate_health import CandidateHealthService
from linrouter_core.runtime.execution_policy import ExecutionPolicyService
from linrouter_core.runtime.execution_runtime_ports import (
    CandidateStatePort, ConcurrencyPort, DebugCapturePort, ExecutionFaults,
    ObservabilityPort, RequestPreparationPort, StreamLifecyclePort,
)


class SerialProtectionState:
    """按上游候选管理串行保护锁与活跃流状态。"""

    def __init__(self) -> None:
        self.locks: Dict[str, threading.Lock] = {}
        self.active_streams: Dict[str, int] = {}
        self.guard = threading.Lock()

    @staticmethod
    def key(candidate: Any) -> str:
        return f"{candidate.group.id}:{candidate.target_model}:{candidate.channel}"

    def lock_for(self, candidate: Any, enabled: bool) -> Optional[threading.Lock]:
        if not enabled:
            return None
        key = self.key(candidate)
        with self.guard:
            lock = self.locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self.locks[key] = lock
            return lock

    def active_count(self, candidate: Any) -> int:
        with self.guard:
            return int(self.active_streams.get(self.key(candidate), 0))

    def mark_stream_active(self, candidate: Any, delta: int) -> None:
        key = self.key(candidate)
        with self.guard:
            next_value = max(0, int(self.active_streams.get(key, 0)) + delta)
            if next_value:
                self.active_streams[key] = next_value
            else:
                self.active_streams.pop(key, None)

    def busy_detail(self, candidate: Any, body: bytes, lock_wait_ms: int) -> str:
        active_streams = self.active_count(candidate)
        fallback_reason = "large_task_in_progress" if active_streams or len(body) > 131072 else "candidate_busy"
        return (
            f"serial_protection_wait_timeout; fallback_reason={fallback_reason}; "
            f"failure_scope=busy; cooldown_applied=false; active_streams={active_streams}; "
            f"lock_wait_ms={lock_wait_ms}; busy_hint=candidate_busy; "
            "request_concurrency=serial_protection"
        )


# 兼容仍导入旧名称的外部集成。
WafLockState = SerialProtectionState


MAX_SSE_FRAME_BYTES = 4 * 1024 * 1024
MAX_SSE_FRAME_WAIT_SECONDS = 15


class StreamFrameProtocolError(URLError):
    """Raised before response commitment when an SSE frame never completes."""

    def __init__(self, reason: str, frame_bytes: int, frame_wait_ms: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.frame_bytes = max(0, int(frame_bytes))
        self.frame_wait_ms = max(0, int(frame_wait_ms))


class ManagedStreamIterator:
    """Ensures stream resources are released even when no chunk is consumed."""

    def __init__(self, iterator: Iterator[bytes], finalize: Callable[[], None]) -> None:
        self._iterator = iterator
        self._finalize = finalize
        self._closed = False

    def __iter__(self) -> "ManagedStreamIterator":
        return self

    def __next__(self) -> bytes:
        return next(self._iterator)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._iterator, "close", None)
            if callable(close):
                close()
        finally:
            self._finalize()


def _read_sse_frame(
    readline: Callable[[int], bytes],
    timeout_seconds: int,
    on_raw_line: Optional[Callable[[bytes], None]] = None,
    *,
    max_frame_bytes: int = MAX_SSE_FRAME_BYTES,
    max_frame_wait_seconds: int = MAX_SSE_FRAME_WAIT_SECONDS,
) -> bytes:
    """Read one complete SSE frame, delimited by a blank line.

    Upstream ``readline`` calls may return an ``event:`` line before the
    matching ``data:`` line.  Forwarding that line immediately gives clients
    a first byte without a parseable SSE event.  Keep the lines together until
    the protocol delimiter arrives; only an actual EOF with no buffered data
    returns ``b""``.
    """
    lines: list[bytes] = []
    frame_bytes = 0
    frame_started_at = 0.0
    while True:
        read_timeout = timeout_seconds
        if frame_started_at and max_frame_wait_seconds > 0:
            remaining_seconds = max_frame_wait_seconds - (time.perf_counter() - frame_started_at)
            if remaining_seconds <= 0:
                raise StreamFrameProtocolError(
                    "stream_frame_wait_limit",
                    frame_bytes,
                    _elapsed_ms(frame_started_at),
                )
            remaining_timeout = max(1, int(remaining_seconds + 0.999))
            if read_timeout <= 0 or read_timeout > remaining_timeout:
                read_timeout = remaining_timeout
        try:
            line = readline(read_timeout)
        except TimeoutError as exc:
            if frame_started_at:
                raise StreamFrameProtocolError(
                    "stream_frame_wait_limit",
                    frame_bytes,
                    _elapsed_ms(frame_started_at),
                ) from exc
            raise
        if line and on_raw_line:
            # The callback receives no persisted content.  It only marks the
            # first logical upstream line for timing attribution.
            on_raw_line(line)
        if not line:
            return b"".join(lines)
        if line in {b"\n", b"\r\n"}:
            if lines:
                return b"".join(lines) + line
            continue
        if not frame_started_at:
            frame_started_at = time.perf_counter()
        frame_bytes += len(line)
        if max_frame_bytes > 0 and frame_bytes > max_frame_bytes:
            raise StreamFrameProtocolError(
                "stream_frame_size_limit",
                frame_bytes,
                _elapsed_ms(frame_started_at),
            )
        lines.append(line)


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def _stream_metrics_text(stream_metrics: Dict[str, Any]) -> str:
    return "; ".join(f"{key}={value}" for key, value in stream_metrics.items())


def _header_value(headers: Any, name: str) -> str:
    for key, value in getattr(headers, "items", lambda: [])():
        if str(key).lower() == name:
            return str(value)
    return ""


def _safe_media_type(value: str) -> str:
    media_type = str(value or "").split(";", 1)[0].strip().lower()
    if not media_type or not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type):
        return "unknown"
    return media_type


def _safe_header_token(value: str) -> str:
    token = str(value or "").split(",", 1)[0].strip().lower()
    if not token:
        return "-"
    return token if re.fullmatch(r"[a-z0-9._-]+", token) else "unknown"


def _safe_http_version(value: Any) -> str:
    normalized = str(value or "").upper().strip()
    return normalized if normalized in {"HTTP/1.0", "HTTP/1.1", "HTTP/2"} else "unknown"


def _frame_is_delimited(frame: bytes) -> bool:
    return frame.endswith(b"\n\n") or frame.endswith(b"\r\n\r\n")


def _frame_has_text_delta(frame: bytes) -> bool:
    """Detect text-bearing deltas without retaining their content."""
    for line in frame.decode("utf-8", "ignore").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or payload.get("event") or "").lower()
        if event_type in {"response.output_text.delta", "response.output_text.done"}:
            if isinstance(payload.get("delta"), str) and payload.get("delta"):
                return True
            if isinstance(payload.get("text"), str) and payload.get("text"):
                return True
        if event_type == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str) and delta.get("text"):
                return True
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                delta = choice.get("delta") if isinstance(choice, dict) else None
                if not isinstance(delta, dict):
                    continue
                content = delta.get("content")
                if isinstance(content, str) and content:
                    return True
                if isinstance(content, list) and any(
                    isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("text")
                    for part in content
                ):
                    return True
    return False


def _stream_wire_mode(
    media_type: str,
    content_encoding: str,
    *,
    saw_non_delimited_frame: bool,
    stream_frame_count: int,
    initial_frame_bytes: int,
) -> str:
    if media_type == "application/json" or media_type.endswith("+json"):
        return "json_compat"
    if (
        content_encoding not in {"-", "identity"}
        or saw_non_delimited_frame
        or (stream_frame_count == 1 and initial_frame_bytes >= 65536)
    ):
        return "buffered_or_non_delimited"
    return "sse"


def _response_headers_for_downstream(response: Any) -> Dict[str, str]:
    """Avoid advertising a content encoding after httpx has decoded it."""
    headers = dict(getattr(response, "headers", {}).items())
    if getattr(response, "content_decoded", False):
        return {
            key: value
            for key, value in headers.items()
            if str(key).lower() not in {"content-encoding", "content-length"}
        }
    return headers


class CandidateRuntime:
    """Candidate enumeration and execution coordination through injected dependencies.

    The class deliberately has no import dependency on the legacy application
    facade or HTTP transport; composition supplies its frozen policy surface.
    """

    def __init__(
        self,
        candidate_health: CandidateHealthService,
        candidate_state: CandidateStatePort,
        policy: ExecutionPolicyPort,
        preparation: RequestPreparationPort,
        upstream: Any,
        concurrency: ConcurrencyPort,
        stream_lifecycle: StreamLifecyclePort,
        observability: ObservabilityPort,
        debug_capture: DebugCapturePort,
        faults: ExecutionFaults,
    ) -> None:
        self.candidate_health = candidate_health
        self.candidate_state = candidate_state
        self.policy = policy
        self.preparation = preparation
        self.upstream = upstream
        self.concurrency = concurrency
        self.stream_lifecycle = stream_lifecycle
        self.observability = observability
        self.debug_capture = debug_capture
        self.faults = faults

    def iter_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Tuple[int, Any]]:
        yield from self.candidate_health.iter_candidates(requested_model, group_id)

    def candidate_from_model(self, idx: int, model: Any, group: Any) -> Any:
        return self.candidate_health.candidate_from_model(idx, model, group)

    def iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Any]:
        yield from self.candidate_health.iter_upstream_candidates(requested_model, group_id)

    def aggregate_member_skip_reason(self, member: Any) -> Tuple[str, str, Any, Any]:
        return self.candidate_health.aggregate_member_skip_reason(member)

    def iter_aggregate_candidates(self, aggregate: Any, **kwargs: Any) -> Iterator[Any]:
        yield from self.candidate_health.iter_aggregate_candidates(aggregate, **kwargs)

    def set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        self.candidate_health.set_cooldown(idx, error, cooldown_seconds, reason)

    def set_success(self, idx: int) -> None:
        self.candidate_health.set_success(idx)

    def _finalize_cancelled(self, request_id: str, path: str, requested_label: str, *, group: Any = None, candidate: Any = None, attempt: int = 0, lock_released: bool = False) -> None:
        """Write one cancellation audit record without touching candidate health/fallback."""
        # 取消是请求本地事件，不得计入失败；但必须释放半开租约，避免
        # 后续请求永久看到 half_open_probe。
        self.candidate_state.release_probe(candidate)
        self.observability.add_log(
            path, candidate.label if candidate is not None else requested_label, "cancelled",
            "lifecycle=manual_cancelled; final_result=manual_cancelled; failure_scope=client_cancelled; "
            f"cooldown_applied=false; cancel_source=dashboard; lock_released={str(lock_released).lower()}",
            group=group, request_id=request_id, attempt=attempt, event="request_cancelled",
            cooldown_applied=False, failure_scope="client_cancelled",
        )
        self.observability.finish_live_request(request_id, "manual_cancelled")

    def _record_explicit_qualified_failure(
        self,
        state: CandidateStatePort,
        candidate: UpstreamCandidate,
        group: Any,
        error: str,
        reason: str,
        *,
        auto_fallback: bool,
    ) -> bool:
        """Feed explicit-model upstream failures into the existing breaker state."""
        if auto_fallback or candidate.aggregate_member_id or candidate.idx is None:
            return False
        return state.record_qualified_failure(
            candidate.idx,
            error,
            self.policy.auto_cooldown_seconds(group),
            reason,
        )

    def _record_stream_terminal_failure(
        self,
        state: CandidateStatePort,
        candidate: UpstreamCandidate,
        group: Any,
        aggregate_model: Optional[AggregateModel],
        error: str,
        reason: str,
        *,
        auto_fallback: bool,
    ) -> bool:
        """Apply the existing route-specific health policy after a started stream fails."""
        if candidate.aggregate_member_id and aggregate_model:
            state.set_aggregate_member_cooldown(
                candidate.aggregate_member_id,
                error,
                state.aggregate_cooldown_seconds(aggregate_model),
                reason,
            )
            # 聚合成员失败同时代表一次底层真实模型的上游失败；成员级策略
            # 可以被聚合开关关闭，但底层模型仍必须按所属连接组策略保护。
            if candidate.idx is not None:
                state.set_cooldown(
                    candidate.idx,
                    error,
                    self.policy.auto_cooldown_seconds(group),
                    reason,
                )
            return True
        if auto_fallback:
            if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                state.set_cooldown(
                    candidate.idx,
                    error,
                    self.policy.auto_cooldown_seconds(group),
                    reason,
                )
                return True
            if candidate.idx is not None:
                state.set_unusable(candidate.idx, error)
                return True
            return False
        return self._record_explicit_qualified_failure(
            state,
            candidate,
            group,
            error,
            reason,
            auto_fallback=auto_fallback,
        )

    def execute_non_stream(
        self,
        path: str,
        payload: Dict[str, Any],
        route: Any = None,
        incoming_headers: Optional[Dict[str, str]] = None,
        raw_body: bytes | None = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Execute the frozen non-stream candidate/request/fallback chain via the router facade."""
        state = self.candidate_state
        state.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = state.route_group_id(route)
        is_route_context = hasattr(route, "group") and hasattr(route, "is_deprecated_global")
        route_group = route.group if is_route_context else state.find_group(group_id) if group_id else None
        is_deprecated_global = bool(route.is_deprecated_global) if is_route_context else False
        if is_deprecated_global:
            return 403, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key", "type": "global_key_deprecated", "code": "use_group_or_aggregate_key"}}, ensure_ascii=False).encode("utf-8")
        route_aggregate = route.aggregate if is_route_context else None
        is_global = bool(route.is_global) if is_route_context else False
        auto_mode = self.policy.is_auto_model(str(requested_model) if requested_model else None, route_group)
        # auto_fallback：组级 auto 或聚合模型下，失败时尝试下一个候选（全局 Key 已退役）
        auto_fallback = auto_mode or bool(route_aggregate)
        # Contract failures are rejected before a live-request record is created.
        aggregate_info = state.resolve_aggregate(str(requested_model) if requested_model else None, route)
        if not aggregate_info and route_group and requested_model and not state.supports_requested_model(str(requested_model), route_group):
            raise self.faults.all_models_failed(
                "当前连接组未配置该模型，请检查模型配置或使用已配置模型",
                attempted=0,
                error_code="model_not_found",
            )
        request_id = uuid.uuid4().hex[:12]
        self.observability.start_live_request(request_id, path, requested_label, stream=False)
        attempt = 0
        last_error: Optional[Exception] = None
        saw_cooldown = False
        saw_request_level = False

        # 聚合模型已在创建 live request 前完成契约解析。
        aggregate_model: Optional[AggregateModel] = None
        resolved_as = ""
        fallback_index = 0
        fallback_chain: List[Dict[str, Any]] = []
        if aggregate_info:
            aggregate_model, resolved_as = aggregate_info
            auto_fallback = True
            candidates_iter: Iterator[UpstreamCandidate] = state.iter_aggregate_candidates(aggregate_model, log_skips=True, path=path, requested_label=requested_label, request_id=request_id, resolved_as=resolved_as)
        else:
            candidates_iter = state.iter_upstream_candidates(str(requested_model) if requested_model else None, group_id)

        for candidate in candidates_iter:
            if self.observability.cancellation_requested(request_id):
                self._finalize_cancelled(request_id, path, requested_label, group=candidate.group, candidate=candidate, attempt=attempt)
                return 499, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
            attempt += 1
            group = candidate.group
            target_url = self.preparation.resolve_url(group.base_url, path)
            self.observability.update_live_request(
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
                aggregate_suffix = self.preparation.aggregate_log_suffix(
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
                self.observability.add_log(path, candidate.label, "skip", skip_detail, group=group, request_id=request_id, attempt=attempt, event="skip")
                state.release_probe(candidate)
                continue
            payload_for_upstream = payload
            tools_normalized = False
            if self.preparation.tools_order_enabled():
                payload_for_upstream, tools_normalized = self.preparation.normalize_tools_order(payload)
            body, body_mode = self.preparation.body_for_upstream(payload_for_upstream, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = self.preparation.headers_for(group, candidate.auth_key, incoming_headers, stream=False)
            upstream_lock = self.concurrency.candidate_lock(candidate, incoming_headers)
            started_at = time.perf_counter()
            if upstream_lock:
                self.observability.update_live_request(request_id, stage="waiting_serial_protection", stage_label="等待串行保护")
            acquired, lock_wait_ms = self.concurrency.acquire(upstream_lock, request_id=request_id)
            if not acquired:
                # 未取得串行保护锁不会实际执行探测；立即归还半开租约。
                state.release_probe(candidate)
                if self.observability.cancellation_requested(request_id):
                    self._finalize_cancelled(
                        request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt,
                    )
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                self.observability.update_live_request(request_id, stage="candidate_busy", stage_label="候选忙/串行保护等待超时", possible_reason="该连接组已开启串行保护，候选仍在处理请求，已临时切换")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self.observability.add_log(
                    path,
                    candidate.label,
                    "timeout",
                    self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, self.concurrency.busy_detail(candidate, body, lock_wait_ms), lock_wait_ms=lock_wait_ms),
                    duration_ms,
                    group=group,
                    request_id=request_id,
                    attempt=attempt,
                    event="serial_protection_timeout",
                    cooldown_applied=False,
                    failure_scope="busy",
                )
                if auto_fallback:
                    continue
                self.observability.finish_live_request(request_id, "error")
                return 503, {"Content-Type": "application/json; charset=utf-8"}, [json.dumps({"error": {"message": "该连接组已开启串行保护，候选仍在处理请求", "type": "candidate_busy", "code": "serial_protection_wait_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")]
            if self.observability.cancellation_requested(request_id):
                lock_released = self.concurrency.release(upstream_lock)
                self._finalize_cancelled(
                    request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt,
                    lock_released=lock_released,
                )
                return 499, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
            try:
                self.observability.update_live_request(request_id, stage="connecting_upstream", stage_label="连接上游")
                resp = self.upstream.request("POST", target_url, outbound_headers, body, stream=False, timeout=120)
                self.observability.set_live_response(request_id, resp)
                if self.observability.cancellation_requested(request_id):
                    self.observability.close_live_response(request_id, resp)
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(
                        request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt,
                        lock_released=lock_released,
                    )
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                with resp:
                    self.observability.update_live_request(request_id, stage="receiving_response", stage_label="接收响应")
                    data = resp.read()
                    if self.observability.cancellation_requested(request_id):
                        self.observability.close_live_response(request_id, resp)
                        lock_released = self.concurrency.release(upstream_lock)
                        self._finalize_cancelled(
                            request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt,
                            lock_released=lock_released,
                        )
                        return 499, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = self.stream_lifecycle.usage_from_response(data)
                    state.mark_success(candidate)
                    if candidate.aggregate_member_id:
                        state.mark_aggregate_member_success(candidate.aggregate_member_id)
                    self.observability.add_log(
                        path,
                        candidate.label,
                        str(resp.status),
                        self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "ok", resp=resp, tools_normalized=tools_normalized, lock_wait_ms=lock_wait_ms, lock_release_reason="response_inline", aggregate_suffix=aggregate_suffix),
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
                            fingerprint=self.preparation.payload_fingerprint(payload_for_upstream, body, urlparse(target_url).path, tools_normalized=tools_normalized),
                            request_id=request_id,
                            usage_source="response_inline",
                        )
                    except Exception:
                        pass
                    self.observability.finish_live_request(request_id, "done")
                    return resp.status, _response_headers_for_downstream(resp), data
            except HTTPError as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                classification = self.policy.classify_candidate_error(err.code, raw, "http")
                cooldown_applied = classification.should_cooldown
                is_request_level = classification.is_request_level
                if cooldown_applied:
                    saw_cooldown = True
                if is_request_level:
                    saw_request_level = True

                # 聚合成员失败：仅冷却类错误才写入 cooldown
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    if cooldown_applied:
                        cooldown_seconds = state.aggregate_cooldown_seconds(aggregate_model)
                        state.set_aggregate_member_cooldown(candidate.aggregate_member_id, raw or str(err), cooldown_seconds, classification.log_reason)
                        if candidate.idx is not None:
                            state.set_cooldown(
                                candidate.idx,
                                raw or str(err),
                                self.policy.auto_cooldown_seconds(group),
                                classification.log_reason,
                            )
                    failure_scope = classification.failure_scope
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": err.code,
                        "reason": self.preparation.short_error(raw),
                        "cooldown_applied": cooldown_applied,
                        "failure_scope": failure_scope,
                        "category": classification.category,
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification.log_reason}; try next; error={self.preparation.short_error(raw)}{self.policy.waf_blocked_suffix(classification, group)}"
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 429 立即重试一次（非聚合路径保持原有行为）
                if classification.category == "rate_limit" and not is_aggregate_candidate:
                    try:
                        retry_started_at = time.perf_counter()
                        with self.upstream.request("POST", target_url, outbound_headers, body, stream=False, timeout=120) as resp:
                            data = resp.read()
                            retry_duration_ms = int((time.perf_counter() - retry_started_at) * 1000)
                            prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = self.stream_lifecycle.usage_from_response(data)
                            state.mark_success(candidate)
                            self.observability.add_log(
                                path,
                                candidate.label,
                                str(resp.status),
                                self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "retry ok", resp=resp, lock_wait_ms=lock_wait_ms, lock_release_reason="retry_ok"),
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
                            self.observability.finish_live_request(request_id, "done")
                            return resp.status, _response_headers_for_downstream(resp), data
                    except Exception as retry_err:
                        last_error = retry_err
                        retry_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        detail = f"retry_failed; error={self.preparation.short_error(str(retry_err))}"
                        self.observability.add_log(path, candidate.label, "retry failed", self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="retry_failed"), retry_duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=False)

                # 自动 fallback（组级 auto 或聚合模型）
                if auto_fallback:
                    if cooldown_applied:
                        if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                            state.set_cooldown(candidate.idx, raw or str(err), self.policy.auto_cooldown_seconds(group), classification.log_reason)
                        elif candidate.idx is not None:
                            state.set_unusable(candidate.idx, raw or str(err))
                        saw_cooldown = True
                    failure_scope = classification.failure_scope
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": err.code,
                            "reason": self.preparation.short_error(raw),
                            "cooldown_applied": cooldown_applied,
                            "failure_scope": failure_scope,
                            "category": classification.category,
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification.log_reason}; try next; error={self.preparation.short_error(raw)}{self.policy.waf_blocked_suffix(classification, group)}"
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 非自动 fallback：保留原有显式模型处理逻辑
                # 该请求已实际发往上游，且未被即时重试恢复。显式模型也要把
                # 合格的上游故障写入同一 breaker 状态机，但保持原有响应路径。
                explicit_failure_recorded = False
                if cooldown_applied:
                    explicit_failure_recorded = self._record_explicit_qualified_failure(
                        state,
                        candidate,
                        group,
                        raw or str(err),
                        classification.log_reason,
                        auto_fallback=auto_fallback,
                    )
                if classification.category == "quota_exhausted":
                    state.mark_unusable(candidate, raw)
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "quota exhausted, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=explicit_failure_recorded, failure_scope="upstream")
                    continue
                if classification.category == "server_error":
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "server error, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=explicit_failure_recorded, failure_scope="upstream")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={self.preparation.short_error(raw)}"
                self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=explicit_failure_recorded)
                self.observability.finish_live_request(request_id, "error")
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                if self.observability.cancellation_requested(request_id):
                    self.observability.close_live_response(request_id)
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(
                        request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt,
                        lock_released=lock_released,
                    )
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                classification = self.policy.classify_candidate_error(None, str(err), "network")
                saw_cooldown = True

                # 聚合成员网络失败：cooldown 聚合成员本身并记录 fallback 链路
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = state.aggregate_cooldown_seconds(aggregate_model)
                    state.set_aggregate_member_cooldown(candidate.aggregate_member_id, str(err), cooldown_seconds, classification.log_reason)
                    if candidate.idx is not None:
                        state.set_cooldown(
                            candidate.idx,
                            str(err),
                            self.policy.auto_cooldown_seconds(group),
                            classification.log_reason,
                        )
                    failure_scope = classification.failure_scope
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": "network",
                        "reason": self.preparation.short_error(str(err)),
                        "cooldown_applied": True,
                        "failure_scope": failure_scope,
                        "category": classification.category,
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification.log_reason}; try next; error={self.preparation.short_error(str(err))}"
                    self.observability.add_log(path, candidate.label, "network", self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                if auto_fallback:
                    if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                        state.set_cooldown(candidate.idx, str(err), self.policy.auto_cooldown_seconds(group), classification.log_reason)
                    elif candidate.idx is not None:
                        state.set_unusable(candidate.idx, str(err))
                    failure_scope = classification.failure_scope
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": "network",
                            "reason": self.preparation.short_error(str(err)),
                            "cooldown_applied": True,
                            "failure_scope": failure_scope,
                            "category": classification.category,
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification.log_reason}; try next; error={self.preparation.short_error(str(err))}"
                    self.observability.add_log(path, candidate.label, "network", self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                failure_scope = classification.failure_scope
                explicit_failure_recorded = self._record_explicit_qualified_failure(
                    state,
                    candidate,
                    group,
                    str(err),
                    classification.log_reason,
                    auto_fallback=auto_fallback,
                )
                detail = f"cooldown_applied={str(explicit_failure_recorded).lower()}; failure_scope={failure_scope}; {classification.log_reason}; error={self.preparation.short_error(str(err))}"
                self.observability.add_log(path, candidate.label, "network", self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=explicit_failure_recorded, failure_scope=failure_scope)
                continue
            finally:
                self.concurrency.release(upstream_lock)
                state.release_probe(candidate)

        if aggregate_model:
            self.observability.finish_live_request(request_id, "error")
            if not saw_cooldown and saw_request_level:
                raise self.faults.all_models_failed(
                    f"聚合模型 {aggregate_model.name} 的所有成员均因请求级错误被拒绝{self.policy.waf_blocked_hint(fallback_chain)}",
                    attempted=attempt,
                    error_code="upstream_request_rejected",
                    fallback_chain=fallback_chain,
                    aggregate_name=aggregate_model.name,
                )
            raise self.faults.all_models_failed(
                f"聚合模型 {aggregate_model.name} 的所有成员均不可用",
                attempted=attempt,
                error_code="aggregate_members_unavailable",
                fallback_chain=fallback_chain,
                aggregate_name=aggregate_model.name,
            )
        self.observability.finish_live_request(request_id, "error")
        if last_error is None:
            raise self.faults.all_models_failed("没有可用模型", attempted=attempt, error_code="no_usable_models")
        if not saw_cooldown and saw_request_level:
            raise self.faults.all_models_failed(

                f"所有候选均因请求级错误被拒绝{self.policy.waf_blocked_hint(fallback_chain)}",
                attempted=attempt,
                error_code="upstream_request_rejected",
            )
        raise self.faults.all_models_failed(
            f"所有可用模型均请求失败，共尝试 {attempt} 个上游",
            attempted=attempt,
            error_code="all_models_failed",
        ) from last_error

    def execute_stream(self, path: str, payload: Dict[str, Any], route: Any = None, incoming_headers: Optional[Dict[str, str]] = None, raw_body: bytes | None = None) -> Any:
        """Execute the frozen stream candidate/request/fallback chain via explicit ports."""
        stream_started_at = time.perf_counter()
        stream_started_at_ms = int(time.time() * 1000)
        state = self.candidate_state
        state.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = state.route_group_id(route)
        route_group = route.group if isinstance(route, self.faults.route_context) else state.find_group(group_id) if group_id else None
        is_deprecated_global = isinstance(route, self.faults.route_context) and route.is_deprecated_global
        if is_deprecated_global:
            def deprecated_iter():
                yield json.dumps({"error": {"message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key", "type": "global_key_deprecated", "code": "use_group_or_aggregate_key"}}, ensure_ascii=False).encode("utf-8")
            return 403, {"Content-Type": "application/json; charset=utf-8"}, deprecated_iter(), ""
        route_aggregate = route.aggregate if isinstance(route, self.faults.route_context) else None
        is_global = isinstance(route, self.faults.route_context) and route.is_global
        auto_mode = self.policy.is_auto_model(str(requested_model) if requested_model else None, route_group)
        auto_fallback = auto_mode or bool(route_aggregate)
        # Contract failures are rejected before a live-request record is created.
        aggregate_info = state.resolve_aggregate(str(requested_model) if requested_model else None, route)
        if not aggregate_info and route_group and requested_model and not state.supports_requested_model(str(requested_model), route_group):
            raise self.faults.all_models_failed(
                "当前连接组未配置该模型，请检查模型配置或使用已配置模型",
                attempted=0,
                error_code="model_not_found",
            )
        request_id = uuid.uuid4().hex[:12]
        self.observability.start_live_request(request_id, path, requested_label, stream=True)
        attempt = 0
        last_error: Optional[Exception] = None
        saw_stream_timeout = False
        saw_cooldown = False
        saw_request_level = False

        # 聚合模型已在创建 live request 前完成契约解析。
        aggregate_model: Optional[AggregateModel] = None
        resolved_as = ""
        fallback_index = 0
        fallback_chain: List[Dict[str, Any]] = []
        if aggregate_info:
            aggregate_model, resolved_as = aggregate_info
            auto_fallback = True
            candidates_iter = state.iter_aggregate_candidates(aggregate_model, log_skips=True, path=path, requested_label=requested_label, request_id=request_id, resolved_as=resolved_as)
        else:
            candidates_iter = state.iter_upstream_candidates(str(requested_model) if requested_model else None, group_id)

        for candidate in candidates_iter:
            if self.observability.cancellation_requested(request_id):
                self._finalize_cancelled(request_id, path, requested_label, group=candidate.group, candidate=candidate, attempt=attempt)
                error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
            attempt += 1
            group = candidate.group
            candidate_selected_ms = _elapsed_ms(stream_started_at)
            stream_metrics: Dict[str, Any] = {
                "candidate_selected_ms": candidate_selected_ms,
                "upstream_request_started_ms": -1,
                "upstream_headers_ms": -1,
                "first_raw_line_ms": -1,
                "first_complete_frame_ms": -1,
                "first_content_delta_ms": -1,
                "first_downstream_flush_ms": -1,
                "initial_frame_bytes": 0,
                "stream_frame_count": 0,
                "upstream_content_type": "-",
                "upstream_content_encoding": "-",
                "upstream_transfer_encoding": "-",
                "upstream_http_version": "unknown",
                "upstream_transport": "unknown",
                "stream_wire_mode": "unknown",
            }
            target_url = self.preparation.resolve_url(group.base_url, path)
            self.observability.update_live_request(
                request_id,
                stage="preparing_upstream",
                stage_label="准备上游流式请求",
                group=group.name,
                candidate=candidate.label,
                model=candidate.label,
                aggregate_model=aggregate_model.name if aggregate_model else "",
                attempt=attempt,
            )
            idle_timeout = self.stream_lifecycle.idle_timeout_seconds(group)
            is_aggregate_candidate = bool(candidate.aggregate_member_id)
            selection_reason = "priority_first" if fallback_index == 0 else "fallback_after_failure"
            aggregate_suffix = ""
            if is_aggregate_candidate and aggregate_model:
                model_name = candidate.model.name if candidate.model else ""
                aggregate_suffix = self.preparation.aggregate_log_suffix(
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
                self.observability.add_log(path, candidate.label, "skip", skip_detail, group=group, request_id=request_id, attempt=attempt, event="skip")
                state.release_probe(candidate)
                continue
            payload_for_upstream = payload
            tools_normalized = False
            if self.preparation.tools_order_enabled():
                payload_for_upstream, tools_normalized = self.preparation.normalize_tools_order(payload)
            body, body_mode = self.preparation.body_for_upstream(payload_for_upstream, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = self.preparation.headers_for(group, candidate.auth_key, incoming_headers, stream=True)
            upstream_lock = self.concurrency.candidate_lock(candidate, incoming_headers)
            resp: Optional[Any] = None
            started_at = time.perf_counter()
            if upstream_lock:
                self.observability.update_live_request(request_id, stage="waiting_serial_protection", stage_label="等待串行保护")
            acquired, lock_wait_ms = self.concurrency.acquire(upstream_lock, request_id=request_id)
            if not acquired:
                # 未取得串行保护锁不会实际执行探测；立即归还半开租约。
                state.release_probe(candidate)
                if self.observability.cancellation_requested(request_id):
                    self._finalize_cancelled(request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt)
                    error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                self.observability.update_live_request(request_id, stage="candidate_busy", stage_label="候选忙/串行保护等待超时", possible_reason="该连接组已开启串行保护，候选仍在处理请求，已临时切换")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self.observability.add_log(
                    path,
                    candidate.label,
                    "timeout",
                    self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, self.concurrency.busy_detail(candidate, body, lock_wait_ms), lock_wait_ms=lock_wait_ms, aggregate_suffix=aggregate_suffix),
                    duration_ms,
                    group=group,
                    request_id=request_id,
                    attempt=attempt,
                    event="serial_protection_timeout",
                    cooldown_applied=False,
                    failure_scope="busy",
                )
                if auto_fallback:
                    continue
                error_body = json.dumps({"error": {"message": "该连接组已开启串行保护，候选仍在处理请求", "type": "candidate_busy", "code": "serial_protection_wait_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                self.observability.finish_live_request(request_id, "error")
                return 503, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
            try:
                if self.observability.cancellation_requested(request_id):
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt, lock_released=lock_released)
                    error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                self.observability.update_live_request(request_id, stage="connecting_upstream", stage_label="连接上游")
                upstream_request_started_ms = _elapsed_ms(stream_started_at)
                stream_metrics["upstream_request_started_ms"] = upstream_request_started_ms
                resp = self.upstream.request("POST", target_url, outbound_headers, body, stream=True, timeout=120)
                upstream_headers_ms = _elapsed_ms(stream_started_at)
                stream_metrics["upstream_headers_ms"] = upstream_headers_ms
                self.observability.set_live_response(request_id, resp)
                if self.observability.cancellation_requested(request_id):
                    self.observability.close_live_response(request_id, resp)
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt, lock_released=lock_released)
                    error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                upstream_content_type = _safe_media_type(_header_value(resp.headers, "content-type"))
                upstream_content_encoding = _safe_header_token(_header_value(resp.headers, "content-encoding"))
                upstream_transfer_encoding = _safe_header_token(_header_value(resp.headers, "transfer-encoding"))
                upstream_http_version = _safe_http_version(getattr(resp, "http_version", ""))
                upstream_transport = _safe_header_token(getattr(resp, "transport", "unknown"))
                stream_metrics.update({
                    "upstream_content_type": upstream_content_type,
                    "upstream_content_encoding": upstream_content_encoding,
                    "upstream_transfer_encoding": upstream_transfer_encoding,
                    "upstream_http_version": upstream_http_version,
                    "upstream_transport": upstream_transport,
                })

                def mark_first_raw_line(_line: bytes) -> None:
                    if stream_metrics["first_raw_line_ms"] < 0:
                        stream_metrics["first_raw_line_ms"] = _elapsed_ms(stream_started_at)

                if bool(getattr(resp, "opaque_stream", False)):
                    # Unknown encodings cannot be parsed as SSE safely.  Keep
                    # their bytes opaque and let the downstream client decode
                    # them, while still closing and accounting for the stream.
                    self.observability.update_live_request(request_id, stage="waiting_first_byte", stage_label="等待未知编码流首块")
                    first_opaque_chunk = resp.read_chunk(idle_timeout)
                    if self.observability.cancellation_requested(request_id):
                        self.observability.close_live_response(request_id, resp)
                        lock_released = self.concurrency.release(upstream_lock)
                        self._finalize_cancelled(request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt, lock_released=lock_released)
                        error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                        return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                    if not first_opaque_chunk:
                        raise URLError("upstream opaque stream closed before first chunk")
                    stream_metrics.update({
                        "stream_wire_mode": "buffered_or_non_delimited",
                        "opaque_first_chunk_ms": _elapsed_ms(stream_started_at),
                        "opaque_first_chunk_bytes": len(first_opaque_chunk),
                        "opaque_chunk_count": 1,
                        "opaque_bytes_received": len(first_opaque_chunk),
                    })
                    self.observability.add_log(
                        path,
                        candidate.label,
                        "streaming",
                        self.preparation.debug_detail(
                            candidate,
                            requested_label,
                            target_url,
                            body_mode,
                            body,
                            payload_for_upstream,
                            outbound_headers,
                            "stream ok; opaque_encoding_passthrough=true; first_byte_metric=opaque_chunk; " + _stream_metrics_text(stream_metrics),
                            resp=resp,
                            tools_normalized=tools_normalized,
                            lock_wait_ms=lock_wait_ms,
                            aggregate_suffix=aggregate_suffix,
                        ),
                        int((time.perf_counter() - started_at) * 1000),
                        group=group,
                        request_id=request_id,
                        attempt=attempt,
                        event="stream_ok",
                    )
                    self.observability.update_live_request(request_id, stage="streaming", stage_label="透传未知编码流")
                    self.concurrency.mark_stream_active(candidate, 1)
                    opaque_chunks = 1
                    opaque_bytes = len(first_opaque_chunk)
                    opaque_state: Dict[str, Any] = {"timeout": False, "transport_error": "", "lifecycle": "", "completion_signal": ""}
                    opaque_release_reason = "client_disconnect"
                    opaque_finalized = False
                    opaque_usage = (0, 0, 0, 0, 0)

                    def finalize_opaque_stream() -> None:
                        nonlocal opaque_finalized
                        if opaque_finalized:
                            return
                        opaque_finalized = True
                        self.observability.close_live_response(request_id, resp)
                        final_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        failure_recorded = False
                        if self.observability.cancellation_requested(request_id):
                            lifecycle_status = "cancelled"
                            lifecycle_result = "manual_cancelled"
                            lifecycle_scope = "client_cancelled"
                            usage_source = "stream_incomplete"
                        elif self.observability.downstream_write_failed(request_id):
                            lifecycle_status = "downstream_disconnected"
                            lifecycle_result = "downstream_write_failed"
                            lifecycle_scope = "downstream"
                            usage_source = "stream_incomplete"
                        elif opaque_state["timeout"]:
                            lifecycle_status = "timeout"
                            lifecycle_result = "stream_idle_timeout"
                            lifecycle_scope = "upstream"
                            usage_source = "stream_incomplete"
                            failure_recorded = self._record_stream_terminal_failure(state, candidate, group, aggregate_model, lifecycle_result, lifecycle_result, auto_fallback=auto_fallback)
                        elif opaque_state["transport_error"]:
                            lifecycle_status = "network"
                            lifecycle_result = "stream_incomplete"
                            lifecycle_scope = "upstream"
                            usage_source = "stream_incomplete"
                            failure_recorded = self._record_stream_terminal_failure(state, candidate, group, aggregate_model, str(opaque_state["transport_error"]), "network", auto_fallback=auto_fallback)
                        else:
                            lifecycle_status = "200"
                            lifecycle_result = "stream_done"
                            lifecycle_scope = ""
                            usage_source = "missing"
                            state.mark_success(candidate)
                            if candidate.aggregate_member_id:
                                state.mark_aggregate_member_success(candidate.aggregate_member_id)
                        stream_metrics["opaque_chunk_count"] = opaque_chunks
                        stream_metrics["opaque_bytes_received"] = opaque_bytes
                        stream_metrics["stream_frame_count"] = 0
                        self.observability.patch_stream_lifecycle(
                            request_id,
                            attempt,
                            candidate.label,
                            opaque_usage,
                            usage_source,
                            final_status=lifecycle_status,
                            lifecycle=lifecycle_result,
                            final_result=lifecycle_result,
                            chunks_received=0,
                            bytes_received=opaque_bytes,
                            duration_ms=final_duration_ms,
                            lock_wait_ms=lock_wait_ms,
                            lock_release_reason=opaque_release_reason,
                            failure_scope=lifecycle_scope,
                            completion_signal=str(opaque_state["completion_signal"]),
                            cooldown_applied=(False if lifecycle_result == "manual_cancelled" else failure_recorded if lifecycle_scope == "upstream" else None),
                            stream_metrics=stream_metrics,
                        )
                        self.observability.finish_live_request(request_id, "done" if lifecycle_result == "stream_done" else ("manual_cancelled" if lifecycle_result == "manual_cancelled" else "ended"))
                        self.concurrency.mark_stream_active(candidate, -1)
                        self.concurrency.release(upstream_lock)
                        state.release_probe(candidate)

                    def opaque_iterator() -> Iterator[bytes]:
                        nonlocal opaque_chunks, opaque_bytes, opaque_release_reason
                        try:
                            yield first_opaque_chunk
                            while True:
                                try:
                                    chunk = resp.read_chunk(idle_timeout)
                                except self.faults.stream_idle_timeout:
                                    opaque_state["timeout"] = True
                                    opaque_release_reason = "stream_idle_timeout"
                                    break
                                except (URLError, TimeoutError, OSError) as err:
                                    if not self.observability.cancellation_requested(request_id):
                                        opaque_state["transport_error"] = str(err)
                                        opaque_state["completion_signal"] = "network_error"
                                        opaque_release_reason = "network_error"
                                    break
                                if not chunk:
                                    opaque_state["lifecycle"] = "stream_done"
                                    opaque_state["completion_signal"] = "eof"
                                    opaque_release_reason = "eof"
                                    break
                                opaque_chunks += 1
                                opaque_bytes += len(chunk)
                                yield chunk
                        finally:
                            finalize_opaque_stream()

                    downstream_headers = _response_headers_for_downstream(resp)
                    return 200, downstream_headers, ManagedStreamIterator(opaque_iterator(), finalize_opaque_stream), request_id

                self.observability.update_live_request(request_id, stage="waiting_first_byte", stage_label="等待首完整 SSE 帧")
                first_chunk = _read_sse_frame(
                    lambda timeout: self.stream_lifecycle.readline_with_idle_timeout(resp, timeout),
                    idle_timeout,
                    mark_first_raw_line,
                )
                if self.observability.cancellation_requested(request_id):
                    self.observability.close_live_response(request_id, resp)
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(request_id, path, requested_label, group=group, candidate=candidate, attempt=attempt, lock_released=lock_released)
                    error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                if not first_chunk:
                    raise URLError("upstream stream closed before first chunk")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                stream_metrics["first_complete_frame_ms"] = _elapsed_ms(stream_started_at)
                stream_metrics["initial_frame_bytes"] = len(first_chunk)
                stream_metrics["stream_frame_count"] = 1
                saw_non_delimited_frame = not _frame_is_delimited(first_chunk)
                if _frame_is_delimited(first_chunk) and _frame_has_text_delta(first_chunk):
                    stream_metrics["first_content_delta_ms"] = stream_metrics["first_complete_frame_ms"]
                stream_metrics["stream_wire_mode"] = _stream_wire_mode(
                    upstream_content_type,
                    upstream_content_encoding,
                    saw_non_delimited_frame=saw_non_delimited_frame,
                    stream_frame_count=1,
                    initial_frame_bytes=len(first_chunk),
                )
                latest_usage, usage_present = self.stream_lifecycle.usage_from_stream_chunk_with_presence(first_chunk)
                detail = self.preparation.debug_detail(
                    candidate,
                    requested_label,
                    target_url,
                    body_mode,
                    body,
                    payload_for_upstream,
                    outbound_headers,
                    f"stream ok; first_byte_ms={stream_metrics['first_complete_frame_ms']}; first_byte_metric=complete_sse_frame_legacy; "
                    f"stream_started_at_ms={stream_started_at_ms}; idle_timeout_seconds={idle_timeout}; "
                    f"initial_chunks_received=1; initial_bytes_received={len(first_chunk)}; chunks_received=1; "
                    f"bytes_received={len(first_chunk)}; final_result=streaming; "
                    + _stream_metrics_text(stream_metrics),
                    resp=resp,
                    tools_normalized=tools_normalized,
                    lock_wait_ms=lock_wait_ms,
                    aggregate_suffix=aggregate_suffix,
                )
                self.observability.add_log(path, candidate.label, "streaming", detail, duration_ms, *latest_usage, group=group, request_id=request_id, attempt=attempt, event="stream_ok")
                self.observability.update_live_request(request_id, stage="streaming", stage_label="接收流式响应")
                self.concurrency.mark_stream_active(candidate, 1)
                try:
                    self.debug_capture.capture(
                        path=path,
                        group=group,
                        model=candidate.label,
                        target_model=candidate.target_model,
                        body=body,
                        body_mode=body_mode,
                        headers=outbound_headers,
                        fingerprint=self.preparation.payload_fingerprint(payload_for_upstream, body, urlparse(target_url).path, tools_normalized=tools_normalized),
                        request_id=request_id,
                        usage_source="",
                    )
                except Exception:
                    pass

                usage_total = latest_usage
                chunks_received = 1
                bytes_received = len(first_chunk)
                stream_state: Dict[str, Any] = {"timeout": False, "transport_error": "", "lifecycle": "", "completion_signal": ""}
                release_reason = "client_disconnect"
                finalized = False

                def terminal_signal_for(chunk: bytes) -> str:
                    signal = self.stream_lifecycle.completion_signal(chunk)
                    if signal.startswith("event:"):
                        return signal.split(":", 1)[1]
                    return signal

                def mark_stream_terminal(signal: str) -> None:
                    if signal in {"response.failed", "response.incomplete"}:
                        stream_state["lifecycle"] = "stream_failed" if signal == "response.failed" else "stream_incomplete"
                    else:
                        stream_state["lifecycle"] = "stream_done"
                    stream_state["completion_signal"] = signal

                first_completion_signal = terminal_signal_for(first_chunk)
                if first_completion_signal:
                    mark_stream_terminal(first_completion_signal)
                    release_reason = first_completion_signal

                def finalize_stream() -> None:
                    nonlocal finalized
                    if finalized:
                        return
                    finalized = True
                    if resp:
                        self.observability.close_live_response(request_id, resp)
                    final_duration_ms = int((time.perf_counter() - started_at) * 1000)
                    stream_failure_recorded = False
                    if self.observability.cancellation_requested(request_id):
                        usage_source = "stream_incomplete"
                        lifecycle_status = "cancelled"
                        lifecycle_result = "manual_cancelled"
                        lifecycle_scope = "client_cancelled"
                    elif self.observability.downstream_write_failed(request_id):
                        usage_source = "stream_incomplete"
                        lifecycle_status = "downstream_disconnected"
                        lifecycle_result = "downstream_write_failed"
                        lifecycle_scope = "downstream"
                    elif stream_state["timeout"]:
                        usage_source = "stream_incomplete"
                        lifecycle_status = "timeout"
                        lifecycle_result = "stream_idle_timeout"
                        lifecycle_scope = "upstream"
                        stream_failure_recorded = self._record_stream_terminal_failure(
                            state,
                            candidate,
                            group,
                            aggregate_model,
                            "stream_idle_timeout",
                            "stream_idle_timeout",
                            auto_fallback=auto_fallback,
                        )
                    elif stream_state["transport_error"]:
                        usage_source = "stream_incomplete"
                        lifecycle_status = "network"
                        lifecycle_result = "stream_incomplete"
                        lifecycle_scope = "upstream"
                        stream_failure_recorded = self._record_stream_terminal_failure(
                            state,
                            candidate,
                            group,
                            aggregate_model,
                            str(stream_state["transport_error"]),
                            "network",
                            auto_fallback=auto_fallback,
                        )
                    elif stream_state["lifecycle"] == "stream_done":
                        usage_source = "stream_final" if usage_present else "missing"
                        lifecycle_status = "200"
                        lifecycle_result = "stream_done"
                        lifecycle_scope = ""
                        state.mark_success(candidate)
                        if candidate.aggregate_member_id:
                            state.mark_aggregate_member_success(candidate.aggregate_member_id)
                    elif stream_state["lifecycle"] in {"stream_failed", "stream_incomplete"}:
                        usage_source = "stream_incomplete"
                        lifecycle_status = str(stream_state["lifecycle"])
                        lifecycle_result = str(stream_state["lifecycle"])
                        lifecycle_scope = "upstream"
                        completion_signal = str(stream_state["completion_signal"] or lifecycle_result)
                        stream_failure_recorded = self._record_stream_terminal_failure(
                            state,
                            candidate,
                            group,
                            aggregate_model,
                            f"{lifecycle_result}: {completion_signal}",
                            completion_signal.replace(".", "_"),
                            auto_fallback=auto_fallback,
                        )
                    else:
                        usage_source = "stream_incomplete"
                        lifecycle_status = "client_disconnected"
                        lifecycle_result = "client_disconnected"
                        lifecycle_scope = "request"
                    stream_metrics["stream_frame_count"] = chunks_received
                    stream_metrics["stream_wire_mode"] = _stream_wire_mode(
                        upstream_content_type,
                        upstream_content_encoding,
                        saw_non_delimited_frame=saw_non_delimited_frame,
                        stream_frame_count=chunks_received,
                        initial_frame_bytes=len(first_chunk),
                    )
                    self.observability.patch_stream_lifecycle(
                        request_id,
                        attempt,
                        candidate.label,
                        usage_total,
                        usage_source,
                        final_status=lifecycle_status,
                        lifecycle=lifecycle_result,
                        final_result=lifecycle_result,
                        chunks_received=chunks_received,
                        bytes_received=bytes_received,
                        duration_ms=final_duration_ms,
                        lock_wait_ms=lock_wait_ms,
                        lock_release_reason=release_reason,
                        failure_scope=lifecycle_scope,
                        completion_signal=str(stream_state["completion_signal"]),
                        cooldown_applied=(
                            False if lifecycle_result == "manual_cancelled"
                            else stream_failure_recorded if lifecycle_scope == "upstream"
                            else None
                        ),
                        final_event="request_cancelled" if lifecycle_result == "manual_cancelled" else ("stream_disconnected_before_completion" if lifecycle_result == "stream_incomplete" and stream_state["completion_signal"] == "missing" else ""),
                        stream_metrics=stream_metrics,
                    )
                    self.observability.finish_live_request(request_id, "done" if stream_state["lifecycle"] == "stream_done" else ("manual_cancelled" if lifecycle_result == "manual_cancelled" else "ended"))
                    self.concurrency.mark_stream_active(candidate, -1)
                    self.concurrency.release(upstream_lock)
                    state.release_probe(candidate)

                def iterator() -> Iterator[bytes]:
                    nonlocal usage_total, usage_present, chunks_received, bytes_received, release_reason, saw_non_delimited_frame
                    try:
                        yield first_chunk
                        if first_completion_signal:
                            return
                        while True:
                            try:
                                chunk = _read_sse_frame(
                                    lambda timeout: self.stream_lifecycle.readline_with_idle_timeout(resp, timeout),
                                    idle_timeout,
                                    mark_first_raw_line,
                                )
                            except self.faults.stream_idle_timeout:
                                stream_state["timeout"] = True
                                release_reason = "stream_idle_timeout"
                                break
                            except (URLError, TimeoutError, OSError) as err:
                                if not self.observability.cancellation_requested(request_id):
                                    stream_state["transport_error"] = str(err)
                                    stream_state["completion_signal"] = "network_error"
                                    release_reason = "network_error"
                                break
                            if not chunk:
                                mark_stream_terminal("eof")
                                release_reason = "eof"
                                break
                            chunks_received += 1
                            bytes_received += len(chunk)
                            stream_metrics["stream_frame_count"] = chunks_received
                            saw_non_delimited_frame = saw_non_delimited_frame or not _frame_is_delimited(chunk)
                            if (
                                stream_metrics["first_content_delta_ms"] < 0
                                and _frame_is_delimited(chunk)
                                and _frame_has_text_delta(chunk)
                            ):
                                stream_metrics["first_content_delta_ms"] = _elapsed_ms(stream_started_at)
                            usage, chunk_usage_present = self.stream_lifecycle.usage_from_stream_chunk_with_presence(chunk)
                            if chunk_usage_present:
                                usage_total = usage
                                usage_present = True
                            completion_signal = terminal_signal_for(chunk)
                            if completion_signal:
                                mark_stream_terminal(completion_signal)
                                release_reason = completion_signal
                            yield chunk
                            if completion_signal:
                                break
                    finally:
                        finalize_stream()

                downstream_headers = _response_headers_for_downstream(resp)
                if stream_metrics["stream_wire_mode"] == "json_compat" and not _frame_is_delimited(first_chunk):
                    downstream_headers.pop("Content-Length", None)
                    downstream_headers.pop("content-length", None)
                    downstream_headers["Content-Length"] = str(len(first_chunk))
                return 200, downstream_headers, ManagedStreamIterator(iterator(), finalize_stream), request_id
            except self.faults.stream_idle_timeout as err:
                if self.observability.cancellation_requested(request_id):
                    if resp:
                        self.observability.close_live_response(request_id, resp)
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(
                        request_id,
                        path,
                        requested_label,
                        group=group,
                        candidate=candidate,
                        attempt=attempt,
                        lock_released=lock_released,
                    )
                    error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                saw_stream_timeout = True
                saw_cooldown = True
                last_error = err
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                if resp:
                    self.observability.close_live_response(request_id, resp)
                self.concurrency.release(upstream_lock)
                state.release_probe(candidate)
                # 聚合成员在首包前 stream 超时：cooldown 聚合成员并继续 fallback
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = state.aggregate_cooldown_seconds(aggregate_model)
                    state.set_aggregate_member_cooldown(candidate.aggregate_member_id, "stream_idle_timeout", cooldown_seconds, "stream_idle_timeout")
                    if candidate.idx is not None:
                        state.set_cooldown(
                            candidate.idx,
                            "stream_idle_timeout",
                            self.policy.auto_cooldown_seconds(group),
                            "stream_idle_timeout",
                        )
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
                    detail = f"cooldown_applied=true; failure_scope=upstream; reason=stream_idle_timeout; idle_timeout_seconds={idle_timeout}; chunks_received=0; bytes_received=0; cooldown_minutes={cooldown_seconds // 60}; fallback_next=true; final_result=timeout; {_stream_metrics_text(stream_metrics)}"
                    self.observability.add_log(path, candidate.label, "timeout", self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="stream_idle_timeout", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="stream_timeout", usage_source="stream_incomplete", cooldown_applied=True, failure_scope="upstream")
                    continue
                cooldown_seconds = self.stream_lifecycle.mark_stream_timeout(candidate, "stream_idle_timeout")
                detail = self.preparation.debug_detail(
                    candidate,
                    requested_label,
                    target_url,
                    body_mode,
                    body,
                    payload,
                    outbound_headers,
                    f"cooldown_applied=true; reason=stream_idle_timeout; idle_timeout_seconds={idle_timeout}; chunks_received=0; bytes_received=0; cooldown_minutes={cooldown_seconds // 60}; fallback_next={str(auto_fallback).lower()}; final_result=timeout; {_stream_metrics_text(stream_metrics)}",
                    lock_wait_ms=lock_wait_ms,
                    lock_release_reason="stream_idle_timeout",
                )
                self.observability.add_log(path, candidate.label, "timeout", detail, duration_ms, group=group, request_id=request_id, attempt=attempt, event="stream_timeout", usage_source="stream_incomplete", cooldown_applied=True, failure_scope="upstream")
                if auto_fallback:
                    self.observability.add_log(path, candidate.label, "fallback", self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "reason=stream_idle_timeout; fallback_next=true", lock_wait_ms=lock_wait_ms), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=True, failure_scope="upstream")
                    continue
                error_body = json.dumps({"error": {"message": "流式响应空闲超时，请稍后重试", "type": "timeout", "code": "stream_idle_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                self.observability.finish_live_request(request_id, "error")
                return 504, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
            except HTTPError as err:
                if resp:
                    self.observability.close_live_response(request_id, resp)
                self.concurrency.release(upstream_lock)
                state.release_probe(candidate)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                if stream_metrics["upstream_request_started_ms"] >= 0 and stream_metrics["upstream_headers_ms"] < 0:
                    stream_metrics["upstream_headers_ms"] = _elapsed_ms(stream_started_at)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                classification = self.policy.classify_candidate_error(err.code, raw, "http")
                cooldown_applied = classification.should_cooldown
                is_request_level = classification.is_request_level
                if cooldown_applied:
                    saw_cooldown = True
                if is_request_level:
                    saw_request_level = True

                # 聚合成员 HTTP 失败：仅冷却类错误才写入 cooldown
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    if cooldown_applied:
                        cooldown_seconds = state.aggregate_cooldown_seconds(aggregate_model)
                        state.set_aggregate_member_cooldown(candidate.aggregate_member_id, raw or str(err), cooldown_seconds, classification.log_reason)
                        if candidate.idx is not None:
                            state.set_cooldown(
                                candidate.idx,
                                raw or str(err),
                                self.policy.auto_cooldown_seconds(group),
                                classification.log_reason,
                            )
                    failure_scope = classification.failure_scope
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": err.code,
                        "reason": self.preparation.short_error(raw),
                        "cooldown_applied": cooldown_applied,
                        "failure_scope": failure_scope,
                        "category": classification.category,
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification.log_reason}; try next; error={self.preparation.short_error(raw)}{self.policy.waf_blocked_suffix(classification, group)}; {_stream_metrics_text(stream_metrics)}"
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 自动 fallback（组级 auto 或聚合模型）
                if auto_fallback:
                    if cooldown_applied:
                        if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                            state.set_cooldown(candidate.idx, raw or str(err), self.policy.auto_cooldown_seconds(group), classification.log_reason)
                        elif candidate.idx is not None:
                            state.set_unusable(candidate.idx, raw or str(err))
                        saw_cooldown = True
                    failure_scope = classification.failure_scope
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": err.code,
                            "reason": self.preparation.short_error(raw),
                            "cooldown_applied": cooldown_applied,
                            "failure_scope": failure_scope,
                            "category": classification.category,
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification.log_reason}; try next; error={self.preparation.short_error(raw)}{self.policy.waf_blocked_suffix(classification, group)}; {_stream_metrics_text(stream_metrics)}"
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 非自动 fallback：保留原有显式模型处理逻辑
                # 该请求已实际发往上游，且未被即时重试恢复。显式模型也要把
                # 合格的上游故障写入同一 breaker 状态机，但保持原有响应路径。
                explicit_failure_recorded = False
                if cooldown_applied:
                    explicit_failure_recorded = self._record_explicit_qualified_failure(
                        state,
                        candidate,
                        group,
                        raw or str(err),
                        classification.log_reason,
                        auto_fallback=auto_fallback,
                    )
                if classification.category == "quota_exhausted":
                    state.mark_unusable(candidate, raw)
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "quota exhausted, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=explicit_failure_recorded, failure_scope="upstream")
                    continue
                if classification.category == "server_error":
                    self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "server error, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=explicit_failure_recorded, failure_scope="upstream")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={self.preparation.short_error(raw)}; {_stream_metrics_text(stream_metrics)}"
                self.observability.add_log(path, candidate.label, str(err.code), self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=explicit_failure_recorded)
                self.observability.finish_live_request(request_id, "error")
                return err.code, headers, [raw.encode("utf-8")], request_id
            except (URLError, TimeoutError, OSError) as err:
                # Dashboard cancellation closes the registered response to interrupt a
                # blocked first-byte read.  That close commonly surfaces here as a
                # transport error, but it is request-local cancellation, not upstream
                # health evidence and must not enter fallback/cooldown handling.
                if self.observability.cancellation_requested(request_id):
                    if resp:
                        self.observability.close_live_response(request_id, resp)
                    lock_released = self.concurrency.release(upstream_lock)
                    self._finalize_cancelled(
                        request_id,
                        path,
                        requested_label,
                        group=group,
                        candidate=candidate,
                        attempt=attempt,
                        lock_released=lock_released,
                    )
                    error_body = json.dumps({"error": {"message": "请求已由用户终止", "type": "request_cancelled", "code": "manual_cancelled", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
                    return 499, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
                if resp:
                    self.observability.close_live_response(request_id, resp)
                self.concurrency.release(upstream_lock)
                state.release_probe(candidate)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                classification = self.policy.classify_candidate_error(None, str(err), "network")
                is_stream_protocol_error = isinstance(err, StreamFrameProtocolError)
                failure_reason = "stream_protocol_error" if is_stream_protocol_error else classification.log_reason
                lock_release_reason = "stream_protocol_error" if is_stream_protocol_error else "network_error"
                log_status = "protocol" if is_stream_protocol_error else "network"
                log_event = "stream_protocol_error" if is_stream_protocol_error else "network"
                if is_stream_protocol_error:
                    stream_metrics.update({
                        "initial_frame_bytes": err.frame_bytes,
                        "stream_wire_mode": "buffered_or_non_delimited",
                        "stream_protocol_error": "true",
                        "stream_protocol_reason": err.reason,
                        "stream_protocol_wait_ms": err.frame_wait_ms,
                    })
                saw_cooldown = True

                # 聚合成员网络失败：cooldown 聚合成员本身并记录 fallback 链路
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = state.aggregate_cooldown_seconds(aggregate_model)
                    state.set_aggregate_member_cooldown(candidate.aggregate_member_id, str(err), cooldown_seconds, failure_reason)
                    if candidate.idx is not None:
                        state.set_cooldown(
                            candidate.idx,
                            str(err),
                            self.policy.auto_cooldown_seconds(group),
                            failure_reason,
                        )
                    failure_scope = classification.failure_scope
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": "network",
                        "reason": self.preparation.short_error(str(err)),
                        "cooldown_applied": True,
                        "failure_scope": failure_scope,
                        "category": failure_reason,
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {failure_reason}; try next; error={self.preparation.short_error(str(err))}; {_stream_metrics_text(stream_metrics)}"
                    self.observability.add_log(path, candidate.label, log_status, self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason=lock_release_reason, aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event=log_event, cooldown_applied=True, failure_scope=failure_scope)
                    continue
                if auto_fallback:
                    if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                        state.set_cooldown(candidate.idx, str(err), self.policy.auto_cooldown_seconds(group), failure_reason)
                    elif candidate.idx is not None:
                        state.set_unusable(candidate.idx, str(err))
                    failure_scope = classification.failure_scope
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": "network",
                            "reason": self.preparation.short_error(str(err)),
                            "cooldown_applied": True,
                            "failure_scope": failure_scope,
                            "category": failure_reason,
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {failure_reason}; try next; error={self.preparation.short_error(str(err))}; {_stream_metrics_text(stream_metrics)}"
                    self.observability.add_log(path, candidate.label, log_status, self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason=lock_release_reason), duration_ms, group=group, request_id=request_id, attempt=attempt, event=log_event, cooldown_applied=True, failure_scope=failure_scope)
                    continue
                failure_scope = classification.failure_scope
                explicit_failure_recorded = self._record_explicit_qualified_failure(
                    state,
                    candidate,
                    group,
                    str(err),
                    failure_reason,
                    auto_fallback=auto_fallback,
                )
                detail = f"cooldown_applied={str(explicit_failure_recorded).lower()}; failure_scope={failure_scope}; {failure_reason}; error={self.preparation.short_error(str(err))}; {_stream_metrics_text(stream_metrics)}"
                self.observability.add_log(path, candidate.label, log_status, self.preparation.debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason=lock_release_reason), duration_ms, group=group, request_id=request_id, attempt=attempt, event=log_event, cooldown_applied=explicit_failure_recorded, failure_scope=failure_scope)
                continue

        if isinstance(last_error, StreamFrameProtocolError):
            self.observability.finish_live_request(request_id, "error")
            error_body = json.dumps({"error": {"message": "上游流式响应不是完整 SSE 帧，已在首帧前中止", "type": "upstream_stream_protocol_error", "code": "stream_protocol_error", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")
            return 502, {"Content-Type": "application/json; charset=utf-8"}, [error_body], request_id
        if aggregate_model:
            self.observability.finish_live_request(request_id, "error")
            if not saw_cooldown and saw_request_level:
                raise self.faults.all_models_failed(
                    f"聚合模型 {aggregate_model.name} 的所有成员均因请求级错误被拒绝{self.policy.waf_blocked_hint(fallback_chain)}",
                    attempted=attempt,
                    stream_timeout=saw_stream_timeout,
                    error_code="upstream_request_rejected",
                    fallback_chain=fallback_chain,
                    aggregate_name=aggregate_model.name,
                )
            raise self.faults.all_models_failed(
                f"聚合模型 {aggregate_model.name} 的所有成员均不可用",
                attempted=attempt,
                stream_timeout=saw_stream_timeout,
                error_code="aggregate_members_unavailable",
                fallback_chain=fallback_chain,
                aggregate_name=aggregate_model.name,
            )
        self.observability.finish_live_request(request_id, "error")
        if last_error is None:
            raise self.faults.all_models_failed(
                "没有可用模型",
                attempted=attempt,
                stream_timeout=saw_stream_timeout,
                error_code="no_usable_models",
            )
        if not saw_cooldown and saw_request_level:
            raise self.faults.all_models_failed(
                f"所有候选均因请求级错误被拒绝{self.policy.waf_blocked_hint(fallback_chain)}",
                attempted=attempt,
                stream_timeout=saw_stream_timeout,
                error_code="upstream_request_rejected",
            )
        raise self.faults.all_models_failed(
            f"所有可用模型均请求失败，共尝试 {attempt} 个上游",
            attempted=attempt,
            stream_timeout=saw_stream_timeout,
            error_code="all_models_failed",
        ) from last_error
