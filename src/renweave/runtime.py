from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class CancellationToken:
    """Thread-safe cooperative cancellation shared by the GUI and pipeline."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class RunLogger:
    """Append-only diagnostic log with machine-readable and human-readable views."""

    def __init__(self, workspace: str | Path) -> None:
        self.directory = Path(workspace).expanduser().resolve() / "logs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.directory / "events.jsonl"
        self.text_path = self.directory / "renweave.log"
        self._lock = threading.Lock()

    def event(self, level: str, event: str, message: str, **details: Any) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        record = {
            "timestamp": timestamp,
            "level": level.upper(),
            "event": event,
            "message": message,
            "details": details,
        }
        detail_text = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in details.items()
            if value not in (None, "", [], {})
        )
        human = f"{timestamp} [{record['level']}] {event}: {message}"
        if detail_text:
            human += f" | {detail_text}"
        with self._lock:
            self._append(self.jsonl_path, json.dumps(record, ensure_ascii=False) + "\n")
            self._append(self.text_path, human + "\n")

    def exception(self, event: str, exc: BaseException, **details: Any) -> None:
        details = dict(details)
        details["exception_type"] = type(exc).__name__
        details["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-16000:]
        self.event("ERROR", event, str(exc), **details)

    @staticmethod
    def _append(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as writer:
            writer.write(text)
            writer.flush()
            os.fsync(writer.fileno())
