"""HTTP route grouping behind the ``RouterHandler`` transport facade.

This module owns only existing route dispatch and API business calculations.  The
handler remains the source of request parsing, response serialization and server
owned dependencies through its explicit facade methods/properties.
"""
from __future__ import annotations

import datetime
import json
import re
import uuid
from dataclasses import asdict
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from linrouter_platform import get_platform
from linrouter_core.config.constants import (
    DEFAULT_AUTO_MODEL_NAME,
    PROVIDER_ARK,
    PROVIDER_PROXY,
    PROVIDER_RELAY,
)
from linrouter_core.config.models import AggregateMember, AggregateModel, ConnectionGroup, ModelConfig
from linrouter_core.runtime.config_api_runtime import (
    ConfigApiError,
    export_backup_payload,
    export_config_payload,
    import_backup_payload,
    import_config_payload,
)
from linrouter_core.runtime.handler_runtime import handle_proxy_request


_GROUP_VERIFICATION_FIELDS = ("provider_type", "base_url", "ark_api_key", "api_key")
_MODEL_VERIFICATION_FIELDS = ("group_id", "ep_id", "upstream_model", "api_key")


def _connectivity_fields_changed(before: Any, after: Any, fields: tuple[str, ...]) -> bool:
    return bool(before) and any(str(getattr(before, field, "") or "") != str(getattr(after, field, "") or "") for field in fields)


def handle_get(handler: Any) -> None:
    parsed = urlparse(handler.path)
    if parsed.path == "/":
        handler._send_text(handler._render_index_page(), content_type="text/html; charset=utf-8")
        return
    if parsed.path == "/health":
        handler._send_json({
            "ok": True,
            "groups": len(handler.store.groups),
            "models": len(handler.store.models),
            "aggregate_models": len(handler.store.aggregate_models),
            "aggregate_members": len(handler.store.aggregate_members),
        })
        return
    if parsed.path.startswith("/") and not parsed.path.startswith("/api/") and not parsed.path.startswith("/v1/"):
        # 服务静态资源（css/js/html 等），统一映射到 static/ 目录
        rel = parsed.path.lstrip("/")
        if ".." in rel:
            handler._send_json({"error": {"message": "禁止访问", "type": "invalid_request_error", "code": "forbidden"}}, status=403)
            return
        file_path = handler._platform().get_resource_path("static", *rel.split("/"))
        handler._send_file(file_path)
        return
    if parsed.path in {"/v1/models", "/models"}:
        ctx = handler._require_route_context()
        if not ctx:
            return
        try:
            handler._send_model_list(ctx)
        except Exception as err:
            router = getattr(handler, 'router', None)
            error_msg = f"local model list failed; error={str(err)}"
            if router and hasattr(router, '_short_error'):
                error_msg = f"local model list failed; error={router._short_error(str(err))}"
            if router and hasattr(router, 'add_log'):
                router.add_log(
                    "/v1/models",
                    "lin-router",
                    "500",
                    error_msg,
                    0,
                    event="models_failed",
                )
            handler._send_json({
                "object": "list",
                "data": [{
                    "id": DEFAULT_AUTO_MODEL_NAME,
                    "object": "model",
                    "created": 0,
                    "owned_by": "lin-router",
                    "root": DEFAULT_AUTO_MODEL_NAME,
                    "parent": None,
                }],
            })
        return
    if parsed.path == "/api/state":
        handler.store.refresh_expired_cooldowns()
        settings = handler.server.settings_store.to_dict()  # type: ignore[attr-defined]
        handler._send_json({
            "config_file": str(handler.store.path),
            "auto_model_name": DEFAULT_AUTO_MODEL_NAME,
            "settings": {
                **settings,
                # 开机自启以注册表真实状态为准
                "auto_start": handler._platform().is_autostart_enabled(),
            },
            "group_meta": {
                group.id: {
                    "auto_model_name": handler.router.group_auto_model_name(group),
                    "model_count": len([m for m in handler.store.models if m.group_id == group.id]),
                    "usable_count": len([m for m in handler.store.models if m.group_id == group.id and m.usable]),
                }
                for group in handler.store.groups
            },
            "groups": [asdict(g) for g in handler.store.groups],
            "models": [asdict(m) for m in handler.store.models],
            "aggregate_models": [asdict(m) for m in handler.store.aggregate_models],
            "aggregate_members": [asdict(m) for m in handler.store.aggregate_members],
            "aggregate_member_revisions": dict(handler.store.aggregate_member_revisions),
            "logs": handler._filtered_recent_logs(),
            "log_file": str(handler.router.log_file),
            "log_write_error": handler.router.log_write_error,
        })
        return
    if parsed.path == "/api/runtime-state":
        params = parse_qs(parsed.query)
        include_skip = str((params.get("include_skip") or params.get("debug") or [""])[0] or "").lower() in {"1", "true", "yes", "on"}
        scope = str((params.get("scope") or [""])[0] or "").strip().lower()
        if scope not in {"", "dashboard", "config"}:
            handler._send_json({
                "error": {
                    "message": "运行态刷新范围无效，仅支持 dashboard 或 config",
                    "type": "invalid_request_error",
                    "code": "invalid_runtime_scope",
                }
            }, status=400)
            return
        payload = handler._runtime_state_payload(
            include_skip=include_skip,
            scope=scope,
            revision=str((params.get("revision") or [""])[0] or ""),
            activity_cursor=str((params.get("activity_cursor") or [""])[0] or ""),
        )
        # 无 scope 的旧调用保留原有完整 shape，避免旧管理台/脚本升级时断裂。
        if not scope:
            payload["live_requests"] = handler.router.live_requests_payload().get("requests", [])
        handler._send_json(payload)
        return
    if parsed.path == "/api/live-requests":
        handler._send_json(handler.router.live_requests_payload())
        return
    if parsed.path.startswith("/api/diagnose/"):
        request_id = parsed.path.split("/api/diagnose/", 1)[1].strip("/")
        payload = handler.router.diagnose_request(request_id)
        handler._send_json(payload, status=200 if payload.get("ok") else 404)
        return
    if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/stats"):
        parts = parsed.path.split("/")
        aggregate_id = parts[3] if len(parts) >= 4 else ""
        params = parse_qs(parsed.query)
        limit = int((params.get("limit") or [100])[0] or 100)
        payload = handler._aggregate_stats_payload(aggregate_id, limit)
        if not payload.get("ok"):
            handler._send_json(payload, status=404)
        else:
            handler._send_json(payload)
        return
    if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/members"):
        parts = parsed.path.split("/")
        if len(parts) != 5:
            handler._send_json({"error": {"message": "请求路径无效", "type": "invalid_request_error", "code": "invalid_path"}}, status=400)
            return
        aggregate_id = parts[3]
        if not handler.store.find_aggregate(aggregate_id):
            handler._send_json({"error": {"message": "聚合模型不存在", "type": "invalid_request_error", "code": "aggregate_not_found"}}, status=404)
            return
        members = sorted(handler.store.get_aggregate_members(aggregate_id), key=lambda member: member.priority)
        handler._send_json({
            "ok": True,
            "aggregate_id": aggregate_id,
            "revision": handler.store.aggregate_member_revision(aggregate_id),
            "members": [asdict(member) for member in members],
        })
        return
    if parsed.path.startswith("/api/client-config/"):
        group_id = parsed.path.split("/", 3)[3]
        group = handler.store.find_group(group_id)
        if not group:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
            return
        handler._send_json({
            "base_url": handler._client_base_url(),
            "api_key": group.route_key,
            "model": DEFAULT_AUTO_MODEL_NAME,
            "group_id": group.id,
            "group_name": group.name,
        })
        return
    if parsed.path == "/api/settings":
        # 返回当前用户设置（开机自启、启动最小化等）
        handler._send_json(handler.server.settings_store.to_dict())
        return
    if parsed.path == "/api/debug/capture":
        capture = handler.router.debug_capture.load_capture()
        if capture is None:
            handler._send_json({"ok": True, "exists": False})
            return
        # 返回快照摘要，不暴露完整 body_base64，避免前端意外泄露长内容
        summary = {k: v for k, v in capture.items() if k != "body_base64"}
        summary["exists"] = True
        summary["has_body"] = bool(capture.get("body_base64"))
        handler._send_json({"ok": True, "capture": summary})
        return
    if parsed.path == "/api/logs" or parsed.path == "/api/logs/":
        params = parse_qs(parsed.query)

        def _first(values, default=""):
            return values[0] if values else default

        # The JSONL repository, not the in-memory recent window, is the
        # canonical history source for server-side filtering and pagination.
        logs = list(reversed(handler.router.all_logs()))
        limit = int(_first(params.get("limit"), "0") or 0)
        offset = int(_first(params.get("offset"), "0") or 0)
        group_filter = _first(params.get("group"))
        status_filter = _first(params.get("status"))
        event_filter = _first(params.get("event"))
        include_skip = str(_first(params.get("include_skip")) or _first(params.get("debug")) or "").lower() in {"1", "true", "yes", "on"}
        aggregate_filter = _first(params.get("aggregate"))
        start_str = _first(params.get("start"))
        end_str = _first(params.get("end"))

        def _ts(s):
            try:
                return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0

        start_ts = _ts(start_str) if start_str else 0
        end_ts = _ts(end_str) if end_str else 0

        def _keep(item):
            if not include_skip and (handler._is_config_skip_log(item) or str(getattr(item, "usage_source", "")) == "manual_probe"):
                return False
            if group_filter and getattr(item, "group_id", "") != group_filter:
                return False
            if aggregate_filter:
                if getattr(item, "aggregate_id", "") != aggregate_filter and getattr(item, "aggregate_model", "") != aggregate_filter:
                    return False
            if event_filter and getattr(item, "event", "") != event_filter:
                return False
            if status_filter:
                status = str(getattr(item, "status", "") or "")
                if status_filter == "2xx":
                    if not status.startswith("2"):
                        return False
                elif status_filter == "cooldown":
                    if getattr(item, "event", "") not in ("cooldown", "fallback", "retry_ok"):
                        return False
                elif status_filter == "error":
                    event = getattr(item, "event", "")
                    if status.startswith("2") or event in ("cooldown", "fallback", "retry_ok"):
                        return False
                elif status not in status_filter:
                    return False
            if start_ts or end_ts:
                t = _ts(getattr(item, "time", ""))
                if start_ts and t < start_ts:
                    return False
                if end_ts and t > end_ts:
                    return False
            return True

        filtered = [item for item in logs if _keep(item)]
        total = len(filtered)
        if offset:
            filtered = filtered[offset:]
        if limit and limit > 0:
            filtered = filtered[:limit]
        handler._send_json({"ok": True, "total": total, "offset": offset, "limit": limit, "logs": [asdict(item) for item in filtered]})
        return
    if parsed.path == "/api/logs/export":
        csv_text = handler.router.export_logs_csv()
        handler._send_text(csv_text, content_type="text/csv; charset=utf-8")
        return
    if parsed.path == "/api/aggregates":
        handler._send_json({
            "ok": True,
            "aggregate_models": [asdict(m) for m in handler.store.aggregate_models],
            "aggregate_members": [asdict(m) for m in handler.store.aggregate_members],
            "aggregate_member_revisions": dict(handler.store.aggregate_member_revisions),
        })
        return
    if parsed.path == "/api/logs/all":
        handler._send_json([asdict(item) for item in handler.router.all_logs()])
        return
    if parsed.path == "/api/config/export":
        payload = export_config_payload(handler.store)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Disposition", 'attachment; filename="lin-router-config-export.json"')
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    if parsed.path == "/api/backup/export":
        payload = export_backup_payload(handler.store, handler.server.settings_store)  # type: ignore[attr-defined]
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Disposition", 'attachment; filename="lin-router-backup.json"')
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return
    handler._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)


def handle_post(handler: Any) -> None:
    parsed = urlparse(handler.path)
    if parsed.path.startswith("/api/groups/") and parsed.path.endswith("/speed-test"):
        payload = handler.router.speed_test_group(parsed.path.split("/")[3])
        code = str(payload.get("code") or "")
        if code == "group_not_found":
            status = 404
        elif code == "speed_test_running":
            status = 409
        elif code == "speed_test_rate_limited":
            status = 429
        else:
            status = 200 if payload.get("ok") else 503
        handler._send_json(payload, status=status)
        return
    if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/speed-test"):
        payload = handler.router.speed_test_aggregate(parsed.path.split("/")[3])
        code = str(payload.get("code") or "")
        if code == "aggregate_not_found":
            status = 404
        elif code == "speed_test_running":
            status = 409
        elif code == "speed_test_rate_limited":
            status = 429
        else:
            status = 200 if payload.get("ok") else 503
        handler._send_json(payload, status=status)
        return
    if parsed.path.startswith("/api/live-requests/") and parsed.path.endswith("/cancel"):
        request_id = parsed.path[len("/api/live-requests/"):-len("/cancel")].strip("/")
        payload = handler.router.cancel_live_request(request_id, source="dashboard")
        status = 200 if payload.get("ok") else (404 if payload.get("code") == "request_not_found" else 400)
        handler._send_json(payload, status=status)
        return
    if parsed.path == "/api/config/import":
        payload = handler._read_multipart_json()
        if payload is None:
            try:
                payload = handler._read_json()
            except Exception as e:
                handler._send_json({"error": {"message": f"配置文件无效：{e}", "type": "invalid_request_error", "code": "invalid_config_file"}}, status=400)
                return
        try:
            handler._send_json(import_config_payload(handler.store, payload))
        except ConfigApiError as error:
            handler._send_json(error.response(), status=400)
        return
    if parsed.path == "/api/backup/import":
        payload = handler._read_multipart_json()
        if payload is None:
            try:
                payload = handler._read_json()
            except Exception as e:
                handler._send_json({"error": {"message": f"备份文件无效：{e}", "type": "invalid_request_error", "code": "invalid_backup_file"}}, status=400)
                return
        try:
            response, new_settings = import_backup_payload(handler.store, payload)
        except ConfigApiError as error:
            handler._send_json(error.response(), status=400)
            return
        settings_store = handler.server.settings_store  # type: ignore[attr-defined]
        if "auto_start" in new_settings:
            handler._platform().set_autostart(bool(new_settings["auto_start"]))
        updated = settings_store.update(new_settings)
        if any(key in new_settings for key in ("upstream_http_client", "upstream_http2", "upstream_keepalive")):
            handler.router._refresh_upstream_client()
        handler._send_json({
            **response,
            "settings": {**updated, "auto_start": handler._platform().is_autostart_enabled()},
        })
        return
    if parsed.path == "/api/groups":
        payload = handler._read_json()
        if not payload.get("name"):
            handler._send_json({"error": {"message": "缺少连接组名称", "type": "invalid_request_error", "code": "missing_group_name"}}, status=400)
            return
        existing = handler.store.find_group(str(payload.get("id") or ""))
        if existing and not payload.get("route_key"):
            payload["route_key"] = existing.route_key
        if not payload.get("provider_type"):
            payload["provider_type"] = existing.provider_type if existing else PROVIDER_ARK
        if existing and "ark_api_key" not in payload:
            payload["ark_api_key"] = existing.ark_api_key
        if existing and "api_key" not in payload:
            payload["api_key"] = existing.api_key
        if existing and "auto_model_cooldown_minutes" not in payload:
            payload["auto_model_cooldown_minutes"] = existing.auto_model_cooldown_minutes
        if existing and "stream_idle_timeout" not in payload:
            payload["stream_idle_timeout"] = existing.stream_idle_timeout
        if existing and "waf_compatible" not in payload:
            payload["waf_compatible"] = existing.waf_compatible
        if existing and "serial_protection" not in payload:
            payload["serial_protection"] = existing.serial_protection
        if existing and "waf_accept_policy" not in payload:
            payload["waf_accept_policy"] = existing.waf_accept_policy
        if existing and "waf_client_mode" not in payload:
            payload["waf_client_mode"] = existing.waf_client_mode
        if existing and "reasoning_support" not in payload:
            payload["reasoning_support"] = existing.reasoning_support
        if existing and "auto_model_name" not in payload:
            payload["auto_model_name"] = existing.auto_model_name
        # 自动路由模型名空值按默认值处理
        auto_name = str(payload.get("auto_model_name") or "").strip() or DEFAULT_AUTO_MODEL_NAME
        # 不允许与同组模型 name/id/ep_id 冲突；仅 all-router-auto 为全局保留名
        group_id_for_check = str(payload.get("id") or existing.id if existing else "").strip()
        conflict_model = next((m for m in handler.store.models if m.group_id == group_id_for_check and auto_name in {m.id, m.name, m.ep_id}), None)
        if conflict_model or auto_name == "all-router-auto":
            handler._send_json({"ok": False, "message": f"自动路由模型名 '{auto_name}' 与已有模型或保留名称冲突"}, status=400)
            return
        group = ConnectionGroup.from_dict(payload)
        if group.provider_type == PROVIDER_PROXY and not group.api_key and group.ark_api_key:
            group.api_key = group.ark_api_key
        if group.provider_type == PROVIDER_RELAY:
            group.ark_api_key = ""
            group.api_key = ""
        if group.provider_type == PROVIDER_ARK:
            group.api_key = ""
        group_verification_changed = _connectivity_fields_changed(existing, group, _GROUP_VERIFICATION_FIELDS)
        if group_verification_changed:
            handler.store.invalidate_group_verification(group.id)
        handler.store.upsert_group(group)
        handler._send_json({"ok": True, "group": asdict(group)})
        return
    if parsed.path.startswith("/api/groups/") and parsed.path.endswith("/clone"):
        group_id = parsed.path.split("/")[3]
        cloned = handler._clone_group(group_id)
        if not cloned:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
            return
        handler._send_json({"ok": True, **cloned})
        return
    if parsed.path == "/api/models":
        payload = handler._read_json()
        if not payload.get("name") or not payload.get("ep_id") or not payload.get("group_id"):
            handler._send_json({"error": {"message": "缺少必填字段", "type": "invalid_request_error", "code": "missing_required_fields"}}, status=400)
            return
        group = handler.store.find_group(str(payload["group_id"]))
        if not group:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=400)
            return
        existing = handler.store.find_model(str(payload.get("id") or ""))
        merged: Dict[str, Any] = asdict(existing) if existing else {}
        merged.update(payload)
        model = ModelConfig.from_dict(merged)
        if existing:
            model.usable = bool(merged.get("usable", existing.usable))
            model.last_error = str(merged.get("last_error", existing.last_error))
            model.last_success_at = str(merged.get("last_success_at", existing.last_success_at))
            model.last_checked_at = str(merged.get("last_checked_at", existing.last_checked_at))
        if group.provider_type != PROVIDER_RELAY:
            model.api_key = ""
            model.price_group = ""
        if group.provider_type in {PROVIDER_RELAY, PROVIDER_PROXY} and not model.upstream_model:
            model.upstream_model = model.ep_id
        if group.provider_type not in {PROVIDER_RELAY, PROVIDER_PROXY}:
            model.upstream_model = ""
        model_verification_changed = _connectivity_fields_changed(existing, model, _MODEL_VERIFICATION_FIELDS)
        if model_verification_changed:
            model.last_success_at = ""
            model.last_checked_at = ""
            model.last_error = ""
        # 模型名/ep_id 不得与所属连接组的自动路由模型名冲突，避免路由歧义
        auto_name = handler.router.group_auto_model_name(group)
        if auto_name in {model.name, model.ep_id}:
            handler._send_json({"ok": False, "message": f"模型名/ep_id 与连接组自动路由模型名 '{auto_name}' 冲突"}, status=400)
            return
        handler.store.upsert_model(model)
        if model_verification_changed:
            handler.store.invalidate_model_member_verification(model.id)
        if group.provider_type in {PROVIDER_RELAY, PROVIDER_PROXY}:
            group.upstream_models = []
            group.upstream_models_fetched_at = ""
            handler.store.upsert_group(group)
        handler._send_json({"ok": True, "model": asdict(model)})
        return
    if parsed.path == "/api/models/batch":
        payload = handler._read_json()
        group_id = str(payload.get("group_id") or "")
        group = handler.store.find_group(group_id)
        if not group_id or not group:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=400)
            return
        raw_text = str(payload.get("text") or "")
        fmt = str(payload.get("format") or "lines").strip().lower()
        defaults = payload.get("defaults") or {}
        preview = bool(payload.get("preview", False))

        def _parse_batch_items(text: str, fmt: str) -> List[Dict[str, Any]]:
            items: List[Dict[str, Any]] = []
            if fmt == "json":
                arr = json.loads(text)
                if not isinstance(arr, list):
                    raise ValueError("JSON 格式必须是数组")
                for idx, entry in enumerate(arr, start=1):
                    if isinstance(entry, dict):
                        copied = dict(entry)
                        copied["line"] = idx
                        items.append(copied)
                    elif isinstance(entry, str):
                        items.append({"ep_id": entry.strip(), "line": idx})
                    else:
                        items.append({"ep_id": "", "line": idx, "parse_error": "JSON 数组项必须是对象或字符串"})
            elif fmt == "models_response":
                obj = json.loads(text)
                data = obj.get("data") if isinstance(obj, dict) else None
                if not isinstance(data, list):
                    raise ValueError("/v1/models 响应必须包含 data 数组")
                for idx, entry in enumerate(data, start=1):
                    if isinstance(entry, dict):
                        items.append({"ep_id": str(entry.get("id") or "").strip(), "line": idx})
                    elif isinstance(entry, str):
                        items.append({"ep_id": entry.strip(), "line": idx})
                    else:
                        items.append({"ep_id": "", "line": idx, "parse_error": "data 项必须是对象或字符串"})
            else:
                # lines 格式：每行一个模型名，空行跳过但保留原始行号
                for idx, line in enumerate(text.splitlines(), start=1):
                    ep = line.strip()
                    if ep:
                        items.append({"ep_id": ep, "line": idx})
            return items

        try:
            raw_items = _parse_batch_items(raw_text, fmt)
        except Exception as err:
            handler._send_json({"ok": False, "message": f"解析失败：{err}"}, status=400)
            return

        existing_ep_ids = {m.ep_id for m in handler.store.models if m.group_id == group_id}
        existing_names = {m.name for m in handler.store.models if m.group_id == group_id}
        is_relay = group.provider_type == PROVIDER_RELAY
        is_proxy = group.provider_type == PROVIDER_PROXY
        need_upstream = is_relay or is_proxy

        processed: List[Dict[str, Any]] = []
        seen_ep_ids: set[str] = set()
        seen_names: set[str] = set()
        name_re = re.compile(r"^[^\s,;]+$")
        for item in raw_items:
            line_no = int(item.get("line") or 0)
            ep_id = str(item.get("ep_id") or item.get("upstream_model") or "").strip()
            name = str(item.get("name") or "").strip() or ep_id
            upstream_model = str(item.get("upstream_model") or "").strip() or ep_id
            # 单个模型字段 > 批量统一字段 > 默认值
            api_key = str(item.get("api_key") if item.get("api_key") is not None else defaults.get("api_key") or "").strip()
            price_group = str(item.get("price_group") if item.get("price_group") is not None else defaults.get("price_group") or "").strip()
            usable = item.get("usable") if isinstance(item.get("usable"), bool) else bool(defaults.get("usable", True))
            price_input = float(item.get("price_input") if item.get("price_input") is not None else defaults.get("price_input") or 0)
            price_output = float(item.get("price_output") if item.get("price_output") is not None else defaults.get("price_output") or 0)

            status = "new"
            reason = "将新增"
            if item.get("parse_error"):
                status = "invalid"
                reason = str(item.get("parse_error"))
            elif not ep_id:
                status = "invalid"
                reason = "模型名为空"
            elif not name_re.match(ep_id) or not name_re.match(name):
                status = "invalid"
                reason = "模型名不能包含空白、逗号或分号"
            elif ep_id in existing_ep_ids or name in existing_names:
                status = "duplicate"
                reason = "已存在同名模型，默认跳过"
            elif ep_id in seen_ep_ids or name in seen_names:
                status = "duplicate"
                reason = "本次导入列表中重复，默认跳过"
            elif need_upstream and not upstream_model:
                status = "invalid"
                reason = "缺少上游模型名"

            if ep_id:
                seen_ep_ids.add(ep_id)
            if name:
                seen_names.add(name)
            processed.append({
                "line": line_no,
                "name": name,
                "ep_id": ep_id,
                "upstream_model": upstream_model if need_upstream else "",
                "api_key": api_key if is_relay else "",
                "has_api_key": bool(api_key) if is_relay else False,
                "price_group": price_group if is_relay else "",
                "price_input": price_input,
                "price_output": price_output,
                "usable": usable,
                "status": status,
                "reason": reason,
            })

        total = len(processed)
        new_count = sum(1 for p in processed if p["status"] == "new")
        duplicate_count = sum(1 for p in processed if p["status"] == "duplicate")
        invalid_count = sum(1 for p in processed if p["status"] == "invalid")

        if preview:
            handler._send_json({
                "ok": True,
                "preview": True,
                "summary": {
                    "total": total,
                    "new": new_count,
                    "duplicate": duplicate_count,
                    "invalid": invalid_count,
                },
                "items": processed,
            })
            return

        if invalid_count > 0:
            handler._send_json({"ok": False, "message": f"存在 {invalid_count} 条无效记录，请修正后再导入"}, status=400)
            return

        added = 0
        skipped = 0
        for p in processed:
            if p["status"] == "duplicate":
                skipped += 1
                continue
            handler.store.upsert_model(ModelConfig(
                id=uuid.uuid4().hex,
                name=p["name"],
                ep_id=p["ep_id"],
                group_id=group_id,
                upstream_model=p["upstream_model"],
                api_key=p["api_key"],
                price_group=p["price_group"],
                price_input=p["price_input"],
                price_output=p["price_output"],
                usable=p["usable"],
            ))
            added += 1
        handler._send_json({"ok": True, "added": added, "skipped": skipped})
        return
    if parsed.path == "/api/models/fetch-upstream":
        payload = handler._read_json()
        group_id = str(payload.get("group_id") or "")
        group = handler.store.find_group(group_id)
        if not group:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=400)
            return
        if group.provider_type not in {PROVIDER_RELAY, PROVIDER_PROXY}:
            handler._send_json({"error": {"message": "仅 relay/proxy 连接组支持拉取上游模型", "type": "invalid_request_error", "code": "upstream_fetch_unsupported_provider"}}, status=400)
            return
        auth_key = handler._effective_group_auth(group, payload)
        if not auth_key:
            handler._send_json({"error": {"message": "缺少上游 API Key", "type": "invalid_request_error", "code": "missing_upstream_api_key"}}, status=400)
            return
        try:
            items = handler._fetch_upstream_models(group, auth_key)
        except Exception as err:
            handler._send_json({"error": {"message": f"拉取上游模型失败：{err}", "type": "api_error", "code": "upstream_fetch_failed"}}, status=500)
            return
        candidates: List[Dict[str, Any]] = []
        for item in items:
            ep_id = str(item.get("id") or "").strip()
            if not ep_id or ep_id == DEFAULT_AUTO_MODEL_NAME:
                continue
            name = str(item.get("display_name") or item.get("name") or ep_id).strip()
            candidates.append({
                "name": name or ep_id,
                "ep_id": ep_id,
                "root": str(item.get("root") or item.get("id") or ep_id).strip(),
            })
        group.upstream_models = candidates
        group.upstream_models_fetched_at = handler.router._now()
        handler.store.upsert_group(group)
        handler._send_json({
            "ok": True,
            "count": len(candidates),
        })
        return
    if parsed.path.endswith("/toggle") and parsed.path.startswith("/api/models/"):
        model_id = parsed.path.split("/")[3]
        model = handler.store.find_model(model_id)
        if not model:
            handler._send_json({"error": {"message": "模型不存在", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
            return
        if model.cooldown_until:
            # 恢复冷却视为用户手动启用
            model.usable = True
            model.disabled_by_user = False
            model.cooldown_until = 0
            model.cooldown_reason = ""
            model.last_error = ""
            model.last_checked_at = handler.router._now()
        elif model.usable:
            # 当前可用 -> 用户手动禁用
            model.usable = False
            model.disabled_by_user = True
        else:
            # 当前不可用（用户禁用或冷却已过期） -> 用户手动启用
            model.usable = True
            model.disabled_by_user = False
            model.cooldown_until = 0
            model.cooldown_reason = ""
            model.last_error = ""
            model.last_checked_at = handler.router._now()
        handler.store.save()
        handler._send_json({"ok": True, "usable": model.usable, "disabled_by_user": model.disabled_by_user})
        return
    if parsed.path.endswith("/usable") and parsed.path.startswith("/api/models/"):
        model_id = parsed.path.split("/")[3]
        model = handler.store.find_model(model_id)
        if not model:
            handler._send_json({"error": {"message": "模型不存在", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
            return
        payload = handler._read_json()
        usable = bool(payload.get("usable", True))
        model.usable = usable
        model.disabled_by_user = not usable
        if usable:
            model.cooldown_until = 0
            model.cooldown_reason = ""
            model.last_error = ""
        model.last_checked_at = handler.router._now()
        handler.store.save()
        handler._send_json({"ok": True, "usable": model.usable, "disabled_by_user": model.disabled_by_user})
        return
    if parsed.path == "/api/models/usable/all":
        payload = handler._read_json()
        usable = bool(payload.get("usable", True))
        changed = False
        with handler.store._lock:
            for model in handler.store.models:
                if model.usable != usable:
                    model.usable = usable
                    changed = True
                model.disabled_by_user = not usable
                if usable:
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    model.last_error = ""
            if changed:
                handler.store.save()
        handler._send_json({"ok": True, "changed": changed})
        return
    if parsed.path.endswith("/toggle") and parsed.path.startswith("/api/groups/"):
        group_id = parsed.path.split("/")[3]
        changed = handler.store.toggle_group(group_id)
        if not changed:
            handler._send_json({"error": {"message": "连接组不存在或为空", "type": "invalid_request_error", "code": "group_not_found_or_empty"}}, status=400)
            return
        handler._send_json({"ok": True})
        return
    if parsed.path.endswith("/usable") and parsed.path.startswith("/api/groups/"):
        group_id = parsed.path.split("/")[3]
        group = handler.store.find_group(group_id)
        if not group:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
            return
        payload = handler._read_json()
        usable = bool(payload.get("usable", True))
        changed = False
        with handler.store._lock:
            for model in handler.store.models:
                if model.group_id != group_id:
                    continue
                if model.usable != usable:
                    model.usable = usable
                    changed = True
                model.disabled_by_user = not usable
                if usable:
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    model.last_error = ""
            if changed:
                handler.store.save()
        handler._send_json({"ok": True, "changed": changed})
        return
    if parsed.path.endswith("/move") and parsed.path.startswith("/api/models/"):
        model_id = parsed.path.split("/")[3]
        payload = handler._read_json()
        moved = handler.store.move_model(model_id, str(payload.get("direction", "")))
        if not moved:
            handler._send_json({"error": {"message": "移动失败", "type": "invalid_request_error", "code": "move_failed"}}, status=400)
            return
        handler._send_json({"ok": True})
        return
    if parsed.path.startswith("/api/groups/") and parsed.path.endswith("/delete-preview"):
        group_id = parsed.path.split("/")[3]
        payload = handler._group_delete_preview(group_id)
        handler._send_json(payload, status=200 if payload.get("ok") else 404)
        return
    if parsed.path.startswith("/api/models/") and parsed.path.endswith("/delete-preview"):
        model_id = parsed.path.split("/")[3]
        payload = handler._model_delete_preview(model_id)
        handler._send_json(payload, status=200 if payload.get("ok") else 404)
        return
    if parsed.path.startswith("/api/models/") and parsed.path.endswith("/recover"):
        model_id = parsed.path.split("/")[3]
        payload = handler.router.recover_model(model_id)
        handler._send_json(payload, status=200 if payload.get("ok") else 400)
        return
    if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/sort-preview"):
        member_id = parsed.path.split("/")[3]
        payload_in = handler._read_json()
        payload = handler._aggregate_member_sort_preview(member_id, str(payload_in.get("direction") or ""))
        handler._send_json(payload, status=200 if payload.get("ok") else 404)
        return
    if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/clear-cooldown-preview"):
        member_id = parsed.path.split("/")[3]
        payload = handler._aggregate_member_clear_cooldown_preview(member_id)
        handler._send_json(payload, status=200 if payload.get("ok") else 404)
        return
    if parsed.path == "/api/reset":
        handler.store.reset_usable()
        handler._send_json({"ok": True})
        return
    if parsed.path == "/api/logs/clear":
        handler.router.clear_logs()
        handler._send_json({"ok": True})
        return
    if parsed.path == "/api/settings":
        # 更新用户设置，未知字段会被忽略
        raw = handler._read_raw_body()
        payload = handler._json_from_raw(raw)
        if not isinstance(payload, dict):
            handler._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
            return
        allowed = {
            "auto_start", "start_minimized", "theme", "auto_refresh_logs",
            "upstream_http_client", "upstream_http2", "upstream_keepalive",
            "debug_mode", "debug_capture_enabled", "debug_capture_last_body",
            "normalize_tools_order",
        }
        new_settings = {k: v for k, v in payload.items() if k in allowed}
        # 开机自启需要同步到 Windows 注册表
        if "auto_start" in new_settings:
            handler._platform().set_autostart(bool(new_settings["auto_start"]))
        settings_store = handler.server.settings_store  # type: ignore[attr-defined]
        updated = settings_store.update(new_settings)
        # 上游客户端相关设置变更后，立即刷新客户端实例
        if any(k in new_settings for k in ("upstream_http_client", "upstream_http2", "upstream_keepalive")):
            handler.router._refresh_upstream_client()
        handler._send_json({
            **updated,
            "auto_start": handler._platform().is_autostart_enabled(),
        })
        return
    # 聚合模型 CRUD（POST /api/aggregates、POST /api/aggregates/{id}/members）
    if parsed.path == "/api/aggregates":
        payload = handler._read_json()
        if not isinstance(payload, dict):
            handler._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
            return
        name = str(payload.get("name") or "").strip()
        if not name:
            handler._send_json({"ok": False, "message": "聚合模型名不能为空"}, status=400)
            return
        aggregate_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex
        existing = handler.store.find_aggregate(aggregate_id)
        merged: Dict[str, Any] = asdict(existing) if existing else {}
        merged.update(payload)
        merged["id"] = aggregate_id
        merged["name"] = name
        aggregate = AggregateModel.from_dict(merged)
        ok, msg = handler.store.upsert_aggregate(aggregate)
        if not ok:
            handler._send_json({"ok": False, "message": msg}, status=400)
            return
        handler._send_json({"ok": True, "aggregate_model": asdict(aggregate)})
        return
    if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/members/batch"):
        parts = parsed.path.split("/")
        if len(parts) != 6:
            handler._send_json({"error": {"message": "请求路径无效", "type": "invalid_request_error", "code": "invalid_path"}}, status=400)
            return
        payload = handler._read_json()
        if not isinstance(payload, dict):
            handler._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
            return
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            handler._send_json({"error": {"message": "连接组不能为空", "type": "invalid_request_error", "code": "missing_group_id"}}, status=400)
            return
        model_ids = payload.get("model_ids")
        if model_ids is not None and (
            not isinstance(model_ids, list)
            or not all(isinstance(model_id, str) and model_id.strip() for model_id in model_ids)
        ):
            handler._send_json({"error": {"message": "模型列表参数无效", "type": "invalid_request_error", "code": "invalid_model_ids"}}, status=400)
            return
        aggregate_id = parts[3]
        batch_result = handler.store.batch_add_aggregate_members(aggregate_id, group_id, model_ids)
        code = str(batch_result.get("code") or "")
        if code in {"aggregate_not_found", "group_not_found"}:
            status = 404
        elif code == "config_save_failed":
            status = 500
        elif not batch_result.get("ok"):
            status = 400
        else:
            status = 200
        handler._send_json(batch_result, status=status)
        return
    if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/members/reorder"):
        parts = parsed.path.split("/")
        if len(parts) != 6:
            handler._send_json({"error": {"message": "请求路径无效", "type": "invalid_request_error", "code": "invalid_path"}}, status=400)
            return
        payload = handler._read_json()
        member_ids = payload.get("member_ids") if isinstance(payload, dict) else None
        expected_revision = payload.get("expected_revision") if isinstance(payload, dict) else None
        if not isinstance(member_ids, list) or not all(isinstance(member_id, str) and member_id.strip() for member_id in member_ids):
            handler._send_json({"error": {"message": "成员排序参数无效", "type": "invalid_request_error", "code": "invalid_member_order"}}, status=400)
            return
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            handler._send_json({"error": {"message": "成员排序版本无效", "type": "invalid_request_error", "code": "invalid_expected_revision"}}, status=400)
            return
        aggregate_id = parts[3]
        ok, message, code, revision = handler.store.reorder_aggregate_members(aggregate_id, member_ids, expected_revision)
        if not ok:
            status = 404 if code == "aggregate_not_found" else (409 if code == "aggregate_member_revision_conflict" else (500 if code == "config_save_failed" else 400))
            error_type = "conflict_error" if status == 409 else "invalid_request_error"
            handler._send_json({"error": {"message": message, "type": error_type, "code": code, "revision": revision}}, status=status)
            return
        members = sorted(handler.store.get_aggregate_members(aggregate_id), key=lambda member: member.priority)
        handler._send_json({"ok": True, "revision": revision, "members": [asdict(member) for member in members]})
        return
    if parsed.path.startswith("/api/aggregates/") and parsed.path.endswith("/members"):
        parts = parsed.path.split("/")
        if len(parts) < 5:
            handler._send_json({"error": {"message": "请求路径无效", "type": "invalid_request_error", "code": "invalid_path"}}, status=400)
            return
        aggregate_id = parts[3]
        payload = handler._read_json()
        if not isinstance(payload, dict):
            handler._send_json({"error": {"message": "请求参数无效", "type": "invalid_request_error", "code": "invalid_payload"}}, status=400)
            return
        if not handler.store.find_aggregate(aggregate_id):
            handler._send_json({"ok": False, "message": "聚合模型不存在"}, status=404)
            return
        member_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex
        existing_member = handler.store.find_aggregate_member(member_id)
        # 更新时允许只传部分字段，group_id/model_id 从已有成员补全
        group_id = str(payload.get("group_id") or (existing_member.group_id if existing_member else "")).strip()
        model_id = str(payload.get("model_id") or (existing_member.model_id if existing_member else "")).strip()
        if not group_id or not model_id:
            handler._send_json({"ok": False, "message": "连接组和模型不能为空"}, status=400)
            return
        member_merged: Dict[str, Any] = asdict(existing_member) if existing_member else {}
        member_merged.update(payload)
        member_merged["id"] = member_id
        member_merged["aggregate_id"] = aggregate_id
        member_merged["group_id"] = group_id
        member_merged["model_id"] = model_id
        member = AggregateMember.from_dict(member_merged)
        ok, msg = handler.store.upsert_aggregate_member(member)
        if not ok:
            handler._send_json({"ok": False, "message": msg}, status=400)
            return
        if bool(payload.get("clear_cooldown")):
            handler.store.clear_aggregate_member_cooldown(member.id, handler.router._now())
        # 支持在同一请求中调整排序（direction: up/down/top/bottom）
        direction = str(payload.get("direction") or "").strip()
        if direction:
            handler.store.move_aggregate_member(member.id, direction)
        handler._send_json({"ok": True, "member": asdict(handler.store.find_aggregate_member(member.id) or member)})
        return
    if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/clear-cooldown"):
        parts = parsed.path.split("/")
        if len(parts) >= 5:
            member_id = parts[3]
            member = handler.store.find_aggregate_member(member_id)
            if not member:
                handler._send_json({"error": {"message": "成员不存在", "type": "invalid_request_error", "code": "aggregate_member_not_found"}}, status=404)
                return
            handler.store.clear_aggregate_member_cooldown(member_id, handler.router._now())
            handler._send_json({"ok": True, "member": asdict(handler.store.find_aggregate_member(member.id) or member)})
            return
    if parsed.path.startswith("/api/aggregate-members/") and parsed.path.endswith("/recover"):
        member_id = parsed.path.split("/")[3]
        payload = handler.router.recover_aggregate_member(member_id)
        handler._send_json(payload, status=200 if payload.get("ok") else 400)
        return
    if parsed.path == "/api/test":
        ctx = handler._require_route_context()
        if not ctx:
            return
        raw = handler._read_raw_body()
        payload = handler._json_from_raw(raw)
        path = str(payload.get("path", "/v1/chat/completions"))
        body = payload.get("body") or {"messages": [{"role": "user", "content": "ping"}]}
        try:
            status, headers, result = handler.router.call(path, body, ctx, dict(handler.headers.items()))
            handler._send_json({"status": status, "headers": headers, "body": result.decode("utf-8", "ignore")})
        except handler._all_models_failed_error_type as err:
            handler._send_all_models_failed_error(err)
        except Exception as err:
            handler._send_json({
                "error": {
                    "message": f"服务器内部错误: {err}",
                    "type": "internal_server_error",
                    "code": "internal_error",
                }
            }, status=500)
        return
    if parsed.path == "/api/debug/replay":
        payload = handler._read_json()
        count = int(payload.get("count", 10)) if isinstance(payload.get("count"), (int, float, str)) else 10
        client_type = str(payload.get("client", "")).lower() or None
        if client_type not in ("urllib", "httpx", None):
            client_type = None
        waf_off_variant = bool(payload.get("waf_off_variant", False))
        results = handler.router.debug_capture.replay(count=count, client_type=client_type, waf_off_variant=waf_off_variant)
        handler._send_json({"ok": True, "count": len(results), "results": results})
        return
    if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):
        ctx = handler._require_route_context()
        if not ctx:
            return
        raw = handler._read_raw_body()
        payload = handler._json_from_raw(raw)
        handle_proxy_request(handler, parsed.path, payload, ctx, raw)
        return
    handler._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)


def _forward_put_as_post(handler: Any, path: str, body: bytes) -> None:
    """Reuse POST handlers without leaking a PUT body into the next request."""
    original_path = handler.path
    handler.path = path
    handler._put_body = body
    try:
        handler.do_POST()
    finally:
        handler.path = original_path
        delattr(handler, "_put_body")


def handle_put(handler: Any) -> None:
    """把 PUT /api/groups/{id}、PUT /api/models/{id} 和 PUT /api/settings 转发到对应的 POST 处理逻辑。"""
    parsed = urlparse(handler.path)
    if parsed.path == "/api/settings":
        # 前端设置面板使用 PUT 保存设置，复用 do_POST 的处理逻辑
        return _forward_put_as_post(handler, handler.path, handler._read_raw_body())
    if parsed.path.startswith("/api/groups/"):
        group_id = parsed.path.split("/")[3]
        payload = handler._read_json()
        payload["id"] = group_id
        return _forward_put_as_post(handler, "/api/groups", json.dumps(payload).encode("utf-8"))
    if parsed.path.startswith("/api/models/"):
        model_id = parsed.path.split("/")[3]
        payload = handler._read_json()
        payload["id"] = model_id
        return _forward_put_as_post(handler, "/api/models", json.dumps(payload).encode("utf-8"))
    if parsed.path.startswith("/api/aggregates/"):
        aggregate_id = parsed.path.split("/")[3]
        payload = handler._read_json()
        payload["id"] = aggregate_id
        return _forward_put_as_post(handler, "/api/aggregates", json.dumps(payload).encode("utf-8"))
    if parsed.path.startswith("/api/aggregate-members/"):
        member_id = parsed.path.split("/")[3]
        payload = handler._read_json()
        payload["id"] = member_id
        # 从已有成员补全 aggregate_id，避免前端漏传
        existing = handler.store.find_aggregate_member(member_id)
        if existing and not payload.get("aggregate_id"):
            payload["aggregate_id"] = existing.aggregate_id
        path = f"/api/aggregates/{payload.get('aggregate_id')}/members"
        return _forward_put_as_post(handler, path, json.dumps(payload).encode("utf-8"))
    handler._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)


def handle_delete(handler: Any) -> None:
    parsed = urlparse(handler.path)
    if parsed.path.startswith("/api/groups/"):
        group_id = parsed.path.split("/")[3]
        # 统一使用 store.remove_group()，确保级联删除组下模型及引用该组的聚合成员
        group_removed, removed_models, removed_members = handler.store.remove_group(group_id)
        if not group_removed:
            handler._send_json({"error": {"message": "连接组不存在", "type": "invalid_request_error", "code": "group_not_found"}}, status=404)
            return
        handler._send_json({"ok": True, "removed_models": removed_models, "removed_members": removed_members})
        return
    if parsed.path.startswith("/api/models/"):
        model_id = parsed.path.split("/")[3]
        if handler.store.remove_model(model_id):
            handler._send_json({"ok": True})
        else:
            handler._send_json({"error": {"message": "模型不存在", "type": "invalid_request_error", "code": "model_not_found"}}, status=404)
        return
    if parsed.path.startswith("/api/aggregates/"):
        aggregate_id = parsed.path.split("/")[3]
        removed_model, removed_members = handler.store.remove_aggregate(aggregate_id)
        if not removed_model:
            handler._send_json({"error": {"message": "聚合模型不存在", "type": "invalid_request_error", "code": "aggregate_not_found"}}, status=404)
            return
        handler._send_json({"ok": True, "removed_members": removed_members})
        return
    if parsed.path.startswith("/api/aggregate-members/"):
        member_id = parsed.path.split("/")[3]
        if handler.store.remove_aggregate_member(member_id):
            handler._send_json({"ok": True})
        else:
            handler._send_json({"error": {"message": "聚合成员不存在", "type": "invalid_request_error", "code": "aggregate_member_not_found"}}, status=404)
        return
    handler._send_json({"error": {"message": "资源不存在", "type": "invalid_request_error", "code": "not_found"}}, status=404)
