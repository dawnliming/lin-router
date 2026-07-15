"""v0.6.3 聚合成员批量添加与手动优先级收口回归。"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from app import ArkProxyRouter, ConfigStore, create_server
from linrouter_core.config.models import AggregateMember, AggregateModel, ConnectionGroup, ModelConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(port: int, path: str, payload: Any) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _config_payload() -> dict[str, Any]:
    return {
        "groups": [
            {
                "id": "g-relay",
                "name": "中转组",
                "provider_type": "relay",
                "base_url": "https://relay.example/v1",
                "route_key": "lr-relay",
            },
            {
                "id": "g-relay-two",
                "name": "第二中转组",
                "provider_type": "relay",
                "base_url": "https://relay-two.example/v1",
                "route_key": "lr-relay-two",
            },
            {
                "id": "g-empty",
                "name": "空中转组",
                "provider_type": "relay",
                "base_url": "https://empty.example/v1",
                "route_key": "lr-empty",
            },
            {
                "id": "g-ark",
                "name": "方舟组",
                "provider_type": "ark",
                "base_url": "https://ark.example/v1",
                "route_key": "lr-ark",
            },
        ],
        "models": [
            {
                "id": "m-existing",
                "name": "已有模型",
                "ep_id": "model-existing",
                "group_id": "g-relay",
                "usable": True,
            },
            {
                "id": "m-new",
                "name": "新增模型",
                "ep_id": "model-new",
                "group_id": "g-relay",
                "usable": True,
            },
            {
                "id": "m-disabled",
                "name": "不可用模型",
                "ep_id": "model-disabled",
                "group_id": "g-relay",
                "usable": False,
            },
            {
                "id": "m-other",
                "name": "其他组模型",
                "ep_id": "model-other",
                "group_id": "g-relay-two",
                "usable": True,
            },
        ],
        "aggregate_models": [
            {
                "id": "ag1",
                "name": "aggregate",
                "route_key": "lr-ag1",
                "strategy": "price_first",
            }
        ],
        "aggregate_members": [
            {
                "id": "am-existing",
                "aggregate_id": "ag1",
                "group_id": "g-relay",
                "model_id": "m-existing",
                "priority": 7,
                "manual_price": 99,
            }
        ],
    }


def _write_config(path: Path) -> None:
    path.write_text(json.dumps(_config_payload(), ensure_ascii=False), encoding="utf-8")


def test_batch_store_appends_in_model_order_and_saves_once(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write_config(path)
    store = ConfigStore(path)
    original_save = store.save
    save_calls = 0

    def counted_save() -> None:
        nonlocal save_calls
        save_calls += 1
        original_save()

    store.save = counted_save  # type: ignore[method-assign]
    result = store.batch_add_aggregate_members("ag1", "g-relay")

    assert result["ok"] is True
    assert result["counts"] == {"added": 1, "skipped": 1, "failed": 1}
    assert result["summary"] == result["counts"]
    assert result["added_count"] == 1
    assert result["added"][0]["model_id"] == "m-new"
    assert result["added"][0]["priority"] == 8
    assert result["skipped"][0]["code"] == "member_exists"
    assert result["failed"][0]["code"] == "model_unusable"
    assert result["revision"] == 1
    assert save_calls == 1
    assert [member.model_id for member in ConfigStore(path).get_aggregate_members("ag1")] == [
        "m-existing",
        "m-new",
    ]


def test_batch_store_rolls_back_members_and_revision_when_save_fails(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write_config(path)
    store = ConfigStore(path)
    members_before = [member.id for member in store.aggregate_members]
    revision_before = store.aggregate_member_revision("ag1")

    def fail_save() -> None:
        raise OSError("disk full")

    store.save = fail_save  # type: ignore[method-assign]
    result = store.batch_add_aggregate_members("ag1", "g-relay", ["m-new"])

    assert result["ok"] is False
    assert result["code"] == "config_save_failed"
    assert result["added_count"] == 0
    assert result["failed"][0]["code"] == "config_save_failed"
    assert [member.id for member in store.aggregate_members] == members_before
    assert store.aggregate_member_revision("ag1") == revision_before
    assert [member.id for member in ConfigStore(path).aggregate_members] == members_before


def test_batch_http_e2e_reports_item_results_and_keeps_groups_isolated(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write_config(path)
    server, port, _ = create_server("127.0.0.1", _free_port(), path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, normalized = _post_json(
            port,
            "/api/aggregates",
            {"id": "ag1", "name": "aggregate", "strategy": "weighted"},
        )
        assert status == 200
        assert normalized["aggregate_model"]["strategy"] == "priority"

        status, first = _post_json(port, "/api/aggregates/ag1/members/batch", {"group_id": "g-relay"})
        assert status == 200
        assert first["counts"] == {"added": 1, "skipped": 1, "failed": 1}
        assert [member["model_id"] for member in first["members"]] == ["m-existing", "m-new"]

        status, repeated = _post_json(port, "/api/aggregates/ag1/members/batch", {"group_id": "g-relay"})
        assert status == 200
        assert repeated["counts"] == {"added": 0, "skipped": 2, "failed": 1}
        assert "未新增" in repeated["message"] or "没有可添加" in repeated["message"]
        assert repeated["revision"] == first["revision"]

        status, other_group = _post_json(
            port,
            "/api/aggregates/ag1/members/batch",
            {"group_id": "g-relay-two", "model_ids": ["m-other", "m-new"]},
        )
        assert status == 200
        assert other_group["counts"] == {"added": 1, "skipped": 0, "failed": 1}
        assert other_group["added"][0]["model_id"] == "m-other"
        assert other_group["failed"][0]["code"] == "model_not_in_group"
        assert [member["priority"] for member in other_group["members"]] == [7, 8, 9]

        # 真实 HTTP 保存后重新加载，确认结果不是仅停留在响应或内存中。
        persisted = ConfigStore(path)
        assert [member.model_id for member in persisted.get_aggregate_members("ag1")] == [
            "m-existing",
            "m-new",
            "m-other",
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("aggregate_id", "group_id", "expected_status", "expected_code", "message_part"),
    [
        ("missing", "g-relay", 404, "aggregate_not_found", "聚合模型不存在"),
        ("ag1", "missing", 404, "group_not_found", "连接组不存在"),
        ("ag1", "g-ark", 400, "aggregate_group_not_relay", "relay"),
    ],
)
def test_batch_http_e2e_rejects_invalid_aggregate_or_group(
    tmp_path: Path,
    aggregate_id: str,
    group_id: str,
    expected_status: int,
    expected_code: str,
    message_part: str,
) -> None:
    path = tmp_path / "config.json"
    _write_config(path)
    server, port, _ = create_server("127.0.0.1", _free_port(), path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, result = _post_json(
            port,
            f"/api/aggregates/{aggregate_id}/members/batch",
            {"group_id": group_id},
        )
        assert status == expected_status
        assert result["ok"] is False
        assert result["code"] == expected_code
        assert message_part in result["message"]
        assert result["counts"] == {"added": 0, "skipped": 0, "failed": 0}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_batch_http_e2e_empty_group_and_invalid_model_ids_are_clear(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    _write_config(path)
    server, port, _ = create_server("127.0.0.1", _free_port(), path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, empty = _post_json(port, "/api/aggregates/ag1/members/batch", {"group_id": "g-empty"})
        assert status == 200
        assert empty["counts"] == {"added": 0, "skipped": 0, "failed": 0}
        assert "没有可添加的模型" in empty["message"]

        status, invalid = _post_json(
            port,
            "/api/aggregates/ag1/members/batch",
            {"group_id": "g-relay", "model_ids": "m-new"},
        )
        assert status == 400
        assert invalid["error"]["code"] == "invalid_model_ids"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("legacy_strategy", ["price_first", "weighted", "unknown", ""])
def test_legacy_aggregate_strategy_reads_and_saves_as_priority(
    tmp_path: Path,
    legacy_strategy: str,
) -> None:
    payload = _config_payload()
    payload["aggregate_models"][0]["strategy"] = legacy_strategy
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(path)
    aggregate = store.find_aggregate("ag1")
    assert aggregate is not None
    assert aggregate.strategy == "priority"

    ok, message = store.upsert_aggregate(aggregate)
    assert ok and message == ""
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["aggregate_models"][0]["strategy"] == "priority"


def test_legacy_price_strategy_never_changes_runtime_priority_order(tmp_path: Path) -> None:
    payload = _config_payload()
    payload["models"][1]["usable"] = True
    payload["aggregate_models"][0]["strategy"] = "price_first"
    payload["aggregate_members"].append(
        {
            "id": "am-cheaper",
            "aggregate_id": "ag1",
            "group_id": "g-relay",
            "model_id": "m-new",
            "priority": 8,
            "manual_price": 1,
        }
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    router = ArkProxyRouter(ConfigStore(path), None, tmp_path / "logs.jsonl")
    aggregate = router.store.find_aggregate("ag1")
    assert aggregate is not None

    candidates = list(router._iter_aggregate_candidates(aggregate))

    assert aggregate.strategy == "priority"
    assert [candidate.model.id for candidate in candidates if candidate.model] == ["m-existing", "m-new"]
    suffix = router._aggregate_log_suffix(
        "aggregate",
        aggregate.name,
        aggregate.id,
        "中转组",
        "已有模型",
        "model-existing",
        "priority_first",
        0,
        [],
        aggregate.strategy,
        99,
    )
    assert "; strategy=priority" in suffix


def test_direct_aggregate_construction_also_normalizes_removed_strategies() -> None:
    assert AggregateModel(id="a1", name="a", strategy="price_first").strategy == "priority"
    assert AggregateModel(id="a2", name="b", strategy="weighted").strategy == "priority"


def test_batch_store_contract_accepts_only_relay_and_usable_models(tmp_path: Path) -> None:
    """直接构造也必须执行同一组后端规则，不能依赖 HTTP 前端过滤。"""
    store = ConfigStore(tmp_path / "config.json")
    store.groups = [ConnectionGroup(id="g1", name="relay", provider_type="relay")]
    store.models = [
        ModelConfig(id="m1", name="one", ep_id="one", group_id="g1", usable=True),
        ModelConfig(id="m2", name="two", ep_id="two", group_id="g1", usable=False),
    ]
    store.aggregate_models = [AggregateModel(id="a1", name="aggregate")]
    store.aggregate_members = [
        AggregateMember(id="am1", aggregate_id="a1", group_id="g1", model_id="m1", priority=3)
    ]

    result = store.batch_add_aggregate_members("a1", "g1")

    assert result["counts"] == {"added": 0, "skipped": 1, "failed": 1}
    assert [item["code"] for item in result["skipped"]] == ["member_exists"]
    assert [item["code"] for item in result["failed"]] == ["model_unusable"]
