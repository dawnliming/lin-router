"""Candidate query and health-state ownership for the v0.6 I2 slice.

This service owns candidate construction/enumeration and candidate or aggregate-member
health writes. It depends on ConfigStore and explicit callbacks only; it does not import
the legacy facade, HTTP transport, execution services, or a broad router dependency.
"""
from __future__ import annotations

import hashlib
import time
from typing import Callable, Iterator, Tuple

from linrouter_core.config.constants import GLOBAL_ROUTE_GROUP_ID, PROVIDER_PROXY, PROVIDER_RELAY
from linrouter_core.config.models import AggregateMember, AggregateModel, ConnectionGroup, ModelConfig
from linrouter_core.config.store import ConfigStore
from linrouter_core.contracts.runtime_types import UpstreamCandidate


class CandidateHealthService:
    """Single business owner for candidate queries and health-state mutation."""

    def __init__(
        self,
        store: ConfigStore,
        *,
        now: Callable[[], str],
        is_auto_model: Callable[[str | None, ConnectionGroup | None], bool],
        mode_for: Callable[[ConnectionGroup | None], str],
        group_for: Callable[[ModelConfig], ConnectionGroup | None],
        auth_for: Callable[[ConnectionGroup, ModelConfig | None], str],
        candidate_type: type[UpstreamCandidate],
        log_aggregate_member_skip: Callable[..., None],
        breaker_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._now = now
        self._is_auto_model = is_auto_model
        self._mode_for = mode_for
        self._group_for = group_for
        self._auth_for = auth_for
        self._candidate_type = candidate_type
        self._log_aggregate_member_skip = log_aggregate_member_skip
        self._breaker_enabled = breaker_enabled or (lambda: False)

    @staticmethod
    def _safe_error_reference(error: str) -> str:
        """Persist an error correlation token, never an upstream response body."""
        text = str(error or "")
        if not text:
            return ""
        encoded = text.encode("utf-8", "replace")
        return f"redacted_sha256:{hashlib.sha256(encoded).hexdigest()[:16]},bytes:{len(encoded)}"

    def _breaker_is_open(self, item: ModelConfig | AggregateMember) -> bool:
        """Keep an open breaker out of automatic selection until a manual probe recovers it."""
        if not self._breaker_enabled():
            # A breaker toggle is a runtime policy switch. Do not let health
            # state persisted while it was enabled keep a candidate unusable
            # after the feature is turned off.
            if getattr(item, "health_state", "") in {"cooling", "breaker_open"}:
                item.health_state = "normal"
                item.consecutive_failures = 0
                item.last_failure_at = 0
                item.breaker_until = 0
                item.breaker_reason = ""
                item.cooldown_until = 0
                item.cooldown_reason = ""
                item.last_error = ""
                if isinstance(item, ModelConfig) and not item.disabled_by_user:
                    item.usable = True
                self._store.save()
            return False
        until = int(getattr(item, "breaker_until", 0) or 0)
        if not until:
            return False
        item.health_state = "breaker_open"
        return True

    def iter_candidates(
        self,
        requested_model: str | None,
        group_id: str | None = None,
    ) -> Iterator[Tuple[int, ModelConfig]]:
        group = self._store.find_group(group_id) if group_id else None
        if self._is_auto_model(requested_model, group):
            requested_model = None
        for idx, model in enumerate(self._store.models):
            if model.cooldown_until and model.cooldown_until <= int(time.time()):
                model.cooldown_until = 0
                model.cooldown_reason = ""
                if not model.disabled_by_user:
                    model.usable = True
                model.last_error = ""
                model.last_checked_at = self._now()
                self._store.save()
            if self._breaker_is_open(model):
                continue
            requested_match = bool(
                requested_model
                and requested_model in {model.id, model.name, model.ep_id}
            )
            # A named request is allowed to make the next breaker attempt while
            # the model is only cooling.  Once the breaker opens it is still
            # excluded above.  Auto and aggregate routing retain their existing
            # cooldown behaviour.
            explicit_breaker_retry = bool(
                self._breaker_enabled()
                and requested_match
                and model.health_state == "cooling"
                and model.cooldown_until
            )
            if model.disabled_by_user or (not model.usable and not explicit_breaker_retry):
                continue
            if group_id and model.group_id != group_id:
                continue
            if requested_model and not requested_match:
                continue
            yield idx, model

    def supports_group_requested_model(self, requested_model: str | None, group: ConnectionGroup | None) -> bool:
        """Return whether an explicit model satisfies a non-proxy group contract.

        Auto model names keep their existing selection semantics; proxy groups are
        intentionally excluded because their explicit pass-through contract is
        handled by ``iter_upstream_candidates``.
        """
        if not requested_model or self._is_auto_model(requested_model, group):
            return True
        if not group or self._mode_for(group) == PROVIDER_PROXY:
            return True
        return any(
            model.group_id == group.id
            and requested_model in {model.id, model.name, model.ep_id}
            for model in self._store.models
        )

    def candidate_from_model(
        self,
        idx: int | None,
        model: ModelConfig | None,
        group: ConnectionGroup,
    ) -> UpstreamCandidate:
        mode = self._mode_for(group)
        channel = model.price_group if model and mode == PROVIDER_RELAY and model.price_group else ("proxy" if mode == PROVIDER_PROXY else "")
        label = model.name if model else ""
        target_model = model.ep_id if model else ""
        return self._candidate_type(
            idx=idx,
            group=group,
            model=model,
            label=label,
            target_model=target_model,
            auth_key=self._auth_for(group, model),
            channel=channel,
        )

    def iter_upstream_candidates(
        self,
        requested_model: str | None,
        group_id: str | None = None,
    ) -> Iterator[UpstreamCandidate]:
        if group_id == GLOBAL_ROUTE_GROUP_ID:
            return
        if group_id:
            group = self._store.find_group(group_id)
            if not group:
                return
            matched = False
            for idx, model in self.iter_candidates(requested_model, group.id):
                matched = True
                yield self.candidate_from_model(idx, model, group)
            if self._mode_for(group) == PROVIDER_PROXY and not matched and requested_model and not self._is_auto_model(requested_model, group):
                yield self._candidate_type(
                    idx=None,
                    group=group,
                    model=None,
                    label=requested_model,
                    target_model=requested_model,
                    auth_key=self._auth_for(group, None),
                    channel="pass-through",
                )
            return
        for idx, model in self.iter_candidates(requested_model):
            group = self._group_for(model)
            if group:
                yield self.candidate_from_model(idx, model, group)

    def aggregate_member_skip_reason(
        self,
        member: AggregateMember,
    ) -> Tuple[str, str, ConnectionGroup | None, ModelConfig | None]:
        group = self._store.find_group(member.group_id)
        model = self._store.find_model(member.model_id)
        now_ts = int(time.time())
        if not member.enabled:
            return "member_disabled", "该聚合成员已手动停用，不参与本次调度。", group, model
        if self._breaker_is_open(member):
            return "member_breaker_open", "该聚合成员已触发熔断，暂不参与本次调度。", group, model
        if member.cooldown_until and member.cooldown_until > now_ts:
            return "member_cooling", "该聚合成员正在冷却中，本次直接跳过。", group, model
        if not group:
            return "underlying_group_missing", "底层连接组不存在，请检查聚合成员配置。", group, model
        if not model:
            return "underlying_model_missing", "底层真实模型不存在，请检查聚合成员配置。", group, model
        if model.disabled_by_user:
            return "underlying_model_disabled", "底层真实模型已停用，请先启用真实模型。", group, model
        if self._breaker_is_open(model):
            return "underlying_model_breaker_open", "底层真实模型已熔断，暂不参与本次调度。", group, model
        if not model.usable:
            return "underlying_model_disabled", "底层真实模型已停用，请先启用真实模型。", group, model
        if model.cooldown_until and model.cooldown_until > now_ts:
            return "underlying_model_cooling", "底层真实模型冷却中，本次直接跳过。", group, model
        return "", "", group, model

    def iter_aggregate_candidates(self, aggregate: AggregateModel, **kwargs: object) -> Iterator[UpstreamCandidate]:
        self._store.refresh_expired_cooldowns()
        members = self._store.get_aggregate_members(aggregate.id)
        # 价格字段仅用于展示和统计；历史 strategy 值也必须按手动 priority 调度。
        members = sorted(members, key=lambda member: member.priority)
        for member in members:
            reason, message, group, model = self.aggregate_member_skip_reason(member)
            if reason:
                if kwargs.get("log_skips", False):
                    self._log_aggregate_member_skip(
                        str(kwargs.get("path", "")), aggregate, member, reason, message, group, model,
                        str(kwargs.get("requested_label", "")), str(kwargs.get("request_id", "")), str(kwargs.get("resolved_as", "")),
                    )
                continue
            if not group or not model:
                continue
            candidate = self.candidate_from_model(self._store.models.index(model), model, group)
            candidate.aggregate_id = aggregate.id
            candidate.aggregate_name = aggregate.name
            candidate.aggregate_member_id = member.id
            candidate.manual_price = member.manual_price
            yield candidate

    def set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        model = self._store.models[idx]
        now_ts = int(time.time())
        if self._breaker_enabled():
            model.consecutive_failures = 0 if not model.last_failure_at or now_ts - model.last_failure_at > 300 else model.consecutive_failures
            model.consecutive_failures += 1
            model.last_failure_at = now_ts
            model.health_state = "breaker_open" if model.consecutive_failures >= 3 else "cooling"
            if model.consecutive_failures >= 3:
                model.breaker_until = now_ts + 60
                model.breaker_reason = reason[:120]
        model.usable = False
        model.last_error = self._safe_error_reference(error)
        model.last_checked_at = self._now()
        model.cooldown_until = int(time.time()) + max(0, cooldown_seconds)
        model.cooldown_reason = reason[:120]
        self._store.save()

    def record_qualified_failure(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> bool:
        """Apply the existing cooldown/breaker state only when it is enabled."""
        if not self._breaker_enabled():
            return False
        self.set_cooldown(idx, error, cooldown_seconds, reason)
        return True

    def set_success(self, idx: int) -> None:
        model = self._store.models[idx]
        if not model.disabled_by_user:
            model.usable = True
        model.last_error = ""
        model.last_success_at = self._now()
        model.last_checked_at = model.last_success_at
        model.cooldown_until = 0
        model.cooldown_reason = ""
        model.health_state = "normal"
        model.consecutive_failures = 0
        model.last_failure_at = 0
        model.breaker_until = 0
        model.breaker_reason = ""
        self._store.save()

    def set_unusable(self, idx: int, error: str) -> None:
        model = self._store.models[idx]
        model.usable = False
        model.last_error = self._safe_error_reference(error)
        model.last_checked_at = self._now()
        model.cooldown_until = 0
        model.cooldown_reason = ""
        self._store.save()

    def set_aggregate_member_cooldown(self, member_id: str, error: str, cooldown_seconds: int, reason: str) -> None:
        member = self._store.find_aggregate_member(member_id)
        if not member:
            return
        now_ts = int(time.time())
        if self._breaker_enabled():
            member.consecutive_failures = 0 if not member.last_failure_at or now_ts - member.last_failure_at > 300 else member.consecutive_failures
            member.consecutive_failures += 1
            member.last_failure_at = now_ts
            member.health_state = "breaker_open" if member.consecutive_failures >= 3 else "cooling"
            if member.consecutive_failures >= 3:
                member.breaker_until = now_ts + 60
                member.breaker_reason = reason[:120]
        member.last_error = self._safe_error_reference(error)
        member.last_checked_at = self._now()
        member.cooldown_until = now_ts + max(0, cooldown_seconds)
        member.cooldown_reason = reason[:120]
        self._store.save()

    def mark_aggregate_member_success(self, member_id: str) -> None:
        member = self._store.find_aggregate_member(member_id)
        if not member:
            return
        member.last_error = ""
        member.last_success_at = self._now()
        member.last_checked_at = member.last_success_at
        member.cooldown_until = 0
        member.cooldown_reason = ""
        member.health_state = "normal"
        member.consecutive_failures = 0
        member.last_failure_at = 0
        member.breaker_until = 0
        member.breaker_reason = ""
        self._store.save()
