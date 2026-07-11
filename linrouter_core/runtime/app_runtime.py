"""Application assembly and CLI startup without importing the legacy app module."""
from __future__ import annotations

import argparse
import json
import socket
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Tuple


def ensure_initial_config(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            {"groups": [], "models": [], "aggregate_models": [], "aggregate_members": []},
            file,
            ensure_ascii=False,
            indent=2,
        )


def pick_port(start_port: int, host: str, max_port_scan: int) -> int:
    for port in range(start_port, start_port + max_port_scan):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port found in range {start_port}-{start_port + max_port_scan - 1}")


def create_application_server(
    host: str,
    port: int,
    config: str | Path,
    *,
    max_port_scan: int,
    store_type: Any,
    settings_store_type: Any,
    router_type: Any,
    handler_type: Any,
) -> Tuple[ThreadingHTTPServer, int, Path]:
    config_path = Path(config)
    ensure_initial_config(config_path)
    store = store_type(config_path)
    store.refresh_expired_cooldowns()
    settings_store = settings_store_type(config_path)
    router = router_type(store, settings_store, log_file=config_path.parent / "lin-router-logs.jsonl")
    selected_port = pick_port(port, host, max_port_scan)

    server = ThreadingHTTPServer((host, selected_port), handler_type)
    server.store = store  # type: ignore[attr-defined]
    server.router = router  # type: ignore[attr-defined]
    server.settings_store = settings_store  # type: ignore[attr-defined]
    return server, selected_port, config_path.resolve()


def run_main(
    *,
    platform: Any,
    create_server: Callable[[str, int, str | Path], Tuple[ThreadingHTTPServer, int, Path]],
    default_config_file: str,
    default_start_port: int,
) -> None:
    default_config = str(platform.get_config_path(default_config_file))
    parser = argparse.ArgumentParser(description="Lin Router proxy UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=default_start_port, type=int)
    parser.add_argument("--config", default=default_config)
    args = parser.parse_args()

    server, port, config_path = create_server(args.host, args.port, args.config)
    print(f"Lin Router running on http://{args.host}:{port}")
    print(f"Config file: {config_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
