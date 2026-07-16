"""运行态 scope / revision / activity_cursor 的隔离 HTTP 契约。"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app import create_server
from linrouter_core.observability import ObservabilityService


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get_json(port: int, path: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _write_config(path: Path) -> None:
    path.write_text(json.dumps({
        "groups": [{
            "id": "g1", "name": "relay", "provider_type": "relay",
            "base_url": "https://relay.example/v1", "route_key": "lr-g1",
        }],
        "models": [{
            "id": "m1", "name": "model", "ep_id": "model", "group_id": "g1",
            "upstream_model": "model", "api_key": "stored-test-key",
        }],
        "aggregate_models": [],
        "aggregate_members": [],
    }, ensure_ascii=False), encoding="utf-8")


def test_runtime_state_scope_preserves_legacy_shape_and_returns_incremental_dashboard_activity(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    _write_config(config_path)
    server, port, _ = create_server("127.0.0.1", _free_port(), config_path)
    server.router.log_file = tmp_path / "logs.jsonl"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.router.add_log("/v1/chat/completions", "model", "200", "selected_model=model", event="ok", request_id="first")

        status, legacy = _get_json(port, "/api/runtime-state")
        assert status == 200
        assert {"models", "aggregate_members", "logs", "live_requests", "log_write_error"} <= set(legacy)

        status, config = _get_json(port, "/api/runtime-state?scope=config")
        assert status == 200
        assert config["scope"] == "config"
        assert config["changed"] is True
        assert "logs" not in config
        assert "activity" not in config

        status, config_unchanged = _get_json(port, f"/api/runtime-state?scope=config&revision={config['revision']}")
        assert status == 200
        assert config_unchanged["changed"] is False
        assert "models" not in config_unchanged
        assert "aggregate_members" not in config_unchanged

        status, dashboard = _get_json(port, "/api/runtime-state?scope=dashboard")
        assert status == 200
        assert dashboard["activity"]["mode"] == "snapshot"
        assert dashboard["activity"]["logs"][0]["request_id"] == "first"

        status, dashboard_unchanged = _get_json(
            port,
            f"/api/runtime-state?scope=dashboard&revision={dashboard['revision']}&activity_cursor={dashboard['activity_cursor']}",
        )
        assert status == 200
        assert dashboard_unchanged["changed"] is False
        assert dashboard_unchanged["activity"] == {
            "cursor": dashboard["activity_cursor"], "changed": False, "mode": "delta", "logs": [],
        }

        server.router.add_log("/v1/chat/completions", "model", "200", "selected_model=model", event="ok", request_id="second")
        status, dashboard_delta = _get_json(
            port,
            f"/api/runtime-state?scope=dashboard&revision={dashboard['revision']}&activity_cursor={dashboard['activity_cursor']}",
        )
        assert status == 200
        assert dashboard_delta["changed"] is True
        assert dashboard_delta["activity"]["mode"] == "delta"
        assert [item["request_id"] for item in dashboard_delta["activity"]["logs"]] == ["second"]

        server.router._live_request_start("live-1", "/v1/chat/completions", "model", stream=True)
        status, active = _get_json(port, f"/api/runtime-state?scope=dashboard&revision={dashboard_delta['revision']}&activity_cursor={dashboard_delta['activity_cursor']}")
        assert status == 200
        assert active["next_poll_ms"] == 1000
        assert active["live_requests"][0]["request_id"] == "live-1"

        status, invalid = _get_json(port, "/api/runtime-state?scope=logs")
        assert status == 400
        assert invalid["error"]["code"] == "invalid_runtime_scope"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_activity_cursor_reports_stream_terminal_update_and_log_reset(tmp_path: Path) -> None:
    service = ObservabilityService(
        tmp_path / "logs.jsonl",
        now=lambda: "2026-07-15 12:00:00",
        sanitize_detail=lambda value: value,
    )
    service.add_log(
        "/v1/chat/completions", "model", "200", "stream_started_at_ms=1", event="stream_ok",
        request_id="stream-1", attempt=1,
    )
    cursor = service.runtime_activity_since()["cursor"]

    assert service.patch_stream_lifecycle(
        "stream-1", 1, "model", (10, 2, 12, 0, 0), "stream", final_status="200",
        lifecycle="stream_done", final_result="stream_done", chunks_received=2, bytes_received=20,
        final_event="stream_done", completion_signal="[DONE]",
    ) is True
    delta = service.runtime_activity_since(cursor)
    assert delta["mode"] == "delta"
    assert delta["logs"][0]["event"] == "stream_done"
    assert "final_result=stream_done" in delta["logs"][0]["detail"]

    service.clear_logs()
    reset = service.runtime_activity_since(delta["cursor"])
    assert reset["mode"] == "snapshot"
    assert reset["changed"] is True
    assert reset["logs"] == []
