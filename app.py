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
MAX_PORT_SCAN = 1


@dataclass
class ConnectionGroup:
    id: str
    name: str
    base_url: str = DEFAULT_BASE_URL
    ark_api_key: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionGroup":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            name=data["name"],
            base_url=data.get("base_url") or DEFAULT_BASE_URL,
            ark_api_key=data.get("ark_api_key") or "",
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

        if not isinstance(models_raw, list):
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
            changed = False
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

    def find_model(self, model_id: str) -> Optional[ModelConfig]:
        return next((m for m in self.models if m.id == model_id), None)


class ArkProxyRouter:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self.logs: List[RequestLog] = []

    def add_log(self, path: str, model: str, status: str, detail: str = "") -> None:
        self.logs.insert(0, RequestLog(self._now(), path, model, status, detail[:300]))
        del self.logs[80:]

    def recent_logs(self) -> List[Dict[str, str]]:
        return [asdict(item) for item in self.logs[:30]]

    def clear_logs(self) -> None:
        self.logs.clear()

    def export_logs_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time", "path", "model", "status", "detail"])
        for item in self.logs:
            writer.writerow([item.time, item.path, item.model, item.status, item.detail])
        return output.getvalue()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    @staticmethod
    def _is_quota_exhausted(status_code: Optional[int], body: str) -> bool:
        return status_code == 429 and "QuotaExceeded" in body and "free trial quota exhausted" in body

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

    def default_model(self) -> Optional[ModelConfig]:
        return next((m for m in self.store.models if m.usable), None)

    def _iter_candidates(self, requested_model: str | None) -> Iterator[Tuple[int, ModelConfig]]:
        for idx, model in enumerate(self.store.models):
            if not model.usable:
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

    def call(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], bytes]:
        requested_model = payload.get("model")
        last_error: Optional[Exception] = None

        for idx, model in self._iter_candidates(str(requested_model) if requested_model else None):
            group = self._group_for(model)
            if not group or not group.ark_api_key:
                continue
            target_url = self._resolve_url(group.base_url, path)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
            try:
                with urlopen(request, timeout=120) as resp:
                    data = resp.read()
                    self._set_success(idx)
                    self.add_log(path, model.name, str(resp.status), "ok")
                    return resp.status, dict(resp.headers.items()), data
            except HTTPError as err:
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                if self._is_quota_exhausted(err.code, raw):
                    self._set_unusable(idx, raw)
                    self.add_log(path, model.name, str(err.code), "quota exhausted")
                    continue
                if self._is_rate_limited(err.code, raw):
                    try:
                        with urlopen(request, timeout=120) as resp:
                            data = resp.read()
                            self._set_success(idx)
                            self.add_log(path, model.name, str(resp.status), "retry ok")
                            return resp.status, dict(resp.headers.items()), data
                    except Exception as retry_err:
                        last_error = retry_err
                        self.add_log(path, model.name, "retry failed", str(retry_err))
                        continue
                if self._is_server_error(err.code):
                    self.add_log(path, model.name, str(err.code), "server error, try next")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                self.add_log(path, model.name, str(err.code), raw)
                return err.code, headers, raw.encode("utf-8")
            except (URLError, TimeoutError, OSError) as err:
                last_error = err
                self.add_log(path, model.name, "network", str(err))
                continue

        if last_error is None:
            raise RuntimeError("No usable models available")
        raise RuntimeError("All available models failed") from last_error

    def stream(self, path: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, str], Iterable[bytes]]:
        requested_model = payload.get("model")
        last_error: Optional[Exception] = None

        for idx, model in self._iter_candidates(str(requested_model) if requested_model else None):
            group = self._group_for(model)
            if not group or not group.ark_api_key:
                continue
            target_url = self._resolve_url(group.base_url, path)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
            try:
                resp = urlopen(request, timeout=120)
                self._set_success(idx)
                self.add_log(path, model.name, "200", "stream ok")

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
                raw = err.read().decode("utf-8", "ignore") if hasattr(err, "read") else str(err)
                last_error = err
                if self._is_quota_exhausted(err.code, raw):
                    self._set_unusable(idx, raw)
                    self.add_log(path, model.name, str(err.code), "quota exhausted")
                    continue
                if self._is_rate_limited(err.code, raw):
                    self.add_log(path, model.name, str(err.code), "rate limited")
                    continue
                if self._is_server_error(err.code):
                    self.add_log(path, model.name, str(err.code), "server error, try next")
                    continue
                headers = dict(getattr(err, "headers", {}) or {})
                self.add_log(path, model.name, str(err.code), raw)
                return err.code, headers, [raw.encode("utf-8")]
            except (URLError, TimeoutError, OSError) as err:
                last_error = err
                self.add_log(path, model.name, "network", str(err))
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
    :root { color-scheme: light; --bg:#f5f7fb; --panel:#fff; --line:#d6dce8; --text:#18212f; --muted:#5b6575; --accent:#2358ff; --danger:#c62828; --ok:#16794c; --warn:#a15c00; }
    * { box-sizing: border-box; }
    body { margin:0; font:14px/1.5 system-ui, -apple-system, Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--text); }
    header { height:58px; padding:0 22px; display:flex; align-items:center; justify-content:space-between; background:#fff; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:18px; }
    h2 { margin:0 0 12px; font-size:15px; }
    .shell { padding:18px; display:grid; grid-template-columns:360px 1fr; gap:18px; }
    .main { display:grid; gap:18px; }
    .side { display:grid; gap:18px; align-content:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:14px; }
    .hero { grid-column:1 / -1; display:grid; grid-template-columns:1fr auto; gap:16px; align-items:center; }
    .heroUrl { padding:10px 12px; border:1px solid #c8d2ff; background:#f1f4ff; border-radius:6px; font-family:Consolas, monospace; }
    label { display:block; margin:10px 0 6px; color:var(--muted); }
    input, textarea, select, button { font:inherit; }
    input, textarea, select { width:100%; border:1px solid var(--line); border-radius:6px; padding:9px 10px; background:#fff; color:var(--text); }
    textarea { min-height:104px; resize:vertical; }
    button { border:1px solid var(--line); background:#fff; color:var(--text); border-radius:6px; padding:8px 10px; cursor:pointer; }
    button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
    button.danger { color:var(--danger); }
    .row { display:flex; gap:8px; flex-wrap:wrap; }
    .row > * { flex:1 1 auto; }
    .muted { color:var(--muted); }
    .tiny { font-size:12px; }
    .status { padding:10px 12px; background:#f1f4ff; border:1px solid #cad4ff; border-radius:6px; margin-bottom:12px; }
    .groupList { display:grid; gap:8px; margin-top:10px; }
    .groupItem { border:1px solid var(--line); border-radius:6px; padding:10px; display:grid; gap:6px; }
    table { width:100%; border-collapse:collapse; }
    th, td { padding:9px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
    th { color:var(--muted); font-weight:600; }
    td.actions { white-space:nowrap; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; background:#edf7f1; color:var(--ok); font-size:12px; }
    .pill.off { background:#fff3e6; color:var(--warn); }
    .log { white-space:pre-wrap; background:#0f172a; color:#d9e2ff; border-radius:6px; padding:12px; min-height:120px; max-height:260px; overflow:auto; }
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
        <div class="muted tiny" style="margin-top:8px">Base URL 填这里；API Key 可填 lin-router；模型可留空，由路由器自动选择可用模型。</div>
      </div>
      <button type="button" id="copyHermesBtn">复制地址</button>
    </section>

    <aside class="side">
      <section class="panel">
        <h2>连接组</h2>
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
        <div class="muted tiny" style="margin-top:10px">同一个 base/key 只需要建一次，多个模型可以共用。</div>
        <div class="groupList" id="groupList"></div>
      </section>

      <section class="panel">
        <h2>模型配置</h2>
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
      </section>

      <section class="panel">
        <h2>批量导入</h2>
        <label>连接组</label>
        <select id="batchGroupPick"></select>
        <label>模型列表</label>
        <textarea id="batchModels" placeholder="模型名称,ep-xxxx&#10;另一个模型,ep-yyyy"></textarea>
        <div class="row" style="margin-top:12px">
          <button class="primary" type="button" id="batchImportBtn">批量导入模型</button>
        </div>
      </section>
    </aside>

    <main class="main">
      <section class="panel">
        <h2>模型列表</h2>
        <div class="status" id="summaryBox">加载中...</div>
        <table>
          <thead><tr><th>优先级</th><th>名称</th><th>EP</th><th>组</th><th>状态</th><th>最近结果</th><th>操作</th></tr></thead>
          <tbody id="modelTbody"></tbody>
        </table>
      </section>

      <section class="panel">
        <h2>代理测试</h2>
        <label>测试模型</label>
        <select id="testModel"></select>
        <label>请求路径</label>
        <input id="proxyPath" value="/v1/chat/completions">
        <label>请求体</label>
        <textarea id="proxyBody">{ "messages": [{"role":"user","content":"hello"}], "temperature": 0.2 }</textarea>
        <div class="row" style="margin-top:12px"><button class="primary" type="button" id="sendTest">发送测试</button></div>
      </section>

      <section class="panel">
        <h2>返回结果</h2>
        <div class="log" id="logBox">等待操作。</div>
      </section>

      <section class="panel">
        <h2>最近请求</h2>
        <div class="row" style="margin-bottom:10px">
          <button type="button" id="clearLogsBtn">清空日志</button>
          <button type="button" id="exportLogsBtn">导出 CSV</button>
        </div>
        <table>
          <thead><tr><th>时间</th><th>模型</th><th>状态</th><th>详情</th></tr></thead>
          <tbody id="logTbody"></tbody>
        </table>
      </section>
    </main>
  </div>
  <script>
    const $ = (id) => document.getElementById(id);
    let state = { groups: [], models: [], logs: [] };
    function log(text) { $('logBox').textContent = text; }
    function esc(text) { return String(text ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s])); }
    function fillGroupPick() {
      const groupOptions = state.groups.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
      $('groupPick').innerHTML = groupOptions;
      $('batchGroupPick').innerHTML = groupOptions;
      $('testModel').innerHTML = ['<option value="">自动选择第一个可用模型</option>']
        .concat(state.models.map(m => `<option value="${esc(m.ep_id)}">${esc(m.name)} · ${esc(m.ep_id)}</option>`))
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
    function renderGroups() {
      $('groupList').innerHTML = state.groups.map(g => `
        <div class="groupItem">
          <strong>${esc(g.name)}</strong>
          <div class="tiny muted">${esc(g.base_url)}</div>
          <div class="tiny muted">Key: ${g.ark_api_key ? '已填写' : '未填写'}</div>
          <div class="row">
            <button type="button" data-group-edit="${g.id}">编辑</button>
            <button type="button" class="danger" data-group-del="${g.id}">删除</button>
          </div>
        </div>
      `).join('') || '<div class="muted">暂无连接组</div>';
      document.querySelectorAll('[data-group-edit]').forEach(btn => btn.onclick = () => fillGroupForm(state.groups.find(x => x.id === btn.dataset.groupEdit)));
      document.querySelectorAll('[data-group-del]').forEach(btn => btn.onclick = () => mutate(`/api/groups/${btn.dataset.groupDel}`, {}, 'DELETE'));
    }
    async function refresh() {
      const resp = await fetch('/api/state');
      state = await resp.json();
      $('serverInfo').textContent = `${location.origin} · config: ${state.config_file}`;
      $('hermesUrl').textContent = `${location.origin}/v1`;
      $('summaryBox').textContent = `组 ${state.groups.length} · 模型 ${state.models.length} · 可用 ${state.models.filter(m => m.usable).length}`;
      fillGroupPick();
      renderGroups();
      $('modelTbody').innerHTML = state.models.map((m, index) => `
        <tr>
          <td>${index + 1}</td>
          <td>${esc(m.name)}</td>
          <td class="tiny">${esc(m.ep_id)}</td>
          <td class="tiny">${esc((state.groups.find(g => g.id === m.group_id) || {}).name || '-')}</td>
          <td><span class="pill ${m.usable ? '' : 'off'}">${m.usable ? '可用' : '停用'}</span></td>
          <td class="tiny">${esc(m.last_error || m.last_success_at || '-')}</td>
          <td class="actions">
            <button type="button" data-edit="${m.id}">编辑</button>
            <button type="button" data-move-up="${m.id}">上移</button>
            <button type="button" data-move-down="${m.id}">下移</button>
            <button type="button" data-toggle="${m.id}">${m.usable ? '停用' : '启用'}</button>
            <button type="button" data-del="${m.id}" class="danger">删除</button>
          </td>
        </tr>
      `).join('');
      document.querySelectorAll('[data-edit]').forEach(btn => btn.onclick = () => fillForm(state.models.find(x => x.id === btn.dataset.edit)));
      document.querySelectorAll('[data-move-up]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.moveUp}/move`, {direction:'up'}));
      document.querySelectorAll('[data-move-down]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.moveDown}/move`, {direction:'down'}));
      document.querySelectorAll('[data-toggle]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.toggle}/toggle`, {}));
      document.querySelectorAll('[data-del]').forEach(btn => btn.onclick = () => mutate(`/api/models/${btn.dataset.del}`, {}, 'DELETE'));
      $('logTbody').innerHTML = (state.logs || []).map(item => `
        <tr><td class="tiny">${esc(item.time)}</td><td>${esc(item.model)}</td><td>${esc(item.status)}</td><td class="tiny">${esc(item.detail)}</td></tr>
      `).join('') || '<tr><td colspan="4" class="muted">暂无请求</td></tr>';
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
    $('sendTest').onclick = async () => {
      const payload = JSON.parse($('proxyBody').value);
      const selectedModel = $('testModel').value;
      if (selectedModel) payload.model = selectedModel;
      const resp = await fetch($('proxyPath').value, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
      const text = await resp.text();
      log(`HTTP ${resp.status}\n${text}`);
    };
    refresh().catch(err => log(String(err)));
  </script>
</body>
</html>
"""


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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(PAGE_HTML, content_type="text/html; charset=utf-8")
            return
        if parsed.path in {"/v1/models", "/models"}:
            self._send_json({
                "object": "list",
                "data": [
                    {
                        "id": model.ep_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "lin-router",
                        "permission": [],
                        "root": model.ep_id,
                        "parent": None,
                        "display_name": model.name,
                    }
                    for model in self.store.models
                    if model.usable
                ],
            })
            return
        if parsed.path == "/api/state":
            self._send_json({
                "config_file": str(self.store.path),
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
            payload = self._read_json()
            path = str(payload.get("path", "/v1/chat/completions"))
            body = payload.get("body") or {"messages": [{"role": "user", "content": "ping"}]}
            if not body.get("model"):
                default_model = self.router.default_model()
                if default_model:
                    body["model"] = default_model.ep_id
            try:
                status, headers, result = self.router.call(path, body)
                self._send_json({"status": status, "headers": headers, "body": result.decode("utf-8", "ignore")})
            except Exception as err:
                self._send_text(str(err), status=500)
            return
        if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):
            payload = self._read_json()
            if not payload.get("model"):
                default_model = self.router.default_model()
                if default_model:
                    payload["model"] = default_model.ep_id
            stream = bool(payload.get("stream"))
            try:
                if stream:
                    status, headers, iterator = self.router.stream(parsed.path, payload)
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
                status, headers, data = self.router.call(parsed.path, payload)
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
