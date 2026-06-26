from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
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


DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_CONFIG_FILE = "lin-router-config.json"
DEFAULT_START_PORT = 18400
DEFAULT_AUTO_MODEL_NAME = "lin-router-auto"
DEFAULT_PUBLIC_API_KEY = "lin-router"
MAX_PORT_SCAN = 1


def new_route_key() -> str:
    return f"lr-{uuid.uuid4().hex[:16]}"


@dataclass
class ConnectionGroup:
    id: str
    name: str
    base_url: str = DEFAULT_BASE_URL
    ark_api_key: str = ""
    route_key: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionGroup":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            base_url=data.get("base_url") or DEFAULT_BASE_URL,
            ark_api_key=data.get("ark_api_key") or "",
            route_key=str(data.get("route_key") or ""),
        )


@dataclass
class ModelConfig:
    id: str
    name: str
    ep_id: str
    group_id: str
    usable: bool = True
    last_error: str = ""
    last_success_at: str = ""
    last_checked_at: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelConfig":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            ep_id=data["ep_id"],
            group_id=str(data.get("group_id") or ""),
            usable=bool(data.get("usable", True)),
            last_error=str(data.get("last_error", "")),
            last_success_at=str(data.get("last_success_at", "")),
            last_checked_at=str(data.get("last_checked_at", "")),
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

    def upsert_group(self, group: ConnectionGroup) -> None:
        with self._lock:
            if not group.route_key:
                existing = self.find_group(group.id)
                group.route_key = existing.route_key if existing and existing.route_key else new_route_key()
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
            new_idx = idx - 1 if direction == "up" else idx + 1
            if new_idx < 0 or new_idx >= len(self.models):
                return False
            self.models[idx], self.models[new_idx] = self.models[new_idx], self.models[idx]
            self.save()
            return True

    def reset_usable(self) -> None:
        with self._lock:
            for model in self.models:
                model.usable = True
                model.last_error = ""
            self.save()

    def find_group(self, group_id: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.id == group_id), None)

    def find_group_by_route_key(self, route_key: str) -> Optional[ConnectionGroup]:
        return next((g for g in self.groups if g.route_key == route_key), None)

    def find_model(self, model_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.id == model_id), None)


class ArkProxyRouter:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self.logs: List[RequestLog] = []

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
    ) -> None:
        self.logs.insert(0, RequestLog(
            self._now(),
            path,
            model,
            status,
            detail[:300],
            duration_ms,
            prompt_tokens,
            completion_tokens,
            total_tokens,
        ))
        del self.logs[80:]

    def recent_logs(self) -> List[Dict[str, str]]:
        return [asdict(item) for item in self.logs[:30]]

    def clear_logs(self) -> None:
        self.logs.clear()

    def export_logs_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time", "path", "model", "status", "duration_ms", "prompt_tokens", "completion_tokens", "total_tokens", "detail"])
        for item in self.logs:
            writer.writerow([
                item.time,
                item.path,
                item.model,
                item.status,
                item.duration_ms,
                item.prompt_tokens,
                item.completion_tokens,
                item.total_tokens,
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
    def _usage_from_response(data: bytes) -> Tuple[int, int, int]:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return 0, 0, 0
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return 0, 0, 0
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        return prompt_tokens, completion_tokens, total_tokens

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
            if not model.usable:
                continue
            if group_id and model.group_id != group_id:
                continue
            if requested_model and requested_model not in {model.id, model.name, model.ep_id}:
                continue
            yield idx, model

    def _group_for(self, model: ModelConfig) -> Optional[ConnectionGroup]:
        return self.store.find_group(model.group_id)

    def _set_unusable(self, idx: int, error: str) -> None:
        model = self.store.models[idx]
        model.usable = False
        model.last_error = error[:500]
        model.last_checked_at = self._now()
        self.store.save()

    def _set_success(self, idx: int) -> None:
        model = self.store.models[idx]
        model.last_error = ""
        model.last_success_at = self._now()
        model.last_checked_at = model.last_success_at
        self.store.save()

    def call(self, path: str, payload: Dict[str, Any], group_id: str | None = None) -> Tuple[int, Dict[str, str], bytes]:
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        last_error: Optional[Exception] = None

        for idx, model in self._iter_candidates(str(requested_model) if requested_model else None, group_id):
            group = self._group_for(model)
            if not group:
                self.add_log(path, model.name, "skip", f"requested={requested_label}; missing connection group")
                continue
            if not group.ark_api_key:
                self.add_log(path, model.name, "skip", f"requested={requested_label}; missing api key")
                continue
            target_url = self._resolve_url(group.base_url, path)
            outbound_payload = dict(payload)
            outbound_payload["model"] = model.ep_id
            body = json.dumps(outbound_payload, ensure_ascii=False).encode("utf-8")
            request = Request(
                target_url,
                data=body,
                headers={
                    "Authorization": f"Bearer {group.ark_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            started_at = time.perf_counter()
            try:
                with urlopen(request, timeout=120) as resp:
                    data = resp.read()
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    prompt_tokens, completion_tokens, total_tokens = self._usage_from_response(data)
                    self._set_success(idx)
                    self.add_log(
                        path,
                        model.name,
                        str(resp.status),
                        f"hit={model.ep_id}; requested={requested_label}; ok",
                        duration_ms,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                    )
                    return resp.status, dict(resp.headers.items()), data
            except HTTPError as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                if self._is_quota_exhausted(err.code, raw):
                    self._set_unusable(idx, raw)
                    self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; quota exhausted, try next", duration_ms)
                    continue
                if self._is_rate_limited(err.code, raw):
                    try:
                        retry_started_at = time.perf_counter()
                        with urlopen(request, timeout=120) as resp:
                            data = resp.read()
                            retry_duration_ms = int((time.perf_counter() - retry_started_at) * 1000)
                            prompt_tokens, completion_tokens, total_tokens = self._usage_from_response(data)
                            self._set_success(idx)
                            self.add_log(
                                path,
                                model.name,
                                str(resp.status),
                                f"hit={model.ep_id}; requested={requested_label}; retry ok",
                                retry_duration_ms,
                                prompt_tokens,
                                completion_tokens,
                                total_tokens,
                            )
                            return resp.status, dict(resp.headers.items()), data
                    except Exception as retry_err:
                        last_error = retry_err
                        retry_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        self.add_log(path, model.name, "retry failed", f"hit={model.ep_id}; requested={requested_label}; {retry_err}", retry_duration_ms)
                        continue
                if self._is_server_error(err.code):
                    self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; server error, try next", duration_ms)
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; {raw}", duration_ms)
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                self.add_log(path, model.name, "network", f"hit={model.ep_id}; requested={requested_label}; {err}", duration_ms)
                continue

        if last_error is None:
            raise RuntimeError("No usable models available")
        raise RuntimeError("All available models failed") from last_error

    def stream(self, path: str, payload: Dict[str, Any], group_id: str | None = None) -> Tuple[int, Dict[str, str], Iterable[bytes]]:
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        last_error: Optional[Exception] = None

        for idx, model in self._iter_candidates(str(requested_model) if requested_model else None, group_id):
            group = self._group_for(model)
            if not group:
                self.add_log(path, model.name, "skip", f"requested={requested_label}; missing connection group")
                continue
            if not group.ark_api_key:
                self.add_log(path, model.name, "skip", f"requested={requested_label}; missing api key")
                continue
            target_url = self._resolve_url(group.base_url, path)
            outbound_payload = dict(payload)
            outbound_payload["model"] = model.ep_id
            body = json.dumps(outbound_payload, ensure_ascii=False).encode("utf-8")
            request = Request(
                target_url,
                data=body,
                headers={
                    "Authorization": f"Bearer {group.ark_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            started_at = time.perf_counter()
            try:
                resp = urlopen(request, timeout=120)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self._set_success(idx)
                self.add_log(path, model.name, "200", f"hit={model.ep_id}; requested={requested_label}; stream ok", duration_ms)

                def iterator() -> Iterator[bytes]:
                    try:
                        while True:
                            chunk = resp.readline()
                            if not chunk:
                                break
                            yield chunk
                    finally:
                        resp.close()

                return 200, dict(resp.headers.items()), iterator()
            except HTTPError as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                if self._is_quota_exhausted(err.code, raw):
                    self._set_unusable(idx, raw)
                    self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; quota exhausted, try next", duration_ms)
                    continue
                if self._is_rate_limited(err.code, raw):
                    self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; rate limited, try next", duration_ms)
                    continue
                if self._is_server_error(err.code):
                    self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; server error, try next", duration_ms)
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                self.add_log(path, model.name, str(err.code), f"hit={model.ep_id}; requested={requested_label}; {raw}", duration_ms)
                return err.code, headers, [raw.encode("utf-8")]
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                self.add_log(path, model.name, "network", f"hit={model.ep_id}; requested={requested_label}; {err}", duration_ms)
                continue

        if last_error is None:
            raise RuntimeError("No usable models available")
        raise RuntimeError("All available models failed") from last_error


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lin Router Hermes</title>
  <style>
    :root { color-scheme: light; --bg:#f5f7fb; --panel:#fff; --line:#d6dce8; --text:#18212f; --muted:#5b6575; --accent:#2358ff; --danger:#c62828; --ok:#16794c; --warn:#a15c00; --bad:#b42318; }
    * { box-sizing: border-box; }
    body { margin:0; font:14px/1.5 system-ui, -apple-system, Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--text); overflow-x:hidden; }
    header { height:58px; padding:0 22px; display:flex; align-items:center; justify-content:space-between; background:#fff; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:18px; }
    h2 { margin:0 0 12px; font-size:15px; }
    .shell { padding:18px; display:grid; grid-template-columns:minmax(280px, 360px) minmax(0, 1fr); gap:18px; max-width:100vw; min-width:0; }
    .main { display:grid; gap:18px; min-width:0; }
    .side { display:grid; gap:18px; align-content:start; min-width:0; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:14px; min-width:0; overflow:hidden; }
    .hero { grid-column:1 / -1; display:grid; grid-template-columns:1fr auto; gap:16px; align-items:center; }
    .heroUrl { padding:10px 12px; border:1px solid #c8d2ff; background:#f1f4ff; border-radius:6px; font-family:Consolas, monospace; }
    label { display:block; margin:10px 0 6px; color:var(--muted); }
    input, textarea, select, button { font:inherit; }
    input, textarea, select { width:100%; min-width:0; border:1px solid var(--line); border-radius:6px; padding:9px 10px; background:#fff; color:var(--text); }
    textarea { min-height:104px; resize:vertical; overflow:auto; overflow-wrap:anywhere; }
    #proxyBody { font-family:Consolas, monospace; white-space:pre; overflow-wrap:normal; }
    button { border:1px solid var(--line); background:#fff; color:var(--text); border-radius:6px; padding:7px 9px; cursor:pointer; }
    button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
    button.danger { color:var(--danger); }
    button:disabled { opacity:.62; cursor:wait; }
    .row { display:flex; gap:8px; flex-wrap:wrap; }
    .row > * { flex:1 1 auto; }
    .muted { color:var(--muted); }
    .tiny { font-size:12px; }
    .status { padding:10px 12px; background:#f1f4ff; border:1px solid #cad4ff; border-radius:6px; margin-bottom:12px; }
    .modelGroups { display:grid; gap:10px; }
    .modelGroup { border:1px solid var(--line); border-radius:6px; background:#fff; overflow:hidden; }
    .modelGroup > summary { padding:12px 14px; cursor:pointer; list-style:none; font-weight:700; background:#f8faff; border-bottom:1px solid var(--line); }
    .modelGroup > summary::-webkit-details-marker { display:none; }
    .modelGroupSummary { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
    .modelGroupActions { display:flex; gap:6px; flex-wrap:wrap; font-weight:400; }
    .modelGroupActions button { padding:5px 8px; font-size:12px; line-height:1.2; }
    .modelGroupBody { padding:0 14px 12px; overflow:auto; }
    .codeLine { font-family:Consolas, monospace; background:#f6f8fc; border:1px solid var(--line); border-radius:6px; padding:7px 8px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    details.panel { padding:0; }
    details.panel > summary { padding:14px; cursor:pointer; font-weight:700; list-style:none; border-bottom:1px solid var(--line); }
    details.panel > summary::-webkit-details-marker { display:none; }
    details.panel > .panelBody { padding:14px; }
    table { width:100%; border-collapse:collapse; table-layout:fixed; }
    th, td { padding:8px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; overflow-wrap:anywhere; }
    th { color:var(--muted); font-weight:600; }
    td.actions { white-space:nowrap; width:280px; }
    .modelTable th:nth-child(1), .modelTable td:nth-child(1) { width:64px; }
    .modelTable th:nth-child(2), .modelTable td:nth-child(2) { width:18%; }
    .modelTable th:nth-child(3), .modelTable td:nth-child(3) { width:20%; }
    .modelTable th:nth-child(4), .modelTable td:nth-child(4) { width:86px; }
    .modelTable th:nth-child(5), .modelTable td:nth-child(5) { width:auto; }
    .modelTable th:nth-child(6), .modelTable td:nth-child(6) { width:280px; }
    .actionGroup { display:flex; align-items:center; gap:5px; flex-wrap:nowrap; overflow-x:auto; padding-bottom:1px; }
    .actionGroup button { padding:5px 8px; font-size:12px; line-height:1.2; min-width:42px; }
    .actionGroup button.danger { min-width:42px; }
    .pill { display:inline-block; padding:2px 7px; border-radius:999px; background:#edf7f1; color:var(--ok); font-size:12px; line-height:1.3; }
    .pill.off { background:#fff3e6; color:var(--warn); }
    .pill.bad { background:#fff0f0; color:var(--bad); }
    .resultText { max-width:420px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .log { white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word; background:#0f172a; color:#d9e2ff; border-radius:6px; padding:12px; min-height:120px; max-height:260px; overflow:auto; }
    @media (max-width: 980px) { .shell { grid-template-columns:1fr; } .hero { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Lin Router Hermes</h1>
    <div class="muted tiny" id="serverInfo"></div>
  </header>
  <div class="shell">
    <section class="panel hero">
      <div>
        <h2>Hermes 接入</h2>
        <div class="heroUrl" id="hermesUrl">加载中...</div>
        <div class="muted tiny" style="margin-top:8px">Base URL 填这里；API Key 必须填右侧连接组里的 Hermes Key，请求会严格按 Key 限定到对应连接组。</div>
      </div>
      <button type="button" id="copyHermesBtn">复制地址</button>
    </section>

    <aside class="side">
      <details class="panel" open>
        <summary>连接组</summary>
        <div class="panelBody">
        <form id="groupForm">
          <input type="hidden" id="groupId">
          <label>组名</label>
          <input id="groupName" placeholder="默认组">
          <label>Base URL</label>
          <input id="groupBase" placeholder="https://ark.cn-beijing.volces.com/api/v3">
          <label>Ark API Key</label>
          <input id="groupKey" type="password" placeholder="sk-xxxx">
          <div class="row" style="margin-top:12px">
            <button class="primary" type="submit">保存组</button>
            <button type="button" id="toggleKeyBtn">显示 Key</button>
          </div>
        </form>
        <div class="muted tiny" style="margin-top:10px">同一个 base/key 只需要建一次。连接组详情和操作已移到右侧模型列表。</div>
        </div>
      </details>

      <details class="panel" open>
        <summary>模型配置</summary>
        <div class="panelBody">
        <form id="modelForm">
          <input type="hidden" id="modelId">
          <label>名称</label>
          <input id="name" placeholder="DeepSeek">
          <label>EP ID</label>
          <input id="epId" placeholder="ep-xxxx">
          <label>连接组</label>
          <select id="groupPick"></select>
          <div class="row" style="margin-top:12px">
            <button class="primary" type="submit">保存模型</button>
            <button type="button" id="resetBtn">全部恢复可用</button>
          </div>
        </form>
        </div>
      </details>

      <details class="panel">
        <summary>批量导入</summary>
        <div class="panelBody">
        <label>连接组</label>
        <select id="batchGroupPick"></select>
        <label>模型列表</label>
        <textarea id="batchModels" placeholder="模型名称,ep-xxxx&#10;另一个模型,ep-yyyy"></textarea>
        <div class="row" style="margin-top:12px">
          <button class="primary" type="button" id="batchImportBtn">批量导入模型</button>
        </div>
        </div>
      </details>
    </aside>

    <main class="main">
      <section class="panel">
        <h2>模型列表</h2>
        <div class="status" id="summaryBox">加载中...</div>
        <div class="modelGroups" id="modelGroups"></div>
      </section>

      <details class="panel" open>
        <summary>代理测试</summary>
        <div class="panelBody">
        <label>测试模板</label>
        <select id="testTemplate">
          <option value="auto">自动切换聊天</option>
          <option value="chat">普通聊天</option>
          <option value="model">指定模型聊天</option>
          <option value="stream">流式请求</option>
        </select>
        <label>测试模型</label>
        <select id="testModel"></select>
        <label>请求路径</label>
        <input id="proxyPath" value="/v1/chat/completions">
        <label>请求体</label>
        <textarea id="proxyBody">{ "messages": [{"role":"user","content":"hello"}], "temperature": 0.2 }</textarea>
        <div class="row" style="margin-top:12px"><button class="primary" type="button" id="sendTest">发送测试</button></div>
        </div>
      </details>

      <details class="panel" open>
        <summary>返回结果</summary>
        <div class="panelBody">
        <div class="log" id="logBox">等待操作。</div>
        </div>
      </details>

      <details class="panel" open>
        <summary>最近请求</summary>
        <div class="panelBody">
        <div class="row" style="margin-bottom:10px">
          <button type="button" id="clearLogsBtn">清空日志</button>
          <button type="button" id="exportLogsBtn">导出 CSV</button>
        </div>
        <table>
          <thead><tr><th>时间</th><th>模型</th><th>状态</th><th>耗时</th><th>Token</th><th>详情</th></tr></thead>
          <tbody id="logTbody"></tbody>
        </table>
        </div>
      </details>
    </main>
  </div>
  <script>
    const $ = (id) => document.getElementById(id);
    const AUTO_MODEL_NAME = "__AUTO_MODEL_NAME__";
    let state = { groups: [], models: [], logs: [] };
    function log(text) { $('logBox').textContent = text; }
    function esc(text) { return String(text ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s])); }
    function formatResponse(text) {
      try {
        return JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        return text;
      }
    }
    function statusInfo(m) {
      if (!m.usable) return { text:'停用', cls:'off', detail:m.last_error ? '额度耗尽或手动停用' : '手动停用' };
      if (m.last_error) return { text:'异常', cls:'bad', detail:m.last_error };
      if (m.last_success_at) return { text:'可用', cls:'', detail:`最近成功 ${m.last_success_at}` };
      return { text:'待测', cls:'off', detail:'尚未成功调用' };
    }
    function logStatusClass(status) {
      const text = String(status || '');
      if (text === '200' || text.startsWith('2')) return '';
      if (text === 'network' || text.includes('failed') || text.startsWith('5')) return 'bad';
      return 'off';
    }
    function selectedModelId() {
      return $('testModel').value || (state.auto_model_name || AUTO_MODEL_NAME);
    }
    function selectedRouteKey() {
      const selected = $('testModel').value;
      if (selected && selected !== (state.auto_model_name || AUTO_MODEL_NAME)) {
        const model = state.models.find(m => m.name === selected || m.ep_id === selected || m.id === selected);
        const group = state.groups.find(g => g.id === model?.group_id);
        return group?.route_key || '';
      }
      const firstUsable = state.models.find(m => m.usable);
      const group = state.groups.find(g => g.id === firstUsable?.group_id) || state.groups[0];
      return group?.route_key || '';
    }
    function applyTestTemplate() {
      const model = selectedModelId();
      const template = $('testTemplate').value;
      const base = { messages:[{role:'user', content:'hello'}], temperature:0.2 };
      if (template === 'auto') {
        $('testModel').value = state.auto_model_name || AUTO_MODEL_NAME;
        $('proxyBody').value = JSON.stringify(base, null, 2);
      } else if (template === 'chat') {
        $('proxyBody').value = JSON.stringify(base, null, 2);
      } else if (template === 'model') {
        $('proxyBody').value = JSON.stringify({ ...base, model }, null, 2);
      } else if (template === 'stream') {
        $('proxyBody').value = JSON.stringify({ ...base, model, stream:true }, null, 2);
      }
      $('proxyPath').value = '/v1/chat/completions';
    }
    function fillGroupPick() {
      const groupOptions = state.groups.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
      $('groupPick').innerHTML = groupOptions;
      $('batchGroupPick').innerHTML = groupOptions;
      const autoName = state.auto_model_name || AUTO_MODEL_NAME;
      $('testModel').innerHTML = [`<option value="${esc(autoName)}">${esc(autoName)} · 智能调度</option>`]
        .concat(state.models.map(m => `<option value="${esc(m.name)}">${esc(m.name)} · ${esc(m.ep_id)}</option>`))
        .join('');
    }
    function fillGroupForm(g) {
      $('groupId').value = g?.id || '';
      $('groupName').value = g?.name || '';
      $('groupBase').value = g?.base_url || '';
      $('groupKey').value = g?.ark_api_key || '';
    }
    function fillForm(m) {
      $('modelId').value = m?.id || '';
      $('name').value = m?.name || '';
      $('epId').value = m?.ep_id || '';
      $('groupPick').value = m?.group_id || (state.groups[0]?.id || '');
    }
    function groupMeta(groupId) {
      return (state.group_meta || {})[groupId] || { auto_model_name:AUTO_MODEL_NAME, model_count:0, usable_count:0 };
    }
    function bindGroupActions() {
      document.querySelectorAll('[data-group-edit]').forEach(btn => btn.onclick = (event) => {
        event.stopPropagation();
        fillGroupForm(state.groups.find(x => x.id === btn.dataset.groupEdit));
      });
      document.querySelectorAll('[data-copy-key]').forEach(btn => btn.onclick = async (event) => {
        event.stopPropagation();
        const group = state.groups.find(x => x.id === btn.dataset.copyKey);
        await navigator.clipboard.writeText(group?.route_key || '');
        log('连接组 Hermes Key 已复制');
      });
      document.querySelectorAll('[data-copy-model]').forEach(btn => btn.onclick = async (event) => {
        event.stopPropagation();
        const meta = groupMeta(btn.dataset.copyModel);
        await navigator.clipboard.writeText(meta.auto_model_name || '');
        log('连接组自动模型名已复制');
      });
      document.querySelectorAll('[data-group-del]').forEach(btn => btn.onclick = (event) => {
        event.stopPropagation();
        const group = state.groups.find(x => x.id === btn.dataset.groupDel);
        if (confirm(`确定删除连接组「${group?.name || btn.dataset.groupDel}」？`)) {
          mutate(`/api/groups/${btn.dataset.groupDel}`, {}, 'DELETE').catch(err => log(String(err)));
        }
      });
    }
    function modelRow(m, globalIndex) {
      const info = statusInfo(m);
      return `
        <tr>
          <td>${globalIndex + 1}</td>
          <td>${esc(m.name)}</td>
          <td class="tiny">${esc(m.ep_id)}</td>
          <td><span class="pill ${info.cls}">${info.text}</span></td>
          <td class="tiny resultText" title="${esc(info.detail)}">${esc(info.detail)}</td>
          <td class="actions"><div class="actionGroup">
            <button type="button" data-edit="${m.id}">编辑</button>
            <button type="button" data-move-up="${m.id}">上移</button>
            <button type="button" data-move-down="${m.id}">下移</button>
            <button type="button" data-toggle="${m.id}">${m.usable ? '停用' : '启用'}</button>
            <button type="button" data-del="${m.id}" class="danger">删除</button>
          </div></td>
        </tr>
      `;
    }
    function bindModelActions() {
      document.querySelectorAll('[data-edit]').forEach(btn => btn.onclick = () => fillForm(state.models.find(x => x.id === btn.dataset.edit)));
      document.querySelectorAll('[data-move-up]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.moveUp}/move`, {direction:'up'}));
      document.querySelectorAll('[data-move-down]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.moveDown}/move`, {direction:'down'}));
      document.querySelectorAll('[data-toggle]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.toggle}/toggle`, {}));
      document.querySelectorAll('[data-del]').forEach(btn => btn.onclick = () => {
        const model = state.models.find(x => x.id === btn.dataset.del);
        if (confirm(`确定删除模型「${model?.name || btn.dataset.del}」？`)) {
          mutate(`/api/models/${btn.dataset.del}`, {}, 'DELETE').catch(err => log(String(err)));
        }
      });
    }
    function renderModels() {
      const globalIndex = new Map(state.models.map((m, i) => [m.id, i]));
      $('modelGroups').innerHTML = state.groups.map(group => {
        const meta = groupMeta(group.id);
        const models = state.models.filter(m => m.group_id === group.id);
        const rows = models.map(m => modelRow(m, globalIndex.get(m.id) ?? 0)).join('');
        return `
          <details class="modelGroup" open>
            <summary>
              <div class="modelGroupSummary">
                <span>${esc(group.name)} · 模型 ${meta.model_count} · 可用 ${meta.usable_count}</span>
                <span class="modelGroupActions">
                  <button type="button" data-group-edit="${group.id}">编辑组</button>
                  <button type="button" data-copy-key="${group.id}">复制 Key</button>
                  <button type="button" data-copy-model="${group.id}">复制自动模型</button>
                  <button type="button" class="danger" data-group-del="${group.id}">删除组</button>
                </span>
              </div>
            </summary>
            <div class="modelGroupBody">
              <div class="tiny muted" style="margin:10px 0">自动模型：${esc(meta.auto_model_name)} · Hermes Key：${esc(group.route_key)} · 调度范围由 Key 决定</div>
              <table class="modelTable">
                <thead><tr><th>优先级</th><th>名称</th><th>EP</th><th>状态</th><th>最近结果</th><th>操作</th></tr></thead>
                <tbody>${rows || '<tr><td colspan="6" class="muted">该连接组暂无模型</td></tr>'}</tbody>
              </table>
            </div>
          </details>
        `;
      }).join('') || '<div class="muted">暂无连接组</div>';
      const orphanModels = state.models.filter(m => !state.groups.some(g => g.id === m.group_id));
      if (orphanModels.length) {
        $('modelGroups').insertAdjacentHTML('beforeend', `
          <details class="modelGroup" open>
            <summary>未分组模型 · ${orphanModels.length}</summary>
            <div class="modelGroupBody">
              <table class="modelTable">
                <thead><tr><th>优先级</th><th>名称</th><th>EP</th><th>状态</th><th>最近结果</th><th>操作</th></tr></thead>
                <tbody>${orphanModels.map(m => modelRow(m, globalIndex.get(m.id) ?? 0)).join('')}</tbody>
              </table>
            </div>
          </details>
        `);
      }
      bindGroupActions();
      bindModelActions();
    }
    async function refresh() {
      const resp = await fetch('/api/state');
      state = await resp.json();
      $('serverInfo').textContent = `${location.origin} · config: ${state.config_file}`;
      $('hermesUrl').textContent = `${location.origin}/v1`;
      $('summaryBox').textContent = `组 ${state.groups.length} · 模型 ${state.models.length} · 可用 ${state.models.filter(m => m.usable).length}`;
      fillGroupPick();
      renderModels();
      $('logTbody').innerHTML = (state.logs || []).map(item => `
        <tr>
          <td class="tiny">${esc(item.time)}</td>
          <td>${esc(item.model)}</td>
          <td><span class="pill ${logStatusClass(item.status)}">${esc(item.status)}</span></td>
          <td class="tiny">${Number(item.duration_ms || 0) ? `${Number(item.duration_ms)} ms` : '-'}</td>
          <td class="tiny">${Number(item.total_tokens || 0) ? `入 ${Number(item.prompt_tokens || 0)} / 出 ${Number(item.completion_tokens || 0)} / 总 ${Number(item.total_tokens || 0)}` : '-'}</td>
          <td class="tiny resultText" title="${esc(item.detail)}">${esc(item.detail)}</td>
        </tr>
      `).join('') || '<tr><td colspan="6" class="muted">暂无请求</td></tr>';
    }
    async function mutate(url, body, method='POST') {
      const resp = await fetch(url, { method, headers:{'Content-Type':'application/json'}, body: method === 'DELETE' ? undefined : JSON.stringify(body) });
      const text = await resp.text();
      if (!resp.ok) throw new Error(text || resp.statusText);
      await refresh();
      log(text || 'ok');
    }
    $('copyHermesBtn').onclick = async () => { await navigator.clipboard.writeText($('hermesUrl').textContent); log('Hermes 地址已复制'); };
    $('groupForm').onsubmit = async (e) => {
      e.preventDefault();
      await mutate('/api/groups', { id:$('groupId').value || undefined, name:$('groupName').value.trim(), base_url:$('groupBase').value.trim() || undefined, ark_api_key:$('groupKey').value.trim() });
      fillGroupForm(null);
    };
    $('modelForm').onsubmit = async (e) => {
      e.preventDefault();
      await mutate('/api/models', { id:$('modelId').value || undefined, name:$('name').value.trim(), ep_id:$('epId').value.trim(), group_id:$('groupPick').value });
      fillForm(null);
    };
    $('resetBtn').onclick = async () => mutate('/api/reset', {});
    $('clearLogsBtn').onclick = async () => mutate('/api/logs/clear', {});
    $('exportLogsBtn').onclick = () => { location.href = '/api/logs/export'; };
    $('batchImportBtn').onclick = async () => { await mutate('/api/models/batch', { group_id:$('batchGroupPick').value, text:$('batchModels').value }); $('batchModels').value = ''; };
    $('toggleKeyBtn').onclick = () => { const input = $('groupKey'); input.type = input.type === 'password' ? 'text' : 'password'; $('toggleKeyBtn').textContent = input.type === 'password' ? '显示 Key' : '隐藏 Key'; };
    $('testTemplate').onchange = applyTestTemplate;
    $('testModel').onchange = () => {
      if (['model', 'stream'].includes($('testTemplate').value)) applyTestTemplate();
    };
    $('sendTest').onclick = async () => {
      const btn = $('sendTest');
      btn.disabled = true;
      btn.textContent = '发送中...';
      try {
        const payload = JSON.parse($('proxyBody').value);
        const selectedModel = $('testModel').value;
        if (selectedModel && selectedModel !== (state.auto_model_name || AUTO_MODEL_NAME)) payload.model = selectedModel;
        const routeKey = selectedRouteKey();
        const startedAt = performance.now();
        const resp = await fetch($('proxyPath').value, { method:'POST', headers:{'Content-Type':'application/json', 'Authorization':`Bearer ${routeKey}`}, body:JSON.stringify(payload) });
        const text = await resp.text();
        const elapsed = Math.round(performance.now() - startedAt);
        log(`HTTP ${resp.status} · ${elapsed} ms\n${formatResponse(text)}`);
        await refresh();
      } catch (err) {
        log(String(err));
      } finally {
        btn.disabled = false;
        btn.textContent = '发送测试';
      }
    };
    refresh().catch(err => log(String(err)));
  </script>
</body>
</html>
"""
PAGE_HTML = PAGE_HTML.replace("__AUTO_MODEL_NAME__", DEFAULT_AUTO_MODEL_NAME)


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
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _route_group(self) -> Optional[ConnectionGroup]:
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        key = auth.split(" ", 1)[1].strip()
        if not key:
            return None
        return self.store.find_group_by_route_key(key)

    def _require_route_group(self) -> Optional[ConnectionGroup]:
        group = self._route_group()
        if group:
            return group
        self._send_json({
            "error": {
                "message": "Missing or invalid Lin Router group API key",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }, status=401)
        return None

    def _visible_models(self, group: Optional[ConnectionGroup]) -> List[ModelConfig]:
        return [
            model
            for model in self.store.models
            if model.usable and (group is None or model.group_id == group.id)
        ]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(PAGE_HTML, content_type="text/html; charset=utf-8")
            return
        if parsed.path in {"/v1/models", "/models"}:
            group = self._require_route_group()
            if not group:
                return
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
                        "group_id": group.id,
                        "group_name": group.name,
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
                    }
                    for model in self._visible_models(group)
                    ],
                ],
            })
            return
        if parsed.path == "/api/state":
            self._send_json({
                "config_file": str(self.store.path),
                "auto_model_name": DEFAULT_AUTO_MODEL_NAME,
                "public_api_key": DEFAULT_PUBLIC_API_KEY,
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
            })
            return
        if parsed.path == "/api/logs/export":
            csv_text = self.router.export_logs_csv()
            self._send_text(csv_text, content_type="text/csv; charset=utf-8")
            return
        if parsed.path == "/health":
            self._send_json({"ok": True, "groups": len(self.store.groups), "models": len(self.store.models)})
            return
        self._send_text("not found", status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/groups":
            payload = self._read_json()
            if not payload.get("name"):
                self._send_text("missing group name", status=400)
                return
            existing = self.store.find_group(str(payload.get("id") or ""))
            if existing and not payload.get("route_key"):
                payload["route_key"] = existing.route_key
            group = ConnectionGroup.from_dict(payload)
            self.store.upsert_group(group)
            self._send_json({"ok": True, "group": asdict(group)})
            return
        if parsed.path == "/api/models":
            payload = self._read_json()
            if not payload.get("name") or not payload.get("ep_id") or not payload.get("group_id"):
                self._send_text("missing required fields", status=400)
                return
            if not self.store.find_group(str(payload["group_id"])):
                self._send_text("group not found", status=400)
                return
            model = ModelConfig.from_dict(payload)
            self.store.upsert_model(model)
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
                self.store.upsert_model(ModelConfig(
                    id=uuid.uuid4().hex,
                    name=name or ep_id,
                    ep_id=ep_id,
                    group_id=group_id,
                    usable=True,
                ))
                added += 1
            self._send_json({"ok": True, "added": added})
            return
        if parsed.path.endswith("/toggle") and parsed.path.startswith("/api/models/"):
            model_id = parsed.path.split("/")[3]
            model = self.store.find_model(model_id)
            if not model:
                self._send_text("model not found", status=404)
                return
            model.usable = not model.usable
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
        if parsed.path == "/api/test":
            group = self._require_route_group()
            if not group:
                return
            payload = self._read_json()
            path = str(payload.get("path", "/v1/chat/completions"))
            body = payload.get("body") or {"messages": [{"role": "user", "content": "ping"}]}
            try:
                status, headers, result = self.router.call(path, body, group.id if group else None)
                self._send_json({"status": status, "headers": headers, "body": result.decode("utf-8", "ignore")})
            except Exception as err:
                self._send_text(str(err), status=500)
            return
        if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):
            group = self._require_route_group()
            if not group:
                return
            payload = self._read_json()
            stream = bool(payload.get("stream"))
            try:
                if stream:
                    status, headers, iterator = self.router.stream(parsed.path, payload, group.id if group else None)
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
                status, headers, data = self.router.call(parsed.path, payload, group.id if group else None)
                self.send_response(status)
                for key, value in headers.items():
                    if key.lower() in {"content-length", "connection", "transfer-encoding"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Lin Router proxy UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=DEFAULT_START_PORT, type=int)
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE)
    args = parser.parse_args()

    config_path = Path(args.config)
    ensure_sample_config(config_path)
    store = ConfigStore(config_path)
    router = ArkProxyRouter(store)
    port = pick_port(args.port, args.host)

    server = ThreadingHTTPServer((args.host, port), RouterHandler)
    server.store = store  # type: ignore[attr-defined]
    server.router = router  # type: ignore[attr-defined]

    print(f"Lin Router running on http://{args.host}:{port}")
    print(f"Config file: {config_path.resolve()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
