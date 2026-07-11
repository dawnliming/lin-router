"""M3a routing-runtime helpers behind the ``ArkProxyRouter`` compatibility facade.

The helpers intentionally receive the router facade rather than owning HTTP execution.  This
keeps candidate ordering, cooldown mutation, error classification, and WAF lock state
extractable without changing ``call``/``stream`` control flow.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterator, Optional, Tuple

from linrouter_core.config.constants import GLOBAL_ROUTE_GROUP_ID, PROVIDER_PROXY, PROVIDER_RELAY


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
