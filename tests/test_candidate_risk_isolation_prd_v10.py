"""冻结 PRD v1.0：上游风控隔离的作用域、阶梯和脱敏契约。"""
from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

from app import ArkProxyRouter, ConfigStore
from settings_store import SettingsStore


RAW_WAF_BODY = "WAF_BODY_SENTINEL: request blocked by upstream"
CREDENTIAL_A = "test-risk-credential-a"
CREDENTIAL_B = "test-risk-credential-b"


def _router(tmp_path: Path) -> ArkProxyRouter:
    config = {
        "groups": [
            {
                "id": "g1",
                "name": "same-host",
                "provider_type": "relay",
                "base_url": "https://same.example.test/v1",
                "route_key": "group-key-1",
            },
            {
                "id": "g2",
                "name": "other-host",
                "provider_type": "relay",
                "base_url": "https://other.example.test/v1",
                "route_key": "group-key-2",
            },
            {
                "id": "g3",
                "name": "proxy-pass-through",
                "provider_type": "proxy",
                "base_url": "https://proxy.example.test/v1",
                "api_key": CREDENTIAL_A,
                "route_key": "group-key-3",
            },
        ],
        "models": [
            {"id": "m1", "name": "m1", "ep_id": "m1", "group_id": "g1", "api_key": CREDENTIAL_A, "usable": True},
            {"id": "m2", "name": "m2", "ep_id": "m2", "group_id": "g1", "api_key": CREDENTIAL_A, "usable": True},
            {"id": "m3", "name": "m3", "ep_id": "m3", "group_id": "g1", "api_key": CREDENTIAL_B, "usable": True},
            {"id": "m4", "name": "m4", "ep_id": "m4", "group_id": "g2", "api_key": CREDENTIAL_A, "usable": True},
        ],
        "aggregate_models": [{"id": "a1", "name": "aggregate", "route_key": "aggregate-key"}],
        "aggregate_members": [
            {"id": "am1", "aggregate_id": "a1", "group_id": "g1", "model_id": "m1", "enabled": True, "priority": 0},
            {"id": "am3", "aggregate_id": "a1", "group_id": "g1", "model_id": "m3", "enabled": True, "priority": 1},
        ],
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return ArkProxyRouter(ConfigStore(path), SettingsStore(path), tmp_path / "logs.jsonl")


def _candidate(router: ArkProxyRouter, model_id: str):
    model = router.store.find_model(model_id)
    assert model is not None
    group = router.store.find_group(model.group_id)
    assert group is not None
    return router.candidate_health.candidate_from_model(router.store.models.index(model), model, group)


def _record_waf_pair(router: ArkProxyRouter, model_id: str) -> dict[str, object]:
    candidate = _candidate(router, model_id)
    router.candidate_health.record_risk_attempt(candidate, "waf_blocked")
    return router.candidate_health.record_risk_attempt(candidate, "waf_blocked")


def test_risk_isolation_is_host_and_credential_scoped_without_breaker_pollution(tmp_path: Path) -> None:
    router = _router(tmp_path)
    status = _record_waf_pair(router, "m1")

    assert status["risk_isolated"] is True
    assert status["risk_level"] == 1
    assert status["risk_cooldown_seconds"] > 0
    assert status["risk_affected_models"] == 2
    assert router.store.find_model("m1").attempt_window == []
    assert router.store.find_model("m1").breaker_level == 0

    # 同 host + 同凭证的 m1/m2 跳过；不同凭证、不同 host 仍可 fallback。
    same_host = [candidate.model.id for candidate in router.candidate_health.iter_upstream_candidates("lin-router-auto", "g1")]
    assert same_host == ["m3"]
    other_host = [candidate.model.id for candidate in router.candidate_health.iter_upstream_candidates("lin-router-auto", "g2")]
    assert other_host == ["m4"]

    aggregate = router.store.find_aggregate("a1")
    assert aggregate is not None
    aggregate_candidates = [candidate.aggregate_member_id for candidate in router.candidate_health.iter_aggregate_candidates(aggregate)]
    assert aggregate_candidates == ["am3"]
    member = router.store.find_aggregate_member("am1")
    assert member is not None
    assert router.candidate_health.aggregate_member_skip_reason(member)[0] == "risk_isolated"

    # 没有实体模型的 proxy pass-through 也必须尊重同一风险 scope。
    proxy_group = router.store.find_group("g3")
    assert proxy_group is not None
    proxy_candidate = router.candidate_health._candidate_type(
        idx=None,
        group=proxy_group,
        model=None,
        label="proxy-model",
        target_model="proxy-model",
        auth_key=CREDENTIAL_A,
        channel="pass-through",
    )
    _ = router.candidate_health.record_risk_attempt(proxy_candidate, "waf_blocked")
    _ = router.candidate_health.record_risk_attempt(proxy_candidate, "waf_blocked")
    assert list(router.candidate_health.iter_upstream_candidates("proxy-model", "g3")) == []

    # 公共运行态和本地索引均不能把凭证、摘要、WAF 页面正文带出。
    public_status = router.candidate_health.risk_status_for_model(router.store.find_model("m1"))
    public_text = json.dumps(public_status, ensure_ascii=False)
    assert CREDENTIAL_A not in public_text
    assert "credential_digest" not in public_text
    assert "host" not in public_text
    assert RAW_WAF_BODY not in public_text

    risk_index = tmp_path / "config.risk-index.json"
    persisted = risk_index.read_text(encoding="utf-8")
    assert CREDENTIAL_A not in persisted
    assert RAW_WAF_BODY not in persisted

    reloaded = ArkProxyRouter(ConfigStore(tmp_path / "config.json"), SettingsStore(tmp_path / "config.json"), tmp_path / "logs-reloaded.jsonl")
    assert reloaded.candidate_health.risk_status_for_model(reloaded.store.find_model("m1"))["risk_isolated"] is True


def test_risk_index_is_bound_to_its_own_config_file(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _router(first_dir)
    second = _router(second_dir)

    _record_waf_pair(first, "m1")

    first_model = first.store.find_model("m1")
    second_model = second.store.find_model("m1")
    assert first_model is not None
    assert second_model is not None
    assert first.candidate_health.risk_status_for_model(first_model)["risk_isolated"] is True
    # 独立配置不能误读同目录外的 mock/测试配置风险索引。
    assert second.candidate_health.risk_status_for_model(second_model)["risk_isolated"] is False
    assert (first_dir / "config.risk-index.json").exists()
    assert not (second_dir / "config.risk-index.json").exists()


def test_manual_probe_waf_attempts_enter_risk_scope_then_future_probe_is_blocked(tmp_path: Path) -> None:
    """手动恢复探测属于真实上游尝试，WAF 拦截必须进入独立风险窗口。"""

    router = _router(tmp_path)
    candidate = _candidate(router, "m1")

    class WafProbeClient:
        calls = 0

        def request(self, _method: str, url: str, *_args: object, **_kwargs: object):
            self.calls += 1
            raise HTTPError(
                url,
                403,
                "forbidden",
                hdrs={},
                fp=io.BytesIO(b'{"error":{"message":"request blocked by upstream WAF"}}'),
            )

    upstream = WafProbeClient()
    router._upstream_client = upstream  # type: ignore[assignment]

    assert router._manual_probe_candidate(candidate)[1] == "waf_blocked"
    assert router._manual_probe_candidate(candidate)[1] == "waf_blocked"

    model = router.store.find_model("m1")
    assert model is not None
    assert upstream.calls == 2
    assert router.candidate_health.risk_status_for_model(model)["risk_isolated"] is True
    assert model.attempt_window == []

    # 已隔离时手动探测不再触达上游；直接入口与测速共用这一防线。
    assert router._manual_probe_candidate(candidate)[1] == "risk_isolated"
    assert upstream.calls == 2


def test_non_waf_outcomes_never_trigger_risk_isolation(tmp_path: Path) -> None:
    """429/5xx/网络错误等非 WAF 结果只记 other，不会触发风险隔离。"""

    router = _router(tmp_path)
    candidate = _candidate(router, "m1")

    # 连续 5 次 other（模拟 429 重试、5xx、网络错误等）不应隔离。
    for _ in range(5):
        status = router.candidate_health.record_risk_attempt(candidate, "other")
    assert status["risk_isolated"] is False
    assert status["risk_level"] == 0

    # 连续 5 次 success 也不应隔离。
    for _ in range(5):
        status = router.candidate_health.record_risk_attempt(candidate, "success")
    assert status["risk_isolated"] is False

    # 只有明确 waf_blocked 才进入风险隔离。
    status = router.candidate_health.record_risk_attempt(candidate, "waf_blocked")
    assert status["risk_isolated"] is False
    status = router.candidate_health.record_risk_attempt(candidate, "waf_blocked")
    assert status["risk_isolated"] is True
    assert status["risk_level"] == 1


def test_risk_release_requires_confirmation_does_not_probe_and_escalates(tmp_path: Path) -> None:
    router = _router(tmp_path)
    _record_waf_pair(router, "m1")
    model = router.store.find_model("m1")
    assert model is not None

    probe_calls: list[object] = []
    router._manual_probe_candidate = lambda candidate: probe_calls.append(candidate)  # type: ignore[method-assign]

    denied = router.release_model_risk_isolation(model.id, confirmed=False)
    assert denied["code"] == "risk_recovery_confirmation_required"
    assert probe_calls == []

    released = router.release_model_risk_isolation(model.id, confirmed=True)
    assert released["ok"] is True
    assert released["risk_isolated"] is False
    assert probe_calls == []

    level_two = _record_waf_pair(router, "m1")
    assert level_two["risk_isolated"] is True
    assert level_two["risk_level"] == 2
    assert level_two["risk_cooldown_seconds"] > 50 * 60

    assert router.release_model_risk_isolation(model.id, confirmed=True)["ok"] is True
    level_three = _record_waf_pair(router, "m1")
    assert level_three["risk_isolated"] is True
    assert level_three["risk_level"] == 3
    assert level_three["risk_cooldown_seconds"] > 5 * 60 * 60

    assert router.release_model_risk_isolation(model.id, confirmed=True)["ok"] is True
    level_four = _record_waf_pair(router, "m1")
    assert level_four["risk_level"] == 4
    # 第四档及以后必须封顶 6 小时，避免无限延长。
    assert 5 * 60 * 60 < int(level_four["risk_cooldown_seconds"]) <= 6 * 60 * 60
    assert probe_calls == []


def test_risk_runtime_data_and_frontend_contract_do_not_expose_scope_values() -> None:
    root = Path(__file__).resolve().parent.parent
    app_source = (root / "app.py").read_text(encoding="utf-8")
    config_source = (root / "static/js/config-tab.js").read_text(encoding="utf-8")
    action_source = (root / "static/js/config-tab-actions.js").read_text(encoding="utf-8")

    assert '"risk_isolated"' in app_source
    assert "credential_digest" not in app_source[app_source.index("def _model_runtime_item"):app_source.index("def _member_runtime_item")]
    assert "上游风控保护" in config_source
    assert "风险隔离" in config_source
    assert "解除后后续请求会再次访问上游" in action_source
    assert "releaseModelRiskIsolation" in action_source


def test_candidate_health_service_construction_tolerates_store_without_path() -> None:
    """P0-1 回归：既有观测/上游契约使用没有 path 属性的 Store 替身，
    风控索引必须退化为纯内存模式，不能让 ArkProxyRouter 构造即 AttributeError。"""

    from linrouter_core.config.models import (
        AggregateMember,
        AggregateModel,
        ConnectionGroup,
        ModelConfig,
    )
    from linrouter_core.config.store import ConfigStore
    from linrouter_core.runtime.candidate_health import CandidateHealthService

    class PathlessStore(ConfigStore):
        """模拟既有契约测试中没有 path 属性的 Store 替身。"""

        @property
        def path(self):  # noqa: D401 - 故意抛出 AttributeError 模拟替身
            raise AttributeError("path")

    group = ConnectionGroup(
        id="g1", name="pathless", provider_type="relay",
        base_url="https://pathless.example.test/v1", route_key="rk",
    )
    model = ModelConfig(
        id="m1", name="m1", ep_id="m1", group_id="g1",
        api_key=CREDENTIAL_A, usable=True,
    )
    store = PathlessStore.__new__(ConfigStore)
    store.groups = [group]
    store.models = [model]
    store.aggregate_models = []
    store.aggregate_members = []

    service = CandidateHealthService(
        store=store,
        now=lambda: 0,
        is_auto_model=lambda *_a, **_kw: False,
        mode_for=lambda _g: "relay",
        group_for=lambda _store, _gid: group,
        auth_for=lambda _g, _m: CREDENTIAL_A,
        candidate_type=lambda **_kw: None,
        log_aggregate_member_skip=lambda *_a, **_kw: None,
    )

    # 纯内存模式下风险隔离在进程内仍即时生效。
    assert service._risk_index_path is None
    assert service._risk_scopes == {}
