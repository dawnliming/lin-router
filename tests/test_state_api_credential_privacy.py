"""状态 API 的上游凭证脱敏与无密钥编辑回归。"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from pathlib import Path

from app import create_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request_json(port: int, path: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_state_api_hides_raw_credentials_and_blank_updates_preserve_them(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    group_secret = "GROUP_SECRET_MUST_NOT_LEAK"
    model_secret = "MODEL_SECRET_MUST_NOT_LEAK"
    config_path.write_text(json.dumps({
        "groups": [
            {
                "id": "g-ark", "name": "ark", "provider_type": "ark",
                "base_url": "https://ark.example.test/v1", "ark_api_key": group_secret,
                "route_key": "ark-route",
            },
            {
                "id": "g-relay", "name": "relay", "provider_type": "relay",
                "base_url": "https://relay.example.test/v1", "route_key": "relay-route",
            },
        ],
        "models": [{
            "id": "m-relay", "name": "relay-model", "ep_id": "upstream-model",
            "upstream_model": "upstream-model", "group_id": "g-relay", "api_key": model_secret,
        }],
        "aggregate_models": [],
        "aggregate_members": [],
    }, ensure_ascii=False), encoding="utf-8")

    server, port, _ = create_server("127.0.0.1", _free_port(), config_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state = _request_json(port, "/api/state")
        serialized = json.dumps(state, ensure_ascii=False)
        assert group_secret not in serialized
        assert model_secret not in serialized

        ark_group = next(item for item in state["groups"] if item["id"] == "g-ark")
        relay_model = state["models"][0]
        assert ark_group["ark_api_key_configured"] is True
        assert "ark_api_key" not in ark_group
        assert relay_model["api_key_configured"] is True
        assert "api_key" not in relay_model

        # 状态接口脱敏后，普通编辑不携带密钥也必须保持现有持久化值。
        _request_json(port, "/api/groups/g-ark", method="PUT", payload={
            "name": "ark-renamed", "provider_type": "ark", "base_url": "https://ark.example.test/v1",
            "ark_api_key": "",
        })
        _request_json(port, "/api/models/m-relay", method="PUT", payload={
            "name": "relay-model-renamed", "ep_id": "upstream-model",
            "upstream_model": "upstream-model", "group_id": "g-relay", "api_key": "",
            "usable": True,
        })
        assert server.store.find_group("g-ark").ark_api_key == group_secret
        assert server.store.find_model("m-relay").api_key == model_secret
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
