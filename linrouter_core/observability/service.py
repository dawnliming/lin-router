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

    LOG_RETENTION = 5000
    # 管理台只需要短窗口内的运行态增量；超出窗口时由调用方回退到快照，
    # 避免为了保持游标而无限增长内存。
    ACTIVITY_JOURNAL_LIMIT = 240

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
        self._logs_lock = threading.RLock()
        self._activity_cursor = 0
        self._activity_journal: List[Dict[str, Any]] = []
        self._live_requests: Dict[str, Dict[str, Any]] = {}
        self._live_requests_lock = threading.RLock()
        self.load()

    def set_log_file(self, log_file: Path) -> None:
        """Compatibility hook for the existing router's mutable log_file attribute."""
        self.log_file = log_file
        self._repository = LogRepository(log_file, self._log_from_row)

    @staticmethod
    def detail_value(detail: str, key: str) -> str:
        matches = re.findall(rf"(?:^|; ){re.escape(key)}=([^;]*)", detail or "")
        # Lifecycle patches historically appended newer values.  Prefer the
        # final value in legacy logs and keep that convention for readers.
        return matches[-1].strip() if matches else ""

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
    def update_detail_fields(detail: str, fields: Dict[str, Any]) -> str:
        """Replace structured detail fields without duplicating lifecycle values."""
        if not fields:
            return detail
        normalized = {
            str(key): " ".join(str(value).replace(";", ",").split())
            for key, value in fields.items()
        }
        keys = set(normalized)
        parts = [
            part for part in (detail or "").split("; ")
            if part.split("=", 1)[0].strip() not in keys
        ]
        parts.extend(f"{key}={value}" for key, value in normalized.items())
        return "; ".join(part for part in parts if part)

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
            items = self._repository.load_recent(self.LOG_RETENTION)
            for item in items:
                if not item.time:
                    item.time = self._now()
            if items:
                self.logs = list(reversed(items))
                # Runtime requests are memory-only.  After a process restart,
                # no persisted log can still be a live local request.
                if self._recover_interrupted_streams_after_restart():
                    self.rewrite_log_file()
        except Exception as exc:
            self.log_write_error = f"日志加载失败: {exc}"

    def _recover_interrupted_streams_after_restart(self) -> bool:
        """Finalize persisted streams that cannot survive this process startup.

        Do not infer a successful HTTP response: the prior process never
        observed an upstream terminal event.  This is idempotent so later
        restarts retain the same explicit recovered terminal state.
        """
        changed = False
        for item in self.logs:
            detail = str(item.detail or "")
            final_result = self.detail_value(detail, "final_result").lower()
            is_stream = (
                str(item.event or "").startswith("stream")
                or "stream_started_at_ms=" in detail
                or final_result == "streaming"
            )
            unfinished = str(item.status or "").lower() == "streaming" or final_result == "streaming"
            if not is_stream or not unfinished:
                continue
            item.status = "interrupted"
            item.event = "stream_interrupted"
            item.usage_source = "stream_recovered"
            item.failure_scope = "process_restart"
            item.cooldown_applied = False
            item.detail = self.update_detail_fields(detail, {
                "stream_finalized": "true",
                "lifecycle": "stream_interrupted_after_restart",
                "final_result": "interrupted",
                "recovery": "recovered_after_restart",
                "recovery_reason": "process_restart",
                "failure_scope": "process_restart",
                "cooldown_applied": "false",
            })
            changed = True
        return changed

    def add_log(
        self, path: str, model: str, status: str, detail: str = "", duration_ms: int = 0,
        prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
        cached_tokens: int = 0, reasoning_tokens: int = 0, group: Any = None, event: str = "",
        request_id: str = "", attempt: int = 0, usage_source: str = "", cooldown_applied: bool = False,
        failure_scope: str = "",
    ) -> None:
        with self._logs_lock:
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
            del self.logs[self.LOG_RETENTION:]
            self._trim()
            self._record_runtime_activity(item)

    def _append(self, item: RequestLog) -> None:
        with self._logs_lock:
            try:
                self._repository.append(item)
            except Exception as exc:
                self.log_write_error = f"日志写入失败: {exc}"
                self.logs.insert(0, RequestLog(self._now(), "/system/log", "-", "warn", f"日志写入失败: {exc}; file={self.log_file}", group_name="系统", provider_type="system", event="system"))
                del self.logs[self.LOG_RETENTION:]

    def trim(self, max_lines: int = LOG_RETENTION) -> None:
        with self._logs_lock:
            try:
                self._repository.trim(max_lines)
            except Exception as exc:
                self.log_write_error = f"日志滚动失败: {exc}"

    def _trim(self) -> None:
        self.trim()

    def recent_logs(self) -> List[Dict[str, Any]]:
        return [asdict(item) for item in self.logs[:30]]

    @staticmethod
    def _activity_key(item: RequestLog) -> str:
        """给可原地回写的流日志一个稳定键，供前端按条目合并。"""
        detail = str(item.detail or "")
        is_stream = item.event.startswith("stream") or "stream_started_at_ms=" in detail
        if item.request_id:
            suffix = "stream" if is_stream else f"{item.event}|{item.time}"
            return f"{item.request_id}|{item.attempt}|{item.model}|{suffix}"
        return f"{item.time}|{item.path}|{item.model}|{item.event}|{item.attempt}"

    def _record_runtime_activity(self, item: Optional[RequestLog] = None, *, reset: bool = False) -> None:
        """记录运行态日志变化；调用方已经持有 _logs_lock。"""
        self._activity_cursor += 1
        entry: Dict[str, Any] = {
            "cursor": self._activity_cursor,
            "reset": bool(reset),
        }
        if item is not None:
            entry["key"] = self._activity_key(item)
            # 日志对象会在流终态原地更新，journal 必须保存当时快照。
            entry["log"] = asdict(item)
        self._activity_journal.append(entry)
        del self._activity_journal[:-self.ACTIVITY_JOURNAL_LIMIT]

    def runtime_activity_since(self, cursor: str = "", limit: int = 30) -> Dict[str, Any]:
        """返回运行态活动的快照或增量，不读取历史文件避免高频 I/O。"""
        limit = max(1, min(int(limit or 30), 30))
        with self._logs_lock:
            current = self._activity_cursor
            current_cursor = str(current)
            requested_text = str(cursor or "").strip()

            def snapshot(*, changed: Optional[bool] = None) -> Dict[str, Any]:
                return {
                    "cursor": current_cursor,
                    "changed": bool(self.logs) if changed is None else changed,
                    "mode": "snapshot",
                    "logs": [asdict(item) for item in self.logs[:limit]],
                }

            if not requested_text:
                return snapshot(changed=True)
            try:
                requested = int(requested_text)
            except (TypeError, ValueError):
                return snapshot()
            if requested == current:
                return {"cursor": current_cursor, "changed": False, "mode": "delta", "logs": []}
            if requested < 0 or requested > current:
                return snapshot()
            if not self._activity_journal:
                return snapshot()
            earliest = int(self._activity_journal[0]["cursor"])
            if requested < earliest - 1:
                return snapshot()

            updates = [entry for entry in self._activity_journal if int(entry["cursor"]) > requested]
            if any(entry.get("reset") for entry in updates):
                return snapshot(changed=True)
            # 同一条流日志可能在首包、终态等阶段多次回写；只下发最新快照。
            latest: Dict[str, Dict[str, Any]] = {}
            for entry in updates:
                key = str(entry.get("key") or "")
                if key and isinstance(entry.get("log"), dict):
                    latest[key] = entry
            logs = [entry["log"] for entry in sorted(latest.values(), key=lambda entry: int(entry["cursor"]), reverse=True)[:limit]]
            return {
                "cursor": current_cursor,
                "changed": bool(updates),
                "mode": "delta",
                "logs": logs,
            }

    def all_logs(self) -> List[RequestLog]:
        try:
            return self._repository.read_all() or list(reversed(self.logs))
        except Exception:
            return list(reversed(self.logs))

    def clear_logs(self) -> None:
        with self._logs_lock:
            self.logs.clear()
            self._record_runtime_activity(reset=True)
            try:
                self._repository.clear()
            except Exception:
                return

    def _find_stream_log(self, request_id: str, attempt: Optional[int] = None, candidate_label: str = "") -> Optional[RequestLog]:
        with self._logs_lock:
            for item in self.logs:
                if item.request_id != request_id or (
                    item.event != "stream_ok" and "stream_started_at_ms=" not in item.detail
                ):
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
        completion_signal: str = "", final_event: str = "", stream_metrics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        with self._logs_lock:
            item = self._find_stream_log(request_id, attempt, candidate_label)
            if not item:
                return False
            item.status = final_status
            if final_event:
                item.event = final_event
            item.duration_ms = duration_ms if duration_ms is not None else item.duration_ms
            item.prompt_tokens, item.completion_tokens, item.total_tokens, item.cached_tokens, item.reasoning_tokens = usage
            item.usage_source = usage_source
            if cooldown_applied is not None:
                item.cooldown_applied = cooldown_applied
            if failure_scope:
                item.failure_scope = failure_scope
            fields: Dict[str, Any] = {
                "stream_finalized": "true",
                "lifecycle": lifecycle,
                "final_result": final_result,
                "chunks_received": chunks_received,
                "bytes_received": bytes_received,
                "lock_released": "true",
            }
            if lifecycle == "manual_cancelled":
                fields.update({"cancel_source": "dashboard", "cooldown_applied": "false", "failure_scope": "client_cancelled"})
            if completion_signal:
                fields["completion_signal"] = completion_signal
                fields["upstream_terminal_missing" if completion_signal == "eof" else "upstream_terminal_received"] = "true"
            if lock_wait_ms is not None:
                fields["lock_wait_ms"] = lock_wait_ms
            if lock_release_reason:
                fields["lock_release_reason"] = lock_release_reason
            if cooldown_applied is not None:
                fields["cooldown_applied"] = str(bool(cooldown_applied)).lower()
            if failure_scope:
                fields["failure_scope"] = failure_scope
            if stream_metrics:
                metrics = dict(stream_metrics)
                # The handler records this after an actual flush.  Do not let
                # a runtime-side placeholder overwrite that completed timing
                # when a later frame finalizes the iterator.
                if int(metrics.get("first_downstream_flush_ms", -1) or -1) < 0:
                    try:
                        existing_flush_ms = int(self.detail_value(item.detail, "first_downstream_flush_ms") or -1)
                    except ValueError:
                        existing_flush_ms = -1
                    if existing_flush_ms >= 0:
                        metrics.pop("first_downstream_flush_ms", None)
                fields.update(metrics)
            item.detail = self.update_detail_fields(item.detail, fields)
            self.rewrite_log_file()
            self._record_runtime_activity(item)
            return True

    def rewrite_log_file(self) -> None:
        with self._logs_lock:
            try:
                self._repository.rewrite_oldest_first(self.logs)
            except Exception as exc:
                self.log_write_error = f"日志回写失败: {exc}"

    def record_stream_transport_event(self, request_id: str, event: str) -> None:
        """Persist transport-side evidence after the HTTP response has begun."""
        if event not in {"downstream_first_flush", "downstream_terminal_forwarded", "downstream_write_failed"}:
            return
        with self._logs_lock:
            item = self._find_stream_log(request_id)
            if not item:
                return
            if event == "downstream_write_failed" and (item.event == "request_cancelled" or item.failure_scope == "client_cancelled"):
                return
            if event == "downstream_first_flush":
                try:
                    started_at_ms = int(self.detail_value(item.detail, "stream_started_at_ms") or 0)
                except ValueError:
                    started_at_ms = 0
                try:
                    recorded = int(self.detail_value(item.detail, "first_downstream_flush_ms") or -1)
                except ValueError:
                    recorded = -1
                if recorded >= 0:
                    return
                elapsed_ms = max(0, int(time.time() * 1000) - started_at_ms) if started_at_ms else -1
                item.detail = self.update_detail_fields(item.detail, {"first_downstream_flush_ms": elapsed_ms})
            else:
                item.detail = self.update_detail_fields(item.detail, {event: "true"})
            if event == "downstream_write_failed":
                item.event = event
                item.failure_scope = "downstream"
            if event == "downstream_first_flush":
                # The stream finalizer, terminal-forwarded event, or write
                # failure will persist this field shortly afterwards.  Do not
                # rewrite up to 5000 log rows in the forwarding hot path.
                self._record_runtime_activity(item)
                return
            self.rewrite_log_file()
            self._record_runtime_activity(item)

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
            self._live_requests[request_id] = {
                "request_id": request_id, "path": path, "requested_model": requested_model,
                "model": requested_model, "group": "", "candidate": "", "aggregate_model": "",
                "stage": "selecting_candidate", "stage_label": "选择候选", "started_at": now,
                "updated_at": now, "elapsed_ms": 0, "stream": bool(stream), "attempt": 0,
                "status": "running", "slow": False, "possible_reason": "", "cancellable": True,
                "cancellation_state": "none", "cancel_requested_at": 0.0,
                "cancelled_at_stage": "", "response": None,
            }

    def update_live_request(self, request_id: str, **patch: Any) -> None:
        if not request_id:
            return
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if item:
                item.update({key: value for key, value in patch.items() if value is not None})
                item["updated_at"] = time.time()

    def set_live_response(self, request_id: str, response: Any) -> None:
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if item:
                item["response"] = response

    def close_live_response(self, request_id: str, response: Any = None) -> bool:
        """Close a registered upstream response at most once for this request."""
        target = None
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if item:
                registered = item.get("response")
                if registered is not None and (response is None or registered is response):
                    target = registered
                    item["response"] = None
        if target is None:
            return False
        try:
            target.close()
        except Exception:
            pass
        return True

    def cancellation_requested(self, request_id: str) -> bool:
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            return bool(item and item.get("cancellation_state") == "requested")

    def request_cancellation(self, request_id: str, source: str = "dashboard") -> Dict[str, Any]:
        if not request_id or len(request_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
            return {"ok": False, "code": "invalid_request_id", "message": "请求标识无效"}
        response = None
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if not item:
                return {"ok": False, "code": "request_not_found", "message": "未找到进行中的请求，可能已结束"}
            if item.get("cancellation_state") == "requested":
                return {"ok": True, "request_id": request_id, "state": "cancellation_already_requested", "message": "该请求已在终止处理中"}
            item["cancellation_state"] = "requested"
            item["cancel_requested_at"] = time.time()
            item["cancelled_at_stage"] = str(item.get("stage") or "")
            item["cancellable"] = False
            item["stage"] = "cancellation_requested"
            item["stage_label"] = "终止中"
            item["cancel_source"] = source
            item["updated_at"] = time.time()
            response = item.get("response")
        self.close_live_response(request_id)
        return {"ok": True, "request_id": request_id, "state": "cancellation_requested", "message": "已发送终止指令，正在释放本地请求资源。"}

    def finish_live_request(self, request_id: str, status: str = "done") -> None:
        if not request_id:
            return
        with self._live_requests_lock:
            item = self._live_requests.get(request_id)
            if item:
                item.update({"status": status, "stage": status, "stage_label": "已完成" if status == "done" else "已结束", "updated_at": time.time(), "cancellable": False, "cancellation_state": "finalized" if status == "manual_cancelled" else item.get("cancellation_state", "none")})
            self._live_requests.pop(request_id, None)

    def live_requests_payload(self) -> Dict[str, Any]:
        now = time.time()
        with self._live_requests_lock:
            items = []
            for item in self._live_requests.values():
                row = dict(item)
                row.pop("response", None)
                elapsed_ms = int((now - float(row.get("started_at") or now)) * 1000)
                row["elapsed_ms"] = elapsed_ms
                row["slow"] = elapsed_ms >= 10000 or str(row.get("stage") or "") in {"waiting_serial_protection", "waiting_first_byte"} and elapsed_ms >= 5000
                if row["slow"] and not row.get("possible_reason"):
                    row["possible_reason"] = {"waiting_serial_protection": "该连接组已开启串行保护，正在等待同一候选完成", "waiting_first_byte": "正在等待首个完整 SSE 帧；上游可能处于缓冲或使用非标准分隔"}.get(str(row.get("stage") or ""), "请求耗时较长，请关注上游状态")
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
