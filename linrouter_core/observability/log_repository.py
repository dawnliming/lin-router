from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, List

from .contracts import RequestLog


class LogRepository:
    """JSONL persistence only. It has no runtime, HTTP, or routing dependency."""

    def __init__(self, path: Path, decode: Callable[[dict], RequestLog]) -> None:
        self.path = path
        self._decode = decode

    def load_recent(self, limit: int) -> List[RequestLog]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as file:
            rows = [json.loads(line) for line in file if line.strip()]
        return [self._decode(row) for row in rows[-limit:] if isinstance(row, dict)]

    def append(self, item: RequestLog) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    def trim(self, max_lines: int) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as file:
            lines = [line for line in file if line.strip()]
        if len(lines) <= max_lines:
            return
        with self.path.open("w", encoding="utf-8") as file:
            file.writelines(lines[-max_lines:])

    def read_all(self) -> List[RequestLog]:
        if not self.path.exists():
            return []
        items: List[RequestLog] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    items.append(self._decode(row))
        return items

    def rewrite_oldest_first(self, newest_first: List[RequestLog]) -> None:
        if not self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(asdict(item), ensure_ascii=False) + "\n" for item in reversed(newest_first)]
        with self.path.open("w", encoding="utf-8") as file:
            file.writelines(lines)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
