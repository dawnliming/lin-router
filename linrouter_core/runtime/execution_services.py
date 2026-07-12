"""Execution-service facades for the frozen v0.6 request plane.

The compatibility router delegates into these services.  They intentionally do
not import ``app.py`` or HTTP transport classes; request semantics remain in
the extracted runtime coordinator until later ports are narrowed further.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from linrouter_core.contracts.execution_ports import ExecutionDependencies
from .router_runtime import CandidateRuntime


class NonStreamExecutionService:
    """Owns the non-stream execution entry point behind an explicit dependency."""

    def __init__(self, dependencies: ExecutionDependencies, candidates: CandidateRuntime) -> None:
        self._dependencies = dependencies
        self._candidates = candidates

    def execute(
        self,
        path: str,
        payload: Dict[str, Any],
        route: Any = None,
        incoming_headers: Optional[Dict[str, str]] = None,
        raw_body: bytes | None = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        return self._candidates.execute_non_stream(path, payload, route, incoming_headers, raw_body)


class StreamExecutionService:
    """Owns the stream execution entry point and returns the runtime iterator unchanged."""

    def __init__(self, dependencies: ExecutionDependencies, candidates: CandidateRuntime) -> None:
        self._dependencies = dependencies
        self._candidates = candidates

    def execute(
        self,
        path: str,
        payload: Dict[str, Any],
        route: Any = None,
        incoming_headers: Optional[Dict[str, str]] = None,
        raw_body: bytes | None = None,
    ) -> tuple[int, Dict[str, str], Iterable[bytes], str]:
        return self._candidates.execute_stream(path, payload, route, incoming_headers, raw_body)
