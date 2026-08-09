from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timezone
from functools import wraps
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


class CancellationRequested(RuntimeError):
    """Internal control-flow signal used only at durable cancellation points."""


class WorkspaceLease:
    """Cross-platform advisory lock preventing two writers in one workspace."""

    def __init__(self, workspace: str | Path) -> None:
        self.path = Path(workspace).expanduser().resolve() / ".renweave-run.lock"
        self.handle = None

    def __enter__(self) -> "WorkspaceLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(
                "Another RenWeave process is already writing to this workspace"
            ) from exc
        return self

    def __exit__(self, *_args) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def exclusive_workspace_run(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with WorkspaceLease(self.workspace):
            return method(self, *args, **kwargs)

    return wrapped


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
            self._append(self.jsonl_path, json.dumps(record, ensure_ascii=False, default=str) + "\n")
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
