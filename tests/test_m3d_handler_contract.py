from __future__ import annotations

import ast
import inspect
import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import AllModelsFailedError, ArkProxyRouter, RouterHandler
from linrouter_core.runtime.handler_runtime import handle_proxy_request


ROOT = Path(__file__).resolve().parent.parent
FROZEN_METHODS = {
    "_require_route_context",
    "call",
    "stream",
    "finalize_stream_if_needed",
}
TARGET_MARKER = '        if parsed.path.startswith("/v1/") or parsed.path.startswith("/chat/"):'


def _methods(source: str, names: set[str]) -> dict[str, str]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    result: dict[str, str] = {}
    for class_node in tree.body:
        if isinstance(class_node, ast.ClassDef):
            for node in class_node.body:
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    result[node.name] = "".join(lines[node.lineno - 1 : node.end_lineno])
    return result


def _post_non_target_parts(source: str) -> tuple[str, str]:
    post = _methods(source, {"do_POST"})["do_POST"]
    # M4b-1 owns the configuration and backup import branches; M3d keeps
    # freezing the proxy branch boundary plus the later unrelated branches.
    start = post.index('        if parsed.path == "/api/groups":')
    target = post.index(TARGET_MARKER, start)
    end = post.index('        self._send_json({"error": {"message": "资源不存在"', target)
    return post[start:target], post[end:]


class FakeIterator:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self) -> Any:
        return iter(self.chunks)

    def close(self) -> None:
        self.closed = True


class FakeRouter:
    def __init__(self) -> None:
        self.finalized: list[str] = []
        self.stream_result: Any = (200, {}, FakeIterator([]), "")
        self.call_result: Any = (200, {}, b"")

    def stream(self, *args: Any) -> Any:
        return self.stream_result

    def call(self, *args: Any) -> Any:
        return self.call_result

    def finalize_stream_if_needed(self, request_id: str) -> None:
        self.finalized.append(request_id)


class FakeHandler:
    _all_models_failed_error_type = AllModelsFailedError

    def __init__(self, router: FakeRouter) -> None:
        self.router = router
        self.headers = {"X-Client": "test"}
        self.wfile = io.BytesIO()
        self.responses: list[int] = []
        self.sent_headers: list[tuple[str, str]] = []
        self.ended = 0
        self.json_errors: list[tuple[dict[str, Any], int]] = []
        self.model_errors: list[Exception] = []

    def send_response(self, status: int) -> None:
        self.responses.append(status)

    def send_header(self, key: str, value: str) -> None:
        self.sent_headers.append((key, value))

    def end_headers(self) -> None:
        self.ended += 1

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self.json_errors.append((payload, status))

    def _send_all_models_failed_error(self, err: Exception) -> None:
        self.model_errors.append(err)


def test_m3d_frozen_methods_and_non_target_post_branches_match_m3c_baseline() -> None:
    baseline = subprocess.check_output(
        ["git", "show", "2ca4d05:app.py"], cwd=ROOT, text=True, encoding="utf-8"
    )
    current = (ROOT / "app.py").read_text(encoding="utf-8")
    assert _methods(baseline, FROZEN_METHODS) == _methods(current, FROZEN_METHODS)


class _NormalizeMovedProxyAst(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = {"self": "handler", "ctx": "route", "raw": "raw_body"}.get(node.id, node.id)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id == "parsed" and node.attr == "path":
            return ast.copy_location(ast.Name(id="path", ctx=node.ctx), node)
        if isinstance(node.value, ast.Name) and node.value.id == "handler" and node.attr == "_all_models_failed_error_type":
            return ast.copy_location(ast.Name(id="ALL_MODELS_ERROR", ctx=node.ctx), node)
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.ExceptHandler:
        self.generic_visit(node)
        if isinstance(node.type, ast.Name) and node.type.id == "AllModelsFailedError":
            node.type = ast.Name(id="ALL_MODELS_ERROR", ctx=ast.Load())
        return node


def _proxy_branch(source: str) -> ast.If:
    for node in ast.walk(_methods(source, {"do_POST"}) and ast.parse(source)):
        if isinstance(node, ast.If) and "/v1/" in ast.unparse(node.test) and "/chat/" in ast.unparse(node.test):
            return node
    raise AssertionError("proxy branch missing")


def test_m3d_moved_proxy_execution_ast_matches_m3c_baseline_after_normalization() -> None:
    baseline = subprocess.check_output(
        ["git", "show", "2ca4d05:app.py"], cwd=ROOT, text=True, encoding="utf-8"
    )
    legacy_try = next(node for node in _proxy_branch(baseline).body if isinstance(node, ast.Try))
    runtime_tree = ast.parse(inspect.getsource(handle_proxy_request))
    runtime_fn = next(node for node in runtime_tree.body if isinstance(node, ast.FunctionDef))
    runtime_try = next(node for node in runtime_fn.body if isinstance(node, ast.Try))

    def normalized(node: ast.AST) -> str:
        return ast.dump(ast.fix_missing_locations(_NormalizeMovedProxyAst().visit(node)), include_attributes=False)

    assert normalized(legacy_try) == normalized(runtime_try)


def test_m3d_non_stream_success_preserves_headers_length_and_body() -> None:
    router = FakeRouter()
    router.call_result = (201, {"X-Upstream": "ok", "Content-Length": "ignored", "Connection": "close", "Transfer-Encoding": "chunked"}, b"done")
    handler = FakeHandler(router)

    handle_proxy_request(handler, "/v1/chat/completions", {"stream": False}, object(), b"{}")

    assert handler.responses == [201]
    assert ("X-Upstream", "ok") in handler.sent_headers
    assert ("Content-Length", "4") in handler.sent_headers
    assert not any(key.lower() in {"connection", "transfer-encoding"} for key, _ in handler.sent_headers)
    assert handler.wfile.getvalue() == b"done"


def test_m3d_stream_success_flushes_in_order_closes_and_finalizes() -> None:
    router = FakeRouter()
    iterator = FakeIterator([b"first", b"second"])
    router.stream_result = (200, {"X-Upstream": "ok", "Content-Length": "ignored"}, iterator, "request-1")
    handler = FakeHandler(router)
    flushes: list[bool] = []
    handler.wfile.flush = lambda: flushes.append(True)  # type: ignore[method-assign]

    handle_proxy_request(handler, "/chat/completions", {"stream": True}, object(), b"{}")

    assert handler.wfile.getvalue() == b"firstsecond"
    assert flushes == [True, True]
    assert iterator.closed is True
    assert router.finalized == ["request-1"]
    assert ("Content-Type", "text/event-stream; charset=utf-8") in handler.sent_headers


def test_m3d_keeps_existing_error_mappings() -> None:
    class ModelFailRouter(FakeRouter):
        def call(self, *args: Any) -> Any:
            raise AllModelsFailedError("失败")

    handler = FakeHandler(ModelFailRouter())
    handle_proxy_request(handler, "/v1/chat/completions", {}, object(), b"{}")
    assert len(handler.model_errors) == 1

    class UnexpectedRouter(FakeRouter):
        def call(self, *args: Any) -> Any:
            raise RuntimeError("boom")

    handler = FakeHandler(UnexpectedRouter())
    handle_proxy_request(handler, "/v1/chat/completions", {}, object(), b"{}")
    assert handler.json_errors == [({"error": {"message": "服务器内部错误: boom", "type": "internal_server_error", "code": "internal_error"}}, 500)]


def test_m3d_invalid_context_does_not_enter_runtime() -> None:
    handler = SimpleNamespace(
        path="/v1/chat/completions",
        _require_route_context=lambda: None,
        _read_raw_body=lambda: (_ for _ in ()).throw(AssertionError("must not read body")),
    )
    RouterHandler.do_POST(handler)
