from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app import ArkProxyRouter, ConfigStore, RouteContext
from tests.test_v053_stats_preview_runtime import write_config


def _group_context(store: ConfigStore) -> RouteContext:
    group = store.find_group_by_route_key("lr-g1")
    assert group is not None
    return RouteContext(
        client_key="lr-g1", group=group, group_id=group.id,
        provider_type=group.provider_type, base_url=group.base_url,
        display_name=group.name, passthrough=False,
    )


def _aggregate_context(store: ConfigStore) -> RouteContext:
    aggregate = store.find_aggregate_by_route_key("lr-ag1")
    assert aggregate is not None
    return RouteContext(
        client_key="lr-ag1", group=None, group_id=f"__aggregate__{aggregate.id}",
        provider_type="aggregate", base_url="", display_name=aggregate.name,
        passthrough=False, aggregate=aggregate,
    )


def _proxy_context(store: ConfigStore) -> RouteContext:
    group = store.find_group_by_route_key("lr-g1")
    assert group is not None
    return RouteContext(
        client_key="lr-g1", group=group, group_id=group.id,
        provider_type=group.provider_type, base_url=group.base_url,
        display_name=group.name, passthrough=True,
    )


def _write_upstream_mapping_fixture(path: Path) -> None:
    write_config(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["models"][0]["upstream_model"] = "internal-only-upstream"
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("stream", [False, True])
def test_group_unknown_model_fails_before_live_request(stream: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        write_config(path)
        router = ArkProxyRouter(ConfigStore(path), settings_store=None)
        payload = {"model": "not-configured", "messages": [{"role": "user", "content": "ping"}]}
        if stream:
            payload["stream"] = True
        with pytest.raises(Exception) as raised:
            (router.stream if stream else router.call)("/v1/chat/completions", payload, _group_context(router.store))
        assert getattr(raised.value, "error_code", "") == "model_not_found"
        assert getattr(raised.value, "attempted", None) == 0
        assert router.live_requests_payload()["count"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_aggregate_unknown_model_fails_before_live_request(stream: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        write_config(path)
        router = ArkProxyRouter(ConfigStore(path), settings_store=None)
        payload = {"model": "not-an-alias", "messages": [{"role": "user", "content": "ping"}]}
        if stream:
            payload["stream"] = True
        with pytest.raises(Exception) as raised:
            (router.stream if stream else router.call)("/v1/chat/completions", payload, _aggregate_context(router.store))
        assert getattr(raised.value, "error_code", "") == "model_not_found"
        assert getattr(raised.value, "attempted", None) == 0
        assert router.live_requests_payload()["count"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_upstream_mapping_is_not_a_client_model_alias(stream: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        _write_upstream_mapping_fixture(path)
        router = ArkProxyRouter(ConfigStore(path), settings_store=None)
        payload = {"model": "internal-only-upstream", "messages": [{"role": "user", "content": "ping"}]}
        if stream:
            payload["stream"] = True
        with pytest.raises(Exception) as raised:
            (router.stream if stream else router.call)("/v1/chat/completions", payload, _group_context(router.store))
        assert getattr(raised.value, "error_code", "") == "model_not_found"
        assert getattr(raised.value, "attempted", None) == 0
        assert router.live_requests_payload()["count"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_proxy_group_keeps_explicit_model_pass_through(stream: bool) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        write_config(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["groups"][0].update({"provider_type": "proxy", "api_key": "proxy-key"})
        path.write_text(json.dumps(payload), encoding="utf-8")
        router = ArkProxyRouter(ConfigStore(path), settings_store=None)

        class _Response:
            status = 200

            def __init__(self, streaming: bool) -> None:
                self.streaming = streaming
                self.headers = {}
                self.lines = [
                    b'data: {"id":"probe","choices":[{"delta":{"content":"ok"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ]

            def read(self):
                return b'{"ok":true}'

            def readline(self, _timeout=None):
                return self.lines.pop(0) if self.lines else b""

            def close(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class _Client:
            def __init__(self):
                self.calls = []

            def request(self, method, url, headers, body, **kwargs):
                self.calls.append((method, url, headers, body, kwargs))
                return _Response(bool(kwargs.get("stream")))

        client = _Client()
        router._upstream_client = client
        router.runtime.upstream = client
        router.non_stream_execution._candidates.upstream = client
        router.stream_execution._candidates.upstream = client
        request = {"model": "client-only-model", "messages": [{"role": "user", "content": "ping"}]}
        if stream:
            request["stream"] = True
        result = (router.stream if stream else router.call)("/v1/chat/completions", request, _proxy_context(router.store))
        assert result[0] == 200
        if stream:
            assert b"ok" in b"".join(result[2])
        assert len(client.calls) == 1
        assert router.live_requests_payload()["count"] == 0
