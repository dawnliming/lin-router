from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
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
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from settings_store import SettingsStore


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


def _set_windows_auto_start(enabled: bool) -> bool:
    """Windows 下设置当前用户开机自启（HKCU Run）。打包为 exe 时生效，开发模式跳过。"""
    if not getattr(sys, "frozen", False):
        # 开发模式下不写注册表，避免把 python.exe 写进去
        return True
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        exe = Path(sys.executable).resolve()
        command = f'"{exe}" --tray'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
            if enabled:
                winreg.SetValueEx(key, "LinRouter", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "LinRouter")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def _get_windows_auto_start() -> bool:
    """读取 Windows 当前用户开机自启是否启用。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, "LinRouter")
            return True
    except (FileNotFoundError, OSError):
        return False


DEFAULT_CONFIG_FILE = "lin-router-config.json"
DEFAULT_START_PORT = 18400
DEFAULT_AUTO_MODEL_NAME = "lin-router-auto"
DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES = 5
DEFAULT_PUBLIC_API_KEY = "lin-router"
GLOBAL_ROUTE_GROUP_ID = "__global__"
PROVIDER_ARK = "ark"
PROVIDER_RELAY = "relay"
PROVIDER_PROXY = "proxy"
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


def resource_path(*parts: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS).joinpath(*parts)
        if bundled.exists():
            return bundled
        return Path(sys.executable).resolve().parent.joinpath(*parts)
    return Path(__file__).resolve().parent.joinpath(*parts)


def render_index_page() -> str:
    page_path = resource_path("static", "index.html")
    html = page_path.read_text(encoding="utf-8")
    return html.replace("__AUTO_MODEL_NAME__", DEFAULT_AUTO_MODEL_NAME)


def new_route_key() -> str:
    return f"lr-{uuid.uuid4().hex[:16]}"


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def build_upstream_headers(api_key: str, *, stream: bool) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }


def build_waf_compatible_headers(incoming_headers: Dict[str, str], upstream_host: str, *, stream: bool) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in incoming_headers.items():
        lower = name.strip().lower()
        if not lower or lower in WAF_STRIP_EXACT or any(lower.startswith(prefix) for prefix in WAF_STRIP_PREFIXES):
            continue
        headers[name] = value
    headers["host"] = upstream_host
    headers["user-agent"] = BROWSER_UA
    if not any(k.lower() == "accept" for k in headers):
        headers["accept"] = "application/json, text/event-stream, */*"
    if not any(k.lower() == "accept-language" for k in headers):
        headers["accept-language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    return headers


def build_passthrough_headers(api_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for name, value in incoming_headers.items():
        lower = name.strip().lower()
        if not lower or lower in PASSTHROUGH_STRIP_EXACT:
            continue
        headers[name] = value
    headers["Authorization"] = f"Bearer {api_key}"
    headers["Content-Type"] = headers.get("Content-Type") or headers.get("content-type") or "application/json"
    if stream and not any(key.lower() == "accept" for key in headers):
        headers["Accept"] = "text/event-stream"
    elif not stream and not any(key.lower() == "accept" for key in headers):
        headers["Accept"] = "application/json"
    return headers


def build_model_fetch_headers(auth_key: str) -> Dict[str, str]:
    return {
        "authorization": f"Bearer {auth_key}",
        "user-agent": BROWSER_UA,
        "accept": "application/json, text/event-stream, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json",
    }


def can_forward_header(name: str) -> bool:
    normalized = name.strip().lower()
    return bool(normalized) and normalized not in BLOCKED_FORWARD_HEADERS and not normalized.startswith("x-stainless-")


def parse_bearer_key(auth_header: str) -> str:
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


@dataclass
class ConnectionGroup:
    id: str
    name: str
    provider_type: str = PROVIDER_ARK
    base_url: str = DEFAULT_BASE_URL
    ark_api_key: str = ""
    api_key: str = ""
    route_key: str = ""
    auto_model_cooldown_minutes: int = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
    waf_compatible: bool = False
    upstream_models: List[Dict[str, Any]] = field(default_factory=list)
    upstream_models_fetched_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionGroup":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            provider_type=str(data.get("provider_type") or PROVIDER_ARK),
            base_url=data.get("base_url") or DEFAULT_BASE_URL,
            ark_api_key=data.get("ark_api_key") or "",
            api_key=str(data.get("api_key") or ""),
            route_key=str(data.get("route_key") or ""),
            auto_model_cooldown_minutes=int(data.get("auto_model_cooldown_minutes") or DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES),
            waf_compatible=bool(data.get("waf_compatible", False)),
            upstream_models=[item for item in data.get("upstream_models", []) if isinstance(item, dict)] if isinstance(data.get("upstream_models", []), list) else [],
            upstream_models_fetched_at=str(data.get("upstream_models_fetched_at") or ""),
        )


@dataclass
class ModelConfig:
    id: str
    name: str
    ep_id: str
    group_id: str
    upstream_model: str = ""
    api_key: str = ""
    price_group: str = ""
    usable: bool = True
    last_error: str = ""
    last_success_at: str = ""
    last_checked_at: str = ""
    cooldown_until: int = 0
    cooldown_reason: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            ep_id=data["ep_id"],
            group_id=str(data.get("group_id") or ""),
            upstream_model=str(data.get("upstream_model") or ""),
            api_key=str(data.get("api_key") or ""),
            price_group=str(data.get("price_group") or ""),
            usable=bool(data.get("usable", True)),
            last_error=str(data.get("last_error", "")),
            last_success_at=str(data.get("last_success_at", "")),
            last_checked_at=str(data.get("last_checked_at", "")),
            cooldown_until=int(data.get("cooldown_until") or 0),
            cooldown_reason=str(data.get("cooldown_reason", "")),
        )


@dataclass
class RequestLog:
    time: str
    path: str
    model: str
    status: str
    detail: str = ""
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    group_id: str = ""
    group_name: str = ""
    provider_type: str = ""
    event: str = ""
    request_id: str = ""
    attempt: int = 0


@dataclass
class UpstreamCandidate:
    idx: Optional[int]
    group: ConnectionGroup
    model: Optional[ModelConfig]
    label: str
    target_model: str
    auth_key: str
    channel: str = ""


@dataclass
class RouteContext:
    client_key: str
    group: ConnectionGroup
    group_id: str
    provider_type: str
    base_url: str
    display_name: str
    # passthrough 标识：proxy/ark 模式为 True，请求体与头部尽量原样转发；
    # relay 模式为 False，允许 WAF 兼容、冷却、自动调度等专属逻辑介入。
    passthrough: bool = True
    # 全局 Key 标识：使用 DEFAULT_PUBLIC_API_KEY 时跨所有组调度
    is_global: bool = False


class ConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.groups: List[ConnectionGroup] = []
        self.models: List[ModelConfig] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.groups = []
            self.models = []
            self.save()
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            self.groups = []
            self.models = []
            return

        if not isinstance(raw, dict):
            self.groups = []
            self.models = []
            return

        groups_raw = raw.get("groups", [])
        models_raw = raw.get("models", [])
        if isinstance(groups_raw, list):
            self.groups = [ConnectionGroup.from_dict(x) for x in groups_raw if isinstance(x, dict)]
        else:
            self.groups = []
        changed = False
        for group in self.groups:
            if not group.route_key:
                group.route_key = new_route_key()
                changed = True
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
                changed = True

        if not isinstance(models_raw, list):
            if changed:
                self.save()
            self.models = []
            return

        legacy_models = [ModelConfig.from_dict(x) for x in models_raw if isinstance(x, dict)]
        if not self.groups and legacy_models:
            group_map: Dict[tuple[str, str], ConnectionGroup] = {}
            migrated_models: List[ModelConfig] = []
            for model in legacy_models:
                legacy = next((x for x in models_raw if isinstance(x, dict) and x.get("id") == model.id), {})
                base_url = str(legacy.get("base_url") or DEFAULT_BASE_URL)
                api_key = str(legacy.get("ark_api_key") or "")
                key = (base_url, api_key)
                group = group_map.get(key)
                if group is None:
                    group = ConnectionGroup(
                        id=uuid.uuid4().hex,
                        name=f"{base_url} · {len(group_map) + 1}",
                        base_url=base_url,
                        provider_type=PROVIDER_ARK,
                        ark_api_key=api_key,
                        route_key=new_route_key(),
                    )
                    group_map[key] = group
                model.group_id = group.id
                migrated_models.append(model)
            self.groups = list(group_map.values())
            self.models = migrated_models
            self.save()
            return

        self.models = legacy_models
        if self.groups:
            group_ids = {g.id for g in self.groups}
            for model in self.models:
                if not model.group_id or model.group_id not in group_ids:
                    model.group_id = self.groups[0].id
                    changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            payload = {
                "groups": [asdict(g) for g in self.groups],
                "models": [asdict(m) for m in self.models],
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def refresh_expired_cooldowns(self) -> bool:
        with self._lock:
            now = int(time.time())
            changed = False
            for model in self.models:
                if model.cooldown_until and model.cooldown_until <= now:
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    model.usable = True
                    model.last_error = ""
                    model.last_checked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                    changed = True
            if changed:
                self.save()
            return changed

    def upsert_group(self, group: ConnectionGroup) -> None:
        with self._lock:
            if not group.route_key:
                existing = self.find_group(group.id)
                group.route_key = existing.route_key if existing and existing.route_key else new_route_key()
            if not group.provider_type:
                group.provider_type = PROVIDER_ARK
            for idx, item in enumerate(self.groups):
                if item.id == group.id:
                    self.groups[idx] = group
                    self.save()
                    return
            self.groups.append(group)
            self.save()

    def upsert_model(self, model: ModelConfig) -> None:
        with self._lock:
            for idx, item in enumerate(self.models):
                if item.id == model.id:
                    self.models[idx] = model
                    self.save()
                    return
            self.models.append(model)
            self.save()

    def remove_model(self, model_id: str) -> bool:
        with self._lock:
            before = len(self.models)
            self.models = [m for m in self.models if m.id != model_id]
            changed = len(self.models) != before
            if changed:
                self.save()
            return changed

    def move_model(self, model_id: str, direction: str) -> bool:
        with self._lock:
            idx = next((i for i, m in enumerate(self.models) if m.id == model_id), -1)
            if idx < 0:
                return False
            group_id = self.models[idx].group_id
            group_positions = [i for i, model in enumerate(self.models) if model.group_id == group_id]
            local_idx = next((i for i, pos in enumerate(group_positions) if pos == idx), -1)
            if local_idx < 0:
                return False
            new_local_idx = local_idx - 1 if direction == "up" else local_idx + 1
            if new_local_idx < 0 or new_local_idx >= len(group_positions):
                return False
            group_models = [self.models[pos] for pos in group_positions]
            group_models[local_idx], group_models[new_local_idx] = group_models[new_local_idx], group_models[local_idx]
            for pos, model in zip(group_positions, group_models):
                self.models[pos] = model
            self.save()
            return True

    def reset_usable(self) -> None:
        with self._lock:
            changed = False
            for model in self.models:
                if not model.usable or model.last_error:
                    model.usable = True
                    model.last_error = ""
                    model.cooldown_until = 0
                    model.cooldown_reason = ""
                    changed = True
            if changed:
                self.save()

    def find_group(self, group_id: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.id == group_id), None)

    def find_group_by_route_key(self, route_key: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.route_key == route_key), None)

    def find_model(self, model_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.id == model_id), None)

    def find_model_by_group_ep(self, group_id: str, ep_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.group_id == group_id and m.ep_id == ep_id), None)


class AllModelsFailedError(RuntimeError):
    """所有候选模型均不可用或均失败时抛出，便于外层返回 503。"""
    def __init__(self, message: str, attempted: int = 0) -> None:
        super().__init__(message)
        self.attempted = attempted


class ArkProxyRouter:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self.logs: List[RequestLog] = []
        self.log_file = self._resolve_log_file()
        self.log_write_error = ""
        self.upstream_locks: Dict[str, threading.Lock] = {}
        self.upstream_locks_guard = threading.Lock()
        self._load_log_file()

    def _resolve_log_file(self) -> Path:
        candidates = [
            self.store.path.parent / "lin-router-logs.jsonl",
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
        group: Optional[ConnectionGroup] = None,
        event: str = "",
        request_id: str = "",
        attempt: int = 0,
    ) -> None:
        detail = self._sanitize_detail(detail)
        group_id = group.id if group else self._detail_value(detail, "group_id")
        group_name = group.name if group else self._detail_value(detail, "group_name")
        provider_type = group.provider_type if group else self._detail_value(detail, "provider")
        item = RequestLog(
            self._now(),
            path,
            model,
            status,
            detail[:5000],
            duration_ms,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_tokens,
            group_id,
            group_name,
            provider_type,
            event or self._infer_event(status, detail),
            request_id,
            attempt,
        )
        self.logs.insert(0, item)
        self._append_log_file(item)
        del self.logs[80:]
        self._trim_log_file()

    def _trim_log_file(self, max_lines: int = 1000) -> None:
        # 日志文件滚动策略：保留最近 1000 条，避免磁盘无限增长
        try:
            if not self.log_file.exists():
                return
            with self.log_file.open("r", encoding="utf-8") as f:
                lines = [line for line in f if line.strip()]
            if len(lines) <= max_lines:
                return
            with self.log_file.open("w", encoding="utf-8") as f:
                f.writelines(lines[-max_lines:])
        except Exception as exc:
            self.log_write_error = f"日志滚动失败: {exc}"

    @staticmethod
    def _detail_value(detail: str, key: str) -> str:
        match = re.search(rf"(?:^|; ){re.escape(key)}=([^;]*)", detail or "")
        return match.group(1).strip() if match else ""

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
        return RequestLog(
            time=str(row.get("time") or ""),
            path=str(row.get("path") or ""),
            model=str(row.get("model") or ""),
            status=str(row.get("status") or ""),
            detail=str(row.get("detail") or ""),
            duration_ms=int(row.get("duration_ms") or 0),
            prompt_tokens=int(row.get("prompt_tokens") or 0),
            completion_tokens=int(row.get("completion_tokens") or 0),
            total_tokens=int(row.get("total_tokens") or 0),
            cached_tokens=int(row.get("cached_tokens") or 0),
            group_id=str(row.get("group_id") or ""),
            group_name=str(row.get("group_name") or ""),
            provider_type=str(row.get("provider_type") or ""),
            event=str(row.get("event") or ""),
            request_id=str(row.get("request_id") or ""),
            attempt=int(row.get("attempt") or 0),
        )

    def _load_log_file(self) -> None:
        try:
            if not self.log_file.exists():
                return
            with self.log_file.open("r", encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            items: List[RequestLog] = []
            for row in rows[-80:]:
                if isinstance(row, dict):
                    row.setdefault("time", row.get("time") or self._now())
                    items.append(self._log_from_row(row))
            if items:
                self.logs = items
        except Exception as exc:
            self.log_write_error = f"日志加载失败: {exc}"
            return

    def _append_log_file(self, item: RequestLog) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
        except Exception as exc:
            self.log_write_error = f"日志写入失败: {exc}"
            # 在内存日志中插入一条系统级告警，方便用户从日志列表也能发现
            self.logs.insert(0, RequestLog(
                time=self._now(),
                path="/system/log",
                model="-",
                status="warn",
                detail=f"日志写入失败: {exc}; file={self.log_file}",
                duration_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cached_tokens=0,
                group_id="",
                group_name="系统",
                provider_type="system",
                event="system",
                request_id="",
                attempt=0,
            ))
            del self.logs[80:]

    def recent_logs(self) -> List[Dict[str, str]]:
        return [asdict(item) for item in self.logs[:30]]

    def all_logs(self) -> List[RequestLog]:
        items: List[RequestLog] = []
        try:
            if self.log_file.exists():
                with self.log_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            continue
                        items.append(self._log_from_row(row))
        except Exception:
            items = []
        return items or list(reversed(self.logs))

    def clear_logs(self) -> None:
        self.logs.clear()
        try:
            if self.log_file.exists():
                self.log_file.unlink()
        except Exception:
            return

    def update_latest_stream_usage(self, path: str, model: str, usage: Tuple[int, int, int, int]) -> None:
        prompt_tokens, completion_tokens, total_tokens, cached_tokens = usage
        if not any(usage):
            return
        for item in self.logs:
            if item.path == path and item.model == model and item.status == "200":
                item.prompt_tokens = prompt_tokens
                item.completion_tokens = completion_tokens
                item.total_tokens = total_tokens
                item.cached_tokens = cached_tokens
                break

    def export_logs_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time", "path", "request_id", "attempt", "group_name", "provider_type", "model", "status", "event", "duration_ms", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "detail"])
        for item in self.all_logs():
            writer.writerow([
                item.time,
                item.path,
                item.request_id,
                item.attempt,
                item.group_name,
                item.provider_type,
                item.model,
                item.status,
                item.event,
                item.duration_ms,
                item.prompt_tokens,
                item.completion_tokens,
                item.total_tokens,
                item.cached_tokens,
                item.detail,
            ])
        return output.getvalue()

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
    def _resolve_url(base_url: str, path: str) -> str:
        base = base_url.rstrip("/")
        suffix = path.lstrip("/")
        if suffix.startswith("v1/"):
            suffix = suffix[3:]
        return f"{base}/{suffix}"

    @staticmethod
    def _usage_from_response(data: bytes) -> Tuple[int, int, int, int]:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return 0, 0, 0, 0
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return 0, 0, 0, 0
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        cached_tokens = int(prompt_details.get("cached_tokens") or 0)
        return prompt_tokens, completion_tokens, total_tokens, cached_tokens

    @staticmethod
    def _usage_from_stream_chunk(chunk: bytes) -> Tuple[int, int, int, int]:
        text = chunk.decode("utf-8", "ignore").strip()
        if not text.startswith("data:"):
            return 0, 0, 0, 0
        data = text[5:].strip()
        if not data or data == "[DONE]":
            return 0, 0, 0, 0
        try:
            payload = json.loads(data)
        except Exception:
            return 0, 0, 0, 0
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return 0, 0, 0, 0
        return ArkProxyRouter._usage_from_response(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def default_model(self) -> Optional[ModelConfig]:
        return next((m for m in self.store.models if m.usable), None)

    @staticmethod
    def group_auto_model_name(group: ConnectionGroup) -> str:
        return DEFAULT_AUTO_MODEL_NAME

    @staticmethod
    def _is_auto_model(requested_model: str | None) -> bool:
        return not requested_model or requested_model == DEFAULT_AUTO_MODEL_NAME

    def _iter_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[Tuple[int, ModelConfig]]:
        if self._is_auto_model(requested_model):
            requested_model = None
        for idx, model in enumerate(self.store.models):
            if model.cooldown_until and model.cooldown_until <= int(time.time()):
                model.cooldown_until = 0
                model.cooldown_reason = ""
                model.usable = True
                model.last_error = ""
                model.last_checked_at = self._now()
                self.store.save()
            if not model.usable:
                continue
            if group_id and model.group_id != group_id:
                continue
            if requested_model and requested_model not in {model.id, model.name, model.ep_id}:
                continue
            yield idx, model

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
        return f"mode={mode}{waf}; hit={candidate.target_model}{channel}; requested={requested_label}; {suffix}"

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
    def _cache_prefix_fingerprint(payload: Dict[str, Any], body: bytes) -> str:
        parts = [
            f"body_4k={hashlib.sha256(body[:4096]).hexdigest()[:16]}",
            f"body_16k={hashlib.sha256(body[:16384]).hexdigest()[:16]}",
            f"body_64k={hashlib.sha256(body[:65536]).hexdigest()[:16]}",
        ]
        messages = payload.get("messages")
        if isinstance(messages, list):
            normalized_messages = ArkProxyRouter._normalize_for_cache(messages)
            for count in (1, 4, 16, 32, 64):
                if len(normalized_messages) >= count:
                    parts.append(f"msg_{count}={ArkProxyRouter._hash_json(normalized_messages[:count])}")
            parts.append(f"msg_all={ArkProxyRouter._hash_json(normalized_messages)}")
        tools = payload.get("tools")
        if isinstance(tools, list):
            normalized_tools = ArkProxyRouter._normalize_for_cache(tools)
            parts.append(f"tools_hash={ArkProxyRouter._hash_json(normalized_tools)}")
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
                text = " ".join(str(value).split())
                if lower == "user-agent" and len(text) > 72:
                    text = text[:72] + "..."
                items.append(f"{lower}={text}")
            elif lower.startswith("x-"):
                x_headers.append(lower)
        if x_headers:
            items.append("x-headers=" + ",".join(sorted(set(x_headers))))
        return "; ".join(items) if items else "headers=none"

    @staticmethod
    def _payload_fingerprint(payload: Dict[str, Any], body: bytes) -> str:
        keys = [
            "model",
            "stream",
            "temperature",
            "top_p",
            "max_tokens",
            "max_completion_tokens",
            "reasoning_effort",
            "service_tier",
            "tool_choice",
            "parallel_tool_calls",
            "store",
        ]
        parts: List[str] = []
        for key in keys:
            if key in payload:
                parts.append(f"{key}={payload.get(key)!r}")
        messages = payload.get("messages")
        if isinstance(messages, list):
            roles: List[str] = []
            content_chars = 0
            for message in messages:
                if isinstance(message, dict):
                    roles.append(str(message.get("role") or "?"))
                    try:
                        content_chars += len(json.dumps(message.get("content"), ensure_ascii=False, separators=(",", ":")))
                    except Exception:
                        content_chars += len(str(message.get("content")))
            parts.append(f"messages={len(messages)}")
            parts.append("roles=" + ",".join(roles[:12]))
            parts.append(f"content_chars={content_chars}")
        for key in ("tools", "functions"):
            value = payload.get(key)
            if isinstance(value, list):
                names: List[str] = []
                for item in value[:12]:
                    if not isinstance(item, dict):
                        continue
                    fn = item.get("function") if isinstance(item.get("function"), dict) else {}
                    names.append(str(fn.get("name") or item.get("name") or item.get("type") or "?"))
                parts.append(f"{key}={len(value)}:{','.join(names)}")
        stream_options = payload.get("stream_options")
        if isinstance(stream_options, dict):
            parts.append("stream_options=" + ",".join(f"{k}={stream_options[k]!r}" for k in sorted(stream_options)))
        parts.append(f"body_len={len(body)}")
        parts.append(f"body_sha256={ArkProxyRouter._body_sha256(body)}")
        parts.append(f"normalized_sha256={ArkProxyRouter._normalized_body_sha256(payload)}")
        parts.append(f"prefix=({ArkProxyRouter._cache_prefix_fingerprint(payload, body)})")
        return "; ".join(parts)

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
    ) -> str:
        base = self._candidate_hit_detail(candidate, requested_label, suffix)
        group_name = str(candidate.group.name).replace(";", ",")
        # mode=passthrough 表示该请求走透传路径（ark/proxy），relay 专属逻辑不介入
        mode_tag = "passthrough" if candidate.group.provider_type != PROVIDER_RELAY else "relay"
        return (
            f"{base}; group_id={candidate.group.id}; group_name={group_name}; provider={candidate.group.provider_type}; mode={mode_tag}; "
            f"upstream={target_url}; body={body_mode}; "
            f"fingerprint=({self._payload_fingerprint(payload, body)}); "
            f"out_headers=({self._safe_header_view(headers)})"
        )

    @staticmethod
    def _short_error(raw: str, limit: int = 900) -> str:
        text = " ".join(str(raw or "").split())
        return text[:limit]

    def _auth_for(self, group: ConnectionGroup, model: Optional[ModelConfig]) -> str:
        mode = self._mode_for(group)
        if mode == PROVIDER_RELAY:
            return model.api_key if model else ""
        if mode == PROVIDER_PROXY:
            return group.api_key or group.ark_api_key
        return group.ark_api_key

    def _headers_for(self, group: ConnectionGroup, auth_key: str, incoming_headers: Dict[str, str], *, stream: bool) -> Dict[str, str]:
        if group.provider_type == PROVIDER_RELAY and group.waf_compatible:
            upstream_host = urlparse(group.base_url).netloc
            headers = build_waf_compatible_headers(incoming_headers, upstream_host, stream=stream)
            headers["authorization"] = f"Bearer {auth_key}"
            if not any(key.lower() == "content-type" for key in headers):
                headers["content-type"] = "application/json"
            return headers
        if incoming_headers:
            return build_passthrough_headers(auth_key, incoming_headers, stream=stream)
        return build_upstream_headers(auth_key, stream=stream)

    def _candidate_from_model(self, idx: int, model: ModelConfig, group: ConnectionGroup) -> UpstreamCandidate:
        mode = self._mode_for(group)
        channel = ""
        if mode == PROVIDER_RELAY and model.price_group:
            channel = model.price_group
        elif mode == PROVIDER_PROXY:
            channel = "proxy"
        return UpstreamCandidate(
            idx=idx,
            group=group,
            model=model,
            label=model.name,
            target_model=model.ep_id,
            auth_key=self._auth_for(group, model),
            channel=channel,
        )

    def _candidate_lock(self, candidate: UpstreamCandidate) -> Optional[threading.Lock]:
        if candidate.group.provider_type != PROVIDER_RELAY or not candidate.group.waf_compatible:
            return None
        key = f"{candidate.group.id}:{candidate.target_model}:{candidate.channel}"
        with self.upstream_locks_guard:
            lock = self.upstream_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self.upstream_locks[key] = lock
            return lock

    @staticmethod
    def _release_lock(lock: Optional[threading.Lock]) -> None:
        if lock:
            lock.release()

    def _iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[UpstreamCandidate]:
        # 全局 Key：跨所有组按 models 数组顺序调度
        if group_id == GLOBAL_ROUTE_GROUP_ID:
            for idx, model in self._iter_candidates(requested_model, None):
                group = self._group_for(model)
                if group:
                    yield self._candidate_from_model(idx, model, group)
            return
        if group_id:
            group = self.store.find_group(group_id)
            if not group:
                return
            matched = False
            candidates = list(self._iter_candidates(requested_model, group.id))
            for idx, model in candidates:
                matched = True
                yield self._candidate_from_model(idx, model, group)
            if self._mode_for(group) == PROVIDER_PROXY and not matched and requested_model and not self._is_auto_model(requested_model):
                yield UpstreamCandidate(
                    idx=None,
                    group=group,
                    model=None,
                    label=requested_model,
                    target_model=requested_model,
                    auth_key=self._auth_for(group, None),
                    channel="pass-through",
                )
            return

        for idx, model in self._iter_candidates(requested_model, None):
            group = self._group_for(model)
            if group:
                yield self._candidate_from_model(idx, model, group)

    def _set_unusable(self, idx: int, error: str) -> None:
        model = self.store.models[idx]
        model.usable = False
        model.last_error = error[:500]
        model.last_checked_at = self._now()
        model.cooldown_until = 0
        model.cooldown_reason = ""
        self.store.save()

    def _set_cooldown(self, idx: int, error: str, cooldown_seconds: int, reason: str) -> None:
        model = self.store.models[idx]
        now_ts = int(time.time())
        model.usable = False
        model.last_error = error[:500]
        model.last_checked_at = self._now()
        model.cooldown_until = now_ts + max(0, cooldown_seconds)
        model.cooldown_reason = reason[:120]
        self.store.save()

    def _set_success(self, idx: int) -> None:
        model = self.store.models[idx]
        model.last_error = ""
        model.last_success_at = self._now()
        model.last_checked_at = model.last_success_at
        self.store.save()

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
        if not group:
            return DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES * 60
        try:
            minutes = int(group.auto_model_cooldown_minutes)
        except Exception:
            minutes = DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES
        return max(0, minutes) * 60

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
        self.store.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = self._route_group_id(route)
        route_group = route.group if isinstance(route, RouteContext) else self.store.find_group(group_id) if group_id else None
        auto_mode = self._is_auto_model(str(requested_model) if requested_model else None)
        # auto_fallback：组级 auto 或全局 Key 模式下，失败时尝试下一个候选
        auto_fallback = auto_mode or (isinstance(route, RouteContext) and route.is_global)
        request_id = uuid.uuid4().hex[:12]
        attempt = 0
        last_error: Optional[Exception] = None

        for candidate in self._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id):
            attempt += 1
            group = candidate.group
            target_url = self._resolve_url(group.base_url, path)
            if not candidate.auth_key:
                self.add_log(path, candidate.label, "skip", f"requested={requested_label}; missing upstream api key", group=group, request_id=request_id, attempt=attempt, event="skip")
                continue
            body, body_mode = self._body_for_upstream(payload, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = self._headers_for(group, candidate.auth_key, incoming_headers, stream=False)
            upstream_lock = self._candidate_lock(candidate)
            if upstream_lock:
                upstream_lock.acquire()
            request = Request(
                target_url,
                data=body,
                headers=outbound_headers,
                method="POST",
            )
            started_at = time.perf_counter()
            try:
                with urlopen(request, timeout=120) as resp:
                    data = resp.read()
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    prompt_tokens, completion_tokens, total_tokens, cached_tokens = self._usage_from_response(data)
                    self._mark_success(candidate)
                    self.add_log(
                        path,
                        candidate.label,
                        str(resp.status),
                        self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "ok"),
                        duration_ms,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cached_tokens,
                        group=group,
                        event="ok",
                    )
                    return resp.status, dict(resp.headers.items()), data
            except HTTPError as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                if auto_fallback and candidate.group.provider_type == PROVIDER_RELAY:
                    cooldown_seconds = self._auto_cooldown_seconds(group)
                    self._set_cooldown(candidate.idx, raw or str(err), cooldown_seconds, f"http_{err.code}")
                    detail = f"cooldown {cooldown_seconds // 60 or 0}m, try next; error={self._short_error(raw)}"
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown")
                    continue
                if self._is_quota_exhausted(err.code, raw):
                    self._mark_unusable(candidate, raw)
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "quota exhausted, try next"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback")
                    continue
                if self._is_rate_limited(err.code, raw):
                    try:
                        retry_started_at = time.perf_counter()
                        with urlopen(request, timeout=120) as resp:
                            data = resp.read()
                            retry_duration_ms = int((time.perf_counter() - retry_started_at) * 1000)
                            prompt_tokens, completion_tokens, total_tokens, cached_tokens = self._usage_from_response(data)
                            self._mark_success(candidate)
                            self.add_log(
                                path,
                                candidate.label,
                                str(resp.status),
                                self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "retry ok"),
                                retry_duration_ms,
                                prompt_tokens,
                                completion_tokens,
                                total_tokens,
                                cached_tokens,
                                group=group,
                                event="retry_ok",
                            )
                            return resp.status, dict(resp.headers.items()), data
                    except Exception as retry_err:
                        last_error = retry_err
                        retry_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        self.add_log(path, candidate.label, "retry failed", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, str(retry_err)), retry_duration_ms, group=group, request_id=request_id, attempt=attempt, event="error")
                        continue
                if self._is_server_error(err.code):
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "server error, try next"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={self._short_error(raw)}"
                self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error")
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                if auto_fallback and candidate.group.provider_type == PROVIDER_RELAY:
                    cooldown_seconds = self._auto_cooldown_seconds(group)
                    self._set_cooldown(candidate.idx, str(err), cooldown_seconds, "network")
                    detail = f"cooldown {cooldown_seconds // 60 or 0}m, try next; error={self._short_error(str(err))}"
                    self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network")
                    continue
                detail = f"error={self._short_error(str(err))}"
                self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network")
                continue
            finally:
                self._release_lock(upstream_lock)

        if last_error is None:
            raise AllModelsFailedError("No usable models available", attempted=attempt - 1)
        raise AllModelsFailedError(f"All available models failed, attempted {attempt - 1}", attempted=attempt - 1) from last_error

    def stream(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None, incoming_headers: Optional[Dict[str, str]] = None, raw_body: bytes | None = None) -> Tuple[int, Dict[str, str], Iterable[bytes]]:
        self.store.refresh_expired_cooldowns()
        incoming_headers = incoming_headers or {}
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = self._route_group_id(route)
        route_group = route.group if isinstance(route, RouteContext) else self.store.find_group(group_id) if group_id else None
        auto_mode = self._is_auto_model(str(requested_model) if requested_model else None)
        # auto_fallback：组级 auto 或全局 Key 模式下，失败时尝试下一个候选
        auto_fallback = auto_mode or (isinstance(route, RouteContext) and route.is_global)
        request_id = uuid.uuid4().hex[:12]
        attempt = 0
        last_error: Optional[Exception] = None

        for candidate in self._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id):
            attempt += 1
            group = candidate.group
            target_url = self._resolve_url(group.base_url, path)
            if not candidate.auth_key:
                self.add_log(path, candidate.label, "skip", f"requested={requested_label}; missing upstream api key", group=group, request_id=request_id, attempt=attempt, event="skip")
                continue
            body, body_mode = self._body_for_upstream(payload, raw_body, str(requested_model) if requested_model else None, candidate.target_model)
            outbound_headers = self._headers_for(group, candidate.auth_key, incoming_headers, stream=True)
            upstream_lock = self._candidate_lock(candidate)
            if upstream_lock:
                upstream_lock.acquire()
            request = Request(
                target_url,
                data=body,
                headers=outbound_headers,
                method="POST",
            )
            started_at = time.perf_counter()
            try:
                resp = urlopen(request, timeout=120)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self._mark_success(candidate)
                self.add_log(path, candidate.label, "200", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "stream ok"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="stream_ok")

                def iterator() -> Iterator[bytes]:
                    latest_usage = (0, 0, 0, 0)
                    try:
                        while True:
                            chunk = resp.readline()
                            if not chunk:
                                break
                            usage = self._usage_from_stream_chunk(chunk)
                            if any(usage):
                                latest_usage = usage
                            yield chunk
                    finally:
                        resp.close()
                        self.update_latest_stream_usage(path, candidate.label, latest_usage)
                        self._release_lock(upstream_lock)

                return 200, dict(resp.headers.items()), iterator()
            except HTTPError as err:
                self._release_lock(upstream_lock)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                if auto_fallback and candidate.group.provider_type == PROVIDER_RELAY:
                    cooldown_seconds = self._auto_cooldown_seconds(group)
                    self._set_cooldown(candidate.idx, raw or str(err), cooldown_seconds, f"http_{err.code}")
                    detail = f"cooldown {cooldown_seconds // 60 or 0}m, try next; error={self._short_error(raw)}"
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="cooldown")
                    continue
                if self._is_quota_exhausted(err.code, raw):
                    self._mark_unusable(candidate, raw)
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "quota exhausted, try next"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback")
                    continue
                if self._is_rate_limited(err.code, raw):
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "rate limited, try next"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback")
                    continue
                if self._is_server_error(err.code):
                    self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, "server error, try next"), duration_ms, group=group, request_id=request_id, attempt=attempt, event="fallback")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                detail = f"error={self._short_error(raw)}"
                self.add_log(path, candidate.label, str(err.code), self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="error")
                return err.code, headers, [raw.encode("utf-8")]
            except (URLError, TimeoutError, OSError) as err:
                self._release_lock(upstream_lock)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                if auto_fallback and candidate.group.provider_type == PROVIDER_RELAY:
                    cooldown_seconds = self._auto_cooldown_seconds(group)
                    self._set_cooldown(candidate.idx, str(err), cooldown_seconds, "network")
                    detail = f"cooldown {cooldown_seconds // 60 or 0}m, try next; error={self._short_error(str(err))}"
                    self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network")
                    continue
                detail = f"error={self._short_error(str(err))}"
                self.add_log(path, candidate.label, "network", self._debug_detail(candidate, requested_label, target_url, body_mode, body, payload, outbound_headers, detail), duration_ms, group=group, request_id=request_id, attempt=attempt, event="network")
                continue

        if last_error is None:
            raise AllModelsFailedError("No usable models available", attempted=attempt - 1)
        raise AllModelsFailedError(f"All available models failed, attempted {attempt - 1}", attempted=attempt - 1) from last_error






class RouterHandler(BaseHTTPRequestHandler):
    server_version = "LinRouter/2.0"

    @property
    def store(self) -> ConfigStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def router(self) -> ArkProxyRouter:
        return self.server.router  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        raw = self._read_raw_body()
        return json.loads(raw.decode("utf-8"))

    def _read_raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b"{}"

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
        target_url = self.router._resolve_url(group.base_url, "/v1/models")
        headers = build_model_fetch_headers(auth_key)
        request = Request(
            target_url,
            headers=headers,
            method="GET",
        )
        started_at = time.perf_counter()
        try:
            with urlopen(request, timeout=60) as resp:
                raw = resp.read()
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self.router.add_log(
                    "/v1/models",
                    group.name,
                    str(resp.status),
                    f"fetch upstream models ok; upstream={target_url}; out_headers=({self.router._safe_header_view(headers)})",
                    duration_ms,
                    group=group,
                    event="fetch_models",
                )
        except HTTPError as err:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            body = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
            self.router.add_log(
                "/v1/models",
                group.name,
                str(err.code),
                f"fetch upstream models failed; upstream={target_url}; error={self.router._short_error(body)}; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models_failed",
            )
            raise RuntimeError(body or f"upstream error {err.code}") from err
        except Exception as err:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            self.router.add_log(
                "/v1/models",
                group.name,
                "network",
                f"fetch upstream models failed; upstream={target_url}; error={self.router._short_error(str(err))}; out_headers=({self.router._safe_header_view(headers)})",
                duration_ms,
                group=group,
                event="fetch_models_failed",
            )
            raise
        payload = json.loads(raw.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("Invalid upstream model list")
        return [item for item in data if isinstance(item, dict)]

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
            waf_compatible=source.waf_compatible,
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
        # 全局 Key：跨所有连接组顺序调度
        if key == DEFAULT_PUBLIC_API_KEY:
            first_group = self.store.groups[0] if self.store.groups else None
            return RouteContext(
                client_key=key,
                group=first_group,
                group_id=GLOBAL_ROUTE_GROUP_ID,
                provider_type="global",
                base_url=first_group.base_url if first_group else DEFAULT_BASE_URL,
                display_name="全局调度",
                passthrough=False,
                is_global=True,
            )
        group = self.store.find_group_by_route_key(key)
        if not group:
            return None
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

    def _route_group(self) -> Optional[ConnectionGroup]:
        ctx = self._route_context()
        return ctx.group if ctx else None

    def _require_route_context(self) -> Optional[RouteContext]:
        ctx = self._route_context()
        if ctx:
            return ctx
        self._send_json({
            "error": {
                "message": "Missing or invalid Lin Router group API key",
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(render_index_page(), content_type="text/html; charset=utf-8")
            return
        if parsed.path in {"/v1/models", "/models"}:
            ctx = self._require_route_context()
            if not ctx:
                return
            group = ctx.group
            # 全局 Key：返回所有组的所有可用模型
            visible_group = None if ctx.is_global else group
            auto_model_name = DEFAULT_AUTO_MODEL_NAME
            self._send_json({
                "object": "list",
                "data": [
                    {
                        "id": auto_model_name,
                        "object": "model",
                        "created": 0,
                        "owned_by": "lin-router",
                        "permission": [],
                        "root": auto_model_name,
                        "parent": None,
                        "display_name": auto_model_name,
                        "router_virtual": True,
                        "group_id": GLOBAL_ROUTE_GROUP_ID if ctx.is_global else group.id,
                        "group_name": "全局调度" if ctx.is_global else group.name,
                        "provider_type": "global" if ctx.is_global else group.provider_type,
                    },
                    *[
                    {
                        "id": model.name,
                        "object": "model",
                        "created": 0,
                        "owned_by": "lin-router",
                        "permission": [],
                        "root": model.name,
                        "parent": None,
                        "display_name": model.name,
                        "ep_id": model.ep_id,
                        "group_id": model.group_id,
                        "provider_type": self._group_for(model).provider_type if self._group_for(model) else "",
                        "price_group": model.price_group,
                    }
                    for model in self._visible_models(visible_group)
                    ],
                ],
            })
            return
        if parsed.path == "/api/state":
            self.store.refresh_expired_cooldowns()
            settings = self.server.settings_store.to_dict()  # type: ignore[attr-defined]
            self._send_json({
                "config_file": str(self.store.path),
                "auto_model_name": DEFAULT_AUTO_MODEL_NAME,
                "settings": {
                    **settings,
                    # 开机自启以注册表真实状态为准
                    "auto_start": _get_windows_auto_start(),
                },
                "group_meta": {
                    group.id: {
                        "auto_model_name": self.router.group_auto_model_name(group),
                        "model_count": len([m for m in self.store.models if m.group_id == group.id]),
                        "usable_count": len([m for m in self.store.models if m.group_id == group.id and m.usable]),
                    }
                    for group in self.store.groups
                },
                "groups": [asdict(g) for g in self.store.groups],
                "models": [asdict(m) for m in self.store.models],
                "logs": self.router.recent_logs(),
                "log_file": str(self.router.log_file),
                "log_write_error": self.router.log_write_error,
            })
            return
        if parsed.path.startswith("/api/client-config/"):
            group_id = parsed.path.split("/", 3)[3]
            group = self.store.find_group(group_id)
            if not group:
                self._send_text("group not found", status=404)
                return
            self._send_json({
                "base_url": self._client_base_url(),
                "api_key": group.route_key,
                "model": DEFAULT_AUTO_MODEL_NAME,
                "group_id": group.id,
                "group_name": group.name,
            })
            return
        if parsed.path == "/api/settings":
            # 返回当前用户设置（开机自启、启动最小化等）
            self._send_json(self.store.settings)
            return
        if parsed.path == "/api/logs/export":
            csv_text = self.router.export_logs_csv()
            self._send_text(csv_text, content_type="text/csv; charset=utf-8")
            return
        if parsed.path == "/api/config/export":
            # 导出当前配置（groups + models），用于备份和迁移
            payload = {
                "groups": [asdict(g) for g in self.store.groups],
                "models": [asdict(m) for m in self.store.models],
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="lin-router-config-export.json"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/health":
            self._send_json({"ok": True, "groups": len(self.store.groups), "models": len(self.store.models)})
            return
        self._send_text("not found", status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config/import":
            # 导入配置：合并模式，按 id 覆盖同名连接组/模型，其余保留
            payload = self._read_json()
            groups_raw = payload.get("groups") or []
            models_raw = payload.get("models") or []
            if not isinstance(groups_raw, list) or not isinstance(models_raw, list):
                self._send_text("invalid payload: groups and models must be arrays", status=400)
                return
            with self.store._lock:
                existing_group_ids = {g.id for g in self.store.groups}
                for item in groups_raw:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    group = ConnectionGroup.from_dict(item)
                    if not group.route_key:
                        group.route_key = new_route_key()
                    if not group.provider_type:
                        group.provider_type = PROVIDER_ARK
                    # 按 id 覆盖，否则追加
                    for idx, existing in enumerate(self.store.groups):
                        if existing.id == group.id:
                            self.store.groups[idx] = group
                            break
                    else:
                        self.store.groups.append(group)
                existing_model_ids = {m.id for m in self.store.models}
                for item in models_raw:
                    if not isinstance(item, dict) or not item.get("name") or not item.get("ep_id"):
                        continue
                    model = ModelConfig.from_dict(item)
                    # 缺失 group_id 时挂到第一个组，避免悬空
                    if not model.group_id or not self.store.find_group(model.group_id):
                        if self.store.groups:
                            model.group_id = self.store.groups[0].id
                        else:
                            continue
                    for idx, existing in enumerate(self.store.models):
                        if existing.id == model.id:
                            self.store.models[idx] = model
                            break
                    else:
                        self.store.models.append(model)
                self.store.save()
            self._send_json({
                "ok": True,
                "groups": len(self.store.groups),
                "models": len(self.store.models),
            })
            return
        if parsed.path == "/api/groups":
            payload = self._read_json()
            if not payload.get("name"):
                self._send_text("missing group name", status=400)
                return
            existing = self.store.find_group(str(payload.get("id") or ""))
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
            if existing and "waf_compatible" not in payload:
                payload["waf_compatible"] = existing.waf_compatible
            group = ConnectionGroup.from_dict(payload)
            if group.provider_type == PROVIDER_PROXY and not group.api_key and group.ark_api_key:
                group.api_key = group.ark_api_key
            if group.provider_type == PROVIDER_RELAY:
                group.ark_api_key = ""
                group.api_key = ""
            if group.provider_type == PROVIDER_ARK:
                group.api_key = ""
            self.store.upsert_group(group)
            self._send_json({"ok": True, "group": asdict(group)})
            return
        if parsed.path.startswith("/api/groups/") and parsed.path.endswith("/clone"):
            group_id = parsed.path.split("/")[3]
            cloned = self._clone_group(group_id)
            if not cloned:
                self._send_text("group not found", status=404)
                return
            self._send_json({"ok": True, **cloned})
            return
        if parsed.path == "/api/models":
            payload = self._read_json()
            if not payload.get("name") or not payload.get("ep_id") or not payload.get("group_id"):
                self._send_text("missing required fields", status=400)
                return
            group = self.store.find_group(str(payload["group_id"]))
            if not group:
                self._send_text("group not found", status=400)
                return
            existing = self.store.find_model(str(payload.get("id") or ""))
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
            self.store.upsert_model(model)
            if group.provider_type in {PROVIDER_RELAY, PROVIDER_PROXY}:
                group.upstream_models = []
                group.upstream_models_fetched_at = ""
                self.store.upsert_group(group)
            self._send_json({"ok": True, "model": asdict(model)})
            return
        if parsed.path == "/api/models/batch":
            payload = self._read_json()
            group_id = str(payload.get("group_id") or "")
            raw_text = str(payload.get("text") or "")
            if not group_id or not self.store.find_group(group_id):
                self._send_text("group not found", status=400)
                return
            added = 0
            for line in raw_text.splitlines():
                item = line.strip()
                if not item:
                    continue
                if "," in item:
                    name, ep_id = [part.strip() for part in item.split(",", 1)]
                else:
                    name = item
                    ep_id = item
                if not ep_id:
                    continue
                group = self.store.find_group(group_id)
                self.store.upsert_model(ModelConfig(
                    id=uuid.uuid4().hex,
                    name=name or ep_id,
                    ep_id=ep_id,
                    group_id=group_id,
                    upstream_model=ep_id,
                    api_key=str(payload.get("api_key") or "") if group and group.provider_type == PROVIDER_RELAY else "",
                    price_group=str(payload.get("price_group") or "") if group and group.provider_type == PROVIDER_RELAY else "",
                    usable=True,
                ))
                added += 1
            self._send_json({"ok": True, "added": added})
            return
        if parsed.path == "/api/models/fetch-upstream":
            payload = self._read_json()
            group_id = str(payload.get("group_id") or "")
            group = self.store.find_group(group_id)
            if not group:
                self._send_text("group not found", status=400)
                return
            if group.provider_type not in {PROVIDER_RELAY, PROVIDER_PROXY}:
                self._send_text("upstream fetch only supports relay/proxy groups", status=400)
                return
            auth_key = self._effective_group_auth(group, payload)
            if not auth_key:
                self._send_text("missing upstream api key", status=400)
                return
            try:
                items = self._fetch_upstream_models(group, auth_key)
            except Exception as err:
                self._send_text(str(err), status=500)
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
            group.upstream_models_fetched_at = self.router._now()
            self.store.upsert_group(group)
            self._send_json({
                "ok": True,
                "count": len(candidates),
            })
            return
        if parsed.path.endswith("/toggle") and parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            model = self.store.find_model(model_id)
            if not model:
                self._send_text("model not found", status=404)
                return
            if model.cooldown_until:
                model.usable = True
                model.cooldown_until = 0
                model.cooldown_reason = ""
                model.last_error = ""
                model.last_checked_at = self.router._now()
            elif model.usable:
                model.usable = False
            else:
                model.usable = True
                model.cooldown_until = 0
                model.cooldown_reason = ""
                model.last_error = ""
                model.last_checked_at = self.router._now()
            self.store.save()
            self._send_json({"ok": True, "usable": model.usable})
            return
        if parsed.path.endswith("/move") and parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            payload = self._read_json()
            moved = self.store.move_model(model_id, str(payload.get("direction", "")))
            if not moved:
                self._send_text("move failed", status=400)
                return
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/reset":
            self.store.reset_usable()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/logs/clear":
            self.router.clear_logs()
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/settings":
            # 更新用户设置，未知字段会被忽略
            raw = self._read_raw_body()
            payload = self._json_from_raw(raw)
            if not isinstance(payload, dict):
                self._send_text("invalid payload", status=400)
                return
            allowed = {"auto_start", "start_minimized"}
            new_settings = {k: v for k, v in payload.items() if k in allowed}
            # 开机自启需要同步到 Windows 注册表
            if "auto_start" in new_settings:
                _set_windows_auto_start(bool(new_settings["auto_start"]))
            settings_store = self.server.settings_store  # type: ignore[attr-defined]
            updated = settings_store.update(new_settings)
            self._send_json({
                **updated,
                "auto_start": _get_windows_auto_start(),
            })
            return
        if parsed.path == "/api/test":
            ctx = self._require_route_context()
            if not ctx:
                return
            raw = self._read_raw_body()
            payload = self._json_from_raw(raw)
            path = str(payload.get("path", "/v1/chat/completions"))
            body = payload.get("body") or {"messages": [{"role": "user", "content": "ping"}]}
            try:
                status, headers, result = self.router.call(path, body, ctx, dict(self.headers.items()))
                self._send_json({"status": status, "headers": headers, "body": result.decode("utf-8", "ignore")})
            except AllModelsFailedError as err:
                self._send_json({
                    "error": {
                        "message": f"{err}，共尝试 {err.attempted} 个上游",
                        "type": "all_models_failed",
                        "code": "service_unavailable",
                    }
                }, status=503)
            except Exception as err:
                self._send_text(str(err), status=500)
            return
        if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):
            ctx = self._require_route_context()
            if not ctx:
                return
            raw = self._read_raw_body()
            payload = self._json_from_raw(raw)
            stream = bool(payload.get("stream"))
            try:
                if stream:
                    status, headers, iterator = self.router.stream(parsed.path, payload, ctx, dict(self.headers.items()), raw)
                    self.send_response(status)
                    for key, value in headers.items():
                        if key.lower() in {"content-length", "connection", "transfer-encoding"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("Content-Type", headers.get("Content-Type", "text/event-stream; charset=utf-8"))
                    self.end_headers()
                    for chunk in iterator:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    return
                status, headers, data = self.router.call(parsed.path, payload, ctx, dict(self.headers.items()), raw)
                self.send_response(status)
                for key, value in headers.items():
                    if key.lower() in {"content-length", "connection", "transfer-encoding"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except AllModelsFailedError as err:
                self._send_json({
                    "error": {
                        "message": f"{err}，共尝试 {err.attempted} 个上游",
                        "type": "all_models_failed",
                        "code": "service_unavailable",
                    }
                }, status=503)
            except Exception as err:
                self._send_text(str(err), status=500)
            return
        self._send_text("not found", status=404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/groups/"):
            group_id = parsed.path.split("/")[3]
            if any(model.group_id == group_id for model in self.store.models):
                self._send_text("group is still used by models", status=400)
                return
            before = len(self.store.groups)
            self.store.groups = [group for group in self.store.groups if group.id != group_id]
            if len(self.store.groups) == before:
                self._send_text("group not found", status=404)
                return
            self.store.save()
            self._send_json({"ok": True})
            return
        if parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            if self.store.remove_model(model_id):
                self._send_json({"ok": True})
            else:
                self._send_text("model not found", status=404)
            return
        self._send_text("not found", status=404)


def ensure_sample_config(path: Path) -> None:
    if path.exists():
        return
    sample_group = ConnectionGroup(
        id=uuid.uuid4().hex,
        name="默认组",
        provider_type=PROVIDER_ARK,
        base_url=DEFAULT_BASE_URL,
        ark_api_key="sk-xxxx",
    )
    sample_model = ModelConfig(
        id=uuid.uuid4().hex,
        name="DeepSeek",
        ep_id="ep-xxxx",
        group_id=sample_group.id,
        usable=True,
    )
    with path.open("w", encoding="utf-8") as f:
        json.dump({"groups": [asdict(sample_group)], "models": [asdict(sample_model)]}, f, ensure_ascii=False, indent=2)


def pick_port(start_port: int, host: str) -> int:
    for port in range(start_port, start_port + MAX_PORT_SCAN):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port found in range {start_port}-{start_port + MAX_PORT_SCAN - 1}")


def create_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_START_PORT,
    config: str | Path = DEFAULT_CONFIG_FILE,
) -> Tuple[ThreadingHTTPServer, int, Path]:
    config_path = Path(config)
    ensure_sample_config(config_path)
    store = ConfigStore(config_path)
    store.reset_usable()
    router = ArkProxyRouter(store)
    settings_store = SettingsStore(config_path)
    selected_port = pick_port(port, host)

    server = ThreadingHTTPServer((host, selected_port), RouterHandler)
    server.store = store  # type: ignore[attr-defined]
    server.router = router  # type: ignore[attr-defined]
    server.settings_store = settings_store  # type: ignore[attr-defined]
    return server, selected_port, config_path.resolve()


def main() -> None:
    # 默认配置文件固定在项目根目录，不跟随命令行工作目录变化
    default_config = str(Path(__file__).resolve().parent / DEFAULT_CONFIG_FILE)
    parser = argparse.ArgumentParser(description="Lin Router proxy UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=DEFAULT_START_PORT, type=int)
    parser.add_argument("--config", default=default_config)
    args = parser.parse_args()

    server, port, config_path = create_server(args.host, args.port, args.config)

    print(f"Lin Router running on http://{args.host}:{port}")
    print(f"Config file: {config_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

