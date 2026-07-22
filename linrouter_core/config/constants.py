"""Configuration-domain constants and stable key generators."""

from __future__ import annotations

import uuid

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_CONFIG_FILE = "lin-router-config.json"
DEFAULT_START_PORT = 18400
DEFAULT_AUTO_MODEL_NAME = "lin-router-auto"
DEFAULT_AUTO_MODEL_COOLDOWN_MINUTES = 5
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 45
MAX_STREAM_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_PUBLIC_API_KEY = "lin-router"
GLOBAL_ROUTE_GROUP_ID = "__global__"
PROVIDER_ARK = "ark"
PROVIDER_RELAY = "relay"
PROVIDER_PROXY = "proxy"


def new_route_key() -> str:
    return f"lr-{uuid.uuid4().hex[:16]}"


def new_aggregate_route_key() -> str:
    return f"lr-ag-{uuid.uuid4().hex[:16]}"
