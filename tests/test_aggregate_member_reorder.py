from linrouter_core.config.models import AggregateMember, AggregateModel
from linrouter_core.config.store import ConfigStore
from linrouter_core.runtime.http_api_runtime import handle_post


class _ReorderHandler:
    def __init__(self, store, payload):
        self.path = "/api/aggregates/agg-1/members/reorder"
        self.store = store
        self.payload = payload
        self.response = None
        self.status = 200

    def _read_json(self):
        return self.payload

    def _send_json(self, response, status=200):
        self.response = response
        self.status = status


def _store_with_members(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.aggregate_models = [AggregateModel(id="agg-1", name="aggregate")]
    store.aggregate_members = [
        AggregateMember(id="member-a", aggregate_id="agg-1", group_id="group", model_id="model-a", priority=1),
        AggregateMember(id="member-b", aggregate_id="agg-1", group_id="group", model_id="model-b", priority=2),
        AggregateMember(id="member-c", aggregate_id="agg-1", group_id="group", model_id="model-c", priority=3),
    ]
    return store


def test_reorder_aggregate_members_replaces_complete_order_and_increments_revision(tmp_path):
    store = _store_with_members(tmp_path)

    ok, message, code, revision = store.reorder_aggregate_members(
        "agg-1", ["member-c", "member-a", "member-b"], expected_revision=0
    )

    assert (ok, message, code, revision) == (True, "", "", 1)
    assert [(member.id, member.priority) for member in store.get_aggregate_members("agg-1")] == [
        ("member-a", 2),
        ("member-b", 3),
        ("member-c", 1),
    ]


def test_reorder_aggregate_members_rejects_missing_or_duplicate_member_ids(tmp_path):
    store = _store_with_members(tmp_path)

    ok, _, code, revision = store.reorder_aggregate_members(
        "agg-1", ["member-a", "member-a", "member-c"], expected_revision=0
    )

    assert not ok
    assert code == "invalid_member_order"
    assert revision == 0
    assert [member.priority for member in store.get_aggregate_members("agg-1")] == [1, 2, 3]


def test_reorder_aggregate_members_rejects_stale_revision_without_writing(tmp_path):
    store = _store_with_members(tmp_path)
    ok, _, _, revision = store.reorder_aggregate_members(
        "agg-1", ["member-c", "member-b", "member-a"], expected_revision=0
    )
    assert ok and revision == 1

    ok, _, code, current_revision = store.reorder_aggregate_members(
        "agg-1", ["member-b", "member-a", "member-c"], expected_revision=0
    )

    assert not ok
    assert code == "aggregate_member_revision_conflict"
    assert current_revision == 1
    assert [member.id for member in sorted(store.get_aggregate_members("agg-1"), key=lambda item: item.priority)] == [
        "member-c", "member-b", "member-a"
    ]


def test_reorder_endpoint_returns_persisted_member_order_and_revision(tmp_path):
    handler = _ReorderHandler(
        _store_with_members(tmp_path),
        {"member_ids": ["member-c", "member-b", "member-a"], "expected_revision": 0},
    )

    handle_post(handler)

    assert handler.status == 200
    assert handler.response["ok"] is True
    assert handler.response["revision"] == 1
    assert [member["id"] for member in handler.response["members"]] == ["member-c", "member-b", "member-a"]


def test_reorder_endpoint_reports_stale_revision_as_structured_conflict(tmp_path):
    store = _store_with_members(tmp_path)
    first = _ReorderHandler(store, {"member_ids": ["member-b", "member-a", "member-c"], "expected_revision": 0})
    handle_post(first)
    stale = _ReorderHandler(store, {"member_ids": ["member-c", "member-b", "member-a"], "expected_revision": 0})
    handle_post(stale)

    assert stale.status == 409
    assert stale.response["error"]["code"] == "aggregate_member_revision_conflict"
    assert stale.response["error"]["revision"] == 1
