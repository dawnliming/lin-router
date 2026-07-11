#!/usr/bin/env python3
"""M1 配置域抽取兼容契约：保持 app.py 公共导入与配置持久化语义。"""

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from app import AggregateMember, AggregateModel, ConfigStore, ConnectionGroup, ModelConfig
from debug_capture import DebugCapture


def test_app_reexports_config_domain_types():
    from linrouter_core.config.models import (
        AggregateMember as CoreAggregateMember,
        AggregateModel as CoreAggregateModel,
        ConnectionGroup as CoreConnectionGroup,
        ModelConfig as CoreModelConfig,
    )
    from linrouter_core.config.store import ConfigStore as CoreConfigStore

    assert ConnectionGroup is CoreConnectionGroup
    assert ModelConfig is CoreModelConfig
    assert AggregateModel is CoreAggregateModel
    assert AggregateMember is CoreAggregateMember
    assert ConfigStore is CoreConfigStore


def test_config_round_trip_preserves_complete_payload():
    payload = {
        "groups": [{
            "id": "g1", "name": "relay", "provider_type": "relay",
            "base_url": "https://relay.example/v1", "route_key": "lr-g1",
            "auto_model_name": "router-auto", "auto_model_cooldown_minutes": 7,
            "stream_idle_timeout": 111, "waf_compatible": True,
            "waf_accept_policy": "all", "waf_client_mode": "auto",
            "reasoning_support": "supported", "upstream_models": [{"id": "m-up"}],
            "upstream_models_fetched_at": "2026-07-11 10:00:00",
        }],
        "models": [{
            "id": "m1", "name": "model", "ep_id": "upstream", "group_id": "g1",
            "upstream_model": "upstream-v2", "api_key": "sk-test", "price_group": "standard",
            "price_input": 1.2, "price_output": 3.4, "usable": False,
            "disabled_by_user": True, "last_error": "bad", "last_success_at": "ok",
            "last_checked_at": "checked", "cooldown_until": 123, "cooldown_reason": "network",
        }],
        "aggregate_models": [{
            "id": "a1", "name": "aggregate", "display_name": "Aggregate", "description": "desc",
            "route_key": "lr-ag-1", "client_model_aliases": ["alias-a", "alias-b"],
            "enabled": False, "strategy": "weighted", "cooldown_minutes": 9,
            "created_at": "created", "updated_at": "updated",
        }],
        "aggregate_members": [{
            "id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1",
            "priority": 2, "manual_price": 4.5, "weight": 80, "enabled": False,
            "cooldown_until": 456, "cooldown_reason": "timeout", "last_error": "err",
            "last_success_at": "success", "last_checked_at": "checked",
        }],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        store = ConfigStore(path)
        before = {
            "groups": [asdict(item) for item in store.groups],
            "models": [asdict(item) for item in store.models],
            "aggregate_models": [asdict(item) for item in store.aggregate_models],
            "aggregate_members": [asdict(item) for item in store.aggregate_members],
        }
        store.save()
        reloaded = ConfigStore(path)
        after = {
            "groups": [asdict(item) for item in reloaded.groups],
            "models": [asdict(item) for item in reloaded.models],
            "aggregate_models": [asdict(item) for item in reloaded.aggregate_models],
            "aggregate_members": [asdict(item) for item in reloaded.aggregate_members],
        }
    assert after == before


def test_empty_and_legacy_group_defaults_remain_compatible():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "new.json"
        store = ConfigStore(path)
        assert store.groups == []
        assert store.models == []

    explicit_empty = ConnectionGroup.from_dict({"id": "new", "name": "new", "base_url": ""})
    legacy = ConnectionGroup.from_dict({"id": "old", "name": "old"})
    assert explicit_empty.base_url == ""
    assert legacy.base_url


def test_debug_capture_replay_uses_injected_usage_callbacks():
    class Response:
        status = 200
        http_version = "HTTP/1.1"

        def __init__(self):
            self.chunks = [b"data: usage", b""]
            self.closed = False

        def readline(self, _timeout):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

    class Client:
        client_type = "urllib"

        def __init__(self):
            self.response = Response()

        def request(self, *_args, **_kwargs):
            return self.response

    client = Client()
    capture = DebugCapture(
        router=None,
        settings_store=None,
        empty_usage=lambda: (0, 0, 0, 0, 0),
        usage_from_stream_chunk=lambda chunk: (3, 2, 5, 1, 0) if chunk == b"data: usage" else (0, 0, 0, 0, 0),
    )
    result = capture._single_replay(client, "https://example.test/v1/chat/completions", {}, b"{}", "/v1/chat/completions", None, 1)

    assert result["prompt_tokens"] == 3
    assert result["cached_tokens"] == 1
    assert result["total_tokens"] == 5
    assert client.response.closed is True

    ssl_context = object()

    class LegacyRouter:
        _debug_capture_browser_user_agent = "legacy-browser-ua"
        _debug_capture_ssl_context = ssl_context
        _empty_usage = staticmethod(lambda: (0, 0, 0, 0, 0))
        _usage_from_stream_chunk = staticmethod(
            lambda chunk: (3, 2, 5, 1, 0) if chunk == b"data: usage" else (0, 0, 0, 0, 0)
        )

    legacy_capture = DebugCapture(LegacyRouter(), None)
    legacy_client = Client()
    legacy_result = legacy_capture._single_replay(
        legacy_client, "https://example.test/v1/chat/completions", {}, b"{}", "/v1/chat/completions", None, 1
    )
    assert legacy_capture._browser_user_agent == "legacy-browser-ua"
    assert legacy_capture._ssl_context is ssl_context
    assert legacy_result["prompt_tokens"] == 3
    assert legacy_result["total_tokens"] == 5
