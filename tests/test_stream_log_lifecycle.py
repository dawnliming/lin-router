#!/usr/bin/env python3
import tempfile
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ConfigStore, ArkProxyRouter, RouteContext
from tests.test_v053_stats_preview_runtime import write_config


def make_router(tmp_path):
    config_path = Path(tmp_path) / "config.json"
    write_config(config_path)
    store = ConfigStore(config_path)
    router = ArkProxyRouter(store, settings_store=None)
    router.logs = []
    router.log_file = Path(tmp_path) / "logs.jsonl"
    return router, store


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FinishedStreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b'event: response.completed\n'
            b'data: {"response":{"status":"completed","usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6,"prompt_tokens_details":{"cached_tokens":0}}}}\n\n'
        )
        self.wfile.flush()

    def log_message(self, format, *args):
        return


class AllZeroResponseCompletedHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b'event: response.completed\n'
            b'data: {"response":{"status":"completed","usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0,"prompt_tokens_details":{"cached_tokens":0}}}}\n\n'
        )
        self.wfile.flush()

    def log_message(self, format, *args):
        return


def add_stream_ok(router, group, request_id, attempt, model):
    router.add_log(
        "/v1/responses",
        model,
        "streaming",
        "first_byte_ms=120; chunks_received=1; bytes_received=40; final_result=streaming",
        duration_ms=120,
        group=group,
        request_id=request_id,
        attempt=attempt,
        event="stream_ok",
    )


def test_stream_lifecycle_patches_matching_candidate_only():
    with tempfile.TemporaryDirectory() as tmp:
        router, store = make_router(tmp)
        group = store.find_group("g1")
        assert group is not None
        add_stream_ok(router, group, "req-stream", 1, "first-candidate")
        add_stream_ok(router, group, "req-stream", 2, "fallback-candidate")

        patched = router.patch_stream_lifecycle(
            "req-stream",
            2,
            "fallback-candidate",
            (100, 20, 120, 40, 10),
            "stream_final",
            final_status="200",
            lifecycle="stream_done",
            final_result="stream_done",
            chunks_received=8,
            bytes_received=900,
            duration_ms=620,
            lock_wait_ms=5,
            lock_release_reason="stream_final",
        )

        assert patched is True
        assert len(router.logs) == 2
        first = next(item for item in router.logs if item.attempt == 1)
        fallback = next(item for item in router.logs if item.attempt == 2)
        assert first.status == "streaming"
        assert first.total_tokens == 0
        assert fallback.event == "stream_ok"
        assert fallback.status == "200"
        assert fallback.duration_ms == 620
        assert fallback.total_tokens == 120
        assert fallback.usage_source == "stream_final"
        assert "first_byte_ms=120" in fallback.detail
        assert "lifecycle=stream_done" in fallback.detail
        assert "chunks_received=8" in fallback.detail
        assert "bytes_received=900" in fallback.detail
        assert fallback.detail.count("chunks_received=") == 1
        assert fallback.detail.count("bytes_received=") == 1
        assert router._detail_value(fallback.detail, "chunks_received") == "8"
        assert router._detail_value(fallback.detail, "bytes_received") == "900"

        router.patch_stream_lifecycle(
            "req-stream",
            1,
            "first-candidate",
            (10, 0, 10, 0, 0),
            "stream_incomplete",
            final_status="timeout",
            lifecycle="stream_idle_timeout",
            final_result="stream_idle_timeout",
            chunks_received=2,
            bytes_received=120,
            duration_ms=800,
            failure_scope="upstream",
        )
        assert len(router.logs) == 2
        assert first.status == "timeout"
        assert first.failure_scope == "upstream"
        assert "lifecycle=stream_idle_timeout" in first.detail


def test_unfinalized_stream_is_patched_as_client_disconnected():
    with tempfile.TemporaryDirectory() as tmp:
        router, store = make_router(tmp)
        group = store.find_group("g1")
        assert group is not None
        add_stream_ok(router, group, "req-disconnect", 1, "candidate")

        router.finalize_stream_if_needed("req-disconnect")

        assert len(router.logs) == 1
        item = router.logs[0]
        assert item.event == "stream_ok"
        assert item.status == "client_disconnected"
        assert item.usage_source == "stream_incomplete"
        assert "lifecycle=client_disconnected" in item.detail
        assert "final_result=client_disconnected" in item.detail


def test_finished_stream_keeps_one_primary_log_with_first_byte_and_usage():
    upstream = ThreadingHTTPServer(("127.0.0.1", get_free_port()), FinishedStreamHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, store = make_router(tmp)
            group = store.find_group("g1")
            model = store.find_model("m1")
            assert group is not None and model is not None
            group.base_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
            group.waf_compatible = False
            store.save()
            context = RouteContext(
                client_key=group.route_key,
                group=group,
                group_id=group.id,
                provider_type=group.provider_type,
                base_url=group.base_url,
                display_name=group.name,
                passthrough=False,
            )

            status, _headers, iterator, request_id = router.stream(
                "/v1/chat/completions",
                {"model": model.name, "messages": [{"role": "user", "content": "ping"}], "stream": True},
                context,
            )
            assert status == 200
            assert b"ok" in b"".join(iterator)

            primary_logs = [item for item in router.logs if item.request_id == request_id and item.event == "stream_ok"]
            assert len(primary_logs) == 1
            item = primary_logs[0]
            assert item.status == "200"
            assert item.prompt_tokens == 4
            assert item.completion_tokens == 2
            assert item.total_tokens == 6
            assert item.cached_tokens == 0
            assert item.usage_source == "stream_final"
            assert "first_byte_ms=" in item.detail
            assert "lifecycle=stream_done" in item.detail
            assert "final_result=stream_done" in item.detail
            assert "completion_signal=response.completed" in item.detail
            assert "first_complete_frame_ms=" in item.detail
            assert "stream_frame_count=1" in item.detail
            assert "stream_wire_mode=sse" in item.detail
            assert not any(
                log.request_id == request_id and log.event in {"stream_done", "stream_idle_timeout", "client_disconnected"}
                for log in router.logs
            )
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_response_completed_all_zero_usage_is_stream_final_not_missing():
    upstream = ThreadingHTTPServer(("127.0.0.1", get_free_port()), AllZeroResponseCompletedHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            router, store = make_router(tmp)
            group = store.find_group("g1")
            model = store.find_model("m1")
            assert group is not None and model is not None
            group.base_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
            group.waf_compatible = False
            store.save()
            context = RouteContext(
                client_key=group.route_key,
                group=group,
                group_id=group.id,
                provider_type=group.provider_type,
                base_url=group.base_url,
                display_name=group.name,
                passthrough=False,
            )

            status, _headers, iterator, request_id = router.stream(
                "/v1/responses",
                {"model": model.name, "input": "ping", "stream": True},
                context,
            )
            assert status == 200
            assert b"response.completed" in b"".join(iterator)

            item = next(log for log in router.logs if log.request_id == request_id and log.event == "stream_ok")
            assert (item.prompt_tokens, item.completion_tokens, item.total_tokens, item.cached_tokens) == (0, 0, 0, 0)
            assert item.usage_source == "stream_final"
            assert "completion_signal=response.completed" in item.detail
    finally:
        upstream.shutdown()
        upstream.server_close()
