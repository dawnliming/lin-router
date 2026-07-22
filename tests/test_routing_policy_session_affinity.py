from __future__ import annotations

from types import SimpleNamespace

from linrouter_core.runtime.session_affinity import SessionAffinityService
from linrouter_core.upstream.request import build_passthrough_headers, build_waf_compatible_headers


def _candidate(key: str) -> SimpleNamespace:
    return SimpleNamespace(
        aggregate_member_id="",
        model=SimpleNamespace(id=key),
        target_model=key,
    )


def test_session_affinity_prefers_explicit_header_and_reorders_only_eligible_candidates() -> None:
    affinity = SessionAffinityService()
    session_id, status = affinity.extract_session_id(
        {"X-LinRouter-Session": "header-session"},
        {"session_id": "payload-session"},
    )
    assert (session_id, status) == ("header-session", "available")

    context = affinity.context("group:g1", "lin-router-auto", session_id)
    first = [_candidate("m1"), _candidate("m2")]
    affinity.bind(context, first[1])

    ordered, reason = affinity.prioritize(context, first)
    assert reason == "sticky_hit"
    assert [candidate.model.id for candidate in ordered] == ["m2", "m1"]


def test_session_affinity_invalid_session_falls_back_without_retaining_value() -> None:
    affinity = SessionAffinityService()
    session_id, status = affinity.extract_session_id(
        {"X-LinRouter-Session": "invalid\nvalue"},
        {},
    )
    assert session_id is None
    assert status == "ignored_invalid_session"


def test_session_header_is_removed_from_waf_and_passthrough_upstream_paths() -> None:
    incoming = {
        "X-LinRouter-Session": "do-not-forward",
        "X-Keep": "ok",
    }
    waf = build_waf_compatible_headers(incoming, "example.com", stream=False)
    passthrough = build_passthrough_headers("key", incoming, stream=False)

    assert all(name.lower() != "x-linrouter-session" for name in waf)
    assert all(name.lower() != "x-linrouter-session" for name in passthrough)
    assert passthrough["X-Keep"] == "ok"
