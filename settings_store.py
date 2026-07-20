from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict


DEFAULT_SETTINGS = {
    "auto_start": False,
    "start_minimized": False,
    "theme": "system",
    "auto_refresh_logs": True,
    "debug_mode": False,
    "upstream_http_client": "urllib",
    "upstream_http2": False,
    "upstream_keepalive": False,
    "debug_capture_enabled": False,
    "debug_capture_last_body": False,
    "normalize_tools_order": False,
    # 冻结 PRD 要求新安装及缺失历史字段均默认启用；显式 False 仍可持久化关闭。
    "smart_breaker_enabled": True,
}


class SettingsStore:
    """独立存放用户设置，避免污染 lin-router-config.json。"""

    def __init__(self, config_path: Path) -> None:
        # settings 文件与配置文件放在同一目录，便于一起迁移
        self.path = config_path.parent / "lin-router-settings.json"
        self._lock = threading.RLock()
        self._settings: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._settings = {**DEFAULT_SETTINGS, **raw}
                # 兼容旧配置：将已废弃的 debug_capture_body 迁移到 PRD 指定的 debug_capture_last_body
                if "debug_capture_body" in self._settings:
                    self._settings["debug_capture_last_body"] = bool(self._settings.pop("debug_capture_body"))
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def update(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            previous = dict(self._settings)
            self._settings = {**DEFAULT_SETTINGS, **self._settings, **new_settings}
            try:
                self.save()
            except Exception:
                # 页面依赖失败回滚；不能让内存值与落盘设置出现不同步。
                self._settings = previous
                raise
            return dict(self._settings)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._settings)
