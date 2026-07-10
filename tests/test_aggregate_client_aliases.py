#!/usr/bin/env python3
import json
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import AggregateModel, ArkProxyRouter, ConfigStore, RouteContext, create_server
from tests.test_v053_stats_preview_runtime import write_config


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_json(port, path, key):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_aggregate_client_alias_routes_and_lists_models():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["aggregate_models"][0]["client_model_aliases"] = ["gpt-5.5", "gpt-5.6-terra", "gpt-5.5", " "]
        raw["models"][0]["name"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        server, port, _ = create_server("127.0.0.1", get_free_port(), config_path)
        server.router.logs = []
        server.router.log_file = Path(tmp) / "logs.jsonl"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            aggregate = server.store.find_aggregate("ag1")
            assert aggregate is not None
            assert aggregate.client_model_aliases == ["gpt-5.5", "gpt-5.6-terra"]

            status, models = request_json(port, "/v1/models", "lr-ag1")
            assert status == 200
            listed = {item["id"]: item for item in models["data"]}
            assert {"agg-cheap", "gpt-5.5", "gpt-5.6-terra"} <= set(listed)
            assert listed["gpt-5.5"]["root"] == "agg-cheap"
            assert listed["gpt-5.5"]["parent"] == "agg-cheap"
            assert listed["gpt-5.5"]["is_client_alias"] is True

            context = server.store.find_aggregate_by_route_key("lr-ag1")
            assert context is not None
            from app import RouteContext
            aggregate_context = RouteContext(
                client_key="lr-ag1", group=None, group_id="__aggregate__ag1",
                provider_type="aggregate", base_url="", display_name="agg-cheap",
                passthrough=False, aggregate=context,
            )
            resolved = server.router._resolve_aggregate("gpt-5.5", aggregate_context)
            assert resolved == (aggregate, "aggregate_alias")
            assert server.router._resolve_aggregate("agg-cheap", aggregate_context) == (aggregate, "aggregate")

            update_body = json.dumps({
                "name": "agg-cheap",
                "client_model_aliases": ["gpt-5.6-sol\ngpt-5.6-sol", "gpt-5.6-terra"],
            }).encode("utf-8")
            update_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/aggregates/ag1",
                data=update_body,
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(update_request, timeout=5) as response:
                updated = json.loads(response.read().decode("utf-8"))
            assert updated["aggregate_model"]["client_model_aliases"] == ["gpt-5.6-sol", "gpt-5.6-terra"]
            assert ConfigStore(config_path).find_aggregate("ag1").client_model_aliases == ["gpt-5.6-sol", "gpt-5.6-terra"]

            group = server.store.find_group_by_route_key("lr-g1")
            assert group is not None
            group_context = RouteContext(
                client_key="lr-g1", group=group, group_id=group.id,
                provider_type=group.provider_type, base_url=group.base_url,
                display_name=group.name, passthrough=False,
            )
            assert server.router._resolve_aggregate("gpt-5.5", group_context) is None
        finally:
            server.shutdown()
            server.server_close()


def test_aggregate_client_alias_conflicts_are_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        write_config(config_path)
        store = ConfigStore(config_path)
        aggregate = store.find_aggregate("ag1")
        assert aggregate is not None
        aggregate.client_model_aliases = ["gpt-5.5", "gpt-5.5", ""]
        assert store.upsert_aggregate(aggregate) == (True, "")
        assert aggregate.client_model_aliases == ["gpt-5.5"]

        scoped_alias = AggregateModel(
            id="ag2",
            name="agg-backup",
            route_key="lr-ag2",
            client_model_aliases=["agg-cheap", "gpt-5.5"],
        )
        assert store.upsert_aggregate(scoped_alias) == (True, "")
        scoped_context = RouteContext(
            client_key="lr-ag2", group=None, group_id="__aggregate__ag2",
            provider_type="aggregate", base_url="", display_name="agg-backup",
            passthrough=False, aggregate=scoped_alias,
        )
        router = ArkProxyRouter(store, settings_store=None)
        assert router._resolve_aggregate("agg-cheap", scoped_context) == (scoped_alias, "aggregate_alias")

        reserved = AggregateModel(id="ag3", name="agg-third", route_key="lr-ag3", client_model_aliases=["all-router-auto"])
        ok, message = store.upsert_aggregate(reserved)
        assert ok is False
        assert "保留名" in message
