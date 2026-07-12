"""v0.6 I1 contracts: compatibility delegation and import-boundary checks."""
from __future__ import annotations

import inspect
from pathlib import Path

from app import AllModelsFailedError as AppAllModelsFailedError
from app import ArkProxyRouter, RouteContext as AppRouteContext
from app import StreamIdleTimeoutError as AppStreamIdleTimeoutError
from app import UpstreamCandidate as AppUpstreamCandidate
from linrouter_core.contracts.runtime_types import (
    AllModelsFailedError,
    RouteContext,
    StreamIdleTimeoutError,
    UpstreamCandidate,
)
from linrouter_core.runtime import CandidateRuntime, NonStreamExecutionService, StreamExecutionService


ROOT = Path(__file__).resolve().parent.parent


def test_v060_execution_types_are_core_contracts_reexported_by_legacy_facade() -> None:
    assert AppUpstreamCandidate is UpstreamCandidate
    assert AppRouteContext is RouteContext
    assert AppAllModelsFailedError is AllModelsFailedError
    assert AppStreamIdleTimeoutError is StreamIdleTimeoutError


def test_v060_execution_modules_do_not_reverse_import_app_or_handler() -> None:
    for relative_path in (
        "linrouter_core/runtime/router_runtime.py",
        "linrouter_core/runtime/execution_services.py",
        "linrouter_core/contracts/runtime_types.py",
        "linrouter_core/contracts/execution_ports.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "import app" not in source
        assert "from app import" not in source
        assert "RouterHandler" not in source
    assert "router: Any" not in (ROOT / "linrouter_core/runtime/router_runtime.py").read_text(encoding="utf-8")


def test_v060_legacy_facade_only_delegates_execution_entries() -> None:
    call_source = inspect.getsource(ArkProxyRouter.call)
    stream_source = inspect.getsource(ArkProxyRouter.stream)
    assert "self.non_stream_execution.execute" in call_source
    assert "self.stream_execution.execute" in stream_source
    assert "_upstream_client.request" not in call_source + stream_source

    assert "self._candidates.execute_non_stream" in inspect.getsource(NonStreamExecutionService.execute)
    assert "self._candidates.execute_stream" in inspect.getsource(StreamExecutionService.execute)
    assert "router: Any" not in inspect.getsource(CandidateRuntime.__init__)
