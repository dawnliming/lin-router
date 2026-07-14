from __future__ import annotations

import json
from pathlib import Path

from app import ArkProxyRouter
from linrouter_core.observability.contracts import RequestLog


class Store:
    groups = []
    models = []


def test_observability_facade_preserves_jsonl_export_and_diagnosis(tmp_path: Path) -> None:
    router = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    router.add_log(
        "/v1/chat/completions", "demo", "502", "network; group_id=g1; group_name=组一; provider=relay",
        duration_ms=12, request_id="request-1", event="network", failure_scope="upstream",
    )

    rows = [json.loads(line) for line in (tmp_path / "logs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event"] == "network"
    assert rows[0]["group_name"] == "组一"
    assert router.recent_logs()[0]["request_id"] == "request-1"
    assert "request_id" in router.export_logs_csv().splitlines()[0]
    assert router.diagnose_request("request-1")["diagnosis"]["root_cause"] == "upstream_error"


def test_observability_runtime_projection_does_not_mutate_router_runtime(tmp_path: Path) -> None:
    router = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    router._live_request_start("request-2", "/v1/models", "demo", stream=False)
    payload = router.live_requests_payload()
    assert payload["count"] == 1
    assert payload["requests"][0]["request_id_short"] == "request-"
    router._live_request_finish("request-2")
    assert router.live_requests_payload()["count"] == 0


def test_request_log_facade_is_observability_contract() -> None:
    from app import RequestLog as AppRequestLog

    assert AppRequestLog is RequestLog


def test_observability_compatibility_facades_forward_trim_and_rewrite_errors(tmp_path: Path) -> None:
    router = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    trim_limits = []
    router.observability._repository.trim = trim_limits.append
    router._trim_log_file(7)
    assert trim_limits == [7]

    router.logs = [RequestLog("t", "/v1/test", "demo", "streaming", event="stream_ok", request_id="request-3")]

    def fail_rewrite(_logs):
        raise OSError("disk full")

    router.observability._repository.rewrite_oldest_first = fail_rewrite
    assert router.patch_stream_lifecycle(
        "request-3", 0, "demo", (0, 0, 0, 0, 0), "none",
        final_status="200", lifecycle="done", final_result="done", chunks_received=1, bytes_received=1,
    )
    assert router.log_write_error == "日志回写失败: disk full"
    router.log_write_error = ""
    router._rewrite_log_file()
    assert router.log_write_error == "日志回写失败: disk full"


def test_observability_loads_and_retains_more_than_legacy_eighty_rows(tmp_path: Path) -> None:
    router = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    for index in range(120):
        router.add_log("/v1/test", "demo", "200", request_id=f"request-{index}")
    restored = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    assert len(restored.logs) == 120
    assert restored.all_logs()[0].request_id == "request-0"


def test_first_downstream_flush_is_recorded_once_without_overwriting_lifecycle_metrics(tmp_path: Path) -> None:
    router = ArkProxyRouter(Store(), None, tmp_path / "logs.jsonl")
    router.add_log(
        "/v1/chat/completions",
        "demo",
        "streaming",
        "stream ok; stream_started_at_ms=1; first_downstream_flush_ms=-1; stream_frame_count=1",
        request_id="request-flush",
        event="stream_ok",
    )

    router.record_stream_transport_event("request-flush", "downstream_first_flush")
    router.record_stream_transport_event("request-flush", "downstream_first_flush")
    item = router.logs[0]
    assert int(router._detail_value(item.detail, "first_downstream_flush_ms")) >= 0
    assert item.detail.count("first_downstream_flush_ms=") == 1

    assert router.patch_stream_lifecycle(
        "request-flush", 0, "demo", (0, 0, 0, 0, 0), "stream_final",
        final_status="200", lifecycle="stream_done", final_result="stream_done", chunks_received=3, bytes_received=20,
        stream_metrics={"first_downstream_flush_ms": -1, "stream_frame_count": 3},
    )
    assert int(router._detail_value(item.detail, "first_downstream_flush_ms")) >= 0
    assert router._detail_value(item.detail, "stream_frame_count") == "3"
