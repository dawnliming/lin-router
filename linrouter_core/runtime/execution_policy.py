"""Pure execution-policy decisions for the v0.6 runtime."""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from linrouter_core.config.constants import (
    DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES,
    DEFAULT_AUTO_MODEL_NAME,
    PROVIDER_RELAY,
)
from linrouter_core.contracts.execution_ports import CandidateErrorClassification


class ExecutionPolicyService:
    """Stateless owner of candidate error and automatic-route policy decisions."""

    def __init__(
        self,
        *,
        is_rate_limited: Callable[[Optional[int], str], bool],
        is_quota_exhausted: Callable[[Optional[int], str], bool],
        is_waf_blocked_error: Callable[[Optional[int], str], bool],
        is_request_level_error: Callable[[Optional[int], str], bool],
    ) -> None:
        self._is_rate_limited = is_rate_limited
        self._is_quota_exhausted = is_quota_exhausted
        self._is_waf_blocked_error = is_waf_blocked_error
        self._is_request_level_error = is_request_level_error

    def classify_candidate_error(
        self,
        status_code: Optional[int],
        raw: str,
        error_kind: str = "http",
    ) -> CandidateErrorClassification:
        if error_kind in ("network", "stream_timeout"):
            return CandidateErrorClassification(True, False, error_kind, error_kind, "upstream")
        if status_code is None:
            return CandidateErrorClassification(True, False, "network", "network", "upstream")
        if status_code >= 500:
            return CandidateErrorClassification(True, False, "server_error", f"server_error_{status_code}", "upstream")
        if status_code == 429:
            if self._is_rate_limited(status_code, raw):
                # 短时限流可由上游自行恢复，不能污染连续失败窗口。
                return CandidateErrorClassification(False, False, "rate_limit", "rate_limit", "upstream")
            if self._is_quota_exhausted(status_code, raw):
                return CandidateErrorClassification(True, False, "quota_exhausted", "quota_exhausted", "upstream")
            return CandidateErrorClassification(False, False, "rate_limit", "rate_limit_429", "upstream")
        if self._is_waf_blocked_error(status_code, raw):
            return CandidateErrorClassification(False, True, "waf_blocked", "waf_blocked", "candidate")
        if self._is_request_level_error(status_code, raw):
            if status_code in (401, 403):
                return CandidateErrorClassification(False, True, "auth_error", "auth_error", "candidate")
            return CandidateErrorClassification(False, True, "request_level", "request_level", "request")
        return CandidateErrorClassification(False, False, "unknown", f"http_{status_code}", "upstream")

    @staticmethod
    def is_auto_model(requested_model: str | None, group: Any) -> bool:
        if not requested_model or requested_model in {DEFAULT_AUTO_MODEL_NAME, "all-router-auto"}:
            return True
        group_auto_model_name = str(getattr(group, "auto_model_name", "") or "").strip()
        return bool(group and requested_model == (group_auto_model_name or DEFAULT_AUTO_MODEL_NAME))

    @staticmethod
    def auto_cooldown_seconds(group: Any) -> int:
        if not group:
            return DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES * 60
        try:
            minutes = int(group.auto_model_cooldown_minutes)
        except Exception:
            minutes = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
        return max(0, minutes) * 60

    @staticmethod
    def waf_blocked_suffix(classification: CandidateErrorClassification, group: Any) -> str:
        if classification.category != "waf_blocked":
            return ""
        if group.provider_type == PROVIDER_RELAY and group.waf_compatible:
            return "; waf_blocked=true; message=上游中转站拦截了请求，可能是中转站账号、渠道权限、频率限制或服务商风控导致; suggestion=该连接组已开启 WAF，仍被拦截，请检查中转站后台"
        return "; waf_blocked=true; message=上游中转站拦截了请求，通常需要开启 WAF 兼容模式或调整中转站风控配置; suggestion=请在该连接组设置中开启「仅中转站 WAF 兼容」后重试"

    @staticmethod
    def waf_blocked_hint(fallback_chain: Sequence[Dict[str, Any]]) -> str:
        waf_items = [item for item in fallback_chain if str(item.get("category")) == "waf_blocked"]
        if not waf_items:
            return ""
        if any(item.get("waf_compatible") for item in waf_items):
            return " 上游中转站返回 403：Your request was blocked。该连接组已开启 WAF，可能是中转站账号、渠道权限、频率限制或服务商风控导致，请检查中转站后台。"
        return " 上游中转站返回 403：Your request was blocked。该连接组未开启 WAF 兼容，建议开启「仅中转站 WAF 兼容」后重试。"
