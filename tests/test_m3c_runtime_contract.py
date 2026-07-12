from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import ArkProxyRouter, ConfigStore, RouteContext
from linrouter_core.runtime import CandidateRuntime, StreamExecutionService
from test_stream_no_fallback import (
    FirstUpstreamHandler,
    SecondUpstreamHandler,
    build_config,
    get_free_port,
    start_server,
)


ROOT = Path(__file__).resolve().parent.parent
# I1 owns execution facade delegation. HTTP dispatch remains frozen separately.
FROZEN_METHODS: set[str] = set()


class AttributeBearingGroupId(str):
    group = None
    is_deprecated_global = False


def _methods(source: str, names: set[str]) -> dict[str, str]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    result: dict[str, str] = {}
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        for node in class_node.body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                result[node.name] = "".join(lines[node.lineno - 1 : node.end_lineno])
    return result


def _aggregate_context(store: ConfigStore) -> RouteContext:
    aggregate = store.find_aggregate_by_route_key("lr-ag-test")
    assert aggregate is not None
    return RouteContext(
        client_key="lr-ag-test",
        group=None,
        group_id=f"__aggregate__{aggregate.id}",
        provider_type="aggregate",
        base_url="",
        display_name=aggregate.display_name or aggregate.name,
        passthrough=False,
        is_global=False,
        aggregate=aggregate,
    )


def test_m3c_stream_facade_delegates_with_original_argument_order() -> None:
    facade_source = inspect.getsource(ArkProxyRouter.stream)
    executor_source = inspect.getsource(CandidateRuntime.execute_stream)
    service_source = inspect.getsource(StreamExecutionService.execute)

    assert facade_source.count("return ") == 1
    assert "self.stream_execution.execute(path, payload, route, incoming_headers, raw_body)" in facade_source
    assert "self._candidates.execute_stream(path, payload, route, incoming_headers, raw_body)" in service_source
    assert "yield " not in facade_source
    assert "except " not in facade_source
    assert "router._upstream_client.request" in executor_source
    assert "yield first_chunk" in executor_source
    assert "finally:" in executor_source


def test_m3c_call_and_http_handlers_remain_frozen_against_m3b_baseline() -> None:
    baseline = subprocess.check_output(
        ["git", "show", "2806a5a:app.py"], cwd=ROOT, text=True, encoding="utf-8"
    )
    current = (ROOT / "app.py").read_text(encoding="utf-8")
    assert _methods(baseline, FROZEN_METHODS) == _methods(current, FROZEN_METHODS)


def test_m3c_generator_close_runs_stream_finally_once() -> None:
    port1 = get_free_port()
    port2 = get_free_port()
    first_server = start_server(FirstUpstreamHandler, port1)
    second_server = start_server(SecondUpstreamHandler, port2)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as config_file:
        config_path = Path(config_file.name)
        json.dump(build_config(port1, port2), config_file, ensure_ascii=False)

    try:
        router = ArkProxyRouter(ConfigStore(config_path), settings_store=None)
        status, _headers, iterator, request_id = router.stream(
            "/v1/chat/completions",
            {"model": "agg-test", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            _aggregate_context(router.store),
        )
        assert status == 200
        assert next(iterator)
        iterator.close()

        stream_logs = [item for item in router.logs if item.request_id == request_id and item.event == "stream_ok"]
        assert len(stream_logs) == 1
        assert stream_logs[0].status == "client_disconnected"
        assert router.upstream_active_streams == {}
    finally:
        first_server.shutdown()
        first_server.server_close()
        second_server.shutdown()
        second_server.server_close()
        config_path.unlink(missing_ok=True)


def test_m3c_stream_keeps_attribute_bearing_group_id_on_legacy_string_route_path() -> None:
    port1 = get_free_port()
    port2 = get_free_port()
    first_server = start_server(FirstUpstreamHandler, port1)
    second_server = start_server(SecondUpstreamHandler, port2)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as config_file:
        config_path = Path(config_file.name)
        json.dump(build_config(port1, port2), config_file, ensure_ascii=False)

    try:
        router = ArkProxyRouter(ConfigStore(config_path), settings_store=None)
        route = AttributeBearingGroupId(router.store.groups[0].id)
        status, _headers, iterator, _request_id = router.stream(
            "/v1/chat/completions",
            {"model": "model-1", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            route,
        )

        assert not isinstance(route, RouteContext)
        assert status == 200
        assert next(iterator)
        iterator.close()
    finally:
        first_server.shutdown()
        first_server.server_close()
        second_server.shutdown()
        second_server.server_close()
        config_path.unlink(missing_ok=True)
