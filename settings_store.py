from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict


DEFAULT_SETTINGS = {"auto_start": False, "start_minimized": False, "theme": "system", "auto_refresh_logs": True}


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
            self._settings = {**DEFAULT_SETTINGS, **self._settings, **new_settings}
            self.save()
            return dict(self._settings)

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._settings)
