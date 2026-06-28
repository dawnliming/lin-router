from __future__ import annotations

import argparse
import csv
import io
import json
import os
import socket
import sys
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
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
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
        detail = self._sanitize_detail(detail)
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
        return f"mode={mode}; hit={candidate.target_model}{channel}; requested={requested_label}; {suffix}"

    def _auth_for(self, group: ConnectionGroup, model: Optional[ModelConfig]) -> str:
        mode = self._mode_for(group)
        if mode == PROVIDER_RELAY:
            return model.api_key if model else ""
        if mode == PROVIDER_PROXY:
            return group.api_key or group.ark_api_key
        return group.ark_api_key

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

    def _iter_upstream_candidates(self, requested_model: str | None, group_id: str | None = None) -> Iterator[UpstreamCandidate]:
        if group_id:
            group = self.store.find_group(group_id)
            if not group:
                return
            matched = False
            for idx, model in self._iter_candidates(requested_model, group.id):
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

    def call(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None) -> Tuple[int, Dict[str, str], bytes]:
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = self._route_group_id(route)
        last_error: Optional[Exception] = None

        for candidate in self._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id):
            group = candidate.group
            target_url = self._resolve_url(group.base_url, path)
            outbound_payload = dict(payload)
            if not candidate.auth_key:
                self.add_log(path, candidate.label, "skip", f"requested={requested_label}; missing upstream api key")
                continue
            outbound_payload["model"] = candidate.target_model
            body = json.dumps(outbound_payload, ensure_ascii=False).encode("utf-8")
            request = Request(
                target_url,
                data=body,
                headers=build_upstream_headers(candidate.auth_key, stream=False),
                method="POST",
            )
            started_at = time.perf_counter()
            try:
                with urlopen(request, timeout=120) as resp:
                    data = resp.read()
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    prompt_tokens, completion_tokens, total_tokens = self._usage_from_response(data)
                    self._mark_success(candidate)
                    self.add_log(
                        path,
                        candidate.label,
                        str(resp.status),
                        self._candidate_hit_detail(candidate, requested_label, "ok"),
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
                    self._mark_unusable(candidate, raw)
                    self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, "quota exhausted, try next"), duration_ms)
                    continue
                if self._is_rate_limited(err.code, raw):
                    try:
                        retry_started_at = time.perf_counter()
                        with urlopen(request, timeout=120) as resp:
                            data = resp.read()
                            retry_duration_ms = int((time.perf_counter() - retry_started_at) * 1000)
                            prompt_tokens, completion_tokens, total_tokens = self._usage_from_response(data)
                            self._mark_success(candidate)
                            self.add_log(
                                path,
                                candidate.label,
                                str(resp.status),
                                self._candidate_hit_detail(candidate, requested_label, "retry ok"),
                                retry_duration_ms,
                                prompt_tokens,
                                completion_tokens,
                                total_tokens,
                            )
                            return resp.status, dict(resp.headers.items()), data
                    except Exception as retry_err:
                        last_error = retry_err
                        retry_duration_ms = int((time.perf_counter() - started_at) * 1000)
                        self.add_log(path, candidate.label, "retry failed", self._candidate_hit_detail(candidate, requested_label, str(retry_err)), retry_duration_ms)
                        continue
                if self._is_server_error(err.code):
                    self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, "server error, try next"), duration_ms)
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, raw), duration_ms)
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                self.add_log(path, candidate.label, "network", self._candidate_hit_detail(candidate, requested_label, str(err)), duration_ms)
                continue

        if last_error is None:
            raise RuntimeError("No usable models available")
        raise RuntimeError("All available models failed") from last_error

    def stream(self, path: str, payload: Dict[str, Any], route: RouteContext | str | None = None) -> Tuple[int, Dict[str, str], Iterable[bytes]]:
        requested_model = payload.get("model")
        requested_label = str(requested_model) if requested_model else DEFAULT_AUTO_MODEL_NAME
        group_id = self._route_group_id(route)
        last_error: Optional[Exception] = None

        for candidate in self._iter_upstream_candidates(str(requested_model) if requested_model else None, group_id):
            group = candidate.group
            target_url = self._resolve_url(group.base_url, path)
            outbound_payload = dict(payload)
            if not candidate.auth_key:
                self.add_log(path, candidate.label, "skip", f"requested={requested_label}; missing upstream api key")
                continue
            outbound_payload["model"] = candidate.target_model
            body = json.dumps(outbound_payload, ensure_ascii=False).encode("utf-8")
            request = Request(
                target_url,
                data=body,
                headers=build_upstream_headers(candidate.auth_key, stream=True),
                method="POST",
            )
            started_at = time.perf_counter()
            try:
                resp = urlopen(request, timeout=120)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                self._mark_success(candidate)
                self.add_log(path, candidate.label, "200", self._candidate_hit_detail(candidate, requested_label, "stream ok"), duration_ms)

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
                    self._mark_unusable(candidate, raw)
                    self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, "quota exhausted, try next"), duration_ms)
                    continue
                if self._is_rate_limited(err.code, raw):
                    self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, "rate limited, try next"), duration_ms)
                    continue
                if self._is_server_error(err.code):
                    self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, "server error, try next"), duration_ms)
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                self.add_log(path, candidate.label, str(err.code), self._candidate_hit_detail(candidate, requested_label, raw), duration_ms)
                return err.code, headers, [raw.encode("utf-8")]
            except (URLError, TimeoutError, OSError) as err:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                last_error = err
                self.add_log(path, candidate.label, "network", self._candidate_hit_detail(candidate, requested_label, str(err)), duration_ms)
                continue

        if last_error is None:
            raise RuntimeError("No usable models available")
        raise RuntimeError("All available models failed") from last_error





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
        request = Request(
            target_url,
            headers=build_upstream_headers(auth_key, stream=False),
            method="GET",
        )
        with urlopen(request, timeout=60) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError("Invalid upstream model list")
        return [item for item in data if isinstance(item, dict)]

    def _route_context(self) -> Optional[RouteContext]:
        key = parse_bearer_key(self.headers.get("Authorization", ""))
        if not key:
            return None
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
                        "provider_type": group.provider_type,
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
                        "provider_type": group.provider_type,
                        "price_group": model.price_group,
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
            if not payload.get("provider_type"):
                payload["provider_type"] = existing.provider_type if existing else PROVIDER_ARK
            if existing and "ark_api_key" not in payload:
                payload["ark_api_key"] = existing.ark_api_key
            if existing and "api_key" not in payload:
                payload["api_key"] = existing.api_key
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
            except HTTPError as err:
                body = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                self._send_text(body or f"upstream error {err.code}", status=err.code)
                return
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
            ctx = self._require_route_context()
            if not ctx:
                return
            payload = self._read_json()
            path = str(payload.get("path", "/v1/chat/completions"))
            body = payload.get("body") or {"messages": [{"role": "user", "content": "ping"}]}
            try:
                status, headers, result = self.router.call(path, body, ctx)
                self._send_json({"status": status, "headers": headers, "body": result.decode("utf-8", "ignore")})
            except Exception as err:
                self._send_text(str(err), status=500)
            return
        if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):
            ctx = self._require_route_context()
            if not ctx:
                return
            payload = self._read_json()
            stream = bool(payload.get("stream"))
            try:
                if stream:
                    status, headers, iterator = self.router.stream(parsed.path, payload, ctx)
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
                status, headers, data = self.router.call(parsed.path, payload, ctx)
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
    selected_port = pick_port(port, host)

    server = ThreadingHTTPServer((host, selected_port), RouterHandler)
    server.store = store  # type: ignore[attr-defined]
    server.router = router  # type: ignore[attr-defined]
    return server, selected_port, config_path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lin Router proxy UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=DEFAULT_START_PORT, type=int)
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE)
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

