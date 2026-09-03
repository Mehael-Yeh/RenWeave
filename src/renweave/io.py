from __future__ import annotations

import json
import os
import tempfile
import shutil
from pathlib import Path
from typing import Any


def discard_intermediate(path: str | Path, *, recursive: bool = False) -> None:
    """Delete only an explicitly named underscore-prefixed intermediate."""
    target = Path(path)
    if not target.name.startswith("_"):
        raise ValueError(f"拒绝删除非下划线前缀的中间产物：{target}")
    if recursive:
        if target.exists():
            shutil.rmtree(target)
        return
    target.unlink(missing_ok=True)


def read_text_preserving(path: Path) -> tuple[str, str, str, bool]:
    data = path.read_bytes()
    has_bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if has_bom else data
    encoding = "utf-8"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = body.decode(encoding)
    newline = "\r\n" if body.count(b"\r\n") >= max(1, body.count(b"\n") // 2) else "\n"
    return text, encoding, newline, has_bom


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f"_{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as writer:
            json.dump(payload, writer, ensure_ascii=False, indent=2)
            writer.write("\n")
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            discard_intermediate(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f"_{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as writer:
            writer.write(text)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            discard_intermediate(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f"_{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            discard_intermediate(temporary)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as reader:
        return json.load(reader)
