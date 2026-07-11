from __future__ import annotations

import ast
import io
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import app
from app import ConfigStore, RouterHandler
from linrouter_core.runtime.config_api_runtime import (
    ConfigApiError,
    export_backup_payload,
    export_config_payload,
    import_backup_payload,
    import_config_payload,
)


class FakeSettingsStore:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {"theme": "dark", "ignored": "keep"}
        self.updates: list[dict[str, Any]] = []

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        self.updates.append(dict(values))
        self.values.update(values)
        return dict(self.values)


class FakePlatform:
    def __init__(self) -> None:
        self.autostart = False
        self.calls: list[bool] = []

    def set_autostart(self, enabled: bool) -> None:
        self.calls.append(enabled)
        self.autostart = enabled

    def is_autostart_enabled(self) -> bool:
        return self.autostart


class FakeRouter:
    def __init__(self) -> None:
        self.refreshes = 0

    def _refresh_upstream_client(self) -> None:
        self.refreshes += 1


class FakeHandler:
    def __init__(self, path: str, store: ConfigStore, router: FakeRouter, settings: FakeSettingsStore, payload: Any) -> None:
        self.path = path
        self.store = store
        self.router = router
        self.server = SimpleNamespace(settings_store=settings)
        self.payload = payload
        self.wfile = io.BytesIO()
        self.responses: list[int] = []
        self.headers: list[tuple[str, str]] = []

    def _read_multipart_json(self) -> Any:
        return None

    def _read_json(self) -> Any:
        return self.payload

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self.responses.append(status)
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def send_response(self, status: int) -> None:
        self.responses.append(status)

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        pass


def _payload() -> dict[str, Any]:
    return {
        "groups": [{"id": "g1", "name": "group", "route_key": "rk"}],
        "models": [{"id": "m1", "name": "model", "ep_id": "endpoint", "group_id": "g1"}],
        "aggregate_models": [{"id": "a1", "name": "aggregate"}],
        "aggregate_members": [{"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1"}],
    }


def _snapshot(store: ConfigStore) -> dict[str, Any]:
    return export_config_payload(store)


def test_m4b_config_export_import_reloads_equivalently_and_merges_by_id(tmp_path: Path) -> None:
    source = ConfigStore(tmp_path / "source.json")
    assert import_config_payload(source, _payload())["ok"] is True
    source = ConfigStore(source.path)
    exported = export_config_payload(source)

    target = ConfigStore(tmp_path / "target.json")
    assert import_config_payload(target, {
        "groups": [{"id": "existing", "name": "existing"}],
        "models": [],
    })["groups"] == 1
    result = import_config_payload(target, exported)
    reloaded = ConfigStore(target.path)
    reloaded_again = ConfigStore(target.path)

    assert result == {"ok": True, "groups": 2, "models": 1, "aggregate_models": 1, "aggregate_members": 1, "skipped_aggregates": []}
    assert _snapshot(reloaded_again) == _snapshot(reloaded)
    assert _snapshot(target)["groups"][1:] == exported["groups"]


def test_m4b_backup_import_overwrites_and_keeps_settings_whitelist(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    import_config_payload(store, _payload())
    backup = export_backup_payload(store, FakeSettingsStore({"theme": "dark", "debug_mode": True}))
    backup["settings"].update({"auto_start": True, "upstream_http2": True, "secret": "discard"})

    restored = ConfigStore(tmp_path / "restored.json")
    import_config_payload(restored, {"groups": [{"id": "old", "name": "old"}], "models": []})
    response, settings = import_backup_payload(restored, backup)

    assert response == {"ok": True, "groups": 1, "models": 1, "aggregate_models": 1, "aggregate_members": 1}
    assert _snapshot(restored) == _snapshot(store)
    assert settings == {"theme": "dark", "debug_mode": True, "auto_start": True, "upstream_http2": True}


def test_m4b_invalid_input_preserves_chinese_message_and_machine_error_fields(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    for func, expected_message, expected_code in (
        (import_config_payload, "配置文件无效：必须是一个 JSON 对象", "invalid_config_file"),
        (import_backup_payload, "备份文件无效：必须是一个 JSON 对象", "invalid_backup_file"),
    ):
        try:
            func(store, [])
        except ConfigApiError as error:
            assert error.response() == {"error": {"message": expected_message, "type": "invalid_request_error", "code": expected_code}}
        else:
            raise AssertionError("invalid payload must raise ConfigApiError")


def test_m4b_handler_keeps_download_headers_and_backup_side_effects(tmp_path: Path, monkeypatch: Any) -> None:
    store = ConfigStore(tmp_path / "config.json")
    import_config_payload(store, _payload())
    settings = FakeSettingsStore({"theme": "light"})
    router = FakeRouter()

    config_export = FakeHandler("/api/config/export", store, router, settings, None)
    RouterHandler.do_GET(config_export)
    assert config_export.responses == [200]
    assert ("Content-Disposition", 'attachment; filename="lin-router-config-export.json"') in config_export.headers
    assert json.loads(config_export.wfile.getvalue()) == export_config_payload(store)

    backup_export = FakeHandler("/api/backup/export", store, router, settings, None)
    RouterHandler.do_GET(backup_export)
    assert backup_export.responses == [200]
    assert ("Content-Disposition", 'attachment; filename="lin-router-backup.json"') in backup_export.headers

    platform = FakePlatform()
    monkeypatch.setattr(app, "get_platform", lambda: platform)
    backup = export_backup_payload(store, settings)
    backup["settings"].update({"auto_start": True, "upstream_http_client": "urllib", "unexpected": 1})
    importer = FakeHandler("/api/backup/import", store, router, settings, backup)
    RouterHandler.do_POST(importer)
    response = json.loads(importer.wfile.getvalue())

    assert importer.responses == [200]
    assert platform.calls == [True]
    assert router.refreshes == 1
    assert settings.updates == [{"theme": "light", "auto_start": True, "upstream_http_client": "urllib"}]
    assert response["settings"]["auto_start"] is True


def test_m4b_only_approved_handler_branches_delegate_to_config_runtime() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "RouterHandler")
    methods = {node.name: node for node in handler.body if isinstance(node, ast.FunctionDef)}
    get_source = ast.unparse(methods["do_GET"])
    post_source = ast.unparse(methods["do_POST"])

    assert get_source.count("export_config_payload(") == 1
    assert get_source.count("export_backup_payload(") == 1
    assert post_source.count("import_config_payload(") == 1
    assert post_source.count("import_backup_payload(") == 1
    for method in ("do_PUT", "do_DELETE"):
        assert "config_api_runtime" not in ast.unparse(methods[method])
