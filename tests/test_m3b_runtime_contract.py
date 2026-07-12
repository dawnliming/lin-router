from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import ArkProxyRouter
from linrouter_core.runtime import CandidateRuntime, NonStreamExecutionService
from test_cooldown_classification import (
    BadRequest400Handler,
    build_one_relay_group_two_models,
    get_free_port,
    group_route_ctx,
    make_router,
    start_server,
    test_group_auto_500_cooldown_and_fallback as assert_recoverable_failure_fallback,
)
from test_waf_lock_busy_classification import (
    test_waf_lock_timeout_is_candidate_busy_not_cooldown as assert_waf_lock_busy_contract,
)


def test_m3b_call_facade_delegates_to_runtime_executor() -> None:
    call_source = inspect.getsource(ArkProxyRouter.call)
    executor_source = inspect.getsource(CandidateRuntime.execute_non_stream)
    service_source = inspect.getsource(NonStreamExecutionService.execute)

    assert "self.non_stream_execution.execute" in call_source
    assert "self._candidates.execute_non_stream" in service_source
    assert "_upstream_client.request" in executor_source
    assert "except HTTPError" in executor_source
    assert "except (URLError, TimeoutError, OSError)" in executor_source


def test_m3b_non_stream_success_after_recoverable_failure() -> None:
    assert_recoverable_failure_fallback()


def test_m3b_explicit_request_error_does_not_fallback() -> None:
    BadRequest400Handler.request_count = 0
    BadRequest400Handler.max_bad = 10
    port = get_free_port()
    server = start_server(BadRequest400Handler, port)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as config_file:
        config_path = config_file.name
        import json

        json.dump(build_one_relay_group_two_models(port), config_file, ensure_ascii=False)

    try:
        router = make_router(config_path)
        context = group_route_ctx(router.store, "lr-group")
        status, _headers, _body = router.call(
            "/v1/chat/completions",
            {"model": "model-1", "messages": [{"role": "user", "content": "hi"}]},
            context,
        )
        assert status == 400
        assert BadRequest400Handler.request_count == 1
        assert router.store.models[0].cooldown_until == 0
        assert router.store.models[1].cooldown_until == 0
    finally:
        server.shutdown()
        Path(config_path).unlink(missing_ok=True)


def test_m3b_waf_lock_wait_preserves_busy_contract() -> None:
    assert_waf_lock_busy_contract()
