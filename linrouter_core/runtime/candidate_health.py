"""Candidate query and health-state ownership for the v0.6 I2 slice.

This service owns candidate construction/enumeration and candidate or aggregate-member
health writes. It depends on ConfigStore and explicit callbacks only; it does not import
the legacy facade, HTTP transport, execution services, or a broad router dependency.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable, Dict, Iterator, Tuple
from urllib.parse import urlparse

from linrouter_core.config.constants import GLOBAL_ROUTE_GROUP_ID, PROVIDER_PROXY, PROVIDER_RELAY
from linrouter_core.config.models import (
    ROUTING_POLICY_COOLDOWN_OFF,
    ROUTING_POLICY_FIXED_COOLDOWN,
    ROUTING_POLICY_SMART_BREAKER,
    ROUTING_POLICY_STICKY_ROUTE,
    AggregateMember,
    AggregateModel,
    ConnectionGroup,
    ModelConfig,
)
from linrouter_core.config.store import ConfigStore
from linrouter_core.contracts.runtime_types import UpstreamCandidate


class CandidateHealthService:
    """Single business owner for candidate queries and health-state mutation."""

    _ATTEMPT_WINDOW_SIZE = 5
    _BREAKER_FAILURE_THRESHOLD = 3
    _BREAKER_COOLDOWN_BY_LEVEL = {1: 60, 2: 180, 3: 300}
    _BREAKER_COOLDOWN_CAP_SECONDS = 600
    _RISK_WINDOW_SIZE = 5
    _RISK_BLOCK_THRESHOLD = 2
    _RISK_COOLDOWN_BY_LEVEL = {1: 15 * 60, 2: 60 * 60}
    _RISK_COOLDOWN_CAP_SECONDS = 6 * 60 * 60

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
        self._breaker_enabled = breaker_enabled or (lambda: True)
        # 半开租约仅表示当前进程内正在执行的真实请求，不能持久化到配置文件。
        self._health_lock = threading.RLock()
        self._half_open_leases: set[str] = set()
        # 风控索引只保留 host 与不可逆凭证摘要；它与具体配置文件一一对应，
        # 避免同一目录下不同路由实例意外共享风控状态，且不参与配置导出、日志或 API。
        # 既有观测/上游契约测试使用没有 path 属性的 Store 替身，风控索引必须容忍该场景：
        # 没有 path 时退化为纯内存模式，不能让风险索引功能破坏路由/观测 facade 构造。
        config_path = getattr(self._store, "path", None)
        if config_path is not None:
            self._risk_index_path = config_path.with_suffix(".risk-index.json")
        else:
            self._risk_index_path = None
        self._risk_scopes = self._load_risk_scopes()

    def is_enabled(self) -> bool:
        """返回当前熔断策略开关，供运行态与设置写入路径复用。"""
        return bool(self._breaker_enabled())

    def policy_status(self, item: ModelConfig | AggregateMember) -> dict[str, bool | str]:
        """解析智能熔断有效性；其他路由策略不显示为熔断开关关闭。"""
        if self.routing_policy(item) != ROUTING_POLICY_SMART_BREAKER:
            return {
                "smart_breaker_effective_enabled": True,
                "smart_breaker_disabled_by": "",
            }
        if not self.is_enabled():
            return {
                "smart_breaker_effective_enabled": False,
                "smart_breaker_disabled_by": "global",
            }
        group = self._store.find_group(item.group_id)
        if group and not bool(getattr(group, "smart_breaker_enabled", True)):
            return {
                "smart_breaker_effective_enabled": False,
                "smart_breaker_disabled_by": "group",
            }
        if isinstance(item, AggregateMember):
            aggregate = self._store.find_aggregate(item.aggregate_id)
            if aggregate and not bool(getattr(aggregate, "smart_breaker_enabled", True)):
                return {
                    "smart_breaker_effective_enabled": False,
                    "smart_breaker_disabled_by": "aggregate",
                }
        return {
            "smart_breaker_effective_enabled": True,
            "smart_breaker_disabled_by": "",
        }

    def routing_policy(self, item: ModelConfig | AggregateMember) -> str:
        """模型取连接组策略，成员取聚合策略；两层健康对象仍保持独立。"""
        owner = self._store.find_group(item.group_id)
        if isinstance(item, AggregateMember):
            owner = self._store.find_aggregate(item.aggregate_id)
        return str(getattr(owner, "routing_policy", ROUTING_POLICY_SMART_BREAKER) or ROUTING_POLICY_SMART_BREAKER)

    def is_policy_enabled(self, item: ModelConfig | AggregateMember) -> bool:
        """固定冷却与智能熔断均可写自动状态；粘性/关闭冷却不写健康状态。"""
        policy = self.routing_policy(item)
        if policy == ROUTING_POLICY_FIXED_COOLDOWN:
            return True
        if policy in {ROUTING_POLICY_COOLDOWN_OFF, ROUTING_POLICY_STICKY_ROUTE}:
            return False
        return bool(self.policy_status(item)["smart_breaker_effective_enabled"])

    @staticmethod
    def _item_key(item: ModelConfig | AggregateMember) -> str:
        prefix = "model" if isinstance(item, ModelConfig) else "member"
        return f"{prefix}:{item.id}"

    def runtime_health_state(self, item: ModelConfig | AggregateMember) -> str:
        """返回包含瞬时半开租约的运行态，不把租约写入持久化配置。"""
        if isinstance(item, ModelConfig) and item.disabled_by_user:
            return "manual_disabled"
        if self._item_key(item) in self._half_open_leases:
            return "half_open_probe"
        return str(getattr(item, "health_state", "normal") or "normal")

    def _clear_item_health(self, item: ModelConfig | AggregateMember) -> None:
        item.consecutive_failures = 0
        item.attempt_window = []
        item.breaker_level = 0
        item.last_failure_at = 0
        item.breaker_until = 0
        item.breaker_reason = ""
        item.cooldown_until = 0
        item.cooldown_reason = ""
        item.last_error = ""
        item.last_checked_at = self._now()
        if isinstance(item, ModelConfig):
            item.health_state = "manual_disabled" if item.disabled_by_user else "normal"
            if not item.disabled_by_user:
                item.usable = True
        else:
            item.health_state = "normal"

    def clear_system_health_states(self) -> None:
        """关闭全局策略时原子清理系统健康状态，保留手动启停语义。"""
        with self._health_lock:
            self._store.clear_system_health_states()
            self._half_open_leases.clear()

    def clear_group_health_states(self, group_id: str) -> None:
        """关闭连接组策略时清理该组模型和跨聚合成员状态。"""
        with self._health_lock:
            self._store.clear_group_health_states(group_id)
            self.release_group_probes(group_id)

    def clear_aggregate_health_states(self, aggregate_id: str) -> None:
        """关闭聚合策略时仅清理该聚合成员状态，不触碰底层模型。"""
        with self._health_lock:
            self._store.clear_aggregate_health_states(aggregate_id)
            self.release_aggregate_member_probes(aggregate_id)

    def release_group_probes(self, group_id: str) -> None:
        """策略关闭后丢弃对应瞬时租约，避免重新开启时遗留半开占用。"""
        with self._health_lock:
            self._half_open_leases = {
                key
                for key in self._half_open_leases
                if not self._lease_belongs_to_group(key, group_id)
            }

    def release_aggregate_member_probes(self, aggregate_id: str) -> None:
        """只释放目标聚合成员租约，底层模型租约仍由连接组策略管理。"""
        with self._health_lock:
            self._half_open_leases = {
                key
                for key in self._half_open_leases
                if not self._lease_belongs_to_aggregate(key, aggregate_id)
            }

    def _lease_belongs_to_group(self, key: str, group_id: str) -> bool:
        if key.startswith("model:"):
            model = self._store.find_model(key.removeprefix("model:"))
            return bool(model and model.group_id == group_id)
        if key.startswith("member:"):
            member = self._store.find_aggregate_member(key.removeprefix("member:"))
            return bool(member and member.group_id == group_id)
        return False

    def _lease_belongs_to_aggregate(self, key: str, aggregate_id: str) -> bool:
        if not key.startswith("member:"):
            return False
        member = self._store.find_aggregate_member(key.removeprefix("member:"))
        return bool(member and member.aggregate_id == aggregate_id)

    @staticmethod
    def _safe_error_reference(error: str) -> str:
        """Persist an error correlation token, never an upstream response body."""
        text = str(error or "")
        if not text:
            return ""
        encoded = text.encode("utf-8", "replace")
        return f"redacted_sha256:{hashlib.sha256(encoded).hexdigest()[:16]},bytes:{len(encoded)}"

    @staticmethod
    def _normalized_risk_host(base_url: str) -> str:
        """风险范围只使用规范化 host；scheme、路径和原始 URL 不进入索引输出。"""
        text = str(base_url or "").strip()
        parsed = urlparse(text if "://" in text else f"//{text}")
        return str(parsed.hostname or "").lower()

    def _load_risk_scopes(self) -> Dict[tuple[str, str], Dict[str, Any]]:
        """读取本地匿名风险索引；损坏文件只丢弃索引，不能阻断启动。

        没有 path 的 Store 替身（观测/上游契约测试）退化为纯内存模式：
        风险隔离在当前进程内仍即时生效，只是不跨重启持久化。
        """
        if self._risk_index_path is None:
            return {}
        try:
            raw = json.loads(self._risk_index_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        scopes: Dict[tuple[str, str], Dict[str, Any]] = {}
        for item in raw.get("scopes", []) if isinstance(raw, dict) else []:
            if not isinstance(item, dict):
                continue
            host = self._normalized_risk_host(str(item.get("host") or ""))
            digest = str(item.get("credential_digest") or "").lower()
            if not host or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                continue
            attempts = [value for value in item.get("attempts", []) if value in {"success", "other", "waf_blocked"}]
            scopes[(host, digest)] = {
                "attempts": attempts[-self._RISK_WINDOW_SIZE:],
                "level": max(0, int(item.get("level", 0) or 0)),
                "until": max(0, int(item.get("until", 0) or 0)),
                "last_event": str(item.get("last_event") or ""),
                "waf_events_since_trigger": max(0, int(item.get("waf_events_since_trigger", 0) or 0)),
            }
        return scopes

    def _save_risk_scopes_locked(self) -> None:
        """原子保存本地风险索引；绝不把索引混入配置、日志或 HTTP 响应。

        纯内存模式（无 path Store 替身）直接跳过持久化，不阻断请求。
        """
        if self._risk_index_path is None:
            return
        rows = [
            {
                "host": host,
                "credential_digest": digest,
                "attempts": list(scope.get("attempts", []) or [])[-self._RISK_WINDOW_SIZE:],
                "level": max(0, int(scope.get("level", 0) or 0)),
                "until": max(0, int(scope.get("until", 0) or 0)),
                "last_event": str(scope.get("last_event") or ""),
                "waf_events_since_trigger": max(0, int(scope.get("waf_events_since_trigger", 0) or 0)),
            }
            for (host, digest), scope in self._risk_scopes.items()
        ]
        tmp_path = self._risk_index_path.with_suffix(self._risk_index_path.suffix + ".tmp")
        try:
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps({"version": 1, "scopes": rows}, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self._risk_index_path)
        except OSError:
            # 风控索引写失败不能泄露或阻断当前请求；进程内隔离仍即时生效。
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def _risk_scope_key(cls, group: ConnectionGroup | None, credential: str) -> tuple[str, str] | None:
        host = cls._normalized_risk_host(getattr(group, "base_url", ""))
        if not host or not credential:
            return None
        digest = hashlib.sha256(str(credential).encode("utf-8", "replace")).hexdigest()
        return host, digest

    def _risk_scope_for(self, group: ConnectionGroup | None, credential: str) -> tuple[tuple[str, str] | None, Dict[str, Any] | None]:
        key = self._risk_scope_key(group, credential)
        return key, self._risk_scopes.get(key) if key else None

    def _risk_affected_model_count(self, key: tuple[str, str] | None) -> int:
        if not key:
            return 0
        count = 0
        for model in self._store.models:
            group = self._store.find_group(model.group_id)
            if group and self._risk_scope_key(group, self._auth_for(group, model)) == key:
                count += 1
        return count

    def _public_risk_status(
        self,
        key: tuple[str, str] | None,
        scope: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        now_ts = int(time.time())
        until = max(0, int((scope or {}).get("until", 0) or 0))
        isolated = bool(until > now_ts)
        return {
            "risk_isolated": isolated,
            "risk_level": max(0, int((scope or {}).get("level", 0) or 0)),
            "risk_until": until if isolated else 0,
            "risk_cooldown_seconds": max(0, until - now_ts) if isolated else 0,
            "risk_affected_models": self._risk_affected_model_count(key) if isolated else 0,
        }

    def risk_status_for_candidate(self, candidate: UpstreamCandidate) -> Dict[str, Any]:
        key, scope = self._risk_scope_for(candidate.group, str(getattr(candidate, "auth_key", "") or ""))
        return self._public_risk_status(key, scope)

    def risk_status_for_model(self, model: ModelConfig) -> Dict[str, Any]:
        group = self._store.find_group(model.group_id)
        credential = self._auth_for(group, model) if group else ""
        key, scope = self._risk_scope_for(group, credential)
        return self._public_risk_status(key, scope)

    def risk_status_for_member(self, member: AggregateMember) -> Dict[str, Any]:
        model = self._store.find_model(member.model_id)
        if not model:
            return self._public_risk_status(None, None)
        return self.risk_status_for_model(model)

    def record_risk_attempt(self, candidate: UpstreamCandidate, outcome: str) -> Dict[str, Any]:
        """记录匿名风险窗口；只有明确 WAF 拦截会触发独立风险隔离。"""
        normalized = "waf_blocked" if outcome == "waf_blocked" else ("success" if outcome == "success" else "other")
        key = self._risk_scope_key(candidate.group, str(getattr(candidate, "auth_key", "") or ""))
        if not key:
            return self._public_risk_status(None, None)
        with self._health_lock:
            scope = self._risk_scopes.setdefault(
                key,
                {"attempts": [], "level": 0, "until": 0, "waf_events_since_trigger": 0},
            )
            attempts = list(scope.get("attempts", []) or [])
            attempts.append(normalized)
            scope["attempts"] = attempts[-self._RISK_WINDOW_SIZE:]
            if normalized == "waf_blocked":
                scope["waf_events_since_trigger"] = int(scope.get("waf_events_since_trigger", 0) or 0) + 1
            now_ts = int(time.time())
            waf_count = sum(item == "waf_blocked" for item in scope["attempts"])
            if (
                normalized == "waf_blocked"
                and waf_count >= self._RISK_BLOCK_THRESHOLD
                and int(scope.get("waf_events_since_trigger", 0) or 0) >= self._RISK_BLOCK_THRESHOLD
                and int(scope.get("until", 0) or 0) <= now_ts
            ):
                scope["level"] = max(0, int(scope.get("level", 0) or 0)) + 1
                cooldown = self._RISK_COOLDOWN_BY_LEVEL.get(scope["level"], self._RISK_COOLDOWN_CAP_SECONDS)
                scope["until"] = now_ts + cooldown
                scope["last_event"] = "waf_blocked"
                scope["waf_events_since_trigger"] = 0
            self._save_risk_scopes_locked()
            return self._public_risk_status(key, scope)

    def release_risk_isolation_for_model(self, model: ModelConfig) -> Dict[str, Any]:
        """人工确认后仅解除当前隔离；保留匿名窗口与等级供后续阶梯判定。"""
        group = self._store.find_group(model.group_id)
        credential = self._auth_for(group, model) if group else ""
        key, scope = self._risk_scope_for(group, credential)
        if not key or not scope or int(scope.get("until", 0) or 0) <= int(time.time()):
            return {"ok": False, "code": "risk_scope_not_isolated", "message": "当前模型没有可恢复的上游风控隔离。"}
        with self._health_lock:
            scope["until"] = 0
            scope["last_event"] = "manual_recovered"
            self._save_risk_scopes_locked()
            return {
                "ok": True,
                "message": "已解除当前上游风控隔离；请谨慎恢复请求，若再次被拦截会进入更长隔离。",
                **self._public_risk_status(key, scope),
            }

    def _health_skip_reason(self, item: ModelConfig | AggregateMember) -> str:
        """返回健康状态跳过原因；到期熔断由第一个真实请求原子领取探测租约。"""
        if not self.is_policy_enabled(item):
            return ""
        now_ts = int(time.time())
        key = self._item_key(item)
        state = self.runtime_health_state(item)
        if state == "half_open_probe":
            return "half_open_probe"
        if state == "breaker_open":
            breaker_until = int(getattr(item, "breaker_until", 0) or 0)
            if breaker_until > now_ts:
                return "breaker_open"
            with self._health_lock:
                if key in self._half_open_leases:
                    return "half_open_probe"
                self._half_open_leases.add(key)
            return ""
        if state == "cooling" and int(getattr(item, "cooldown_until", 0) or 0) > now_ts:
            return "cooling"
        return ""

    def _attach_probe_keys(self, candidate: UpstreamCandidate, *items: ModelConfig | AggregateMember | None) -> None:
        # 聚合候选会先绑定底层模型、后绑定成员；必须合并而非覆盖，才能在
        # 取消、异常或下游断开时同时归还两把半开租约。
        keys = list(getattr(candidate, "health_probe_keys", ()) or ())
        keys.extend(
            self._item_key(item)
            for item in items
            if item and self._item_key(item) in self._half_open_leases
        )
        keys = list(dict.fromkeys(keys))
        if keys:
            # UpstreamCandidate 是运行态对象；动态字段不会进入配置持久化。
            setattr(candidate, "health_probe_keys", tuple(keys))

    def release_probe(self, candidate: UpstreamCandidate | None) -> None:
        """取消、下游断开或异常终态释放半开租约，不改变失败累计。"""
        if candidate is None:
            return
        keys = tuple(getattr(candidate, "health_probe_keys", ()) or ())
        if not keys:
            return
        with self._health_lock:
            self._half_open_leases.difference_update(keys)

    def _release_member_probe(self, member: AggregateMember) -> None:
        """成员未能构造完整候选时，仅归还该成员刚领取的半开 lease。"""
        with self._health_lock:
            self._half_open_leases.discard(self._item_key(member))

    def iter_candidates(
        self,
        requested_model: str | None,
        group_id: str | None = None,
    ) -> Iterator[Tuple[int, ModelConfig]]:
        self._store.refresh_expired_cooldowns()
        group = self._store.find_group(group_id) if group_id else None
        if self._is_auto_model(requested_model, group):
            requested_model = None
        for idx, model in enumerate(self._store.models):
            requested_match = bool(
                requested_model
                and requested_model in {model.id, model.name, model.ep_id}
            )
            if group_id and model.group_id != group_id:
                continue
            if requested_model and not requested_match:
                continue
            if self.risk_status_for_model(model)["risk_isolated"]:
                # 风控隔离按 host+凭证精确匹配；不同凭证的候选仍可参与 fallback。
                continue
            # A named request is allowed to make the next breaker attempt while
            # the model is only cooling.  Once the breaker opens it is still
            # excluded below.  Auto and aggregate routing retain their existing
            # cooldown behaviour.
            explicit_breaker_retry = bool(
                self.is_policy_enabled(model)
                and requested_match
                and model.health_state == "cooling"
                and model.cooldown_until
            )
            health_skip = self._health_skip_reason(model)
            if health_skip and not (health_skip == "cooling" and explicit_breaker_retry):
                continue
            # 到期 breaker 已由本请求领取半开租约，usable=False 仍是持久态
            # 标记，不能再把唯一探测请求过滤掉。其他不可用状态保持原语义。
            has_probe_lease = self._item_key(model) in self._half_open_leases
            if model.disabled_by_user or (
                not model.usable
                and not explicit_breaker_retry
                and not has_probe_lease
            ):
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
        candidate = self._candidate_type(
            idx=idx,
            group=group,
            model=model,
            label=label,
            target_model=target_model,
            auth_key=self._auth_for(group, model),
            channel=channel,
        )
        self._attach_probe_keys(candidate, model)
        return candidate

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
                candidate = self._candidate_type(
                    idx=None,
                    group=group,
                    model=None,
                    label=requested_model,
                    target_model=requested_model,
                    auth_key=self._auth_for(group, None),
                    channel="pass-through",
                )
                # proxy pass-through 没有 ModelConfig，仍必须按 host+凭证风险范围
                # 跳过；否则可绕过同一凭证下已生效的上游风控隔离。
                if not self.risk_status_for_candidate(candidate)["risk_isolated"]:
                    yield candidate
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
        if not member.enabled:
            return "member_disabled", "该聚合成员已手动停用，不参与本次调度。", group, model
        member_health_skip = self._health_skip_reason(member)
        if member_health_skip == "breaker_open":
            return "member_breaker_open", "该聚合成员已触发熔断，暂不参与本次调度。", group, model
        if member_health_skip == "half_open_probe":
            return "member_half_open_probe", "该聚合成员正在恢复试探，其他请求继续切换候选。", group, model
        if member_health_skip == "cooling":
            return "member_cooling", "该聚合成员正在冷却中，本次直接跳过。", group, model
        if not group:
            self._release_member_probe(member)
            return "underlying_group_missing", "底层连接组不存在，请检查聚合成员配置。", group, model
        if not model:
            self._release_member_probe(member)
            return "underlying_model_missing", "底层真实模型不存在，请检查聚合成员配置。", group, model
        if self.risk_status_for_model(model)["risk_isolated"]:
            self._release_member_probe(member)
            return "risk_isolated", "检测到上游风控拦截，当前凭证已暂停自动请求。", group, model
        if model.disabled_by_user:
            self._release_member_probe(member)
            return "underlying_model_disabled", "底层真实模型已停用，请先启用真实模型。", group, model
        model_health_skip = self._health_skip_reason(model)
        if model_health_skip == "breaker_open":
            self._release_member_probe(member)
            return "underlying_model_breaker_open", "底层真实模型已熔断，暂不参与本次调度。", group, model
        if model_health_skip == "half_open_probe":
            self._release_member_probe(member)
            return "underlying_model_half_open_probe", "底层真实模型正在恢复试探，其他请求继续切换候选。", group, model
        # 聚合成员和底层模型都可能在到期 breaker 时领取探测租约。只有
        # 持有模型租约的同一候选可越过 usable=False，防止成员留死 lease。
        model_has_probe_lease = self._item_key(model) in self._half_open_leases
        if not model.usable and not model_has_probe_lease:
            self._release_member_probe(member)
            return "underlying_model_disabled", "底层真实模型已停用，请先启用真实模型。", group, model
        if model_health_skip == "cooling":
            self._release_member_probe(member)
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
            self._attach_probe_keys(candidate, member)
            yield candidate

    def _record_failure(self, item: ModelConfig | AggregateMember, error: str, reason: str) -> bool:
        """按策略写入自动状态；固定冷却不累计失败、不进入 breaker。"""
        if not self.is_policy_enabled(item):
            return False
        with self._health_lock:
            now_ts = int(time.time())
            if self.routing_policy(item) == ROUTING_POLICY_FIXED_COOLDOWN:
                owner = self._store.find_group(item.group_id)
                if isinstance(item, AggregateMember):
                    owner = self._store.find_aggregate(item.aggregate_id)
                # 固定冷却复用既有作用域字段，避免配置存在第二套时长来源。
                minutes_field = "cooldown_minutes" if isinstance(item, AggregateMember) else "auto_model_cooldown_minutes"
                minutes = max(1, int(getattr(owner, minutes_field, 5) or 5))
                item.consecutive_failures = 0
                item.last_failure_at = 0
                item.last_error = self._safe_error_reference(error)
                item.last_checked_at = self._now()
                item.health_state = "cooling"
                item.cooldown_until = now_ts + minutes * 60
                item.cooldown_reason = reason[:120]
                item.breaker_until = 0
                item.breaker_reason = ""
                if isinstance(item, ModelConfig):
                    item.usable = False
                self._half_open_leases.discard(self._item_key(item))
                self._store.save()
                return True
            attempts = list(getattr(item, "attempt_window", []) or [])
            attempts.append("qualified_failure")
            item.attempt_window = attempts[-self._ATTEMPT_WINDOW_SIZE:]
            item.consecutive_failures = sum(result == "qualified_failure" for result in item.attempt_window)
            item.last_failure_at = now_ts
            item.last_error = self._safe_error_reference(error)
            item.last_checked_at = self._now()
            item.cooldown_until = 0
            item.cooldown_reason = ""
            item.breaker_until = 0
            item.breaker_reason = ""

            if item.consecutive_failures < self._BREAKER_FAILURE_THRESHOLD:
                item.health_state = "observing"
                if isinstance(item, ModelConfig) and not item.disabled_by_user:
                    item.usable = True
            else:
                item.breaker_level = max(0, int(getattr(item, "breaker_level", 0) or 0)) + 1
                cooldown_seconds = self._BREAKER_COOLDOWN_BY_LEVEL.get(
                    item.breaker_level,
                    self._BREAKER_COOLDOWN_CAP_SECONDS,
                )
                item.health_state = "breaker_open"
                item.breaker_until = now_ts + cooldown_seconds
                item.breaker_reason = reason[:120]
                if isinstance(item, ModelConfig):
                    item.usable = False

            self._half_open_leases.discard(self._item_key(item))
            self._store.save()
        return True

    def set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        """兼容旧调用点；智能策略忽略调用方传入的固定冷却秒数。"""
        del cooldown_seconds
        self._record_failure(self._store.models[idx], error, reason)

    def record_qualified_failure(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> bool:
        """仅将运行时已分类的合格失败纳入智能熔断统计。"""
        del cooldown_seconds
        return self._record_failure(self._store.models[idx], error, reason)

    def set_success(self, idx: int) -> None:
        model = self._store.models[idx]
        if not self.is_policy_enabled(model):
            return
        with self._health_lock:
            if not model.disabled_by_user:
                model.usable = True
            model.last_error = ""
            model.last_success_at = self._now()
            model.last_checked_at = model.last_success_at
            model.cooldown_until = 0
            model.cooldown_reason = ""
            model.health_state = "normal"
            if self.routing_policy(model) == ROUTING_POLICY_SMART_BREAKER:
                attempts = list(getattr(model, "attempt_window", []) or [])
                attempts.append("success")
                model.attempt_window = attempts[-self._ATTEMPT_WINDOW_SIZE:]
                model.consecutive_failures = sum(result == "qualified_failure" for result in model.attempt_window)
            else:
                model.attempt_window = []
                model.consecutive_failures = 0
                model.breaker_level = 0
            model.last_failure_at = 0
            if model.attempt_window == ["success"] * self._ATTEMPT_WINDOW_SIZE:
                model.breaker_level = 0
            model.breaker_until = 0
            model.breaker_reason = ""
            self._half_open_leases.discard(self._item_key(model))
            self._store.save()

    def set_unusable(self, idx: int, error: str) -> None:
        model = self._store.models[idx]
        if not self.is_policy_enabled(model):
            return
        model.usable = False
        model.last_error = self._safe_error_reference(error)
        model.last_checked_at = self._now()
        model.cooldown_until = 0
        model.cooldown_reason = ""
        self._half_open_leases.discard(self._item_key(model))
        self._store.save()

    def set_aggregate_member_cooldown(self, member_id: str, error: str, cooldown_seconds: int, reason: str) -> None:
        member = self._store.find_aggregate_member(member_id)
        if not member:
            return
        del cooldown_seconds
        self._record_failure(member, error, reason)

    def mark_aggregate_member_success(self, member_id: str) -> None:
        member = self._store.find_aggregate_member(member_id)
        if not member:
            return
        if not self.is_policy_enabled(member):
            return
        with self._health_lock:
            member.last_error = ""
            member.last_success_at = self._now()
            member.last_checked_at = member.last_success_at
            member.cooldown_until = 0
            member.cooldown_reason = ""
            member.health_state = "normal"
            if self.routing_policy(member) == ROUTING_POLICY_SMART_BREAKER:
                attempts = list(getattr(member, "attempt_window", []) or [])
                attempts.append("success")
                member.attempt_window = attempts[-self._ATTEMPT_WINDOW_SIZE:]
                member.consecutive_failures = sum(result == "qualified_failure" for result in member.attempt_window)
            else:
                member.attempt_window = []
                member.consecutive_failures = 0
                member.breaker_level = 0
            member.last_failure_at = 0
            if member.attempt_window == ["success"] * self._ATTEMPT_WINDOW_SIZE:
                member.breaker_level = 0
            member.breaker_until = 0
            member.breaker_reason = ""
            self._release_member_probe(member)
            self._store.save()
