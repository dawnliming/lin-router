#!/usr/bin/env python3
import json
import socket
import tempfile
import threading
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_server


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def post_json(port, path, payload=None):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def write_config(path):
    payload = {
        "groups": [{
            "id": "g1",
            "name": "relay",
            "provider_type": "relay",
            "base_url": "https://relay.example/v1",
            "route_key": "lr-g1",
        }],
        "models": [{
            "id": "m1",
            "name": "cheap",
            "ep_id": "cheap-upstream",
            "group_id": "g1",
            "upstream_model": "cheap-upstream",
            "api_key": "sk-test",
            "usable": True,
        }, {
            "id": "m2",
            "name": "backup",
            "ep_id": "backup-upstream",
            "group_id": "g1",
            "upstream_model": "backup-upstream",
            "api_key": "sk-test-2",
            "usable": True,
        }],
        "aggregate_models": [{
            "id": "ag1",
            "name": "agg-cheap",
            "route_key": "lr-ag1",
            "enabled": True,
            "strategy": "priority",
            "cooldown_minutes": 5,
        }],
        "aggregate_members": [{
            "id": "am1",
            "aggregate_id": "ag1",
            "group_id": "g1",
            "model_id": "m1",
            "priority": 1,
            "enabled": True,
        }, {
            "id": "am2",
            "aggregate_id": "ag1",
            "group_id": "g1",
            "model_id": "m2",
            "priority": 2,
            "enabled": True,
        }],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_aggregate_stats_runtime_state_and_delete_preview():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path)
        server, port, _ = create_server("127.0.0.1", get_free_port(), config_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            router = server.router
            group = server.store.find_group("g1")
            assert group is not None
            router.add_log(
                "/v1/chat/completions", "agg-cheap", "skip",
                "aggregate_id=ag1; aggregate_model=agg-cheap; skip_reason=member_disabled; aggregate_member_id=am1",
                event="skip", request_id="req-1", group=group,
            )
            router.add_log(
                "/v1/chat/completions", "agg-cheap", "200",
                "aggregate_id=ag1; aggregate_model=agg-cheap; aggregate_member_id=am1; selected_model=cheap; fallback_index=0",
                duration_ms=1000, prompt_tokens=100, cached_tokens=80, total_tokens=120,
                event="stream_done", request_id="req-1", attempt=1, group=group,
            )
            router.add_log(
                "/v1/chat/completions", "agg-cheap", "skip",
                "aggregate_id=ag1; aggregate_model=agg-cheap; skip_reason=member_cooling; aggregate_member_id=am1",
                event="skip", request_id="req-2", group=group,
            )
            router.add_log(
                "/v1/chat/completions", "agg-cheap", "busy",
                "aggregate_id=ag1; aggregate_model=agg-cheap; fallback_reason=large_task_in_progress; aggregate_member_id=am1",
                event="waf_lock_timeout", request_id="req-2", group=group, failure_scope="busy",
            )
            router.add_log(
                "/v1/chat/completions", "agg-cheap", "200",
                "aggregate_id=ag1; aggregate_model=agg-cheap; aggregate_member_id=am1; selected_model=cheap; fallback_index=1",
                duration_ms=2000, prompt_tokens=100, cached_tokens=20, total_tokens=120,
                event="stream_done", request_id="req-2", attempt=2, group=group,
            )

            status, logs_default = get_json(port, "/api/logs")
            assert status == 200
            assert all(item["event"] != "skip" for item in logs_default["logs"])

            status, logs_debug = get_json(port, "/api/logs?include_skip=true")
            assert status == 200
            assert any(item["event"] == "skip" for item in logs_debug["logs"])

            status, stats = get_json(port, "/api/aggregates/ag1/stats?limit=100")
            assert status == 200
            assert stats["request_count"] == 2
            assert stats["success_count"] == 2
            assert stats["fallback_success_count"] == 1
            assert stats["cooldown_skip_count"] == 1
            assert stats["busy_switch_count"] == 1
            assert stats["cache_hit_rate"] == 0.5

            status, limited_stats = get_json(port, "/api/aggregates/ag1/stats?limit=1")
            assert status == 200
            assert limited_stats["request_count"] == 1
            assert limited_stats["fallback_success_count"] == 1
            assert limited_stats["busy_switch_count"] == 1
            assert limited_stats["cache_hit_rate"] == 0.2

            status, runtime = get_json(port, "/api/runtime-state")
            assert status == 200
            assert runtime["models"][0]["derived_status"] == "healthy"
            assert runtime["aggregate_members"][0]["derived_status"] == "healthy"
            assert all(item["event"] != "skip" for item in runtime["logs"])

            status, runtime_debug = get_json(port, "/api/runtime-state?include_skip=true")
            assert status == 200
            assert any(item["event"] == "skip" for item in runtime_debug["logs"])

            status, sort_preview = post_json(port, "/api/aggregate-members/am2/sort-preview", {"direction": "up"})
            assert status == 200
            assert sort_preview["candidate_chain_before"][0]["member_id"] == "am1"
            assert sort_preview["candidate_chain_after"][0]["member_id"] == "am2"
            assert server.store.find_aggregate_member("am2").priority == 2

            member = server.store.find_aggregate_member("am1")
            member.enabled = False
            member.cooldown_until = 9999999999
            member.cooldown_reason = "read_timeout"
            status, cooldown_preview = post_json(port, "/api/aggregate-members/am1/clear-cooldown-preview")
            assert status == 200
            assert cooldown_preview["cooldown_before"]["cooldown_reason"] == "read_timeout"
            assert cooldown_preview["cooldown_after"]["cooldown_until"] == 0
            assert cooldown_preview["candidate_chain_before"][0]["derived_status"] == "manual_disabled"
            assert cooldown_preview["candidate_chain_after"][0]["derived_status"] == "healthy"

            status, group_preview = post_json(port, "/api/groups/g1/delete-preview")
            assert status == 200
            assert group_preview["affected_models"] == 2
            assert group_preview["affected_aggregate_members"][0]["aggregate_name"] == "agg-cheap"

            status, model_preview = post_json(port, "/api/models/m1/delete-preview")
            assert status == 200
            assert model_preview["affected_aggregate_members"][0]["member_id"] == "am1"
        finally:
            server.shutdown()
            server.server_close()
