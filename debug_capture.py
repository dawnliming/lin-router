from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CAPTURE_DIR = Path(".tmp") / "cache-debug"
CAPTURE_FILE = CAPTURE_DIR / "latest.json"


class DebugCapture:
    """缓存诊断捕获与重放。

    仅用于本地调试，不进入 git，不写入日志正文。
    """

    def __init__(
        self,
        router: Any,
        settings_store: Any,
        *,
        browser_user_agent: str = "",
        ssl_context: Any = None,
        empty_usage: Any = None,
        usage_from_stream_chunk: Any = None,
    ) -> None:
        self.router = router
        self.settings_store = settings_store
        # 保留旧的两参数构造兼容：有 Router 时从其注入的诊断依赖恢复原 replay 语义；
        # app.py 的显式参数仍优先，避免本模块反向 import app。
        self._browser_user_agent = browser_user_agent or getattr(router, "_debug_capture_browser_user_agent", "")
        self._ssl_context = ssl_context if ssl_context is not None else getattr(router, "_debug_capture_ssl_context", None)
        self._empty_usage = empty_usage or getattr(router, "_empty_usage", None) or (lambda: (0, 0, 0, 0, 0))
        self._usage_from_stream_chunk = (
            usage_from_stream_chunk
            or getattr(router, "_usage_from_stream_chunk", None)
            or self._empty_usage
        )

    def _enabled(self) -> bool:
        if self.settings_store is None:
            return False
        return bool(self.settings_store.get("debug_capture_enabled", False))

    def _capture_body(self) -> bool:
        if self.settings_store is None:
            return False
        return bool(self.settings_store.get("debug_capture_last_body", False))

    @staticmethod
    def _ensure_dir() -> None:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _header_summary(headers: Dict[str, str]) -> Dict[str, Any]:
        lower = {k.lower(): v for k, v in headers.items()}
        ua = lower.get("user-agent", "")
        ua_family = "other"
        ua_lower = ua.lower()
        if "codex" in ua_lower:
            ua_family = "codex"
        elif any(k in ua_lower for k in ("chrome", "safari", "firefox", "edge", "mozilla")):
            ua_family = "browser"
        return {
            "accept": lower.get("accept", ""),
            "content_type": lower.get("content-type", ""),
            "user_agent_family": ua_family,
        }

    def capture(
        self,
        path: str,
        group: Any,
        model: str,
        target_model: str,
        body: bytes,
        body_mode: str,
        headers: Dict[str, str],
        fingerprint: str,
        request_id: str,
        usage_source: str = "",
    ) -> None:
        if not self._enabled():
            return
        try:
            self._ensure_dir()
            snapshot: Dict[str, Any] = {
                "captured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "request_id": request_id,
                "path": path,
                "group_id": getattr(group, "id", ""),
                "group_name": getattr(group, "name", ""),
                "provider_type": getattr(group, "provider_type", ""),
                "model": model,
                "target_model": target_model,
                "body_mode": body_mode,
                "body_sha256": self._sha256(body),
                "body_len": len(body),
                "fingerprint": fingerprint,
                "headers_summary": self._header_summary(headers),
                "usage_source": usage_source,
            }
            if self._capture_body():
                snapshot["body_base64"] = base64.b64encode(body).decode("ascii")
            with CAPTURE_FILE.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _sha256(data: bytes) -> str:
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def load_capture(self) -> Optional[Dict[str, Any]]:
        try:
            if not CAPTURE_FILE.exists():
                return None
            with CAPTURE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def replay(
        self,
        count: int = 10,
        client_type: Optional[str] = None,
        waf_off_variant: bool = False,
    ) -> List[Dict[str, Any]]:
        capture = self.load_capture()
        if capture is None:
            return [{"error": "no capture found"}]
        try:
            return self._replay_capture(capture, count, client_type, waf_off_variant)
        except Exception as exc:
            return [{"error": str(exc)}]

    def _replay_capture(
        self,
        capture: Dict[str, Any],
        count: int,
        client_type: Optional[str],
        waf_off_variant: bool,
    ) -> List[Dict[str, Any]]:
        from upstream_client import UpstreamClient

        group_id = capture.get("group_id", "")
        group = self.router.store.find_group(group_id) if group_id else None
        if group is None:
            return [{"error": f"group {group_id} not found"}]
        path = capture.get("path", "/v1/chat/completions")
        target_model = capture.get("target_model", "")
        body_base64 = capture.get("body_base64")
        if body_base64:
            body = base64.b64decode(body_base64)
        else:
            return [{"error": "capture does not include body; enable debug_capture_last_body"}]

        # 构造 headers
        headers_summary = capture.get("headers_summary", {})
        headers: Dict[str, str] = {
            "content-type": headers_summary.get("content_type", "application/json"),
            "accept": headers_summary.get("accept", "text/event-stream"),
        }
        if not waf_off_variant and group.waf_compatible:
            # WAF 模式下补浏览器 UA
            headers["user-agent"] = self._browser_user_agent

        # 找 auth key
        model = self.router.store.find_model_by_group_ep(group.id, target_model)
        auth_key = self.router._auth_for(group, model)
        if not auth_key:
            return [{"error": "missing upstream api key"}]
        headers["authorization"] = f"Bearer {auth_key}"

        # 构造临时 group 以切换 WAF（仅内存中，不改配置）
        replay_group = group
        if waf_off_variant:
            import dataclasses
            replay_group = dataclasses.replace(group, waf_compatible=False)
            headers = {
                "content-type": headers.get("content-type", "application/json"),
                "accept": headers.get("accept", "text/event-stream"),
                "authorization": headers["authorization"],
            }

        upstream_client = self.router._upstream_client
        if client_type:
            http2 = bool(self.settings_store.get("upstream_http2", False)) if self.settings_store else False
            keepalive = bool(self.settings_store.get("upstream_keepalive", False)) if self.settings_store else False
            upstream_client = UpstreamClient(client_type=client_type, http2=http2, keepalive=keepalive, ssl_context=self._ssl_context)

        target_url = self.router._resolve_url(replay_group.base_url, path)
        results: List[Dict[str, Any]] = []
        waf_off_unusable = False
        for i in range(max(1, min(count, 50))):
            result = self._single_replay(upstream_client, target_url, headers, body, path, replay_group, i + 1)
            if result.get("status") == 429 and waf_off_variant:
                waf_off_unusable = True
                break
            results.append(result)
        if waf_off_unusable:
            for r in results:
                r["waf_off_unusable"] = True
        if client_type and upstream_client is not self.router._upstream_client:
            upstream_client.close()
        return results

    def _single_replay(
        self,
        upstream_client: Any,
        target_url: str,
        headers: Dict[str, str],
        body: bytes,
        path: str,
        group: Any,
        idx: int,
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        try:
            resp = upstream_client.request("POST", target_url, headers, body, stream=True, timeout=120)
        except Exception as exc:
            return {"index": idx, "status": "error", "error": str(exc)}
        usage_total = self._empty_usage()
        status = getattr(resp, "status", 200)
        http_version = getattr(resp, "http_version", "")
        try:
            while True:
                chunk = resp.readline(120)
                if not chunk:
                    break
                usage = self._usage_from_stream_chunk(chunk)
                if any(usage):
                    usage_total = usage
        except Exception:
            pass
        finally:
            resp.close()
        prompt_tokens, completion_tokens, total_tokens, cached_tokens, reasoning_tokens = usage_total
        hit_rate = cached_tokens / prompt_tokens if prompt_tokens > 0 else 0
        return {
            "index": idx,
            "status": status,
            "http_version": http_version,
            "http_client": getattr(upstream_client, "client_type", "unknown"),
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "total_tokens": total_tokens,
            "hit_rate": round(hit_rate, 4),
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
        }
