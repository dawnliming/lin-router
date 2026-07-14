import json

from app import ArkProxyRouter
from linrouter_core.config.models import ConnectionGroup
from linrouter_core.contracts.runtime_types import UpstreamCandidate


def test_payload_fingerprint_keeps_only_safe_tool_and_stream_option_diagnostics():
    payload = {
        "model": "private-model-name",
        "stream": True,
        "temperature": 0.7,
        "reasoning_effort": "future-effort-do-not-persist",
        "messages": [{"role": "private-role-name", "content": "request body must not appear"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "private_tool_name",
                "description": "private tool description",
                "parameters": {"type": "object"},
            },
        }],
        "functions": [{"name": "private_legacy_function_name", "description": "private legacy description"}],
        "stream_options": {
            "private_stream_option_key": "private stream option value",
            "include_usage": True,
        },
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    fingerprint = ArkProxyRouter._payload_fingerprint(payload, body, "/v1/chat/completions", tools_normalized=True)

    for secret in (
        "private-model-name",
        "private-role-name",
        "private_tool_name",
        "private tool description",
        "private_legacy_function_name",
        "private legacy description",
        "private_stream_option_key",
        "private stream option value",
        "future-effort-do-not-persist",
    ):
        assert secret not in fingerprint

    assert "stream=true" in fingerprint
    assert "temperature_sha256=" in fingerprint
    assert "messages=1" in fingerprint
    assert "roles_sha256=" in fingerprint
    assert "tools_count=1" in fingerprint
    assert "tools_bytes=" in fingerprint
    assert "tools_sha256=" in fingerprint
    assert "functions_count=1" in fingerprint
    assert "functions_bytes=" in fingerprint
    assert "functions_sha256=" in fingerprint
    assert "stream_options_present=true" in fingerprint
    assert "stream_options_keys_count=2" in fingerprint
    assert "stream_options_bytes=" in fingerprint
    assert "stream_options_sha256=" in fingerprint
    assert "reasoning_effort=unrecognized" in fingerprint
    assert "reasoning_effort_status=unrecognized" in fingerprint
    assert "reasoning_effort_bytes=" in fingerprint
    assert "reasoning_effort_sha256=" in fingerprint
    assert "tools_normalized=true" in fingerprint
    assert "body_sha256=" in fingerprint

    router = object.__new__(ArkProxyRouter)
    candidate = UpstreamCandidate(
        idx=0,
        group=ConnectionGroup(id="g1", name="relay", provider_type="relay", base_url="https://relay.example"),
        model=None,
        label="target-model",
        target_model="target-model",
        auth_key="",
    )
    detail = router._debug_detail(
        candidate,
        "client-model",
        "https://relay.example/v1/chat/completions",
        "rebuilt",
        body,
        payload,
        {"Content-Type": "application/json", "Accept": "text/event-stream"},
        "ok",
        tools_normalized=True,
    )
    for secret in (
        "private-model-name",
        "private-role-name",
        "private_tool_name",
        "private tool description",
        "private_legacy_function_name",
        "private legacy description",
        "private_stream_option_key",
        "private stream option value",
        "future-effort-do-not-persist",
    ):
        assert secret not in detail


def test_reasoning_log_fields_redact_unknown_effort_and_keep_allowlist_values():
    group = ConnectionGroup(id="g1", name="relay", provider_type="relay", base_url="https://relay.example")

    unknown_value = "future-effort-do-not-persist"
    unknown_payload = {"model": "client-model", "reasoning": {"effort": unknown_value}}
    unknown_body = json.dumps(unknown_payload, separators=(",", ":")).encode("utf-8")
    unknown_fields = ArkProxyRouter._reasoning_log_fields(
        "/v1/responses", unknown_payload, unknown_body, "raw-model-patch", group,
    )

    assert unknown_value not in unknown_fields
    assert "requested_reasoning_effort=unrecognized" in unknown_fields
    assert "reasoning_value_status=unrecognized" in unknown_fields
    assert "reasoning_effort_bytes=" in unknown_fields
    assert "reasoning_effort_sha256=" in unknown_fields
    assert "reasoning_preserved=true" in unknown_fields

    allowed_payload = {"model": "client-model", "messages": [], "reasoning_effort": "HIGH"}
    allowed_body = json.dumps(allowed_payload, separators=(",", ":")).encode("utf-8")
    allowed_fields = ArkProxyRouter._reasoning_log_fields(
        "/v1/chat/completions", allowed_payload, allowed_body, "rebuilt", group,
    )

    assert "requested_reasoning_effort=high" in allowed_fields
    assert "reasoning_value_status=recognized" in allowed_fields
    assert "reasoning_effort_sha256=" not in allowed_fields
    assert "reasoning_preserved=true" in allowed_fields
