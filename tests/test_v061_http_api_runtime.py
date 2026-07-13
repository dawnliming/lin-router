from __future__ import annotations

import json
from types import SimpleNamespace

from linrouter_core.observability.contracts import RequestLog
from linrouter_core.runtime.http_api_runtime import handle_get, handle_put


class _PutHandler:
    def __init__(self) -> None:
        self.path = "/api/groups/group-1"
        self._raw = '{"name":"更新后的中文名称"}'.encode("utf-8")
        self.post_paths: list[str] = []
        self.post_payloads: list[dict[str, str]] = []
        self.get_paths: list[str] = []

    def _read_raw_body(self) -> bytes:
        return self._raw

    def _read_json(self) -> dict[str, str]:
        return json.loads(self._read_raw_body().decode("utf-8"))

    def do_POST(self) -> None:
        self.post_paths.append(self.path)
        self.post_payloads.append(json.loads(self._put_body.decode("utf-8")))

    def do_GET(self) -> None:
        assert not hasattr(self, "_put_body")
        self.get_paths.append(self.path)

    def _send_json(self, *_args, **_kwargs) -> None:
        raise AssertionError("unexpected error response")


class _LogsHandler:
    def __init__(self, logs: list[RequestLog]) -> None:
        self.path = "/api/logs?offset=1&limit=2"
        self.router = SimpleNamespace(all_logs=lambda: logs)
        self.response: dict[str, object] | None = None

    @staticmethod
    def _is_config_skip_log(_item: RequestLog) -> bool:
        return False

    def _send_json(self, payload: dict[str, object], status: int = 200) -> None:
        assert status == 200
        self.response = payload


def test_put_forwarding_restores_path_and_clears_cached_body() -> None:
    handler = _PutHandler()

    handle_put(handler)
    handler.do_GET()

    assert handler.post_paths == ["/api/groups"]
    assert handler.post_payloads == [{"name": "更新后的中文名称", "id": "group-1"}]
    assert handler.get_paths == ["/api/groups/group-1"]
    assert handler.path == "/api/groups/group-1"
    assert not hasattr(handler, "_put_body")


def test_logs_api_reads_persisted_history_before_server_side_pagination() -> None:
    persisted_logs = [
        RequestLog(f"2026-01-01T00:00:0{index}", "/v1/test", "demo", "200", request_id=f"request-{index}")
        for index in range(4)
    ]
    handler = _LogsHandler(persisted_logs)

    handle_get(handler)

    assert handler.response is not None
    assert handler.response["total"] == 4
    assert handler.response["offset"] == 1
    assert handler.response["limit"] == 2
    assert [item["request_id"] for item in handler.response["logs"]] == ["request-2", "request-1"]
