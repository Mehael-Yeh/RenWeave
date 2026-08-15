from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .emitter import BuildManifest
from .rpa import RpaArchive, RpaError, RpaWriter


@dataclass(slots=True, frozen=True)
class PackagedMember:
    name: str
    size: int
    sha256: str


@dataclass(slots=True)
class PackageManifest:
    schema_version: int
    format: str
    archive_path: str
    archive_size: int
    archive_sha256: str
    members: list[PackagedMember]
    runtime_ready: bool = False
    source_members: int = 0
    compiled_members: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class TranslationPackager:
    """Build and independently verify a distributable Ren'Py RPA language pack."""

    def package(
        self,
        build: BuildManifest,
        packages_dir: str | Path,
        *,
        compiled_project: str | Path | None = None,
    ) -> PackageManifest:
        language_dir = Path(build.output_dir).expanduser().resolve()
        prefix = PurePosixPath("game", "tl", build.renpy_language)
        payloads: dict[str, bytes] = {}
        expected: list[PackagedMember] = []
        for emitted in build.files:
            relative = PurePosixPath(emitted.relative_path)
            try:
                local_relative = relative.relative_to(prefix)
            except ValueError as exc:
                raise RpaError(f"构建产物不在目标语言目录：{emitted.relative_path}") from exc
            source = language_dir.joinpath(*local_relative.parts).resolve()
            try:
                source.relative_to(language_dir)
            except ValueError as exc:
                raise RpaError(f"构建产物逃逸目标语言目录：{emitted.relative_path}") from exc
            if not source.is_file() or source.is_symlink():
                raise RpaError(f"构建产物缺失或为符号链接：{source}")
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != emitted.sha256:
                raise RpaError(f"构建产物在归档前发生变化：{source}")
            member_name = PurePosixPath("tl", build.renpy_language, *local_relative.parts).as_posix()
            payloads[member_name] = payload
            expected.append(PackagedMember(member_name, len(payload), digest))

        compiled_members = 0
        if compiled_project is not None:
            compiled_root = (
                Path(compiled_project).expanduser().resolve()
                / "game" / "tl" / build.renpy_language
            )
            for emitted in build.files:
                relative = PurePosixPath(emitted.relative_path).relative_to(prefix)
                compiled = compiled_root.joinpath(*relative.parts).with_suffix(".rpyc").resolve()
                try:
                    compiled.relative_to(compiled_root)
                except ValueError as exc:
                    raise RpaError(f"编译产物逃逸目标语言目录：{compiled}") from exc
                if not compiled.is_file() or compiled.is_symlink():
                    raise RpaError(f"缺少已验证的 RPYC 编译产物：{compiled}")
                payload = compiled.read_bytes()
                if not payload:
                    raise RpaError(f"RPYC 编译产物为空：{compiled}")
                member_name = PurePosixPath(
                    "tl", build.renpy_language, *relative.with_suffix(".rpyc").parts
                ).as_posix()
                digest = hashlib.sha256(payload).hexdigest()
                payloads[member_name] = payload
                expected.append(PackagedMember(member_name, len(payload), digest))
                compiled_members += 1

        destination = Path(packages_dir).expanduser().resolve() / f"renweave-{build.renpy_language}.rpa"
        RpaWriter().write(destination, payloads)
        expected_by_name = {item.name: item for item in expected}
        with RpaArchive(destination) as archive:
            if set(archive.names()) != set(expected_by_name):
                raise RpaError("RPA 写后校验发现成员清单不一致")
            for name in archive.names():
                payload = archive.read(name)
                item = expected_by_name[name]
                if len(payload) != item.size or hashlib.sha256(payload).hexdigest() != item.sha256:
                    raise RpaError(f"RPA 写后校验失败：{name}")
        archive_payload = destination.read_bytes()
        return PackageManifest(
            schema_version=2,
            format=RpaWriter.VERSION,
            archive_path=str(destination),
            archive_size=len(archive_payload),
            archive_sha256=hashlib.sha256(archive_payload).hexdigest(),
            members=sorted(expected, key=lambda item: (item.name.casefold(), item.name)),
            runtime_ready=compiled_members == len(build.files) and bool(build.files),
            source_members=len(build.files),
            compiled_members=compiled_members,
        )
