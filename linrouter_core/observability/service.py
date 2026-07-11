from __future__ import annotations

import csv
import io
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .contracts import RequestLog, RuntimeSnapshotProvider
from .diagnostics import diagnose_logs
from .log_repository import LogRepository


class ObservabilityService:
    """Owns persisted logs and read-only runtime/diagnostic projections.

    Runtime routing code may append records and publish ephemeral request stages, but
    this service intentionally exposes no candidate, cooldown, retry, or upstream APIs.
    """

    def __init__(
        self,
        log_file: Path,
        *,
        now: Callable[[], str],
        sanitize_detail: Callable[[str], str],
        runtime_snapshot_provider: Optional[RuntimeSnapshotProvider] = None,
    ) -> None:
        self.logs: List[RequestLog] = []
        self.log_file = log_file
        self.log_write_error = ""
        self._now = now
        self._sanitize_detail = sanitize_detail
        self._runtime_snapshot_provider = runtime_snapshot_provider
        self._repository = LogRepository(log_file, self._log_from_row)
        self._live_requests: Dict[str, Dict[str, Any]] = {}
        self._live_requests_lock = threading.Lock()
        self.load()

    def set_log_file(self, log_file: Path) -> None:
        """Compatibility hook for the existing router's mutable log_file attribute."""
        self.log_file = log_file
        self._repository = LogRepository(log_file, self._log_from_row)

    @staticmethod
    def detail_value(detail: str, key: str) -> str:
        match = re.search(rf"(?:^|; ){re.escape(key)}=([^;]*)", detail or "")
        return match.group(1).strip() if match else ""

    @staticmethod
    def infer_event(status: str, detail: str) -> str:
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

    @staticmethod
    def append_detail(detail: str, suffix: str) -> str:
        return suffix if not detail else f"{detail}; {suffix}"

    @staticmethod
    def _log_from_row(row: Dict[str, Any]) -> RequestLog:
        return RequestLog(
            time=str(row.get("time") or ""), path=str(row.get("path") or ""),
            model=str(row.get("model") or ""), status=str(row.get("status") or ""),
            detail=str(row.get("detail") or ""), duration_ms=int(row.get("duration_ms") or 0),
            prompt_tokens=int(row.get("prompt_tokens") or 0), completion_tokens=int(row.get("completion_tokens") or 0),
            total_tokens=int(row.get("total_tokens") or 0), cached_tokens=int(row.get("cached_tokens") or 0),
            reasoning_tokens=int(row.get("reasoning_tokens") or 0), group_id=str(row.get("group_id") or ""),
            group_name=str(row.get("group_name") or ""), provider_type=str(row.get("provider_type") or ""),
            event=str(row.get("event") or ""), request_id=str(row.get("request_id") or ""),
            attempt=int(row.get("attempt") or 0), usage_source=str(row.get("usage_source") or ""),
            requested_model=str(row.get("requested_model") or ""), resolved_as=str(row.get("resolved_as") or ""),
            aggregate_model=str(row.get("aggregate_model") or ""), aggregate_id=str(row.get("aggregate_id") or ""),
            aggregate_member_id=str(row.get("aggregate_member_id") or ""), selected_group=str(row.get("selected_group") or ""),
            selected_model=str(row.get("selected_model") or ""), selected_upstream_model=str(row.get("selected_upstream_model") or ""),
            selection_reason=str(row.get("selection_reason") or ""), fallback_index=int(row.get("fallback_index") or 0),
            fallback_chain=str(row.get("fallback_chain") or ""), member_cooled_down=bool(row.get("member_cooled_down")),
            cooldown_applied=bool(row.get("cooldown_applied")), failure_scope=str(row.get("failure_scope") or ""),
        )

    def load(self) -> None:
        try:
            items = self._repository.load_recent(80)
            for item in items:
                if not item.time:
                    item.time = self._now()
            if items:
                self.logs = list(reversed(items))
        except Exception as exc:
            self.log_write_error = f"日志加载失败: {exc}"

    def add_log(
        self, path: str, model: str, status: str, detail: str = "", duration_ms: int = 0,
        prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
        cached_tokens: int = 0, reasoning_tokens: int = 0, group: Any = None, event: str = "",
        request_id: str = "", attempt: int = 0, usage_source: str = "", cooldown_applied: bool = False,
        failure_scope: str = "",
    ) -> None:
        detail = self._sanitize_detail(detail)
        item = RequestLog(
            self._now(), path, model, status, detail[:5000], duration_ms, prompt_tokens, completion_tokens,
            total_tokens, cached_tokens, reasoning_tokens,
            getattr(group, "id", "") if group else self.detail_value(detail, "group_id"),
            getattr(group, "name", "") if group else self.detail_value(detail, "group_name"),
            getattr(group, "provider_type", "") if group else self.detail_value(detail, "provider"),
            event or self.infer_event(status, detail), request_id, attempt, usage_source,
            requested_model=self.detail_value(detail, "requested"), resolved_as=self.detail_value(detail, "resolved_as"),
            aggregate_model=self.detail_value(detail, "aggregate_model"), aggregate_id=self.detail_value(detail, "aggregate_id"),
            aggregate_member_id=self.detail_value(detail, "aggregate_member_id"), selected_group=self.detail_value(detail, "selected_group"),
            selected_model=self.detail_value(detail, "selected_model"), selected_upstream_model=self.detail_value(detail, "selected_upstream_model"),
            selection_reason=self.detail_value(detail, "selection_reason"), fallback_index=int(self.detail_value(detail, "fallback_index") or 0),
            fallback_chain=self.detail_value(detail, "fallback_chain"), member_cooled_down=self.detail_value(detail, "member_cooled_down") == "true",
            cooldown_applied=cooldown_applied or self.detail_value(detail, "cooldown_applied") == "true",
            failure_scope=failure_scope or self.detail_value(detail, "failure_scope") or "",
        )
        self.logs.insert(0, item)
        self._append(item)
        del self.logs[80:]
        self._trim()

    def _append(self, item: RequestLog) -> None:
        try:
            self._repository.append(item)
        except Exception as exc:
            self.log_write_error = f"日志写入失败: {exc}"
            self.logs.insert(0, RequestLog(self._now(), "/system/log", "-", "warn", f"日志写入失败: {exc}; file={self.log_file}", group_name="系统", provider_type="system", event="system"))
            del self.logs[80:]

    def trim(self, max_lines: int = 1000) -> None:
        try:
            self._repository.trim(max_lines)
        except Exception as exc:
            self.log_write_error = f"日志滚动失败: {exc}"

    def _trim(self) -> None:
        self.trim()

    def recent_logs(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.logs[:30]]

    def all_logs(self) -> List[RequestLog]:
        try:
            return self._repository.read_all() or list(reversed(self.logs))
        except Exception:
            return list(reversed(self.logs))

    def clear_logs(self) -> None:
        self.logs.clear()
        try:
            self._repository.clear()
        except Exception:
            return

    def _find_stream_log(self, request_id: str, attempt: Optional[int] = None, candidate_label: str = "") -> Optional[RequestLog]:
        for item in self.logs:
            if item.request_id != request_id or item.event != "stream_ok":
                continue
            if attempt is not None and item.attempt != attempt:
                continue
            if candidate_label and item.model != candidate_label:
                continue
            return item
        return None

    def patch_stream_lifecycle(
        self, request_id: str, attempt: int, candidate_label: str, usage: Tuple[int, int, int, int, int],
        usage_source: str, *, final_status: str, lifecycle: str, final_result: str, chunks_received: int,
        bytes_received: int, duration_ms: Optional[int] = None, lock_wait_ms: Optional[int] = None,
        lock_release_reason: str = "", cooldown_applied: Optional[bool] = None, failure_scope: str = "",
    ) -> bool:
        item = self._find_stream_log(request_id, attempt, candidate_label)
        if not item:
            return False
        item.status = final_status
        item.duration_ms = duration_ms if duration_ms is not None else item.duration_ms
        item.prompt_tokens, item.completion_tokens, item.total_tokens, item.cached_tokens, item.reasoning_tokens = usage
        item.usage_source = usage_source
        if cooldown_applied is not None:
            item.cooldown_applied = cooldown_applied
        if failure_scope:
            item.failure_scope = failure_scope
        suffix_parts = ["stream_finalized=true", f"lifecycle={lifecycle}", f"final_result={final_result}", f"chunks_received={chunks_received}", f"bytes_received={bytes_received}", "lock_released=true"]
        if lock_wait_ms is not None:
            suffix_parts.append(f"lock_wait_ms={lock_wait_ms}")
        if lock_release_reason:
            suffix_parts.append(f"lock_release_reason={lock_release_reason}")
        item.detail = self.append_detail(item.detail, "; ".join(suffix_parts))
        self.rewrite_log_file()
        return True

    def rewrite_log_file(self) -> None:
        try:
            self._repository.rewrite_oldest_first(self.logs)
        except Exception as exc:
            self.log_write_error = f"日志回写失败: {exc}"

    def export_logs_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time", "path", "request_id", "attempt", "group_name", "provider_type", "model", "status", "event", "duration_ms", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens", "usage_source", "cooldown_applied", "failure_scope", "detail"])
        for item in self.all_logs():
            writer.writerow([item.time, item.path, item.request_id, item.attempt, item.group_name, item.provider_type, item.model, item.status, item.event, item.duration_ms, item.prompt_tokens, item.completion_tokens, item.total_tokens, item.cached_tokens, item.reasoning_tokens, item.usage_source, item.cooldown_applied, item.failure_scope, item.detail])
        return output.getvalue()

    def start_live_request(self, request_id: str, path: str, requested_model: str, *, stream: bool) -> None:
        if not request_id:
            return
        now = time.time()
        with self._live_requests_lock:
            self._live_requests[request_id] = {"request_id": request_id, "path": path, "requested_model": requested_model, "model": requested_model, "group": "", "candidate": "", "aggregate_model": "", "stage": "selecting_candidate", "stage_label": "选择候选", "started_at": now, "updated_at": now, "elapsed_ms": 0, "stream": bool(stream), "attempt": 0, "status": "running", "slow": False, "possible_reason": ""}

    def update_live_request(self, request_id: str, **patch: Any) -> None:
        if not request_id:
            return
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if item:
                item.update({key: value for key, value in patch.items() if value is not None})
                item["updated_at"] = time.time()

    def finish_live_request(self, request_id: str, status: str = "done") -> None:
        if not request_id:
            return
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if item:
                item.update({"status": status, "stage": status, "stage_label": "已完成" if status == "done" else "已结束", "updated_at": time.time()})
            self._live_requests.pop(request_id, None)

    def live_requests_payload(self) -> Dict[str, Any]:
        now = time.time()
        with self._live_requests_lock:
            items = []
            for item in self._live_requests.values():
                row = dict(item)
                elapsed_ms = int((now - float(row.get("started_at") or now)) * 1000)
                row["elapsed_ms"] = elapsed_ms
                row["slow"] = elapsed_ms >= 10000 or str(row.get("stage") or "") in {"waiting_waf_lock", "waiting_first_byte"} and elapsed_ms >= 5000
                if row["slow"] and not row.get("possible_reason"):
                    row["possible_reason"] = {"waiting_waf_lock": "候选可能正在处理大上下文请求，正在等待 WAF 锁", "waiting_first_byte": "上游首包较慢，可能是模型正在执行复杂任务"}.get(str(row.get("stage") or ""), "请求耗时较长，请关注上游状态")
                row["request_id_short"] = str(row.get("request_id") or "")[:8]
                items.append(row)
        items.sort(key=lambda row: row.get("started_at") or 0, reverse=True)
        return {"ok": True, "requests": items, "count": len(items), "server_time": int(now)}

    def diagnose_logs(self, logs: List[RequestLog]) -> Dict[str, Any]:
        return diagnose_logs(logs, self._sanitize_detail)

    def diagnose_request(self, request_id: str) -> Dict[str, Any]:
        related = [item for item in self.logs if item.request_id == request_id]
        if not related:
            related = [item for item in self.all_logs() if item.request_id == request_id]
        if not related:
            return {"ok": False, "message": "未找到该请求记录", "code": "request_not_found"}
        return {"ok": True, "diagnosis": diagnose_logs(list(reversed(related)), self._sanitize_detail)}

    def runtime_snapshot(self) -> Dict[str, Any]:
        return dict(self._runtime_snapshot_provider() if self._runtime_snapshot_provider else {})
