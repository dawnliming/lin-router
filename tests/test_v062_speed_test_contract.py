from __future__ import annotations

import json

from app import ArkProxyRouter, ConfigStore
from linrouter_core.runtime.http_api_runtime import handle_post


class _Response:
    status = 200
    def read(self):
        return b'{"ok":true}'
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False


class _Client:
    def __init__(self):
        self.calls = []
    def request(self, method, url, headers, body, **kwargs):
        self.calls.append((method, url, headers, body, kwargs))
        return _Response()


class _Handler:
    def __init__(self, router, path):
        self.router = router
        self.path = path
        self.response = None
        self.status = None
    def _send_json(self, payload, status=200):
        self.response, self.status = payload, status


def _router(tmp_path):
    config = {
        "groups": [{"id": "g1", "name": "relay", "provider_type": "relay", "base_url": "https://relay.example/v1", "route_key": "key"}],
        "models": [
            {"id": "m1", "name": "first", "ep_id": "model-1", "group_id": "g1", "api_key": "key-1", "usable": True},
            {"id": "m2", "name": "second", "ep_id": "model-2", "group_id": "g1", "api_key": "key-2", "usable": True},
        ],
        "aggregate_models": [{"id": "a1", "name": "aggregate", "route_key": "agg-key"}],
        "aggregate_members": [
            {"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "priority": 1, "enabled": True},
            {"id": "am2", "aggregate_id": "a1", "group_id": "g1", "model_id": "m2", "priority": 2, "enabled": True},
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    router = ArkProxyRouter(ConfigStore(path), None, tmp_path / "logs.jsonl")
    router._upstream_client = _Client()
    return router


def test_group_speed_test_runs_each_enabled_model_without_polluting_health_or_logs(tmp_path):
    router = _router(tmp_path)
    before_models = [json.dumps(model.__dict__, sort_keys=True) for model in router.store.models]
    before_logs = list(router.logs)

    result = router.speed_test_group("g1")

    assert result["ok"] is True
    assert result["source"] == "health_check"
    assert [item["model"] for item in result["results"]] == ["first", "second"]
    assert len(router._upstream_client.calls) == 2
    assert result["total_ms"] >= 0
    assert all(item["total_ms"] >= 0 for item in result["results"])
    assert [json.dumps(model.__dict__, sort_keys=True) for model in router.store.models] == before_models
    assert router.logs == before_logs


def test_aggregate_speed_test_uses_current_candidate_chain_and_stops_after_success(tmp_path):
    router = _router(tmp_path)

    result = router.speed_test_aggregate("a1")

    assert result["ok"] is True
    assert result["attempts"] == 1
    assert result["results"][0]["model"] == "first"
    assert len(router._upstream_client.calls) == 1


def test_speed_test_routes_return_stable_conflict_and_not_found_contracts(tmp_path):
    router = _router(tmp_path)
    missing = _Handler(router, "/api/groups/missing/speed-test")
    handle_post(missing)
    assert missing.status == 404
    assert missing.response["code"] == "group_not_found"

    router._speed_test_state["group:g1"] = {"running": True}
    running = _Handler(router, "/api/groups/g1/speed-test")
    handle_post(running)
    assert running.status == 409
    assert running.response["code"] == "speed_test_running"
