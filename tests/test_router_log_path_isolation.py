"""未显式指定日志路径时，临时配置不得把请求日志写入仓库根目录。"""
from __future__ import annotations

import json
from pathlib import Path

from app import ArkProxyRouter, ConfigStore


def test_default_log_file_is_sibling_of_config_store_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"groups": [], "models": []}), encoding="utf-8")

    router = ArkProxyRouter(ConfigStore(config_path), settings_store=None)

    assert router.log_file == tmp_path / "lin-router-logs.jsonl"
