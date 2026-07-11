"""M3a routing-runtime helpers behind the ``ArkProxyRouter`` compatibility facade.

The helpers intentionally receive the router facade rather than owning HTTP execution.  This
keeps candidate ordering, cooldown mutation, error classification, and WAF lock state
extractable without changing ``call``/``stream`` control flow.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from typing import Any, Dict, Iterator, Optional, Tuple

from linrouter_core.config.constants import DEFAULT_AUTO_MODEL_NAME, GLOBAL_ROUTE_GROUP_ID, PROVIDER_PROXY, PROVIDER_RELAY


class CandidateErrorClassifier:
    """Pure candidate-error classification shared by the router facade."""

    @staticmethod
    def classify(router: Any, status_code: Optional[int], raw: str, error_kind: str = "http") -> Dict[str, Any]:
        if error_kind in ("network", "stream_timeout"):
            return {"should_cooldown": True, "is_request_level": False, "category": error_kind, "log_reason": error_kind, "failure_scope": "upstream"}
        if status_code is None:
            return {"should_cooldown": True, "is_request_level": False, "category": "network", "log_reason": "network", "failure_scope": "upstream"}
        if status_code >= 500:
            return {"should_cooldown": True, "is_request_level": False, "category": "server_error", "log_reason": f"server_error_{status_code}", "failure_scope": "upstream"}
        if status_code == 429:
            if router._is_rate_limited(status_code, raw):
                return {"should_cooldown": True, "is_request_level": False, "category": "rate_limit", "log_reason": "rate_limit", "failure_scope": "upstream"}
            if router._is_quota_exhausted(status_code, raw):
                return {"should_cooldown": True, "is_request_level": False, "category": "quota_exhausted", "log_reason": "quota_exhausted", "failure_scope": "upstream"}
            return {"should_cooldown": True, "is_request_level": False, "category": "rate_limit", "log_reason": "rate_limit_429", "failure_scope": "upstream"}
        if router._is_waf_blocked_error(status_code, raw):
            return {"should_cooldown": False, "is_request_level": True, "category": "waf_blocked", "log_reason": "waf_blocked", "failure_scope": "candidate"}
        if router._is_request_level_error(status_code, raw):
            if status_code in (401, 403):
                return {"should_cooldown": False, "is_request_level": True, "category": "auth_error", "log_reason": "auth_error", "failure_scope": "candidate"}
            return {"should_cooldown": False, "is_request_level": True, "category": "request_level", "log_reason": "request_level", "failure_scope": "request"}
        return {"should_cooldown": False, "is_request_level": False, "category": "unknown", "log_reason": f"http_{status_code}", "failure_scope": "upstream"}


class WafLockState:
    """Per-upstream WAF lock and active-stream state, independent of HTTP execution."""

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
            f"waf_lock_wait_timeout; fallback_reason={fallback_reason}; "
            f"failure_scope=busy; cooldown_applied=false; active_streams={active_streams}; "
            f"lock_wait_ms={lock_wait_ms}; busy_hint=candidate_busy"
        )


class CandidateRuntime:
    """Candidate enumeration and health/cooldown mutations used through router facades."""

    def __init__(self, router: Any) -> None:
        self.router = router

    def iter_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Tuple[int, Any]]:
        router = self.router
        group = router.store.find_group(group_id) if group_id else None
        if router._is_auto_model(requested_model, group):
            requested_model = None
        for idx, model in enumerate(router.store.models):
            if model.cooldown_until and model.cooldown_until <= int(time.time()):
                model.cooldown_until = 0
                model.cooldown_reason = ""
                if not model.disabled_by_user:
                    model.usable = True
                model.last_error = ""
                model.last_checked_at = router._now()
                router.store.save()
            if model.disabled_by_user or not model.usable:
                continue
            if group_id and model.group_id != group_id:
                continue
            if requested_model and requested_model not in {model.id, model.name, model.ep_id}:
                continue
            yield idx, model

    def candidate_from_model(self, idx: int, model: Any, group: Any) -> Any:
        mode = self.router._mode_for(group)
        channel = model.price_group if mode == PROVIDER_RELAY and model.price_group else ("proxy" if mode == PROVIDER_PROXY else "")
        return self.router._upstream_candidate_type(idx=idx, group=group, model=model, label=model.name, target_model=model.ep_id, auth_key=self.router._auth_for(group, model), channel=channel)

    def iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Any]:
        router = self.router
        if group_id == GLOBAL_ROUTE_GROUP_ID:
            return
        if group_id:
            group = router.store.find_group(group_id)
            if not group:
                return
            matched = False
            for idx, model in self.iter_candidates(requested_model, group.id):
                matched = True
                yield self.candidate_from_model(idx, model, group)
            if router._mode_for(group) == PROVIDER_PROXY and not matched and requested_model and not router._is_auto_model(requested_model, group):
                yield router._upstream_candidate_type(idx=None, group=group, model=None, label=requested_model, target_model=requested_model, auth_key=router._auth_for(group, None), channel="pass-through")
            return
        for idx, model in self.iter_candidates(requested_model):
            group = router._group_for(model)
            if group:
                yield self.candidate_from_model(idx, model, group)

    def aggregate_member_skip_reason(self, member: Any) -> Tuple[str, str, Any, Any]:
        router = self.router
        group = router.store.find_group(member.group_id)
        model = router.store.find_model(member.model_id)
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

    def iter_aggregate_candidates(self, aggregate: Any, **kwargs: Any) -> Iterator[Any]:
        router = self.router
        router.store.refresh_expired_cooldowns()
        members = router.store.get_aggregate_members(aggregate.id)
        strategy = aggregate.strategy or "priority"
        if strategy == "price_first":
            members = sorted(members, key=lambda m: (m.manual_price is None, m.manual_price if m.manual_price is not None else 0, m.priority))
        else:
            members = sorted(members, key=lambda m: m.priority)
        for member in members:
            reason, message, group, model = self.aggregate_member_skip_reason(member)
            if reason:
                if kwargs.get("log_skips", False):
                    router._log_aggregate_member_skip(kwargs.get("path", ""), aggregate, member, reason, message, group, model, kwargs.get("requested_label", ""), kwargs.get("request_id", ""), kwargs.get("resolved_as", ""))
                continue
            if not group or not model:
                continue
            candidate = self.candidate_from_model(router.store.models.index(model), model, group)
            candidate.aggregate_id = aggregate.id
            candidate.aggregate_name = aggregate.name
            candidate.aggregate_member_id = member.id
            candidate.manual_price = member.manual_price
            yield candidate

    def set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        model = self.router.store.models[idx]
        model.usable = False
        model.last_error = error[:500]
        model.last_checked_at = self.router._now()
        model.cooldown_until = int(time.time()) + max(0, cooldown_seconds)
        model.cooldown_reason = reason[:120]
        self.router.store.save()

    def set_success(self, idx: int) -> None:
        model = self.router.store.models[idx]
        model.last_error = ""
        model.last_success_at = self.router._now()
        model.last_checked_at = model.last_success_at
        self.router.store.save()

    def execute_non_stream(
        self,
        path: str,
        payload: Dict[str, Any],
        route: Any = None,
        incoming_headers: Optional[Dict[str, str]] = None,
        raw_body: bytes | None = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        """Execute the frozen non-stream candidate/request/fallback chain via the router facade."""
        router = self.router
        router.store.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = router._route_group_id(route)
        is_route_context = hasattr(route, "group") and hasattr(route, "is_deprecated_global")
        route_group = route.group if is_route_context else router.store.find_group(group_id) if group_id else None
        is_deprecated_global = bool(route.is_deprecated_global) if is_route_context else False
        if is_deprecated_global:
            return 403, {"Content-Type": "application/json; charset=utf-8"}, json.dumps({"error": {"message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key", "type": "global_key_deprecated", "code": "use_group_or_aggregate_key"}}, ensure_ascii=False).encode("utf-8")
        route_aggregate = route.aggregate if is_route_context else None
        is_global = bool(route.is_global) if is_route_context else False
        auto_mode = router._is_auto_model(str(requested_model) if requested_model else None, route_group)
        # auto_fallback：组级 auto 或聚合模型下，失败时尝试下一个候选（全局 Key 已退役）
        auto_fallback = auto_mode or bool(route_aggregate)
        request_id = uuid.uuid4().hex[:12]
        router._live_request_start(request_id, path, requested_label, stream=False)
        attempt = 0
        last_error: Optional[Exception] = None
        saw_cooldown = False
        saw_request_level = False

        # 聚合模型解析
        aggregate_info = router._resolve_aggregate(
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
            candidates_iter: Iterator[UpstreamCandidate] = router._iter_aggregate_candidates(aggregate_model, log_skips=True, path=path, requested_label=requested_label, request_id=request_id, resolved_as=resolved_as)
        else:
            candidates_iter = router._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id)

        for candidate in candidates_iter:
            attempt += 1
            group = candidate.group
            target_url = router._resolve_url(group.base_url, path)
            router._live_request_update(
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
                aggregate_suffix = router._aggregate_log_suffix(
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
                router.add_log(path, candidate.label, "skip", skip_detail, group=group, request_id=request_id, attempt=attempt, event="skip")
                continue
            payload_for_upstream = payload
            tools_normalized = False
            if router._tools_order_enabled():
                payload_for_upstream, tools_normalized = router._normalize_tools_order(payload)
            body, body_mode = router._body_for_upstream(payload_for_upstream, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = router._headers_for(group, candidate.auth_key, incoming_headers, stream=False)
            upstream_lock = router._candidate_lock(candidate, incoming_headers)
            started_at = time.perf_counter()
            if upstream_lock:
                router._live_request_update(request_id, stage="waiting_waf_lock", stage_label="等待 WAF 锁")
            acquired, lock_wait_ms = router._acquire_upstream_lock(upstream_lock)
            if not acquired:
                router._live_request_update(request_id, stage="candidate_busy", stage_label="候选忙/等待锁超时", possible_reason="候选正在处理大上下文请求，已临时切换")
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                router.add_log(
                    path,
                    candidate.label,
                    "timeout",
                    router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, router._waf_lock_busy_detail(candidate, body, lock_wait_ms), lock_wait_ms=lock_wait_ms),
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
                router._live_request_finish(request_id, "error")
                return 503, {"Content-Type": "application/json; charset=utf-8"}, [json.dumps({"error": {"message": "候选正在处理大上下文请求，已临时切换到下一个候选", "type": "timeout", "code": "waf_lock_wait_timeout", "request_id": request_id}}, ensure_ascii=False).encode("utf-8")]
            try:
                router._live_request_update(request_id, stage="connecting_upstream", stage_label="连接上游")
                resp = router._upstream_client.request("POST", target_url, outbound_headers, body, stream=False, timeout=120)
                with resp:
                    router._live_request_update(request_id, stage="receiving_response", stage_label="接收响应")
                    data = resp.read()
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = router._usage_from_response(data)
                    router._mark_success(candidate)
                    if candidate.aggregate_member_id:
                        router._mark_aggregate_member_success(candidate.aggregate_member_id)
                    router.add_log(
                        path,
                        candidate.label,
                        str(resp.status),
                        router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "ok", resp=resp, tools_normalized=tools_normalized, lock_wait_ms=lock_wait_ms, lock_release_reason="response_inline", aggregate_suffix=aggregate_suffix),
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
                        router.debug_capture.capture(
                            path=path,
                            group=group,
                            model=candidate.label,
                            target_model=candidate.target_model,
                            body=body,
                            body_mode=body_mode,
                            headers=outbound_headers,
                            fingerprint=router._payload_fingerprint(payload_for_upstream, body, urlparse(target_url).path, tools_normalized=tools_normalized),
                            request_id=request_id,
                            usage_source="response_inline",
                        )
                    except Exception:
                        pass
                    router._live_request_finish(request_id, "done")
                    return resp.status, dict(resp.headers.items()), data
            except HTTPError as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                classification = router._classify_candidate_error(err.code, raw, "http")
                cooldown_applied = classification["should_cooldown"]
                is_request_level = classification["is_request_level"]
                if cooldown_applied:
                    saw_cooldown = True
                if is_request_level:
                    saw_request_level = True

                # 聚合成员失败：仅冷却类错误才写入 cooldown
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    if cooldown_applied:
                        cooldown_seconds = router._aggregate_cooldown_seconds(aggregate_model)
                        router._set_aggregate_member_cooldown(candidate.aggregate_member_id, raw or str(err), cooldown_seconds, classification["log_reason"])
                    failure_scope = classification["failure_scope"]
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": err.code,
                        "reason": router._short_error(raw),
                        "cooldown_applied": cooldown_applied,
                        "failure_scope": failure_scope,
                        "category": classification["category"],
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={router._short_error(raw)}{router._waf_blocked_suffix(classification, group)}"
                    router.add_log(path, candidate.label, str(err.code), router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 429 立即重试一次（非聚合路径保持原有行为）
                if classification["category"] == "rate_limit" and not is_aggregate_candidate:
                    try:
                        retry_started_at = time.perf_counter()
                        with router._upstream_client.request("POST", target_url, outbound_headers, body, stream=False, timeout=120) as resp:
                            data = resp.read()
                            retry_duration_ms = int((time.perf_counter() - retry_started_at) * 1000)
                            prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = router._usage_from_response(data)
                            router._mark_success(candidate)
                            router.add_log(
                                path,
                                candidate.label,
                                str(resp.status),
                                router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "retry ok", resp=resp, lock_wait_ms=lock_wait_ms, lock_release_reason="retry_ok"),
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
                            router._live_request_finish(request_id, "done")
                            return resp.status, dict(resp.headers.items()), data
                    except Exception as retry_err:
                        last_error = retry_err
                        retry_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        router.add_log(path, candidate.label, "retry failed", router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, str(retry_err), lock_wait_ms=lock_wait_ms, lock_release_reason="retry_failed"), retry_duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=False)

                # 自动 fallback（组级 auto 或聚合模型）
                if auto_fallback:
                    if cooldown_applied:
                        if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                            router._set_cooldown(candidate.idx, raw or str(err), router._auto_cooldown_seconds(group), classification["log_reason"])
                        elif candidate.idx is not None:
                            router._set_unusable(candidate.idx, raw or str(err))
                        saw_cooldown = True
                    failure_scope = classification["failure_scope"]
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": err.code,
                            "reason": router._short_error(raw),
                            "cooldown_applied": cooldown_applied,
                            "failure_scope": failure_scope,
                            "category": classification["category"],
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={router._short_error(raw)}{router._waf_blocked_suffix(classification, group)}"
                    router.add_log(path, candidate.label, str(err.code), router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown" if cooldown_applied else "fallback", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
                    continue

                # 非自动 fallback：保留原有显式模型处理逻辑
                if router._is_quota_exhausted(err.code, raw):
                    router._mark_unusable(candidate, raw)
                    router.add_log(path, candidate.label, str(err.code), router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "quota exhausted, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=False, failure_scope="upstream")
                    continue
                if router._is_server_error(err.code):
                    router.add_log(path, candidate.label, str(err.code), router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, "server error, try next", lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback", cooldown_applied=False, failure_scope="upstream")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={router._short_error(raw)}"
                router.add_log(path, candidate.label, str(err.code), router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="http_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error", cooldown_applied=False)
                router._live_request_finish(request_id, "error")
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                classification = router._classify_candidate_error(None, str(err), "network")
                saw_cooldown = True

                # 聚合成员网络失败：cooldown 聚合成员本身并记录 fallback 链路
                if is_aggregate_candidate and aggregate_model and candidate.aggregate_member_id:
                    cooldown_seconds = router._aggregate_cooldown_seconds(aggregate_model)
                    router._set_aggregate_member_cooldown(candidate.aggregate_member_id, str(err), cooldown_seconds, classification["log_reason"])
                    failure_scope = classification["failure_scope"]
                    fallback_chain.append({
                        "member_id": candidate.aggregate_member_id,
                        "group": group.name,
                        "model": candidate.model.name if candidate.model else candidate.label,
                        "manual_price": candidate.manual_price,
                        "status": "network",
                        "reason": router._short_error(str(err)),
                        "cooldown_applied": True,
                        "failure_scope": failure_scope,
                        "category": classification["category"],
                        "waf_compatible": group.waf_compatible,
                    })
                    fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={router._short_error(str(err))}"
                    router.add_log(path, candidate.label, "network", router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error", aggregate_suffix=aggregate_suffix), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                if auto_fallback:
                    if candidate.group.provider_type == PROVIDER_RELAY and candidate.idx is not None:
                        router._set_cooldown(candidate.idx, str(err), router._auto_cooldown_seconds(group), classification["log_reason"])
                    elif candidate.idx is not None:
                        router._set_unusable(candidate.idx, str(err))
                    failure_scope = classification["failure_scope"]
                    if not is_aggregate_candidate:
                        fallback_chain.append({
                            "member_id": "",
                            "group": group.name,
                            "model": candidate.model.name if candidate.model else candidate.label,
                            "manual_price": candidate.manual_price,
                            "status": "network",
                            "reason": router._short_error(str(err)),
                            "cooldown_applied": True,
                            "failure_scope": failure_scope,
                            "category": classification["category"],
                            "waf_compatible": group.waf_compatible,
                        })
                        fallback_index += 1
                    detail = f"cooldown_applied=true; failure_scope={failure_scope}; {classification['log_reason']}; try next; error={router._short_error(str(err))}"
                    router.add_log(path, candidate.label, "network", router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=True, failure_scope=failure_scope)
                    continue
                failure_scope = classification["failure_scope"]
                detail = f"cooldown_applied=false; failure_scope={failure_scope}; {classification['log_reason']}; error={router._short_error(str(err))}"
                router.add_log(path, candidate.label, "network", router._debug_detail(candidate, requested_label, target_url, body_mode, body, payload_for_upstream, outbound_headers, detail, lock_wait_ms=lock_wait_ms, lock_release_reason="network_error"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network", cooldown_applied=False, failure_scope=failure_scope)
                continue
            finally:
                router._release_lock(upstream_lock)

        if aggregate_model:
            router._live_request_finish(request_id, "error")
            if not saw_cooldown and saw_request_level:
                raise router._all_models_failed_error_type(
                    f"聚合模型 {aggregate_model.name} 的所有成员均因请求级错误被拒绝{router._waf_blocked_hint(fallback_chain)}",
                    attempted=attempt,
                    error_code="upstream_request_rejected",
                    fallback_chain=fallback_chain,
                    aggregate_name=aggregate_model.name,
                )
            raise router._all_models_failed_error_type(
                f"聚合模型 {aggregate_model.name} 的所有成员均不可用",
                attempted=attempt,
                error_code="aggregate_members_unavailable",
                fallback_chain=fallback_chain,
                aggregate_name=aggregate_model.name,
            )
        router._live_request_finish(request_id, "error")
        if last_error is None:
            raise router._all_models_failed_error_type("没有可用模型", attempted=attempt, error_code="no_usable_models")
        if not saw_cooldown and saw_request_level:
            raise router._all_models_failed_error_type(
                f"所有候选均因请求级错误被拒绝{router._waf_blocked_hint(fallback_chain)}",
                attempted=attempt,
                error_code="upstream_request_rejected",
            )
        raise router._all_models_failed_error_type(
            f"所有可用模型均请求失败，共尝试 {attempt} 个上游",
            attempted=attempt,
            error_code="all_models_failed",
        ) from last_error
