"""进程内显式会话粘性映射。

该服务只保存会话绑定键的不可逆摘要和候选标识；不接触配置文件、日志或请求体。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable


SESSION_HEADER = "x-linrouter-session"
MAX_SESSION_ID_LENGTH = 128
DEFAULT_IDLE_TTL_SECONDS = 30 * 60
DEFAULT_MAX_ENTRIES = 4096


@dataclass(frozen=True)
class StickyContext:
    """单次请求的匿名粘性上下文，不包含可观察的原始会话值。"""

    scope: str
    requested_model: str
    session_digest: str


@dataclass(frozen=True)
class _Binding:
    candidate_key: str
    updated_at: float


class SessionAffinityService:
    """按路由 scope 保存 LRU + TTL 的内存态候选绑定。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_IDLE_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: callable = time.monotonic,
    ) -> None:
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.RLock()
        self._bindings: OrderedDict[str, _Binding] = OrderedDict()

    @staticmethod
    def extract_session_id(incoming_headers: dict[str, str], payload: dict[str, Any]) -> tuple[str | None, str]:
        """按 Header 优先、payload 回退读取；非法值只禁用粘性而不拒绝请求。"""
        header_value = ""
        for name, value in (incoming_headers or {}).items():
            if str(name).strip().lower() == SESSION_HEADER:
                header_value = str(value or "")
                break
        raw_value = header_value if header_value.strip() else payload.get("session_id")
        if raw_value is None:
            return None, "absent"
        if not isinstance(raw_value, str):
            return None, "ignored_invalid_session"
        session_id = raw_value.strip()
        if (
            not session_id
            or len(session_id) > MAX_SESSION_ID_LENGTH
            or "\r" in session_id
            or "\n" in session_id
            or any(ord(char) < 32 or ord(char) == 127 for char in session_id)
        ):
            return None, "ignored_invalid_session"
        return session_id, "available"

    @staticmethod
    def strip_session_header(incoming_headers: dict[str, str]) -> dict[str, str]:
        """返回供上游 Header 构造和调试使用的安全副本。"""
        return {
            name: value
            for name, value in (incoming_headers or {}).items()
            if str(name).strip().lower() != SESSION_HEADER
        }

    @staticmethod
    def candidate_key(candidate: Any) -> str:
        member_id = str(getattr(candidate, "aggregate_member_id", "") or "")
        if member_id:
            return f"member:{member_id}"
        model = getattr(candidate, "model", None)
        model_id = str(getattr(model, "id", "") or "")
        return f"model:{model_id or getattr(candidate, 'target_model', '')}"

    @staticmethod
    def context(scope: str, requested_model: str, session_id: str) -> StickyContext:
        digest_input = f"{scope}\x00{requested_model}\x00{session_id}".encode("utf-8")
        return StickyContext(
            scope=scope,
            requested_model=requested_model,
            session_digest=hashlib.sha256(digest_input).hexdigest(),
        )

    @staticmethod
    def _binding_key(context: StickyContext) -> str:
        return f"{context.scope}:{context.requested_model}:{context.session_digest}"

    def prioritize(self, context: StickyContext, candidates: Iterable[Any]) -> tuple[list[Any], str]:
        """只在已通过健康筛选的候选中前置命中项，不改写正常 priority 顺序。"""
        ordered = list(candidates)
        key = self._binding_key(context)
        now = self._clock()
        with self._lock:
            binding = self._bindings.get(key)
            if binding is None:
                return ordered, "sticky_miss"
            if now - binding.updated_at >= self._ttl_seconds:
                self._bindings.pop(key, None)
                return ordered, "sticky_expired"
            for index, candidate in enumerate(ordered):
                if self.candidate_key(candidate) == binding.candidate_key:
                    self._bindings.move_to_end(key)
                    self._bindings[key] = _Binding(binding.candidate_key, now)
                    return [candidate, *ordered[:index], *ordered[index + 1:]], "sticky_hit"
            # 候选已不再可用：仅删当前 scope 的失效绑定，随后完全回退原 priority。
            self._bindings.pop(key, None)
            return ordered, "sticky_invalidated"

    def bind(self, context: StickyContext, candidate: Any) -> None:
        """仅由非流式成功或带明确完成信号的流式终态调用。"""
        key = self._binding_key(context)
        with self._lock:
            self._bindings[key] = _Binding(self.candidate_key(candidate), self._clock())
            self._bindings.move_to_end(key)
            while len(self._bindings) > self._max_entries:
                self._bindings.popitem(last=False)

    def invalidate_scope(self, scope: str) -> None:
        """配置成功变更后按 group/aggregate scope 精确清理。"""
        with self._lock:
            for key in [key for key in self._bindings if key.startswith(f"{scope}:")]:
                self._bindings.pop(key, None)
