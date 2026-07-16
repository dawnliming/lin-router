from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import ssl

try:
    import certifi
    _ssl_context = ssl.create_default_context(cafile=certifi.where())
except Exception:  # certifi 未安装时回退到系统默认，Windows 不受影响
    _ssl_context = ssl.create_default_context()

from linrouter_platform import get_platform
from settings_store import SettingsStore
from debug_capture import DebugCapture
from upstream_client import StreamIdleTimeoutError as UpstreamStreamIdleTimeoutError


from linrouter_core.config.constants import (
    DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES,
    DEFAULT_AUTO_MODEL_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_CONFIG_FILE,
    DEFAULT_PUBLIC_API_KEY,
    DEFAULT_START_PORT,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    GLOBAL_ROUTE_GROUP_ID,
    MAX_STREAM_IDLE_TIMEOUT_SECONDS,
    PROVIDER_ARK,
    PROVIDER_PROXY,
    PROVIDER_RELAY,
    new_aggregate_route_key,
    new_route_key,
)
from linrouter_core.config.models import AggregateMember, AggregateModel, ConnectionGroup, ModelConfig
from linrouter_core.config.store import ConfigStore
from linrouter_core.observability import ObservabilityService, RequestLog
from linrouter_core.contracts import AllModelsFailedError, RouteContext, StreamIdleTimeoutError, UpstreamCandidate
from linrouter_core.contracts.execution_ports import CandidateErrorClassification
from linrouter_core.runtime import (
    CandidateHealthService,
    CandidateRuntime,
    ExecutionPolicyService,
    NonStreamExecutionService,
    SerialProtectionState,
    StreamExecutionService,
)
from linrouter_core.runtime.http_api_runtime import handle_delete, handle_get, handle_post, handle_put
from linrouter_core.runtime.execution_runtime_ports import (
    CandidateStatePort,
    ConcurrencyPort as RuntimeConcurrencyPort,
    DebugCapturePort,
    ExecutionFaults,
    ObservabilityPort,
    RequestPreparationPort,
    StreamLifecyclePort,
)
from linrouter_core.runtime.app_runtime import (
    create_application_server,
    ensure_initial_config as _ensure_initial_config,
    pick_port as _pick_port,
    run_main,
)
from linrouter_core.upstream import UpstreamAdapter
from linrouter_core.upstream import request as upstream_request

MAX_PORT_SCAN = 1

BLOCKED_FORWARD_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "transfer-encoding",
    "host",
    "openai-organization",
    "openai-project",
    "x-request-id",
}

WAF_STRIP_PREFIXES = (
    "x-stainless-",
)

WAF_STRIP_EXACT = {
    "host",
    "connection",
    "content-length",
    "user-agent",
    "cache-control",
    "pragma",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "authorization",
    "openai-organization",
    "openai-project",
    "x-request-id",
}

PASSTHROUGH_STRIP_EXACT = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "authorization",
}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def render_index_page() -> str:
    page_path = get_platform().get_resource_path("static", "index.html")
    html = page_path.read_text(encoding="utf-8")
    return html.replace("__AUTO_MODEL_NAME__", DEFAULT_AUTO_MODEL_NAME)



def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def build_upstream_headers(api_key: str, *, stream: bool) -> Dict[str, str]:
    return upstream_request.build_upstream_headers(api_key, stream=stream)


def build_waf_compatible_headers(incoming_headers: Dict[str, str], upstream_host: str, *, stream: bool) -> Dict[str, str]:
    return upstream_request.build_waf_compatible_headers(incoming_headers, upstream_host, stream=stream)


def build_passthrough_headers(api_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
    return upstream_request.build_passthrough_headers(api_key, incoming_headers, stream=stream)


def build_model_fetch_headers(auth_key: str) -> Dict[str, str]:
    return upstream_request.build_model_fetch_headers(auth_key)


def can_forward_header(name: str) -> bool:
    return upstream_request.can_forward_header(name)


def parse_bearer_key(auth_header: str) -> str:
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


REQUEST_LEVEL_ERROR_TYPES = {
    "invalid_request_error",
    "unsupported_parameter_error",
    "unsupported_parameter",
    "content_policy",
    "content_policy_violation",
    "request_format_error",
    "invalid_message_format",
    "authentication_error",
    "permission_error",
}


class ArkProxyRouter:
    @property
    def logs(self) -> List[RequestLog]:
        return self.observability.logs if hasattr(self, "observability") else self._legacy_logs

    @logs.setter
    def logs(self, value: List[RequestLog]) -> None:
        if hasattr(self, "observability"):
            self.observability.logs = value
        else:
            self._legacy_logs = value

    @property
    def log_file(self) -> Path:
        return self.observability.log_file if hasattr(self, "observability") else self._log_file

    @log_file.setter
    def log_file(self, value: str | Path) -> None:
        path = Path(value)
        self._log_file = path
        if hasattr(self, "observability"):
            self.observability.set_log_file(path)

    def __init__(
        self,
        store: ConfigStore,
        settings_store: Optional[SettingsStore] = None,
        log_file: Optional[str | Path] = None,
        upstream_adapter: Optional[UpstreamAdapter] = None,
    ) -> None:
        self.store = store
        self.settings_store = settings_store
        self.log_file = Path(log_file) if log_file else self._resolve_log_file()
        self.observability = ObservabilityService(
            self.log_file,
            now=self._now,
            sanitize_detail=self._sanitize_detail,
        )
        self.log_write_error = self.observability.log_write_error
        self._runtime_locks = SerialProtectionState()
        # Health checks are isolated from production observability and candidate health.
        self._speed_test_state: Dict[str, Dict[str, Any]] = {}
        self._speed_test_guard = threading.Lock()
        # 旧属性 facade：保留给运行时诊断和外部调用者。
        self.upstream_locks = self._runtime_locks.locks
        self.upstream_active_streams = self._runtime_locks.active_streams
        self.upstream_locks_guard = self._runtime_locks.guard
        self._upstream_candidate_type = UpstreamCandidate
        self._all_models_failed_error_type = AllModelsFailedError
        self._stream_idle_timeout_error_type = StreamIdleTimeoutError
        self._route_context_type = RouteContext
        self.execution_policy = ExecutionPolicyService(
            is_rate_limited=self._is_rate_limited,
            is_quota_exhausted=self._is_quota_exhausted,
            is_waf_blocked_error=self._is_waf_blocked_error,
            is_request_level_error=self._is_request_level_error,
        )
        self.candidate_health = CandidateHealthService(
            store,
            now=self._now,
            is_auto_model=self._is_auto_model,
            mode_for=self._mode_for,
            group_for=self._group_for,
            auth_for=self._auth_for,
            candidate_type=UpstreamCandidate,
            log_aggregate_member_skip=self._log_aggregate_member_skip,
            breaker_enabled=lambda: bool(self.settings_store and self.settings_store.get("smart_breaker_enabled", False)),
        )
        # The compatibility facade only composes ports; execution loops are owned by CandidateRuntime.
        self.upstream_adapter = upstream_adapter or UpstreamAdapter(_ssl_context)
        self._upstream_client = self._create_upstream_client()
        # 供 DebugCapture 的旧两参数构造路径读取；避免 debug_capture.py 反向 import app。
        self._debug_capture_browser_user_agent = BROWSER_UA
        self._debug_capture_ssl_context = _ssl_context
        self.debug_capture = DebugCapture(
            self,
            settings_store,
            browser_user_agent=BROWSER_UA,
            ssl_context=_ssl_context,
            empty_usage=self._empty_usage,
            usage_from_stream_chunk=self._usage_from_stream_chunk,
        )
        candidate_state = CandidateStatePort(
            refresh=lambda: getattr(self.store, "refresh_expired_cooldowns", lambda: None)(),
            find_group=lambda group_id: getattr(self.store, "find_group", lambda _group_id: None)(group_id),
            route_group_id=self._route_group_id,
            resolve_aggregate=self._resolve_aggregate,
            supports_requested_model=self.candidate_health.supports_group_requested_model,
            iter_candidates=self._iter_upstream_candidates,
            iter_aggregate=self._iter_aggregate_candidates,
            aggregate_cooldown_seconds=self._aggregate_cooldown_seconds,
            set_aggregate_cooldown=self._set_aggregate_member_cooldown,
            set_cooldown=self._set_cooldown,
            record_qualified_failure=self._record_qualified_failure,
            set_unusable=self._set_unusable,
            mark_success=self._mark_success,
            mark_aggregate_success=self._mark_aggregate_member_success,
            mark_unusable=self._mark_unusable,
        )
        preparation = RequestPreparationPort(
            resolve_url=self._resolve_url,
            tools_enabled=self._tools_order_enabled,
            normalize_tools=self._normalize_tools_order,
            body_for=self._body_for_upstream,
            headers_for=self._headers_for,
            aggregate_log_suffix=self._aggregate_log_suffix,
            debug_detail=self._debug_detail,
            short_error=self._short_error,
            fingerprint=self._payload_fingerprint,
        )
        concurrency = RuntimeConcurrencyPort(
            candidate_lock=self._candidate_lock,
            acquire=self._acquire_upstream_lock,
            release=self._release_lock,
            busy_detail=self._serial_protection_busy_detail,
            mark_stream_active=self._mark_stream_active,
        )
        stream_lifecycle = StreamLifecyclePort(
            idle_timeout=self._stream_idle_timeout_seconds,
            readline=self._readline_with_idle_timeout,
            response_usage=self._usage_from_response,
            chunk_usage=self._usage_from_stream_chunk,
            chunk_usage_with_presence=self._usage_from_stream_chunk_with_presence,
            completion_signal=self._stream_completion_signal,
            mark_timeout=self._mark_stream_timeout,
        )
        observability = ObservabilityPort(
            start=self._live_request_start,
            update=self._live_request_update,
            finish=self._live_request_finish,
            add_log=self.add_log,
            patch_stream=self.patch_stream_lifecycle,
            cancellation_requested=self._cancellation_requested,
            set_response=self._set_live_response,
            close_response=self._close_live_response,
        )
        self.runtime = CandidateRuntime(
            self.candidate_health, candidate_state, self.execution_policy, preparation,
            self._upstream_client, concurrency, stream_lifecycle, observability,
            DebugCapturePort(self.debug_capture.capture),
            ExecutionFaults(AllModelsFailedError, StreamIdleTimeoutError, RouteContext),
        )
        self.non_stream_execution = NonStreamExecutionService(self.runtime)
        self.stream_execution = StreamExecutionService(self.runtime)

    def _create_upstream_client(self) -> "UpstreamClient":
        from upstream_client import UpstreamClient

        if self.settings_store is None:
            return UpstreamClient(client_type="urllib", ssl_context=_ssl_context)
        client_type = str(self.settings_store.get("upstream_http_client", "urllib")).lower()
        http2 = bool(self.settings_store.get("upstream_http2", False))
        keepalive = bool(self.settings_store.get("upstream_keepalive", False))
        return UpstreamClient(client_type=client_type, http2=http2, keepalive=keepalive, ssl_context=_ssl_context)

    def _refresh_upstream_client(self) -> None:
        if self.settings_store is None:
            return
        client_type = str(self.settings_store.get("upstream_http_client", "urllib")).lower()
        http2 = bool(self.settings_store.get("upstream_http2", False))
        keepalive = bool(self.settings_store.get("upstream_keepalive", False))
        current = self._upstream_client
        if (
            current.client_type != client_type
            or current.http2 != http2
            or current.keepalive != keepalive
        ):
            current.close()
            self._upstream_client = UpstreamClient(client_type=client_type, http2=http2, keepalive=keepalive, ssl_context=_ssl_context)

    def _resolve_log_file(self) -> Path:
        candidates = [
            get_platform().get_log_dir() / "lin-router-logs.jsonl",
            Path.home() / ".lin-router" / "logs" / "lin-router-logs.jsonl",
            Path(tempfile.gettempdir()) / "lin-router-logs.jsonl",
        ]
        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                with candidate.open("a", encoding="utf-8"):
                    pass
                return candidate
            except Exception:
                continue
        return candidates[-1]

    def add_log(
        self,
        path: str,
        model: str,
        status: str,
        detail: str = "",
        duration_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        group: Optional[ConnectionGroup] = None,
        event: str = "",
        request_id: str = "",
        attempt: int = 0,
        usage_source: str = "",
        cooldown_applied: bool = False,
        failure_scope: str = "",
    ) -> None:
        self.observability.add_log(
            path, model, status, detail, duration_ms, prompt_tokens, completion_tokens, total_tokens,
            cached_tokens, reasoning_tokens, group, event, request_id, attempt, usage_source,
            cooldown_applied, failure_scope,
        )
        self.logs = self.observability.logs
        self.log_write_error = self.observability.log_write_error

    def _trim_log_file(self, max_lines: int = 5000) -> None:
        # 兼容 facade：实际 JSONL 滚动由 observability repository 执行。
        self.observability.trim(max_lines)
        self.log_write_error = self.observability.log_write_error

    @staticmethod
    def _detail_value(detail: str, key: str) -> str:
        matches = re.findall(rf"(?:^|; ){re.escape(key)}=([^;]*)", detail or "")
        return matches[-1].strip() if matches else ""

    @staticmethod
    def _infer_event(status: str, detail: str) -> str:
        text = f"{status} {detail}".lower()
        if "cooldown" in text:
            return "cooldown"
        if "retry ok" in text:
            return "retry_ok"
        if "try next" in text:
            return "fallback"
        if "stream ok" in text:
            return "stream_ok"
        if "skip" in text or "missing upstream api key" in text:
            return "skip"
        if "network" in text:
            return "network"
        if str(status).startswith("2"):
            return "ok"
        return "error"

    def _sanitize_detail(self, detail: str) -> str:
        if not detail:
            return ""
        safe = str(detail)
        secrets: List[str] = []
        for group in self.store.groups:
            secrets.extend([group.ark_api_key, group.api_key])
        for model in self.store.models:
            secrets.append(model.api_key)
        for secret in secrets:
            if secret and secret in safe:
                safe = safe.replace(secret, mask_secret(secret))
        return safe

    @staticmethod
    def _log_from_row(row: Dict[str, Any]) -> RequestLog:
        return ObservabilityService._log_from_row(row)

    def _load_log_file(self) -> None:
        # 兼容 facade：服务构造时已加载，显式调用仍可重新加载。
        self.observability.load()
        self.logs = self.observability.logs
        self.log_write_error = self.observability.log_write_error

    def _append_log_file(self, item: RequestLog) -> None:
        # 兼容 facade；生产写入应经 add_log/service 统一处理。
        self.observability._append(item)
        self.log_write_error = self.observability.log_write_error

    def recent_logs(self) -> List[Dict[str, Any]]:
        return self.observability.recent_logs()

    def runtime_activity_since(self, cursor: str = "", limit: int = 30) -> Dict[str, Any]:
        """提供管理台活动增量；日志查询 API 仍由持久化历史接口负责。"""
        return self.observability.runtime_activity_since(cursor, limit)

    def all_logs(self) -> List[RequestLog]:
        return self.observability.all_logs()

    def clear_logs(self) -> None:
        self.observability.clear_logs()
        self.logs = self.observability.logs

    def _find_stream_log(
        self,
        request_id: str,
        attempt: Optional[int] = None,
        candidate_label: str = "",
    ) -> Optional[RequestLog]:
        return self.observability._find_stream_log(request_id, attempt, candidate_label)

    def patch_stream_lifecycle(
        self,
        request_id: str,
        attempt: int,
        candidate_label: str,
        usage: Tuple[int, int, int, int, int],
        usage_source: str,
        *,
        final_status: str,
        lifecycle: str,
        final_result: str,
        chunks_received: int,
        bytes_received: int,
        duration_ms: Optional[int] = None,
        lock_wait_ms: Optional[int] = None,
        lock_release_reason: str = "",
        cooldown_applied: Optional[bool] = None,
        failure_scope: str = "",
        completion_signal: str = "",
        final_event: str = "",
        stream_metrics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        patched = self.observability.patch_stream_lifecycle(
            request_id, attempt, candidate_label, usage, usage_source,
            final_status=final_status, lifecycle=lifecycle, final_result=final_result,
            chunks_received=chunks_received, bytes_received=bytes_received, duration_ms=duration_ms,
            lock_wait_ms=lock_wait_ms, lock_release_reason=lock_release_reason,
            cooldown_applied=cooldown_applied, failure_scope=failure_scope,
            completion_signal=completion_signal, final_event=final_event, stream_metrics=stream_metrics,
        )
        self.log_write_error = self.observability.log_write_error
        return patched

    def update_latest_stream_usage(
        self,
        request_id: str,
        usage: Tuple[int, int, int, int, int],
        usage_source: str,
        *,
        lock_wait_ms: Optional[int] = None,
        lock_release_reason: str = "",
    ) -> None:
        item = self._find_stream_log(request_id)
        if item:
            self.patch_stream_lifecycle(
                request_id, item.attempt, item.model, usage, usage_source, final_status=item.status,
                lifecycle="stream_usage_updated", final_result="streaming",
                chunks_received=int(self._detail_value(item.detail, "chunks_received") or 0),
                bytes_received=int(self._detail_value(item.detail, "bytes_received") or 0),
                lock_wait_ms=lock_wait_ms, lock_release_reason=lock_release_reason,
            )

    def finalize_stream_if_needed(self, request_id: str) -> None:
        if not request_id:
            return
        item = self._find_stream_log(request_id)
        if not item or "stream_finalized=true" in item.detail:
            return
        try:
            started_at_ms = int(self._detail_value(item.detail, "stream_started_at_ms") or 0)
        except ValueError:
            started_at_ms = 0
        duration_ms = max(item.duration_ms, int(time.time() * 1000) - started_at_ms) if started_at_ms else item.duration_ms
        self.patch_stream_lifecycle(
            request_id, item.attempt, item.model,
            (item.prompt_tokens, item.completion_tokens, item.total_tokens, item.cached_tokens, item.reasoning_tokens),
            "stream_incomplete", final_status="client_disconnected", lifecycle="client_disconnected",
            final_result="client_disconnected", chunks_received=int(self._detail_value(item.detail, "chunks_received") or 1),
            bytes_received=int(self._detail_value(item.detail, "bytes_received") or 0), duration_ms=duration_ms,
            lock_release_reason="client_disconnect", failure_scope="request",
        )

    def _rewrite_log_file(self) -> None:
        self.observability.rewrite_log_file()
        self.log_write_error = self.observability.log_write_error

    def export_logs_csv(self) -> str:
        return self.observability.export_logs_csv()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    @staticmethod
    def _is_quota_exhausted(status_code: Optional[int], body: str) -> bool:
        if status_code != 429 or ArkProxyRouter._is_rate_limited(status_code, body):
            return False
        body_lower = body.lower()
        quota_markers = (
            "quotaexceeded",
            "setlimitexceeded",
            "insufficientquota",
            "insufficient_quota",
            "free trial quota exhausted",
            "quota exhausted",
            "reached the set inference limit",
            "model service has been paused",
            "余额不足",
            "额度不足",
            "额度已用完",
            "配额不足",
            "配额已用完",
        )
        return any(marker in body_lower for marker in quota_markers) or status_code == 429

    @staticmethod
    def _is_rate_limited(status_code: Optional[int], body: str) -> bool:
        return status_code == 429 and "RateLimitExceeded" in body

    @staticmethod
    def _is_server_error(status_code: Optional[int]) -> bool:
        return status_code is not None and status_code >= 500

    @staticmethod
    def _upstream_error_type(raw: str) -> Optional[str]:
        """从上游 JSON 错误体中提取 error.type。"""
        try:
            data = json.loads(raw)
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("type") or "").strip() or None
        except Exception:
            pass
        return None

    @staticmethod
    def _is_request_level_error(status_code: Optional[int], raw: str) -> bool:
        """判断是否为不应触发 cooldown 的请求级/鉴权错误。"""
        if status_code in (401, 403):
            return True
        if status_code == 400:
            err_type = ArkProxyRouter._upstream_error_type(raw)
            if err_type in REQUEST_LEVEL_ERROR_TYPES:
                return True
            lower = raw.lower()
            if any(k in lower for k in ("invalid request", "unsupported parameter", "content policy", "bad request")):
                return True
        if status_code is not None and 400 <= status_code < 500 and status_code != 429:
            return True
        return False

    @staticmethod
    def _is_waf_blocked_error(status_code: Optional[int], raw: str) -> bool:
        """识别上游中转站 WAF/风控拦截类 403 错误。"""
        if status_code != 403:
            return False
        lower = raw.lower()
        markers = (
            "your request was blocked",
            "request was blocked",
            "blocked by waf",
            "waf blocked",
            "access denied",
            "blocked",
            "waf",
            "风控",
        )
        return any(marker in lower for marker in markers)

    def _classify_candidate_error(
        self,
        status_code: Optional[int],
        raw: str,
        error_kind: str = "http",
    ) -> CandidateErrorClassification:
        return self.execution_policy.classify_candidate_error(status_code, raw, error_kind)

    def _waf_blocked_suffix(self, classification: CandidateErrorClassification, group: ConnectionGroup) -> str:
        """Compatibility facade for the WAF-detail policy decision."""
        return self.execution_policy.waf_blocked_suffix(classification, group)

    def _waf_blocked_hint(self, fallback_chain: List[Dict[str, Any]]) -> str:
        """Compatibility facade for the WAF fallback-chain policy decision."""
        return self.execution_policy.waf_blocked_hint(fallback_chain)

    def _resolve_url(self, base_url: str, path: str) -> str:
        return self.upstream_adapter.resolve_endpoint(base_url, path)

    @staticmethod
    def _empty_usage() -> Tuple[int, int, int, int, int]:
        return 0, 0, 0, 0, 0

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value or 0)
        except Exception:
            return 0

    @staticmethod
    def _usage_from_payload(payload: Any) -> Tuple[int, int, int, int, int]:
        if not isinstance(payload, dict):
            return ArkProxyRouter._empty_usage()
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            response = payload.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return ArkProxyRouter._empty_usage()

        prompt_tokens = ArkProxyRouter._int_value(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion_tokens = ArkProxyRouter._int_value(usage.get("completion_tokens") or usage.get("output_tokens"))
        total_tokens = ArkProxyRouter._int_value(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)

        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
        cached_tokens = ArkProxyRouter._int_value(prompt_details.get("cached_tokens") or input_details.get("cached_tokens"))
        reasoning_tokens = ArkProxyRouter._int_value(output_details.get("reasoning_tokens"))
        return prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens

    @staticmethod
    def _usage_from_response(data: bytes) -> Tuple[int, int, int, int, int]:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return ArkProxyRouter._empty_usage()
        return ArkProxyRouter._usage_from_payload(payload)

    @staticmethod
    def _usage_from_payload_with_presence(payload: Any) -> Tuple[Tuple[int, int, int, int, int], bool]:
        if not isinstance(payload, dict):
            return ArkProxyRouter._empty_usage(), False
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            response = payload.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            return ArkProxyRouter._empty_usage(), False
        return ArkProxyRouter._usage_from_payload(payload), True

    @staticmethod
    def _usage_from_stream_chunk_with_presence(chunk: bytes) -> Tuple[Tuple[int, int, int, int, int], bool]:
        """Extract the latest explicit usage payload from an SSE frame, including all-zero usage."""
        usage = ArkProxyRouter._empty_usage()
        present = False
        for line in chunk.decode("utf-8", "ignore").splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except Exception:
                continue
            parsed, parsed_present = ArkProxyRouter._usage_from_payload_with_presence(payload)
            if parsed_present:
                usage = parsed
                present = True
        return usage, present

    @staticmethod
    def _usage_from_stream_chunk(chunk: bytes) -> Tuple[int, int, int, int, int]:
        """Compatibility wrapper for callers that only need usage values."""
        usage, _ = ArkProxyRouter._usage_from_stream_chunk_with_presence(chunk)
        return usage

    @staticmethod
    def _stream_completion_signal(chunk: bytes) -> str:
        """解析结构化 SSE 终态信号，不依赖 TCP EOF。"""
        text = chunk.decode("utf-8", "ignore").strip()
        if not text:
            return ""
        event_signal = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    return "[DONE]"
                try:
                    payload = json.loads(data)
                except Exception:
                    continue
                signal = str(payload.get("type") or payload.get("event") or "").strip().lower()
                if signal in {"response.completed", "response.failed", "response.incomplete"}:
                    return signal
                response = payload.get("response")
                if isinstance(response, dict):
                    status = str(response.get("status") or "").strip().lower()
                    if status in {"completed", "failed", "incomplete"}:
                        return f"response.{status}"
            elif line.startswith("event:"):
                signal = line[6:].strip().lower()
                if signal in {"response.completed", "response.failed", "response.incomplete"}:
                    event_signal = f"event:{signal}"
        return event_signal

    def default_model(self) -> Optional[ModelConfig]:
        return next((m for m in self.store.models if m.usable), None)

    @staticmethod
    def group_auto_model_name(group: ConnectionGroup | None) -> str:
        if group and group.auto_model_name and group.auto_model_name.strip():
            return group.auto_model_name.strip()
        return DEFAULT_AUTO_MODEL_NAME

    def _is_auto_model(self, requested_model: str | None, group: ConnectionGroup | None = None) -> bool:
        return self.execution_policy.is_auto_model(requested_model, group)

    def _iter_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Tuple[int, ModelConfig]]:
        yield from self.runtime.iter_candidates(requested_model, group_id)

    def _group_for(self, model: ModelConfig) -> Optional[ConnectionGroup]:
        return self.store.find_group(model.group_id)

    @staticmethod
    def _mode_for(group: Optional[ConnectionGroup]) -> str:
        return group.provider_type if group and group.provider_type else PROVIDER_ARK

    def _hit_detail(self, group: ConnectionGroup, model: ModelConfig, requested_label: str, suffix: str) -> str:
        mode = self._mode_for(group)
        channel = f"; channel={model.price_group}" if mode == PROVIDER_RELAY and model.price_group else ""
        return f"mode={mode}; hit={model.ep_id}{channel}; requested={requested_label}; {suffix}"

    def _candidate_hit_detail(self, candidate: UpstreamCandidate, requested_label: str, suffix: str) -> str:
        mode = self._mode_for(candidate.group)
        channel = f"; channel={candidate.channel}" if candidate.channel else ""
        waf = "; waf=on" if candidate.group.provider_type == PROVIDER_RELAY and candidate.group.waf_compatible else ""
        aggregate = ""
        if candidate.aggregate_id:
            aggregate = f"; aggregate={candidate.aggregate_name}; aggregate_id={candidate.aggregate_id}; aggregate_member_id={candidate.aggregate_member_id}"
        return f"mode={mode}{waf}; hit={candidate.target_model}{channel}; requested={requested_label}{aggregate}; {suffix}"

    @staticmethod
    def _aggregate_log_suffix(
        resolved_as: str,
        aggregate_model: str,
        aggregate_id: str,
        selected_group: str,
        selected_model: str,
        selected_upstream_model: str,
        selection_reason: str,
        fallback_index: int,
        fallback_chain: List[Dict[str, Any]],
        strategy: str = "priority",
        manual_price: float | None = None,
    ) -> str:
        chain_str = ""
        if fallback_chain:
            chain_str = "; fallback_chain=" + json.dumps(fallback_chain, ensure_ascii=False, separators=(",", ":"))
        price_str = f"; manual_price={manual_price}" if manual_price is not None else ""
        return (
            f"resolved_as={resolved_as}"
            f"; aggregate_model={aggregate_model}"
            f"; aggregate_id={aggregate_id}"
            f"; selected_group={selected_group}"
            f"; selected_model={selected_model}"
            f"; selected_upstream_model={selected_upstream_model}"
            f"; selection_reason={selection_reason}"
            f"; fallback_index={fallback_index}"
            f"; strategy={strategy}"
            f"{price_str}"
            f"{chain_str}"
        )

    @staticmethod
    def _body_sha256(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()[:16]

    @staticmethod
    def _normalize_for_cache(value: Any) -> Any:
        volatile_keys = {
            "id",
            "created",
            "object",
            "request_id",
            "x-request-id",
            "response_id",
            "previous_response_id",
            "trace_id",
            "tool_call_id",
            "run_id",
            "session_id",
        }
        if isinstance(value, dict):
            items: Dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in volatile_keys:
                    continue
                items[str(key)] = ArkProxyRouter._normalize_for_cache(item)
            return items
        if isinstance(value, list):
            return [ArkProxyRouter._normalize_for_cache(item) for item in value]
        return value

    @staticmethod
    def _normalized_body_sha256(payload: Dict[str, Any]) -> str:
        normalized = ArkProxyRouter._normalize_for_cache(payload)
        text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _hash_json(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _safe_value_sha256(value: Any) -> str:
        """Return a stable value fingerprint without keeping the original value."""
        try:
            return ArkProxyRouter._hash_json(value)
        except (TypeError, ValueError):
            # Request payloads should be JSON-compatible, but logging must stay
            # safe even when a caller passes an unexpected Python object.
            return hashlib.sha256(type(value).__name__.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _safe_reasoning_effort(value: Any) -> Tuple[str, str, str, int]:
        """Classify reasoning effort without persisting unrecognized input."""
        allowed_efforts = {"low", "medium", "high", "xhigh", "max", "ultra"}
        if isinstance(value, str) and value.lower() in allowed_efforts:
            return "recognized", value.lower(), "", ArkProxyRouter._json_bytes(value)
        return "unrecognized", "unrecognized", ArkProxyRouter._safe_value_sha256(value), ArkProxyRouter._json_bytes(value)

    @staticmethod
    def _json_bytes(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return len(str(value).encode("utf-8"))

    @staticmethod
    def _cache_prefix_fingerprint(payload: Dict[str, Any], body: bytes) -> str:
        parts = [
            f"body_len={len(body)}",
            f"body_4k={hashlib.sha256(body[:4096]).hexdigest()[:16]}",
            f"body_16k={hashlib.sha256(body[:16384]).hexdigest()[:16]}",
            f"body_64k={hashlib.sha256(body[:65536]).hexdigest()[:16]}",
            f"body_128k={hashlib.sha256(body[:131072]).hexdigest()[:16]}",
            f"body_256k={hashlib.sha256(body[:262144]).hexdigest()[:16]}",
            f"body_all={hashlib.sha256(body).hexdigest()[:16]}",
        ]
        messages = payload.get("messages")
        system_bytes = 0
        developer_bytes = 0
        user_assistant_bytes = 0
        tool_result_bytes = 0
        if isinstance(messages, list):
            normalized_messages = ArkProxyRouter._normalize_for_cache(messages)
            for count in (1, 4, 16, 32, 64):
                if len(normalized_messages) >= count:
                    parts.append(f"msg_{count}={ArkProxyRouter._hash_json(normalized_messages[:count])}")
            parts.append(f"msg_all={ArkProxyRouter._hash_json(normalized_messages)}")
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                content = message.get("content")
                size = ArkProxyRouter._json_bytes(content)
                if role == "system":
                    system_bytes += size
                elif role == "developer":
                    developer_bytes += size
                elif role in ("user", "assistant"):
                    user_assistant_bytes += size
                elif role == "tool":
                    tool_result_bytes += size
        parts.append(f"system_bytes={system_bytes}")
        parts.append(f"developer_bytes={developer_bytes}")
        parts.append(f"user_assistant_bytes={user_assistant_bytes}")
        parts.append(f"tool_result_bytes={tool_result_bytes}")
        tools = payload.get("tools")
        tools_bytes = 0
        tools_count = 0
        if isinstance(tools, list):
            tools_count = len(tools)
            tools_bytes = ArkProxyRouter._json_bytes(tools)
            normalized_tools = ArkProxyRouter._normalize_for_cache(tools)
            parts.append(f"tools_count={tools_count}")
            parts.append(f"tools_bytes={tools_bytes}")
            parts.append(f"tools_hash={ArkProxyRouter._hash_json(normalized_tools)}")
        else:
            parts.append("tools_count=0")
            parts.append("tools_bytes=0")
        return "; ".join(parts)

    @staticmethod
    def _safe_header_view(headers: Dict[str, str]) -> str:
        interesting = {
            "accept",
            "accept-language",
            "cache-control",
            "content-length",
            "content-type",
            "origin",
            "pragma",
            "referer",
            "user-agent",
        }
        items: List[str] = []
        x_headers: List[str] = []
        seen: set[str] = set()
        for name, value in headers.items():
            lower = name.strip().lower()
            if lower in seen:
                continue
            seen.add(lower)
            if lower in interesting:
                # Header values are user-controlled on passthrough routes and
                # can carry URLs or sensitive metadata.  Persist presence only.
                items.append(f"{lower}=present")
            elif lower.startswith("x-"):
                x_headers.append("x")
        if x_headers:
            items.append("x-headers=present")
        return "; ".join(items) if items else "headers=none"

    @staticmethod
    def _safe_upstream_target(target_url: str) -> Tuple[str, str]:
        """Keep a stable endpoint identifier without persisting URL components."""
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        origin_hash = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:12] if origin else "unknown"
        path = parsed.path or "/"
        known_endpoints = {"/v1/chat/completions", "/chat/completions", "/v1/responses", "/v1/models"}
        endpoint = path if path in known_endpoints else f"custom_path_sha256:{hashlib.sha256(path.encode('utf-8')).hexdigest()[:12]}"
        return origin_hash, endpoint

    @staticmethod
    def _safe_request_media_mode(value: str) -> str:
        text = str(value or "").lower()
        if "text/event-stream" in text:
            return "text_event_stream"
        if "application/json" in text:
            return "application_json"
        return "other" if text.strip() else "missing"

    @staticmethod
    def _payload_fingerprint(payload: Dict[str, Any], body: bytes, path: str = "", tools_normalized: bool = False) -> str:
        value_keys = [
            "model",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "service_tier",
            "tool_choice",
        ]
        boolean_keys = ("stream", "parallel_tool_calls", "store")
        parts: List[str] = []
        for key in value_keys:
            if key in payload:
                value = payload.get(key)
                parts.append(f"{key}_present=true")
                parts.append(f"{key}_sha256={ArkProxyRouter._safe_value_sha256(value)}")
                parts.append(f"{key}_bytes={ArkProxyRouter._json_bytes(value)}")
        for key in boolean_keys:
            if key in payload:
                value = payload.get(key)
                if isinstance(value, bool):
                    parts.append(f"{key}={'true' if value else 'false'}")
                else:
                    parts.append(f"{key}_present=true")
                    parts.append(f"{key}_sha256={ArkProxyRouter._safe_value_sha256(value)}")
                    parts.append(f"{key}_bytes={ArkProxyRouter._json_bytes(value)}")
        if "reasoning_effort" in payload:
            value_status, safe_effort, value_hash, value_bytes = ArkProxyRouter._safe_reasoning_effort(payload.get("reasoning_effort"))
            parts.append(f"reasoning_effort={safe_effort}")
            parts.append(f"reasoning_effort_status={value_status}")
            parts.append(f"reasoning_effort_bytes={value_bytes}")
            if value_hash:
                parts.append(f"reasoning_effort_sha256={value_hash}")
        messages = payload.get("messages")
        messages_count = 0
        tool_result_bytes = 0
        if isinstance(messages, list):
            messages_count = len(messages)
            roles: List[str] = []
            content_chars = 0
            for message in messages:
                if isinstance(message, dict):
                    roles.append(str(message.get("role") or "?"))
                    try:
                        content_chars += len(json.dumps(message.get("content"), ensure_ascii=False, separators=(",", ":")))
                    except Exception:
                        content_chars += len(str(message.get("content")))
                    if message.get("role") == "tool":
                        tool_result_bytes += ArkProxyRouter._json_bytes(message.get("content"))
            parts.append(f"messages={messages_count}")
            parts.append(f"roles_sha256={ArkProxyRouter._safe_value_sha256(roles)}")
            parts.append(f"content_chars={content_chars}")
        # Responses API 结构化统计
        is_responses = path == "/v1/responses" or "input" in payload
        if is_responses:
            input_value = payload.get("input")
            input_items = 0
            input_bytes = 0
            if isinstance(input_value, list):
                input_items = len(input_value)
                input_bytes = ArkProxyRouter._json_bytes(input_value)
            elif input_value is not None:
                input_bytes = ArkProxyRouter._json_bytes(input_value)
            parts.append(f"responses_input_present={'true' if input_value is not None else 'false'}")
            parts.append(f"responses_input_items={input_items}")
            parts.append(f"responses_input_bytes={input_bytes}")
            parts.append(f"instructions_bytes={ArkProxyRouter._json_bytes(payload.get('instructions'))}")
            parts.append(f"previous_response_id_present={'true' if payload.get('previous_response_id') else 'false'}")
            response_tools = payload.get("tools")
            if isinstance(response_tools, list):
                parts.append(f"response_tools_count={len(response_tools)}")
                parts.append(f"response_tools_bytes={ArkProxyRouter._json_bytes(response_tools)}")
            else:
                parts.append("response_tools_count=0")
                parts.append("response_tools_bytes=0")
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                parts.append(f"response_metadata_keys={len(list(metadata.keys()))}")
            else:
                parts.append("response_metadata_keys=0")
        tools = payload.get("tools")
        tools_bytes = 0
        if isinstance(tools, list):
            tools_bytes = ArkProxyRouter._json_bytes(tools)
            parts.append(f"tools_count={len(tools)}")
            parts.append(f"tools_bytes={tools_bytes}")
            parts.append(f"tools_sha256={ArkProxyRouter._safe_value_sha256(tools)}")
        else:
            parts.append("tools_count=0")
            parts.append("tools_bytes=0")
        functions = payload.get("functions")
        if isinstance(functions, list):
            parts.append(f"functions_count={len(functions)}")
            parts.append(f"functions_bytes={ArkProxyRouter._json_bytes(functions)}")
            parts.append(f"functions_sha256={ArkProxyRouter._safe_value_sha256(functions)}")
        else:
            parts.append("functions_count=0")
            parts.append("functions_bytes=0")
        stream_options = payload.get("stream_options")
        if isinstance(stream_options, dict):
            parts.append("stream_options_present=true")
            parts.append(f"stream_options_keys_count={len(stream_options)}")
            parts.append(f"stream_options_bytes={ArkProxyRouter._json_bytes(stream_options)}")
            parts.append(f"stream_options_sha256={ArkProxyRouter._safe_value_sha256(stream_options)}")
        elif "stream_options" in payload:
            parts.append("stream_options_present=true")
            parts.append("stream_options_keys_count=0")
            parts.append(f"stream_options_bytes={ArkProxyRouter._json_bytes(stream_options)}")
            parts.append(f"stream_options_sha256={ArkProxyRouter._safe_value_sha256(stream_options)}")
        else:
            parts.append("stream_options_present=false")
            parts.append("stream_options_keys_count=0")
            parts.append("stream_options_bytes=0")
        if tools_normalized:
            parts.append("tools_normalized=true")
        # Payload 减重预警标记
        body_len = len(body)
        if body_len > 262144:
            parts.append("payload_very_large=true")
        elif body_len > 131072:
            parts.append("payload_large=true")
        if tools_bytes > 65536:
            parts.append("tools_large=true")
        if tool_result_bytes > 65536:
            parts.append("tool_results_large=true")
        if messages_count > 60:
            parts.append("messages_many=true")
        parts.append(f"body_len={body_len}")
        parts.append(f"body_sha256={ArkProxyRouter._body_sha256(body)}")
        parts.append(f"normalized_sha256={ArkProxyRouter._normalized_body_sha256(payload)}")
        parts.append(f"prefix=({ArkProxyRouter._cache_prefix_fingerprint(payload, body)})")
        return "; ".join(parts)

    @staticmethod
    def _reasoning_log_fields(path: str, payload: Dict[str, Any], body: bytes, body_mode: str, group: ConnectionGroup) -> str:
        normalized_path = str(path or '').rstrip('/')
        if normalized_path.endswith('/responses'):
            request_api = 'responses'
            reasoning = payload.get('reasoning')
            field_present = isinstance(reasoning, dict) and 'effort' in reasoning
            effort = reasoning.get('effort') if isinstance(reasoning, dict) else None
            field_source = 'reasoning.effort' if field_present else 'none'
        elif normalized_path.endswith('/chat/completions'):
            request_api = 'chat_completions'
            field_present = 'reasoning_effort' in payload
            effort = payload.get('reasoning_effort')
            field_source = 'reasoning_effort' if field_present else 'none'
        else:
            request_api = 'unknown'
            effort = None
            field_present = False
            field_source = 'none'

        if not field_present:
            requested_effort = 'unset'
            value_status = 'absent'
            preserved = 'n/a'
            effort_hash = ''
            effort_bytes = 0
        else:
            value_status, requested_effort, effort_hash, effort_bytes = ArkProxyRouter._safe_reasoning_effort(effort)
            preserved = False
            try:
                outbound = json.loads(body.decode('utf-8'))
                if field_source == 'reasoning.effort':
                    outbound_reasoning = outbound.get('reasoning') if isinstance(outbound, dict) else None
                    preserved = isinstance(outbound_reasoning, dict) and outbound_reasoning.get('effort') == effort
                else:
                    preserved = isinstance(outbound, dict) and outbound.get('reasoning_effort') == effort
            except Exception:
                preserved = False
        support = str(getattr(group, 'reasoning_support', 'unknown') or 'unknown').lower()
        if support not in {'supported', 'unsupported', 'unknown'}:
            support = 'unknown'
        effort_detail = f'; reasoning_effort_bytes={effort_bytes}'
        if effort_hash:
            effort_detail += f'; reasoning_effort_sha256={effort_hash}'
        return (
            f'request_api={request_api}'
            f'; requested_reasoning_effort={requested_effort}'
            f'; reasoning_field_source={field_source}'
            f'; reasoning_value_status={value_status}'
            f'; reasoning_preserved={preserved if isinstance(preserved, str) else str(preserved).lower()}'
            f'{effort_detail}'
            f'; upstream_reasoning_support={support}'
            f'; body_mode={body_mode}'
        )
    def _debug_detail(
        self,
        candidate: UpstreamCandidate,
        requested_label: str,
        target_url: str,
        body_mode: str,
        body: bytes,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        suffix: str,
        resp: Optional[Any] = None,
        tools_normalized: bool = False,
        lock_wait_ms: Optional[int] = None,
        lock_release_reason: str = "",
        aggregate_suffix: str = "",
    ) -> str:
        if aggregate_suffix:
            suffix = f"{suffix}; {aggregate_suffix}" if suffix else aggregate_suffix
        base = self._candidate_hit_detail(candidate, requested_label, suffix)
        group_name = str(candidate.group.name).replace(";", ",")
        # mode=passthrough 表示该请求走透传路径（ark/proxy），relay 专属逻辑不介入
        mode_tag = "passthrough" if candidate.group.provider_type != PROVIDER_RELAY else "relay"
        upstream_origin_hash, upstream_endpoint = self._safe_upstream_target(target_url)
        request_path = urlparse(target_url).path
        lower_headers = {k.lower(): v for k, v in headers.items()}
        waf_applied = lower_headers.get("user-agent", "") == BROWSER_UA
        header_policy = "waf_browser" if waf_applied else "passthrough"
        accept = lower_headers.get("accept", "")
        content_type = lower_headers.get("content-type", "")
        user_agent = lower_headers.get("user-agent", "")
        user_agent_family = self._user_agent_family(user_agent)
        serial_protection_enabled = self._candidate_lock_enabled(candidate, headers)
        request_concurrency = "serial_protection" if serial_protection_enabled else "parallel"
        configured_http_client = getattr(self, "_upstream_client", None) and getattr(self._upstream_client, "client_type", "urllib") or "urllib"
        http_client = getattr(resp, "transport", "") if resp else configured_http_client
        http_version = str(getattr(resp, "http_version", "") or "") if resp else ""
        extra = (
            f"; header_policy={header_policy}"
            f"; accept_mode={self._safe_request_media_mode(accept)}"
            f"; content_type_mode={self._safe_request_media_mode(content_type)}"
            f"; user_agent_family={user_agent_family}"
            f"; waf_compatible={'true' if candidate.group.waf_compatible else 'false'}"
            f"; waf_client_mode={str(getattr(candidate.group, 'waf_client_mode', 'always') or 'always')}"
            f"; waf_applied={str(waf_applied).lower()}"
            f"; waf_decision={'waf_compatible' if waf_applied else self._waf_decision(candidate.group, headers)}"
            f"; client_family={self._incoming_client_family(headers)}"
            f"; serial_protection_enabled={str(serial_protection_enabled).lower()}"
            f"; request_concurrency={request_concurrency}"
            f"; http_client={http_client}"
            f"; upstream_http_version={http_version or '-'}"
        )
        if lock_wait_ms is not None:
            extra += f"; lock_wait_ms={lock_wait_ms}"
        if lock_release_reason:
            extra += f"; lock_release_reason={lock_release_reason}"
        return (
            f"{base}; group_id={candidate.group.id}; group_name={group_name}; provider={candidate.group.provider_type}; mode={mode_tag}; "
            f"upstream_origin_hash={upstream_origin_hash}; upstream_endpoint={upstream_endpoint}; body={body_mode}; {self._reasoning_log_fields(request_path, payload, body, body_mode, candidate.group)}; "
            f"fingerprint=({self._payload_fingerprint(payload, body, request_path, tools_normalized=tools_normalized)}); "
            f"out_headers=({self._safe_header_view(headers)})"
            f"{extra}"
        )

    @staticmethod
    def _user_agent_family(user_agent: str) -> str:
        ua = str(user_agent).lower()
        if "codex" in ua:
            return "codex"
        if any(k in ua for k in ("chrome", "safari", "firefox", "edge", "mozilla")):
            return "browser"
        return "other"

    @staticmethod
    def _short_error(raw: str, limit: int = 900) -> str:
        """Return correlation metadata without persisting an upstream body."""
        del limit  # Kept for callers that still supply the legacy argument.
        text = str(raw or "")
        if not text:
            return "empty"
        encoded = text.encode("utf-8", "replace")
        return f"redacted_sha256:{hashlib.sha256(encoded).hexdigest()[:16]},bytes:{len(encoded)}"

    def _tools_order_enabled(self) -> bool:
        if self.settings_store is None:
            return False
        return bool(self.settings_store.get("normalize_tools_order", False))

    @staticmethod
    def _normalize_tools_order(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        tools = payload.get("tools")
        if not isinstance(tools, list) or len(tools) <= 1:
            return payload, False
        try:
            def _tool_key(item: Any) -> str:
                if not isinstance(item, dict):
                    return ""
                fn = item.get("function")
                if isinstance(fn, dict):
                    return str(fn.get("name") or "")
                return str(item.get("name") or item.get("type") or "")

            sorted_tools = sorted(tools, key=_tool_key)
            if sorted_tools == tools:
                return payload, False
            new_payload = dict(payload)
            new_payload["tools"] = sorted_tools
            return new_payload, True
        except Exception:
            return payload, False

    def _auth_for(self, group: ConnectionGroup, model: Optional[ModelConfig]) -> str:
        mode = self._mode_for(group)
        if mode == PROVIDER_RELAY:
            return model.api_key if model else ""
        if mode == PROVIDER_PROXY:
            return group.api_key or group.ark_api_key
        return group.ark_api_key

    @staticmethod
    def _incoming_client_family(incoming_headers: Dict[str, str]) -> str:
        headers = {str(name).lower(): str(value) for name, value in (incoming_headers or {}).items()}
        user_agent = headers.get("user-agent", "").lower()
        if "codex" in user_agent or any(name.startswith("x-codex-") for name in headers):
            return "codex"
        if any(marker in user_agent for marker in ("hermes", "claude-code", "openai-cli")):
            return "agent"
        if any(marker in user_agent for marker in ("chrome", "safari", "firefox", "edge", "mozilla")):
            return "browser"
        return "other"

    def _waf_decision(self, group: ConnectionGroup, incoming_headers: Dict[str, str]) -> str:
        if group.provider_type != PROVIDER_RELAY or not group.waf_compatible:
            return "disabled"
        mode = str(getattr(group, "waf_client_mode", "always") or "always").lower()
        if mode == "auto_bypass_codex" and self._incoming_client_family(incoming_headers) == "codex":
            return "codex_direct"
        return "waf_compatible"

    def _headers_for(self, group: ConnectionGroup, auth_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
        waf_compatible = self._waf_decision(group, incoming_headers) == "waf_compatible"
        return self.upstream_adapter.build_request(
            base_url=group.base_url,
            auth_key=auth_key,
            incoming_headers=incoming_headers,
            stream=stream,
            waf_compatible=waf_compatible,
            waf_accept_policy=str(group.waf_accept_policy or "default"),
        )

    def _candidate_from_model(self, idx: int, model: ModelConfig, group: ConnectionGroup) -> UpstreamCandidate:
        return self.runtime.candidate_from_model(idx, model, group)

    def _candidate_lock(self, candidate: UpstreamCandidate, incoming_headers: Optional[Dict[str, str]] = None) -> Optional[threading.Lock]:
        return self._runtime_locks.lock_for(candidate, self._candidate_lock_enabled(candidate, incoming_headers))

    def _candidate_lock_key(self, candidate: UpstreamCandidate) -> str:
        return self._runtime_locks.key(candidate)

    def _active_stream_count(self, candidate: UpstreamCandidate) -> int:
        return self._runtime_locks.active_count(candidate)

    def _mark_stream_active(self, candidate: UpstreamCandidate, delta: int) -> None:
        self._runtime_locks.mark_stream_active(candidate, delta)

    def _serial_protection_busy_detail(self, candidate: UpstreamCandidate, body: bytes, lock_wait_ms: int) -> str:
        return self._runtime_locks.busy_detail(candidate, body, lock_wait_ms)

    # 兼容旧 facade；WAF Header 策略不再管理此状态。
    def _waf_lock_busy_detail(self, candidate: UpstreamCandidate, body: bytes, lock_wait_ms: int) -> str:
        return self._serial_protection_busy_detail(candidate, body, lock_wait_ms)

    def _candidate_lock_enabled(self, candidate: UpstreamCandidate, incoming_headers: Optional[Dict[str, str]] = None) -> bool:
        return candidate.group.provider_type == PROVIDER_RELAY and bool(getattr(candidate.group, "serial_protection", False))

    @staticmethod
    def _release_lock(lock: Optional[threading.Lock]) -> bool:
        if not lock:
            return False
        lock.release()
        return True

    def _acquire_upstream_lock(self, lock: Optional[threading.Lock], timeout: float = 10.0, request_id: str = "") -> Tuple[bool, int]:
        """Acquire serial protection in short waits so an active request can be cancelled promptly."""
        if not lock:
            return True, 0
        started = time.perf_counter()
        while True:
            remaining = timeout - (time.perf_counter() - started)
            if remaining <= 0:
                return False, int((time.perf_counter() - started) * 1000)
            if lock.acquire(timeout=min(0.1, remaining)):
                return True, int((time.perf_counter() - started) * 1000)
            if request_id and self._cancellation_requested(request_id):
                return False, int((time.perf_counter() - started) * 1000)

    @staticmethod
    def _append_detail(detail: str, suffix: str) -> str:
        if not detail:
            return suffix
        return f"{detail}; {suffix}"

    def _live_request_start(self, request_id: str, path: str, requested_model: str, *, stream: bool) -> None:
        self.observability.start_live_request(request_id, path, requested_model, stream=stream)

    def _live_request_update(self, request_id: str, **patch: Any) -> None:
        self.observability.update_live_request(request_id, **patch)

    def _live_request_finish(self, request_id: str, status: str = "done") -> None:
        self.observability.finish_live_request(request_id, status)

    def _cancellation_requested(self, request_id: str) -> bool:
        return self.observability.cancellation_requested(request_id)

    def is_live_request_cancelled(self, request_id: str) -> bool:
        return self.observability.cancellation_requested(request_id)

    def _set_live_response(self, request_id: str, response: Any) -> None:
        self.observability.set_live_response(request_id, response)

    def _close_live_response(self, request_id: str, response: Any = None) -> bool:
        return self.observability.close_live_response(request_id, response)

    def cancel_live_request(self, request_id: str, source: str = "dashboard") -> Dict[str, Any]:
        return self.observability.request_cancellation(request_id, source)

    def record_stream_transport_event(self, request_id: str, event: str) -> None:
        self.observability.record_stream_transport_event(request_id, event)

    def live_requests_payload(self) -> Dict[str, Any]:
        return self.observability.live_requests_payload()

    def diagnose_request(self, request_id: str) -> Dict[str, Any]:
        return self.observability.diagnose_request(request_id)

    def _diagnose_logs(self, logs: List[RequestLog]) -> Dict[str, Any]:
        return self.observability.diagnose_logs(logs)

    def _begin_speed_test(self, key: str) -> Dict[str, Any] | None:
        with self._speed_test_guard:
            previous = self._speed_test_state.get(key, {})
            if previous.get("running"):
                return {"ok": False, "code": "speed_test_running", "message": "该对象正在测速，请等待本次完成"}
            if time.time() - float(previous.get("completed_at", 0)) < 60:
                return {"ok": False, "code": "speed_test_rate_limited", "message": "刚完成测速，请稍后再试", "result": previous.get("result")}
            self._speed_test_state[key] = {"running": True}
        return None

    def _finish_speed_test(self, key: str, result: Dict[str, Any]) -> Dict[str, Any]:
        with self._speed_test_guard:
            self._speed_test_state[key] = {"running": False, "completed_at": time.time(), "result": result}
        return result

    def speed_test_group(self, group_id: str) -> Dict[str, Any]:
        blocked = self._begin_speed_test(f"group:{group_id}")
        if blocked:
            return blocked
        group = self.store.find_group(group_id)
        if not group:
            return self._finish_speed_test(f"group:{group_id}", {"ok": False, "code": "group_not_found", "message": "连接组不存在"})
        started = time.perf_counter()
        results = []
        for idx, model in enumerate(self.store.models):
            if model.group_id != group_id or not model.usable or model.disabled_by_user:
                continue
            candidate = self._candidate_from_model(idx, model, group)
            probe_started = time.perf_counter()
            ok, reason, detail = self._manual_probe_candidate(candidate)
            results.append({"model": model.name, "group": group.name, "ok": ok, "reason": reason, "message": self._manual_probe_summary(reason, detail), "detail": detail, "total_ms": round((time.perf_counter() - probe_started) * 1000, 2)})
        success_count = sum(1 for item in results if item["ok"])
        result = {"ok": success_count > 0, "results": results, "completed": len(results), "attempts": len(results), "success": success_count, "failure": len(results) - success_count, "fallback": False, "total_ms": round((time.perf_counter() - started) * 1000, 2), "message": "测速完成" if results else "该连接组没有可测速模型", "source": "health_check"}
        return self._finish_speed_test(f"group:{group_id}", result)

    def speed_test_aggregate(self, aggregate_id: str) -> Dict[str, Any]:
        blocked = self._begin_speed_test(f"aggregate:{aggregate_id}")
        if blocked:
            return blocked
        aggregate = self.store.find_aggregate(aggregate_id)
        if not aggregate:
            return self._finish_speed_test(f"aggregate:{aggregate_id}", {"ok": False, "code": "aggregate_not_found", "message": "聚合模型不存在"})
        started = time.perf_counter()
        results = []
        for candidate in self.runtime.iter_aggregate_candidates(aggregate):
            probe_started = time.perf_counter()
            ok, reason, detail = self._manual_probe_candidate(candidate)
            results.append({"model": candidate.label, "group": candidate.group.name, "ok": ok, "reason": reason, "message": self._manual_probe_summary(reason, detail), "detail": detail, "total_ms": round((time.perf_counter() - probe_started) * 1000, 2)})
            if ok:
                break
        result = {"ok": any(item["ok"] for item in results), "aggregate": aggregate.name, "results": results, "attempts": len(results), "fallback": len(results) > 1, "total_ms": round((time.perf_counter() - started) * 1000, 2), "message": "测速成功" if any(item["ok"] for item in results) else "聚合模型当前没有可用成员", "source": "health_check"}
        if not results:
            result["code"] = "aggregate_members_unavailable"
        return self._finish_speed_test(f"aggregate:{aggregate_id}", result)

    def _manual_probe_candidate(self, candidate: UpstreamCandidate) -> Tuple[bool, str, str]:
        """对单个候选执行最小非流式探测，不计入正式请求与收益统计。"""
        if not candidate.auth_key:
            return False, "missing_upstream_api_key", "缺少上游 API Key"
        target_url = self._resolve_url(candidate.group.base_url, "/v1/chat/completions")
        payload = {
            "model": candidate.target_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._headers_for(candidate.group, candidate.auth_key, {}, stream=False)
        upstream_lock = self._candidate_lock(candidate)
        acquired, _ = self._acquire_upstream_lock(upstream_lock, timeout=5.0)
        if not acquired:
            return False, "serial_protection_wait_timeout", "该连接组已开启串行保护，候选仍在处理请求"
        try:
            with self._upstream_client.request("POST", target_url, headers, body, stream=False, timeout=20) as resp:
                resp.read()
                if 200 <= int(resp.status) < 300:
                    return True, "probe_ok", "最小探测成功"
                return False, f"http_{resp.status}", f"上游返回 HTTP {resp.status}"
        except HTTPError as err:
            raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
            classification = self._classify_candidate_error(err.code, raw, "http")
            return False, classification.log_reason, self._short_error(raw)
        except (URLError, TimeoutError, OSError) as err:
            return False, "network", self._short_error(str(err))
        finally:
            self._release_lock(upstream_lock)

    @staticmethod
    def _manual_probe_summary(reason: str, detail: str) -> str:
        normalized_reason = str(reason or '').lower()
        if normalized_reason in {'serial_protection_wait_timeout', 'waf_lock_wait_timeout'}:
            return '该连接组已开启串行保护，候选仍在处理请求；未判定为上游故障。'
        if normalized_reason.startswith('server_error_'):
            return f'上游服务暂时异常（HTTP {normalized_reason.rsplit("_", 1)[-1]}）。'
        if normalized_reason.startswith('http_'):
            return f'上游返回 HTTP {normalized_reason.rsplit("_", 1)[-1]}。'
        if normalized_reason in {'network', 'read_timeout', 'stream_idle_timeout'}:
            return '连接或等待上游响应超时，请稍后重试。'
        if normalized_reason == 'missing_upstream_api_key':
            return '缺少上游 API Key。'
        return '最小探测未通过，请检查上游服务状态。'

    @staticmethod
    def _manual_probe_failure_scope(reason: str) -> Tuple[str, bool]:
        normalized = str(reason or '').lower()
        if normalized in {'serial_protection_wait_timeout', 'waf_lock_wait_timeout'}:
            return 'local_lock', False
        if normalized in {'waf_blocked', 'request_level'}:
            return 'request', False
        if normalized in {'auth_error', 'missing_upstream_api_key'}:
            return 'candidate', False
        if normalized.startswith('http_'):
            try:
                status_code = int(normalized.rsplit('_', 1)[-1])
            except ValueError:
                status_code = 0
            if 400 <= status_code < 500 and status_code != 429:
                return 'request', False
        return 'upstream', True

    def _finalize_failed_probe_state(self, item: Any, before: Dict[str, Any], cooldown_applied: bool) -> None:
        if not cooldown_applied:
            item.health_state = str(before.get('health_state') or 'normal')
        elif item.health_state == 'half_open_probe':
            # Breaker disabled: regular cooldown still applies, but the
            # breaker-only probe state must not remain visible or routable.
            item.health_state = 'normal'
        self.store.save()

    def recover_model(self, model_id: str) -> Dict[str, Any]:
        model = self.store.find_model(model_id)
        if not model:
            return {"ok": False, "message": "模型不存在", "code": "model_not_found"}
        if model.disabled_by_user or (not model.usable and not model.cooldown_until):
            return {"ok": False, "message": "该模型为手动停用状态，请先手动启用；系统不会自动恢复手动停用模型。", "code": "manual_disabled"}
        group = self.store.find_group(model.group_id)
        if not group:
            return {"ok": False, "message": "模型所属连接组不存在，无法执行探测。", "code": "group_not_found"}
        before = asdict(model)
        model.health_state = "half_open_probe"
        self.store.save()
        candidate = self._candidate_from_model(self.store.models.index(model), model, group)
        ok, reason, detail = self._manual_probe_candidate(candidate)
        if not ok:
            failure_scope, cooldown_applied = self._manual_probe_failure_scope(reason)
            if cooldown_applied:
                self._set_cooldown(candidate.idx, self._manual_probe_summary(reason, detail), self._auto_cooldown_seconds(group), reason)
            self._finalize_failed_probe_state(model, before, cooldown_applied)
            summary = self._manual_probe_summary(reason, detail)
            probe_request_id = f"manual-probe-{uuid.uuid4().hex}"
            self.add_log("/api/models/recover", model.name, "probe_failed", f"manual_probe=true; model_id={model.id}; probe_result=failed; reason={reason}; summary={summary}; cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}", group=group, request_id=probe_request_id, event="manual_probe", usage_source="manual_probe", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
            message = "该连接组已开启串行保护，候选正忙；模型保持当前状态，请稍后重试。" if not cooldown_applied else "最小探测未通过，模型保持冷却，请稍后重试或检查上游服务。"
            return {"ok": False, "message": message, "code": "probe_failed", "before": before, "model": asdict(model)}
        self._set_success(candidate.idx)
        model.usable = True
        model.disabled_by_user = False
        self.store.save()
        self.add_log("/api/models/recover", model.name, "probe_ok", f"manual_probe=true; model_id={model.id}; probe_result=success; summary=最小探测成功，模型已恢复参与调度。; cooldown_applied=false; failure_scope=manual", group=group, request_id=f"manual-probe-{uuid.uuid4().hex}", event="manual_probe", usage_source="manual_probe", failure_scope="manual")
        return {"ok": True, "message": "最小探测成功，已恢复该模型参与调度。", "model": asdict(model), "before": before}

    def recover_aggregate_member(self, member_id: str) -> Dict[str, Any]:
        member = self.store.find_aggregate_member(member_id)
        if not member:
            return {"ok": False, "message": "成员不存在", "code": "aggregate_member_not_found"}
        if member.enabled is False:
            return {"ok": False, "message": "该聚合成员已手动停用，请先手动启用；系统不会自动恢复手动停用成员。", "code": "manual_disabled"}
        model = self.store.find_model(member.model_id)
        group = self.store.find_group(member.group_id)
        aggregate = self.store.find_aggregate(member.aggregate_id)
        if not model or not group:
            return {"ok": False, "message": "成员底层模型或连接组不存在，无法执行探测。", "code": "member_target_missing"}
        if model.disabled_by_user or not model.usable and not model.cooldown_until:
            return {"ok": False, "message": "底层模型为手动停用状态，不能自动恢复聚合成员。", "code": "underlying_manual_disabled"}
        before = asdict(member)
        member.health_state = "half_open_probe"
        self.store.save()
        candidate = self._candidate_from_model(self.store.models.index(model), model, group)
        ok, reason, detail = self._manual_probe_candidate(candidate)
        if not ok:
            failure_scope, cooldown_applied = self._manual_probe_failure_scope(reason)
            if cooldown_applied:
                self._set_aggregate_member_cooldown(member.id, self._manual_probe_summary(reason, detail), self._aggregate_cooldown_seconds(aggregate) if aggregate else self._auto_cooldown_seconds(group), reason)
            self._finalize_failed_probe_state(member, before, cooldown_applied)
            summary = self._manual_probe_summary(reason, detail)
            probe_request_id = f"manual-probe-{uuid.uuid4().hex}"
            self.add_log("/api/aggregate-members/recover", model.name, "probe_failed", f"manual_probe=true; aggregate_member_id={member.id}; probe_result=failed; reason={reason}; summary={summary}; cooldown_applied={str(cooldown_applied).lower()}; failure_scope={failure_scope}", group=group, request_id=probe_request_id, event="manual_probe", usage_source="manual_probe", cooldown_applied=cooldown_applied, failure_scope=failure_scope)
            message = "该连接组已开启串行保护，候选正忙；成员保持当前状态，请稍后重试。" if not cooldown_applied else "最小探测未通过，成员保持冷却，请稍后重试或检查上游服务。"
            return {"ok": False, "message": message, "code": "probe_failed", "before": before, "member": asdict(member)}
        self._mark_aggregate_member_success(member.id)
        self.store.save()
        self.add_log("/api/aggregate-members/recover", model.name, "probe_ok", f"manual_probe=true; aggregate_member_id={member.id}; probe_result=success; summary=最小探测成功，成员已恢复参与调度。; cooldown_applied=false; failure_scope=manual", group=group, request_id=f"manual-probe-{uuid.uuid4().hex}", event="manual_probe", usage_source="manual_probe", failure_scope="manual")
        return {"ok": True, "message": "最小探测成功，已恢复该聚合成员参与调度。", "member": asdict(member), "before": before}
    def _iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[UpstreamCandidate]:
        yield from self.runtime.iter_upstream_candidates(requested_model, group_id)

    def _resolve_aggregate(
        self,
        requested_model: str | None,
        route: RouteContext | str | None,
    ) -> Optional[Tuple[AggregateModel, str]]:
        """解析聚合模型。返回 (AggregateModel, resolved_as)。

        resolved_as:
        - "aggregate": 直接命中 AggregateModel.name
        - all-router-auto 与旧全局默认聚合模型已退役，不再映射
        """
        if not requested_model:
            return None
        aggregate = getattr(route, "aggregate", None) if isinstance(route, RouteContext) else None
        if aggregate:
            if not aggregate.enabled:
                raise AllModelsFailedError(
                    f"聚合模型 {aggregate.name} 已禁用",
                    attempted=0,
                    error_code="aggregate_disabled",
                )
            if requested_model == aggregate.name:
                return aggregate, "aggregate"
            if requested_model in aggregate.client_model_aliases:
                return aggregate, "aggregate_alias"
            # 聚合模型 Key 只能请求自身聚合模型名或已配置别名
            raise AllModelsFailedError(
                f"聚合模型 Key 只能请求 {aggregate.name} 或已配置客户端别名",
                attempted=0,
                error_code="model_not_found",
            )
        # 连接组 Key / 旧全局 Key 不再通过 name 命中聚合模型
        if requested_model == "all-router-auto":
            raise AllModelsFailedError(
                "all-router-auto 已停用，请改用具体聚合模型名和聚合模型 Key",
                attempted=0,
                error_code="global_auto_deprecated",
            )
        return None

    def _aggregate_member_skip_reason(self, member: AggregateMember) -> Tuple[str, str, Optional[ConnectionGroup], Optional[ModelConfig]]:
        """返回聚合成员跳过原因；空 reason 表示可参与调度。"""
        return self.runtime.aggregate_member_skip_reason(member)

    def _aggregate_member_usable(self, member: AggregateMember) -> bool:
        """检查聚合成员是否可用（存在、启用、未 cooldown、真实模型未被用户手动禁用）。"""
        reason, _, _, _ = self._aggregate_member_skip_reason(member)
        return not reason

    def _log_aggregate_member_skip(
        self,
        path: str,
        aggregate: AggregateModel,
        member: AggregateMember,
        reason: str,
        message: str,
        group: Optional[ConnectionGroup],
        model: Optional[ModelConfig],
        requested_label: str,
        request_id: str,
        resolved_as: str,
    ) -> None:
        selected_group = group.name if group else member.group_id
        selected_model = model.name if model else member.model_id
        selected_upstream_model = (model.upstream_model or model.ep_id) if model else ""
        suffix = self._aggregate_log_suffix(
            resolved_as=resolved_as,
            aggregate_model=aggregate.name,
            aggregate_id=aggregate.id,
            selected_group=selected_group,
            selected_model=selected_model,
            selected_upstream_model=selected_upstream_model,
            selection_reason=f"skip_{reason}",
            fallback_index=0,
            fallback_chain=[],
            strategy=aggregate.strategy or "priority",
            manual_price=member.manual_price,
        )
        detail = (
            f"requested={requested_label}; skip_reason={reason}; aggregate_member_id={member.id}; "
            f"cooldown_applied=false; failure_scope=skip; {message}; {suffix}"
        )
        label = selected_model or member.model_id or member.id
        self.add_log(path, label, "skip", detail, group=group, event="skip", request_id=request_id, attempt=0, cooldown_applied=False, failure_scope="skip")

    def _iter_aggregate_candidates(
        self,
        aggregate: AggregateModel,
        *,
        log_skips: bool = False,
        path: str = "",
        requested_label: str = "",
        request_id: str = "",
        resolved_as: str = "",
    ) -> Iterator[UpstreamCandidate]:
        """兼容 facade：由 M3a runtime 保持成员排序、跳过与日志时机。"""
        yield from self.runtime.iter_aggregate_candidates(
            aggregate,
            log_skips=log_skips,
            path=path,
            requested_label=requested_label,
            request_id=request_id,
            resolved_as=resolved_as,
        )

    def _aggregate_cooldown_seconds(self, aggregate: AggregateModel) -> int:
        try:
            minutes = int(aggregate.cooldown_minutes)
        except Exception:
            minutes = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
        return max(0, minutes) * 60

    def _set_aggregate_member_cooldown(self, member_id: str, error: str, cooldown_seconds: int, reason: str) -> None:
        self.candidate_health.set_aggregate_member_cooldown(member_id, error, cooldown_seconds, reason)

    def _mark_aggregate_member_success(self, member_id: str) -> None:
        self.candidate_health.mark_aggregate_member_success(member_id)

    def _set_unusable(self, idx: int, error: str) -> None:
        self.candidate_health.set_unusable(idx, error)

    def _set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        self.candidate_health.set_cooldown(idx, error, cooldown_seconds, reason)

    def _record_qualified_failure(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> bool:
        return self.candidate_health.record_qualified_failure(idx, error, cooldown_seconds, reason)

    def _set_success(self, idx: int) -> None:
        self.candidate_health.set_success(idx)

    def _mark_unusable(self, candidate: UpstreamCandidate, error: str) -> None:
        if candidate.idx is not None:
            self._set_unusable(candidate.idx, error)

    def _mark_success(self, candidate: UpstreamCandidate) -> None:
        if candidate.idx is not None:
            self._set_success(candidate.idx)

    @staticmethod
    def _route_group_id(route: RouteContext | str | None) -> str | None:
        if isinstance(route, RouteContext):
            return route.group_id
        return route

    def _auto_cooldown_seconds(self, group: Optional[ConnectionGroup]) -> int:
        return self.execution_policy.auto_cooldown_seconds(group)

    @staticmethod
    def _stream_idle_timeout_seconds(group: Optional[ConnectionGroup]) -> int:
        if not group:
            return DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        try:
            seconds = int(group.stream_idle_timeout)
        except Exception:
            seconds = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
        return max(0, min(MAX_STREAM_IDLE_TIMEOUT_SECONDS, seconds))

    def _mark_stream_timeout(self, candidate: UpstreamCandidate, error: str) -> int:
        if candidate.idx is None:
            return 0
        cooldown_seconds = self._auto_cooldown_seconds(candidate.group)
        self._set_cooldown(candidate.idx, error, cooldown_seconds, "stream_timeout")
        return cooldown_seconds

    @staticmethod
    def _readline_with_idle_timeout(resp: Any, timeout_seconds: int) -> bytes:
        # UpstreamResponse 等包装对象支持带超时的 readline
        try:
            if hasattr(resp, "readline") and callable(getattr(resp, "readline")):
                try:
                    return resp.readline(timeout_seconds)
                except TypeError:
                    pass
            if timeout_seconds <= 0:
                return resp.readline()
            result: queue.Queue[Any] = queue.Queue(maxsize=1)

            def read_once() -> None:
                try:
                    result.put(resp.readline())
                except Exception as exc:
                    result.put(exc)

            worker = threading.Thread(target=read_once, daemon=True)
            worker.start()
            try:
                item = result.get(timeout=timeout_seconds)
            except queue.Empty as exc:
                raise StreamIdleTimeoutError("stream_idle_timeout") from exc
            if isinstance(item, Exception):
                raise item
            return item
        except UpstreamStreamIdleTimeoutError as exc:
            raise StreamIdleTimeoutError("stream_idle_timeout") from exc

    @staticmethod
    def _body_for_upstream(payload: Dict[str, Any], raw_body: bytes | None, requested_model: str | None, target_model: str) -> Tuple[bytes, str]:
        if raw_body and requested_model:
            if requested_model == target_model:
                return raw_body, "raw"
            target = json.dumps(target_model, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            source_variants = {
                json.dumps(requested_model, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                json.dumps(requested_model, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
            }
            for source in source_variants:
                pattern = rb'("model"\s*:\s*)' + re.escape(source)
                patched, count = re.subn(pattern, rb"\1" + target, raw_body, count=1)
                if count:
                    return patched, "raw-model-patch"
        outbound_payload = dict(payload)
        outbound_payload["model"] = target_model
        return json.dumps(outbound_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "json-rebuild"

    def call(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None, incoming_headers: Optional[Dict[str, str]] = None, raw_body: bytes | None = None) -> Tuple[int, Dict[str, str], bytes]:
        return self.non_stream_execution.execute(path, payload, route, incoming_headers, raw_body)

    def stream(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None, incoming_headers: Optional[Dict[str, str]] = None, raw_body: bytes | None = None) -> Tuple[int, Dict[str, str], Iterable[bytes], str]:
        return self.stream_execution.execute(path, payload, route, incoming_headers, raw_body)






class RouterHandler(BaseHTTPRequestHandler):
    server_version = "LinRouter/2.0"
    protocol_version = "HTTP/1.1"
    _all_models_failed_error_type = AllModelsFailedError

    @property
    def store(self) -> ConfigStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def router(self) -> ArkProxyRouter:
        return self.server.router  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    @staticmethod
    def _platform() -> Any:
        return get_platform()

    @staticmethod
    def _render_index_page() -> str:
        return render_index_page()

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_all_models_failed_error(self, err: AllModelsFailedError) -> None:
        """统一 AllModelsFailedError 错误响应：message 中文，type/code 英文机器码。

        状态码按错误语义区分：
        - 客户端请求错误（模型不存在、已退役、已禁用）使用 4xx；
        - 仅「所有聚合成员都不可用」或「无可用模型」才返回 503；
        - 流式空闲超时返回 504。
        """
        err_code = getattr(err, "error_code", "") or ""
        if err_code == "model_not_found":
            status = 400
        elif err_code == "upstream_request_rejected":
            status = 400
        elif err_code == "global_auto_deprecated":
            status = 410
        elif err_code == "aggregate_disabled":
            status = 403
        elif getattr(err, "stream_timeout", False):
            status = 504
        else:
            status = 503
        err_type = "all_models_failed"
        code = "stream_idle_timeout" if status == 504 else "service_unavailable"
        message = str(err)
        details: Dict[str, Any] | None = None
        if err_code == "aggregate_disabled":
            err_type = "aggregate_disabled"
            code = "aggregate_disabled"
        elif err_code == "global_auto_deprecated":
            err_type = "global_auto_deprecated"
            code = "use_aggregate_model"
        elif err_code == "model_not_found":
            err_type = "model_not_found"
            code = "model_not_found"
        elif err_code == "upstream_request_rejected":
            err_type = "upstream_request_rejected"
            code = "upstream_request_rejected"
            details = {
                "attempted": err.attempted,
                "cooldown_applied": False,
                "fallback_chain": [],
            }
            for item in err.fallback_chain:
                details["fallback_chain"].append({
                    "member_id": item.get("member_id", ""),
                    "group": item.get("group", ""),
                    "model": item.get("model", ""),
                    "manual_price": item.get("manual_price"),
                    "status": item.get("status", ""),
                    "reason": item.get("reason", ""),
                    "cooldown_applied": item.get("cooldown_applied", False),
                })
        elif err_code == "aggregate_members_unavailable":
            err_type = "all_aggregate_members_failed"
            code = "aggregate_members_unavailable"
            aggregate = self.store.find_aggregate_by_name(getattr(err, "aggregate_name", "")) if getattr(err, "aggregate_name", "") else None
            details = {
                "aggregate_model": getattr(err, "aggregate_name", ""),
                "attempted": err.attempted,
                "strategy": aggregate.strategy if aggregate else "priority",
                "fallback_chain": [],
            }
            for item in err.fallback_chain:
                member_id = item.get("member_id") or ""
                member = self.store.find_aggregate_member(member_id) if member_id else None
                chain_entry = {
                    "member_id": member_id,
                    "group": item.get("group", ""),
                    "model": item.get("model", ""),
                    "manual_price": item.get("manual_price"),
                    "status": item.get("status", ""),
                    "reason": item.get("reason", ""),
                }
                if member and member.cooldown_until:
                    chain_entry["cooldown_until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(member.cooldown_until))
                details["fallback_chain"].append(chain_entry)
        elif err_code == "no_usable_models":
            code = "no_usable_models"
        elif err_code == "all_models_failed":
            code = "all_models_failed"
        payload: Dict[str, Any] = {
            "error": {
                "message": message,
                "type": err_type,
                "code": code,
            }
        }
        if details:
            payload["error"]["details"] = details
        self._send_json(payload, status=status)

    def _send_model_list(self, ctx: Any) -> None:
        # 旧全局 Key 已退役，/v1/models 不再返回所有真实模型
        if getattr(ctx, "is_deprecated_global", False):
            self._send_json({
                "error": {
                    "message": "全局 Key 已停用，请改用连接组 Key 或聚合模型 Key",
                    "type": "global_key_deprecated",
                    "code": "use_group_or_aggregate_key",
                }
            }, status=403)
            return
        # 聚合模型 Key：只返回自身聚合模型
        if getattr(ctx, "aggregate", None):
            aggregate = ctx.aggregate
            if not aggregate.enabled:
                self._send_json({
                    "error": {
                        "message": f"聚合模型 {aggregate.name} 已禁用",
                        "type": "aggregate_disabled",
                        "code": "aggregate_disabled",
                    }
                }, status=403)
                return
            model_data = [{
                "id": aggregate.name,
                "object": "model",
                "created": 0,
                "owned_by": "lin-router",
                "root": aggregate.name,
                "parent": None,
                "display_name": aggregate.display_name or aggregate.name,
                "is_aggregate": True,
                "aggregate_id": aggregate.id,
                "usable": True,
            }]
            for alias in aggregate.client_model_aliases:
                model_data.append({
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "lin-router",
                    "root": aggregate.name,
                    "parent": aggregate.name,
                    "display_name": alias,
                    "is_aggregate": True,
                    "is_client_alias": True,
                    "aggregate_id": aggregate.id,
                    "usable": True,
                })
            self._send_json({"object": "list", "data": model_data})
            return
        group = ctx.group
        visible_group = group
        # 先统计匹配模型数，便于排查“只能看到 auto”的问题
        matched_models = [
            model for model in self.store.models
            if not visible_group or model.group_id == visible_group.id
        ]
        # 连接组 Key 使用组自定义 auto_model_name（默认 lin-router-auto）
        auto_model_name = self.router.group_auto_model_name(group)
        # 成功的 /v1/models 请求不再记录到最近请求，避免客户端频繁拉取模型列表时刷屏
        data = [{
            "id": auto_model_name,
            "object": "model",
            "created": 0,
            "owned_by": "lin-router",
            "root": auto_model_name,
            "parent": None,
        }]
        # /v1/models 返回对应连接组下的全部已配置模型（包含禁用的），方便客户端查看完整列表
        for model in matched_models:
            model_group = self.store.find_group(model.group_id)
            data.append({
                "id": model.name,
                "object": "model",
                "created": 0,
                "owned_by": "lin-router",
                "root": model.name,
                "parent": None,
                "display_name": model.name,
                "ep_id": model.ep_id,
                "group_id": model.group_id,
                "provider_type": model_group.provider_type if model_group else "",
                "price_group": model.price_group,
                "usable": model.usable,
            })
        self._send_json({"object": "list", "data": data})

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path, content_type: str = "") -> None:
        import mimetypes
        if not file_path.exists():
            self._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)
            return
        data = file_path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_raw_body()
        return json.loads(raw.decode("utf-8"))

    def _read_raw_body(self) -> bytes:
        # do_PUT 转发到 do_POST 时会把 body 缓存在这里，避免重复读取 rfile
        cached = getattr(self, "_put_body", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b"{}"

    def _read_multipart_json(self) -> Optional[Dict[str, Any]]:
        """解析 multipart/form-data 上传的 JSON 配置文件，返回解析后的 dict。"""
        import email
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return None
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return None
        body = self.rfile.read(length)
        try:
            msg = email.message_from_bytes(
                b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
            )
            for part in msg.get_payload() or []:
                if not isinstance(part, email.message.Message):
                    continue
                name = part.get_param("name", header="Content-Disposition")
                if name == "file" or part.get_filename():
                    data = part.get_payload(decode=True)
                    if data:
                        return json.loads(data.decode("utf-8"))
        except Exception:
            return None
        return None

    @staticmethod
    def _json_from_raw(raw: bytes) -> Dict[str, Any]:
        return json.loads(raw.decode("utf-8"))

    def _client_base_url(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        return f"http://{host}/v1"

    def _effective_group_auth(self, group: ConnectionGroup, payload: Dict[str, Any] | None = None) -> str:
        payload = payload or {}
        api_key = str(payload.get("api_key") or "").strip()
        if api_key:
            return api_key
        if group.provider_type == PROVIDER_PROXY:
            return group.api_key or group.ark_api_key
        if group.provider_type == PROVIDER_RELAY:
            return group.ark_api_key or group.api_key
        return group.ark_api_key or group.api_key

    def _fetch_upstream_models(self, group: ConnectionGroup, auth_key: str) -> List[Dict[str, Any]]:
        # RouterHandler remains responsible for logs and external error semantics;
        # the injected adapter owns only the upstream protocol operation.
        target_url = self.router._resolve_url(group.base_url, "/v1/models")
        upstream_origin_hash, upstream_endpoint = self.router._safe_upstream_target(target_url)
        headers = build_model_fetch_headers(auth_key)
        started_at = time.perf_counter()
        try:
            target_url, headers, status, models = self.router.upstream_adapter.fetch_models(group.base_url, auth_key)
            upstream_origin_hash, upstream_endpoint = self.router._safe_upstream_target(target_url)
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.router.add_log(
                "/v1/models",
                group.name,
                str(status),
                f"fetch upstream models ok; upstream_origin_hash={upstream_origin_hash}; upstream_endpoint={upstream_endpoint}; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models",
            )
            return models
        except HTTPError as err:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            body = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
            self.router.add_log(
                "/v1/models",
                group.name,
                str(err.code),
                f"fetch upstream models failed; upstream_origin_hash={upstream_origin_hash}; upstream_endpoint={upstream_endpoint}; error=upstream_http_error; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models_failed",
            )
            raise RuntimeError(body or f"获取上游模型失败：HTTP {err.code}") from err
        except Exception as err:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.router.add_log(
                "/v1/models",
                group.name,
                "network",
                f"fetch upstream models failed; upstream_origin_hash={upstream_origin_hash}; upstream_endpoint={upstream_endpoint}; error=network_error; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models_failed",
            )
            raise

    def _clone_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        source = self.store.find_group(group_id)
        if not source:
            return None
        cloned = ConnectionGroup(
            id=uuid.uuid4().hex,
            name=f"{source.name} - 副本",
            provider_type=source.provider_type,
            base_url=source.base_url,
            ark_api_key=source.ark_api_key,
            api_key=source.api_key,
            route_key=new_route_key(),
            auto_model_cooldown_minutes=source.auto_model_cooldown_minutes if source.provider_type == PROVIDER_RELAY else 0,
            stream_idle_timeout=source.stream_idle_timeout,
            waf_compatible=source.waf_compatible,
            serial_protection=source.serial_protection,
            waf_accept_policy=source.waf_accept_policy,
            waf_client_mode=source.waf_client_mode,
            reasoning_support=source.reasoning_support,
            upstream_models=[dict(item) for item in source.upstream_models],
            upstream_models_fetched_at=source.upstream_models_fetched_at,
        )
        if cloned.provider_type == PROVIDER_PROXY and not cloned.api_key and cloned.ark_api_key:
            cloned.api_key = cloned.ark_api_key
        if cloned.provider_type in {PROVIDER_RELAY, PROVIDER_ARK}:
            cloned.api_key = ""
            cloned.ark_api_key = "" if cloned.provider_type == PROVIDER_RELAY else cloned.ark_api_key
        self.store.upsert_group(cloned)

        copied = 0
        source_models = [model for model in self.store.models if model.group_id == source.id]
        for model in source_models:
            self.store.upsert_model(ModelConfig(
                id=uuid.uuid4().hex,
                name=model.name,
                ep_id=model.ep_id,
                group_id=cloned.id,
                upstream_model=model.upstream_model,
                api_key=model.api_key,
                price_group=model.price_group,
                usable=model.usable,
                disabled_by_user=model.disabled_by_user,
                last_error=model.last_error,
                last_success_at=model.last_success_at,
                last_checked_at=model.last_checked_at,
                cooldown_until=model.cooldown_until,
                cooldown_reason=model.cooldown_reason,
            ))
            copied += 1
        return {"group": asdict(cloned), "copied_models": copied}

    def _route_context(self) -> Optional[RouteContext]:
        key = parse_bearer_key(self.headers.get("Authorization", ""))
        if not key:
            return None
        # 聚合模型 Key 优先识别
        aggregate = self.store.find_aggregate_by_route_key(key)
        if aggregate:
            return RouteContext(
                client_key=key,
                group=None,
                group_id=f"__aggregate__{aggregate.id}",
                provider_type="aggregate",
                base_url="",
                display_name=aggregate.display_name or aggregate.name,
                passthrough=False,
                is_global=False,
                aggregate=aggregate,
            )
        # 连接组 Key
        group = self.store.find_group_by_route_key(key)
        if group:
            return RouteContext(
                client_key=key,
                group=group,
                group_id=group.id,
                provider_type=group.provider_type,
                base_url=group.base_url,
                display_name=group.name,
                # 仅 relay 模式不走 passthrough；ark/proxy 均视为透传，避免中转站专属逻辑污染
                passthrough=group.provider_type != PROVIDER_RELAY,
            )
        # 旧全局 Key 已退役
        if key == DEFAULT_PUBLIC_API_KEY:
            return RouteContext(
                client_key=key,
                group=None,
                group_id=GLOBAL_ROUTE_GROUP_ID,
                provider_type="global",
                base_url=DEFAULT_BASE_URL,
                display_name="全局调度（已退役）",
                passthrough=False,
                is_global=True,
                is_deprecated_global=True,
            )
        return None

    def _route_group(self) -> Optional[ConnectionGroup]:
        ctx = self._route_context()
        return ctx.group if ctx else None

    def _require_route_context(self) -> Optional[RouteContext]:
        ctx = self._route_context()
        if ctx:
            return ctx
        self._send_json({
            "error": {
                "message": "缺少或无效的 Lin Router API Key，请使用连接组 Key 或聚合模型 Key",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }, status=401)
        return None

    def _require_route_group(self) -> Optional[ConnectionGroup]:
        ctx = self._require_route_context()
        return ctx.group if ctx else None

    def _visible_models(self, group: Optional[ConnectionGroup]) -> List[ModelConfig]:
        return [
            model
            for model in self.store.models
            if model.usable and (group is None or model.group_id == group.id)
        ]

    CONFIG_SKIP_REASONS = {
        "member_disabled",
        "member_cooling",
        "underlying_model_disabled",
        "underlying_model_cooling",
        "underlying_group_disabled",
    }

    def _log_detail_dict(self, detail: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        if not detail:
            return result
        for part in str(detail).split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _log_value(self, log: Any, key: str, default: Any = "") -> Any:
        if isinstance(log, dict):
            return log.get(key, default)
        return getattr(log, key, default)

    def _is_config_skip_log(self, log: Any) -> bool:
        if self._log_value(log, "event") != "skip":
            return False
        detail = self._log_detail_dict(str(self._log_value(log, "detail", "") or ""))
        return detail.get("skip_reason") in self.CONFIG_SKIP_REASONS

    def _log_matches_aggregate(self, log: RequestLog, aggregate: AggregateModel) -> bool:
        return (
            log.aggregate_id == aggregate.id
            or log.aggregate_model == aggregate.name
            or log.requested_model == aggregate.name
            or log.model == aggregate.name
        )

    def _aggregate_stats_payload(self, aggregate_id: str, limit: int = 100) -> Dict[str, Any]:
        aggregate = self.store.find_aggregate(aggregate_id)
        if not aggregate:
            return {"ok": False, "message": "聚合模型不存在"}
        limit = max(1, min(int(limit or 100), 500))
        logs = [log for log in reversed(self.router.all_logs()) if self._log_matches_aggregate(log, aggregate)]
        by_request: Dict[str, List[RequestLog]] = {}
        synthetic_idx = 0
        for log in logs:
            key = log.request_id or f"__log_{synthetic_idx}"
            if not log.request_id:
                synthetic_idx += 1
            by_request.setdefault(key, []).append(log)

        request_groups: List[List[RequestLog]] = []
        for group_logs in by_request.values():
            if any(not self._is_config_skip_log(log) for log in group_logs):
                request_groups.append(group_logs)
        request_groups = request_groups[:limit]

        request_count = len(request_groups)
        success_count = 0
        fallback_success_count = 0
        first_choice_success = 0
        cooldown_skip_count = 0
        busy_switch_count = 0
        prompt_tokens = 0
        cached_tokens = 0
        first_complete_frame_durations: List[int] = []
        first_content_delta_durations: List[int] = []
        done_durations: List[int] = []
        member_risk: Dict[str, Dict[str, Any]] = {}

        for group_logs in request_groups:
            non_skip = [log for log in group_logs if not self._is_config_skip_log(log)]
            success_logs = [log for log in non_skip if str(log.status).startswith("2") and log.event in {"ok", "stream_ok", "stream_done", "retry_ok"}]
            final_success = next((log for log in non_skip if str(log.status).startswith("2") and log.event in {"stream_done", "ok", "retry_ok"}), None) or (success_logs[-1] if success_logs else None)
            if final_success:
                success_count += 1
                prompt_tokens += int(final_success.prompt_tokens or 0)
                cached_tokens += int(final_success.cached_tokens or 0)
                if int(final_success.attempt or 1) <= 1 and int(final_success.fallback_index or 0) <= 0:
                    first_choice_success += 1
            first_stream = next((log for log in non_skip if log.event == "stream_ok" and int(log.duration_ms or 0) > 0), None)
            if first_stream:
                stream_detail = self._log_detail_dict(first_stream.detail)
                try:
                    first_complete_frame_ms = int(stream_detail.get("first_complete_frame_ms") or first_stream.duration_ms or 0)
                except (TypeError, ValueError):
                    first_complete_frame_ms = 0
                try:
                    first_content_delta_ms = int(stream_detail.get("first_content_delta_ms") or -1)
                except (TypeError, ValueError):
                    first_content_delta_ms = -1
                if first_complete_frame_ms > 0:
                    first_complete_frame_durations.append(first_complete_frame_ms)
                if first_content_delta_ms >= 0:
                    first_content_delta_durations.append(first_content_delta_ms)
            done = next((log for log in non_skip if log.event == "stream_done" and int(log.duration_ms or 0) > 0), None)
            if done:
                done_durations.append(int(done.duration_ms or 0))

            had_prior_runtime_issue = False
            for log in group_logs:
                detail = self._log_detail_dict(log.detail)
                reason = detail.get("skip_reason") or detail.get("fallback_reason") or detail.get("cooldown_reason") or ""
                event_blob = f"{log.event};{log.detail};{reason}"
                if reason in {"member_cooling", "underlying_model_cooling"}:
                    cooldown_skip_count += 1
                if "candidate_busy" in event_blob or "large_task_in_progress" in event_blob or log.event in {"serial_protection_timeout", "waf_lock_timeout"}:
                    busy_switch_count += 1
                    had_prior_runtime_issue = True
                if log.event in {"fallback", "cooldown", "network", "stream_timeout", "stream_idle_timeout"} or log.failure_scope in {"upstream", "candidate", "busy", "local_lock"}:
                    had_prior_runtime_issue = True
                if log.aggregate_member_id:
                    risk = member_risk.setdefault(log.aggregate_member_id, {
                        "member_id": log.aggregate_member_id,
                        "model": log.selected_model or log.model,
                        "timeout_count": 0,
                        "waf_blocked_count": 0,
                        "failure_count": 0,
                        "last_error": "",
                    })
                    if "timeout" in event_blob:
                        risk["timeout_count"] += 1
                    if "waf" in event_blob.lower():
                        risk["waf_blocked_count"] += 1
                    if not str(log.status).startswith("2") and not self._is_config_skip_log(log):
                        risk["failure_count"] += 1
                        risk["last_error"] = self.router._short_error(log.detail or log.status)
            if final_success and had_prior_runtime_issue:
                fallback_success_count += 1

        success_rate = (success_count / request_count) if request_count else None
        first_choice_success_rate = (first_choice_success / success_count) if success_count else None
        cache_hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens else None
        avg_first_complete_frame_ms = round(sum(first_complete_frame_durations) / len(first_complete_frame_durations)) if first_complete_frame_durations else None
        avg_first_content_delta_ms = round(sum(first_content_delta_durations) / len(first_content_delta_durations)) if first_content_delta_durations else None
        avg_done_ms = round(sum(done_durations) / len(done_durations)) if done_durations else None
        high_risk_members = [
            item for item in member_risk.values()
            if item.get("timeout_count") or item.get("waf_blocked_count") or item.get("failure_count", 0) >= 2
        ]
        high_risk_members.sort(key=lambda x: (x.get("timeout_count", 0) + x.get("waf_blocked_count", 0) + x.get("failure_count", 0)), reverse=True)

        return {
            "ok": True,
            "aggregate_id": aggregate.id,
            "aggregate_name": aggregate.name,
            "range": {"type": "last_n", "limit": limit},
            "request_count": request_count,
            "success_count": success_count,
            "success_rate": success_rate,
            "fallback_success_count": fallback_success_count,
            "first_choice_success_rate": first_choice_success_rate,
            "cooldown_skip_count": cooldown_skip_count,
            "busy_switch_count": busy_switch_count,
            # Retain the older key for API compatibility, but expose an
            # unambiguous value for clients that still need the complete-frame
            # timing.  UI comparisons use first_content_delta instead.
            "avg_first_chunk_ms": avg_first_complete_frame_ms,
            "avg_first_complete_frame_ms": avg_first_complete_frame_ms,
            "avg_first_content_delta_ms": avg_first_content_delta_ms,
            "avg_done_ms": avg_done_ms,
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "cache_hit_rate": cache_hit_rate,
            "high_risk_members": high_risk_members[:5],
        }

    def _model_runtime_item(self, model: ModelConfig) -> Dict[str, Any]:
        now = int(time.time())
        if model.cooldown_until and model.cooldown_until > now:
            status = "cooling"
            reason = model.cooldown_reason or "模型正在健康冷却"
        elif model.usable is False and model.disabled_by_user:
            status = "manual_disabled"
            reason = "用户已停用该模型"
        elif model.usable is False:
            status = "unavailable"
            reason = model.last_error or "模型当前不可用"
        elif model.last_error:
            status = "warning"
            reason = model.last_error
        else:
            status = "healthy"
            reason = "模型可参与调度"
        return {
            "group_id": model.group_id,
            "model_id": model.id,
            "derived_status": status,
            "derived_reason": reason,
            "usable": model.usable,
            "disabled_by_user": model.disabled_by_user,
            "cooldown_until": model.cooldown_until,
            "cooldown_reason": model.cooldown_reason,
            "last_error": model.last_error,
            "last_success_at": getattr(model, "last_success_at", ""),
            "last_failure_at": getattr(model, "last_failure_at", ""),
        }

    def _member_runtime_item(self, member: AggregateMember) -> Dict[str, Any]:
        now = int(time.time())
        group = self.store.find_group(member.group_id)
        model = self.store.find_model(member.model_id)
        if member.enabled is False:
            status = "manual_disabled"
            reason = "用户已停用该聚合成员"
        elif member.cooldown_until and member.cooldown_until > now:
            status = "cooling"
            reason = member.cooldown_reason or "聚合成员因上游健康失败短期冷却"
        elif not group:
            status = "config_error"
            reason = "底层连接组缺失"
        elif not model:
            status = "config_error"
            reason = "底层真实模型缺失"
        elif model.usable is False and model.disabled_by_user:
            status = "underlying_model_disabled"
            reason = "底层真实模型已手动停用"
        elif model.cooldown_until and model.cooldown_until > now:
            status = "underlying_model_cooling"
            reason = model.cooldown_reason or "底层真实模型正在冷却"
        elif member.last_error:
            status = "warning"
            reason = member.last_error
        else:
            status = "healthy"
            reason = "成员可参与聚合调度"
        return {
            "aggregate_id": member.aggregate_id,
            "member_id": member.id,
            "derived_status": status,
            "derived_reason": reason,
            "enabled": member.enabled,
            "cooldown_until": member.cooldown_until,
            "cooldown_reason": member.cooldown_reason,
            "last_error": member.last_error,
            "last_success_at": getattr(member, "last_success_at", ""),
            "last_failure_at": getattr(member, "last_failure_at", ""),
            "underlying_model_status": self._model_runtime_item(model)["derived_status"] if model else "missing",
        }

    def _filtered_recent_logs(self, include_skip: bool = False) -> List[RequestLog]:
        logs = self.router.recent_logs()
        if include_skip:
            return logs
        return [
            log for log in logs
            if not self._is_config_skip_log(log)
            and str(getattr(log, "usage_source", "")) != "manual_probe"
        ]

    @staticmethod
    def _runtime_live_request_signature(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """排除 elapsed_ms 等时钟字段，避免空闲状态因计时而无意义递增 revision。"""
        keys = (
            "request_id", "path", "requested_model", "model", "group", "candidate", "aggregate_model",
            "stage", "stage_label", "stream", "attempt", "status", "slow", "possible_reason",
            "cancellable", "cancellation_state", "cancelled_at_stage",
        )
        return [{key: item.get(key) for key in keys} for item in items]

    @staticmethod
    def _runtime_revision(scope: str, payload: Dict[str, Any]) -> str:
        encoded = json.dumps({"scope": scope, **payload}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"r-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"

    def _runtime_state_payload(
        self,
        include_skip: bool = False,
        *,
        scope: str = "",
        revision: str = "",
        activity_cursor: str = "",
    ) -> Dict[str, Any]:
        """兼容旧全量响应，并为管理台提供按 scope 的轻量运行态投影。"""
        self.store.refresh_expired_cooldowns()
        models = [self._model_runtime_item(model) for model in self.store.models]
        aggregate_members = [self._member_runtime_item(member) for member in self.store.aggregate_members]
        log_write_error = self.router.log_write_error
        if not scope:
            return {
                "ok": True,
                "models": models,
                "aggregate_members": aggregate_members,
                "logs": self._filtered_recent_logs(include_skip=include_skip),
                "log_write_error": log_write_error,
            }

        live_requests = self.router.live_requests_payload().get("requests", [])
        activity: Dict[str, Any] | None = None
        revision_payload: Dict[str, Any] = {
            "models": models,
            "aggregate_members": aggregate_members,
            "live_requests": self._runtime_live_request_signature(live_requests),
            "log_write_error": log_write_error,
        }
        if scope == "dashboard":
            raw_activity = self.router.runtime_activity_since(activity_cursor, limit=30)
            activity_logs = [
                item for item in raw_activity.get("logs", [])
                if include_skip
                or (
                    not self._is_config_skip_log(item)
                    and str(self._log_value(item, "usage_source", "") or "") != "manual_probe"
                )
            ]
            activity = {
                "cursor": str(raw_activity.get("cursor") or ""),
                "changed": bool(raw_activity.get("changed")),
                "mode": str(raw_activity.get("mode") or "snapshot"),
                "logs": activity_logs,
            }
            revision_payload["activity_cursor"] = activity["cursor"]

        runtime_revision = self._runtime_revision(scope, revision_payload)
        changed = str(revision or "") != runtime_revision
        response: Dict[str, Any] = {
            "ok": True,
            "scope": scope,
            "runtime_revision": runtime_revision,
            # revision 是前端请求参数名；保留同值降低过渡期接入复杂度。
            "revision": runtime_revision,
            "changed": changed,
            "next_poll_ms": 1000 if live_requests else 5000,
        }
        # 活跃请求的 elapsed_ms 是可见信息，运行中即使状态 revision 未变也允许轻量回传。
        if changed or live_requests:
            response.update({
                "models": models,
                "aggregate_members": aggregate_members,
                "live_requests": live_requests,
                "log_write_error": log_write_error,
            })
        if activity is not None:
            response["activity"] = activity
            response["activity_cursor"] = activity["cursor"]
        return response

    def _aggregate_member_chain_item(self, member: AggregateMember) -> Dict[str, Any]:
        group = self.store.find_group(member.group_id)
        model = self.store.find_model(member.model_id)
        runtime = self._member_runtime_item(member)
        return {
            "member_id": member.id,
            "aggregate_id": member.aggregate_id,
            "priority": member.priority,
            "group_id": member.group_id,
            "group_name": group.name if group else "未知连接组",
            "model_id": member.model_id,
            "model_name": model.name if model else "未知模型",
            "enabled": member.enabled,
            "cooldown_until": member.cooldown_until,
            "cooldown_reason": member.cooldown_reason,
            "derived_status": runtime["derived_status"],
            "derived_reason": runtime["derived_reason"],
        }

    def _candidate_chain_for_members(self, members: List[AggregateMember]) -> List[Dict[str, Any]]:
        return [self._aggregate_member_chain_item(member) for member in sorted(members, key=lambda m: m.priority)]

    def _aggregate_member_sort_preview(self, member_id: str, direction: str) -> Dict[str, Any]:
        member = self.store.find_aggregate_member(member_id)
        if not member:
            return {"ok": False, "message": "成员不存在", "can_apply": False}
        direction = str(direction or "").strip()
        if direction not in {"up", "down", "top", "bottom"}:
            return {"ok": False, "message": "排序方向无效", "can_apply": False}
        siblings = sorted(self.store.get_aggregate_members(member.aggregate_id), key=lambda m: m.priority)
        idx = next((i for i, item in enumerate(siblings) if item.id == member_id), -1)
        if idx < 0:
            return {"ok": False, "message": "成员不存在", "can_apply": False}
        target_idx = {"up": idx - 1, "down": idx + 1, "top": 0, "bottom": len(siblings) - 1}[direction]
        target_idx = max(0, min(target_idx, len(siblings) - 1))
        after = list(siblings)
        changed = target_idx != idx
        if changed:
            moved = after.pop(idx)
            after.insert(target_idx, moved)
        after_chain = []
        for order, item in enumerate(after, start=1):
            chain_item = self._aggregate_member_chain_item(item)
            chain_item["priority"] = order
            after_chain.append(chain_item)
        aggregate = self.store.find_aggregate(member.aggregate_id)
        return {
            "ok": True,
            "can_apply": True,
            "changed": changed,
            "direction": direction,
            "aggregate_id": member.aggregate_id,
            "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
            "member_id": member.id,
            "candidate_chain_before": self._candidate_chain_for_members(siblings),
            "candidate_chain_after": after_chain,
        }

    def _aggregate_member_clear_cooldown_preview(self, member_id: str) -> Dict[str, Any]:
        member = self.store.find_aggregate_member(member_id)
        if not member:
            return {"ok": False, "message": "成员不存在", "can_apply": False}
        before_chain = self._candidate_chain_for_members(self.store.get_aggregate_members(member.aggregate_id))
        after_member = AggregateMember.from_dict(asdict(member))
        after_member.enabled = True
        after_member.cooldown_until = 0
        after_member.cooldown_reason = ""
        after_member.last_error = ""
        after_members = []
        for item in self.store.get_aggregate_members(member.aggregate_id):
            after_members.append(after_member if item.id == member_id else item)
        aggregate = self.store.find_aggregate(member.aggregate_id)
        return {
            "ok": True,
            "can_apply": True,
            "changed": bool((not member.enabled) or member.cooldown_until or member.cooldown_reason or member.last_error),
            "aggregate_id": member.aggregate_id,
            "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
            "member_id": member.id,
            "cooldown_before": {
                "enabled": member.enabled,
                "cooldown_until": member.cooldown_until,
                "cooldown_reason": member.cooldown_reason,
                "last_error": member.last_error,
            },
            "cooldown_after": {
                "enabled": True,
                "cooldown_until": 0,
                "cooldown_reason": "",
                "last_error": "",
            },
            "candidate_chain_before": before_chain,
            "candidate_chain_after": self._candidate_chain_for_members(after_members),
        }

    def _group_delete_preview(self, group_id: str) -> Dict[str, Any]:
        group = self.store.find_group(group_id)
        if not group:
            return {"ok": False, "message": "连接组不存在", "can_delete": False}
        models = [m for m in self.store.models if m.group_id == group_id]
        model_ids = {m.id for m in models}
        affected_members = []
        for member in self.store.aggregate_members:
            if member.group_id == group_id or member.model_id in model_ids:
                aggregate = self.store.find_aggregate(member.aggregate_id)
                model = self.store.find_model(member.model_id)
                affected_members.append({
                    "aggregate_id": member.aggregate_id,
                    "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
                    "member_id": member.id,
                    "model": model.name if model else member.model_id,
                })
        warnings = []
        aggregate_counts: Dict[str, int] = {}
        for item in affected_members:
            aggregate_counts[item["aggregate_name"]] = aggregate_counts.get(item["aggregate_name"], 0) + 1
        for name, count in aggregate_counts.items():
            warnings.append(f"删除后聚合模型 {name} 将失去 {count} 个成员")
        return {
            "ok": True,
            "group_id": group.id,
            "group_name": group.name,
            "can_delete": True,
            "affected_models": len(models),
            "affected_model_names": [m.name for m in models],
            "affected_aggregate_members": affected_members,
            "warnings": warnings,
            "reversible": False,
        }

    def _model_delete_preview(self, model_id: str) -> Dict[str, Any]:
        model = self.store.find_model(model_id)
        if not model:
            return {"ok": False, "message": "模型不存在", "can_delete": False}
        group = self.store.find_group(model.group_id)
        affected_members = []
        for member in self.store.aggregate_members:
            if member.model_id == model_id:
                aggregate = self.store.find_aggregate(member.aggregate_id)
                affected_members.append({
                    "aggregate_id": member.aggregate_id,
                    "aggregate_name": aggregate.name if aggregate else "未知聚合模型",
                    "member_id": member.id,
                    "model": model.name,
                })
        warnings = [f"聚合模型 {item['aggregate_name']} 依赖该模型，删除后对应成员会失效" for item in affected_members]
        return {
            "ok": True,
            "model_id": model.id,
            "model_name": model.name,
            "group_id": model.group_id,
            "group_name": group.name if group else "未知连接组",
            "can_delete": True,
            "affected_aggregate_members": affected_members,
            "warnings": warnings,
            "reversible": False,
        }

    def do_GET(self) -> None:
        return handle_get(self)

    def do_POST(self) -> None:
        return handle_post(self)

    def do_PUT(self) -> None:
        return handle_put(self)

    def do_DELETE(self) -> None:
        return handle_delete(self)


def ensure_initial_config(path: Path) -> None:
    return _ensure_initial_config(path)


def pick_port(start_port: int, host: str) -> int:
    return _pick_port(start_port, host, MAX_PORT_SCAN)


def create_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_START_PORT,
    config: str | Path = DEFAULT_CONFIG_FILE,
) -> Tuple[ThreadingHTTPServer, int, Path]:
    return create_application_server(
        host,
        port,
        config,
        max_port_scan=MAX_PORT_SCAN,
        store_type=ConfigStore,
        settings_store_type=SettingsStore,
        router_type=ArkProxyRouter,
        handler_type=RouterHandler,
    )


def main() -> None:
    return run_main(
        platform=get_platform(),
        create_server=create_server,
        default_config_file=DEFAULT_CONFIG_FILE,
        default_start_port=DEFAULT_START_PORT,
    )


if __name__ == "__main__":
    main()
