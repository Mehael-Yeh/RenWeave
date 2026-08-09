from __future__ import annotations

import io
import hashlib
import pickle
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterator


class RpaError(RuntimeError):
    pass


class UnsafeArchivePath(RpaError):
    pass


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        raise pickle.UnpicklingError(f"archive index requested forbidden global: {module}.{name}")


def _safe_unpickle(data: bytes):
    return _RestrictedUnpickler(io.BytesIO(data), encoding="latin1").load()


@dataclass(slots=True, frozen=True)
class RpaSegment:
    offset: int
    length: int
    prefix: bytes = b""


@dataclass(slots=True, frozen=True)
class ExtractedFile:
    archive_path: str
    member_name: str
    output_path: str
    size: int
    sha256: str


@dataclass(slots=True)
class ExtractionManifest:
    archive_path: str
    version: str
    output_dir: str
    files: list[ExtractedFile]

    def to_dict(self) -> dict:
        return asdict(self)


class RpaArchive:
    """Read-only RPA 2.0/3.0/3.2 archive reader with safe extraction."""

    SUPPORTED = frozenset({"RPA-2.0", "RPA-3.0", "RPA-3.2"})

    def __init__(
        self,
        path: str | Path,
        *,
        max_index_bytes: int = 256 * 1024 * 1024,
        max_member_bytes: int = 4 * 1024 * 1024 * 1024,
        max_entries: int = 2_000_000,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.max_index_bytes = max_index_bytes
        self.max_member_bytes = max_member_bytes
        self.max_entries = max_entries
        self._reader: BinaryIO | None = None
        self.version = ""
        self.key = 0
        self.index_offset = 0
        self.members: dict[str, tuple[RpaSegment, ...]] = {}

    def __enter__(self) -> "RpaArchive":
        self.open()
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def open(self) -> None:
        if not self.path.is_file():
            raise RpaError(f"RPA 文件不存在：{self.path}")
        self.close()
        self._reader = self.path.open("rb")
        try:
            self._load_header_and_index()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.members, key=str.casefold))

    def read(self, name: str) -> bytes:
        if self._reader is None:
            raise RpaError("archive is not open")
        try:
            segments = self.members[name]
        except KeyError as exc:
            raise KeyError(f"RPA 中不存在该成员：{name}") from exc
        chunks = []
        for segment in segments:
            payload_length = segment.length - len(segment.prefix)
            if payload_length < 0:
                raise RpaError(f"成员长度小于前缀长度：{name}")
            self._reader.seek(segment.offset)
            payload = self._reader.read(payload_length)
            if len(payload) != payload_length:
                raise RpaError(f"成员数据被截断：{name}")
            chunks.append(segment.prefix + payload)
        result = b"".join(chunks)
        if len(result) > self.max_member_bytes:
            raise RpaError(f"成员超过安全大小限制：{name}")
        return result

    def extract(
        self,
        output_dir: str | Path,
        *,
        include: Callable[[str], bool] | None = None,
    ) -> ExtractionManifest:
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        extracted = []
        for name in self.names():
            if include is not None and not include(name):
                continue
            relative = self.safe_member_path(name)
            target = destination.joinpath(*relative.parts)
            resolved_target = target.resolve(strict=False)
            try:
                resolved_target.relative_to(destination)
            except ValueError as exc:
                raise UnsafeArchivePath(f"归档成员通过符号链接逃逸输出目录：{name!r}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = self.read(name)
            target.write_bytes(payload)
            extracted.append(ExtractedFile(
                archive_path=str(self.path),
                member_name=name,
                output_path=str(target),
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ))
        return ExtractionManifest(
            archive_path=str(self.path),
            version=self.version,
            output_dir=str(destination),
            files=extracted,
        )

    @staticmethod
    def safe_member_path(name: str) -> PurePosixPath:
        normalized = name.replace("\\", "/")
        components = normalized.split("/")
        relative = PurePosixPath(normalized)
        if not normalized or relative.is_absolute():
            raise UnsafeArchivePath(f"拒绝绝对或空归档路径：{name!r}")
        if any(part in {"", ".", ".."} for part in components):
            raise UnsafeArchivePath(f"拒绝路径穿越成员：{name!r}")
        if ":" in relative.parts[0]:
            raise UnsafeArchivePath(f"拒绝驱动器路径成员：{name!r}")
        return relative

    def _load_header_and_index(self) -> None:
        assert self._reader is not None
        header = self._reader.readline(512)
        if not header.endswith(b"\n"):
            raise RpaError("RPA 头无效或过长")
        try:
            fields = header.decode("ascii").strip().split()
        except UnicodeDecodeError as exc:
            raise RpaError("RPA 头不是 ASCII") from exc
        if len(fields) < 2 or fields[0] not in self.SUPPORTED:
            raise RpaError(f"不支持的 RPA 格式：{fields[0] if fields else 'unknown'}")
        self.version = fields[0]
        try:
            self.index_offset = int(fields[1], 16)
            key_fields = fields[2:] if self.version == "RPA-3.0" else fields[3:]
            self.key = 0
            for field in key_fields:
                self.key ^= int(field, 16)
        except ValueError as exc:
            raise RpaError("RPA 头包含无效十六进制字段") from exc

        archive_size = self.path.stat().st_size
        if self.index_offset <= len(header) or self.index_offset >= archive_size:
            raise RpaError("RPA 索引偏移超出文件范围")
        index_size = archive_size - self.index_offset
        if index_size > self.max_index_bytes:
            raise RpaError("RPA 索引超过安全大小限制")
        self._reader.seek(self.index_offset)
        compressed = self._reader.read(index_size)
        try:
            serialized = self._bounded_decompress(compressed)
            raw_index = _safe_unpickle(serialized)
        except (zlib.error, pickle.UnpicklingError, EOFError, ValueError) as exc:
            raise RpaError("无法安全解码 RPA 索引") from exc
        self.members = self._normalize_index(raw_index, archive_size)

    def _normalize_index(self, raw_index, archive_size: int) -> dict[str, tuple[RpaSegment, ...]]:
        if not isinstance(raw_index, dict):
            raise RpaError("RPA 索引不是字典")
        if len(raw_index) > self.max_entries:
            raise RpaError("RPA 索引条目数超过安全限制")
        normalized: dict[str, tuple[RpaSegment, ...]] = {}
        for raw_name, raw_segments in raw_index.items():
            if isinstance(raw_name, bytes):
                name = raw_name.decode("utf-8", errors="surrogateescape")
            elif isinstance(raw_name, str):
                name = raw_name
            else:
                raise RpaError("RPA 成员名类型无效")
            self.safe_member_path(name)
            if not isinstance(raw_segments, (list, tuple)) or not raw_segments:
                raise RpaError(f"RPA 成员索引为空：{name}")
            segments = []
            total_size = 0
            for raw_segment in raw_segments:
                if not isinstance(raw_segment, (list, tuple)) or len(raw_segment) not in {2, 3}:
                    raise RpaError(f"RPA 成员段格式无效：{name}")
                raw_offset, raw_length = raw_segment[:2]
                if not isinstance(raw_offset, int) or not isinstance(raw_length, int):
                    raise RpaError(f"RPA 成员段偏移类型无效：{name}")
                offset = raw_offset ^ self.key if self.version in {"RPA-3.0", "RPA-3.2"} else raw_offset
                length = raw_length ^ self.key if self.version in {"RPA-3.0", "RPA-3.2"} else raw_length
                prefix = raw_segment[2] if len(raw_segment) == 3 else b""
                if isinstance(prefix, str):
                    prefix = prefix.encode("latin1")
                if not isinstance(prefix, bytes):
                    raise RpaError(f"RPA 成员前缀类型无效：{name}")
                if offset < 0 or length < len(prefix) or offset + length - len(prefix) > self.index_offset:
                    raise RpaError(f"RPA 成员范围越界：{name}")
                total_size += length
                if total_size > self.max_member_bytes:
                    raise RpaError(f"RPA 成员超过安全大小限制：{name}")
                segments.append(RpaSegment(offset, length, prefix))
            normalized[name] = tuple(segments)
        return normalized

    def _bounded_decompress(self, compressed: bytes) -> bytes:
        decoder = zlib.decompressobj()
        output = decoder.decompress(compressed, self.max_index_bytes + 1)
        if len(output) > self.max_index_bytes or decoder.unconsumed_tail:
            raise RpaError("RPA 解压后索引超过安全大小限制")
        remaining = self.max_index_bytes + 1 - len(output)
        output += decoder.flush(remaining)
        if len(output) > self.max_index_bytes or not decoder.eof:
            raise RpaError("RPA 解压后索引无效或超过安全大小限制")
        return output


def script_member(name: str) -> bool:
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.casefold()
    return suffix in {".rpy", ".rpyc", ".rpym", ".rpymc", ".py", ".pyc"}
