from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .io import atomic_write_bytes, atomic_write_json, read_json


UNRPYC_VERSION = "2.0.2"
UNRPYC_COMMIT = "e16a767bbdd75abcf47a318b20480db4a07f7dfa"
UNRPYC_ARCHIVE_URL = f"https://github.com/CensoredUsername/unrpyc/archive/{UNRPYC_COMMIT}.zip"
UNRPYC_ARCHIVE_SHA256 = "25a273473cdf205a5ada8e0e9681dc5d31de2ba8bfec29d3f51faa49111b4e0d"
MAX_TOOL_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_TOOL_EXTRACTED_BYTES = 80 * 1024 * 1024
COMPILED_SUFFIXES = {".rpyc": ".rpy", ".rpymc": ".rpym"}
ISOLATED_BOOTSTRAP = (
    "import runpy,sys;"
    "tool_dir=sys.argv.pop(1);"
    "entry=sys.argv.pop(1);"
    "sys.path.insert(0,tool_dir);"
    "sys.argv[0]=entry;"
    "runpy.run_path(entry,run_name='__main__')"
)


class DecompilationError(RuntimeError):
    def __init__(self, message: str, manifest=None) -> None:
        super().__init__(message)
        self.manifest = manifest


@dataclass(slots=True, frozen=True)
class DecompiledFile:
    source_path: str
    relative_path: str
    output_path: str
    sha256: str


@dataclass(slots=True)
class DecompilationManifest:
    schema_version: int
    tool: str
    tool_version: str
    output_roots: list[str]
    skipped_with_source: list[str]
    files: list[DecompiledFile]
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class UnrpycToolManager:
    def __init__(self, tools_root: str | Path) -> None:
        self.tools_root = Path(tools_root).expanduser().resolve()
        self.install_dir = self.tools_root / f"unrpyc-{UNRPYC_VERSION}-{UNRPYC_COMMIT[:8]}"

    def resolve(
        self,
        explicit_path: str | Path | None = None,
        *,
        allow_download: bool = True,
    ) -> Path:
        configured = explicit_path or os.environ.get("RENWEAVE_UNRPYC", "")
        if configured:
            return self._entrypoint(Path(configured).expanduser().resolve())
        installed = self.install_dir / "unrpyc.py"
        if installed.is_file():
            return self._validate_install(installed)
        if not allow_download:
            raise DecompilationError(
                "发现仅有 RPYC 的脚本，但 unrpyc 尚未安装；请提供 --unrpyc 或允许工具下载"
            )
        return self._download()

    @staticmethod
    def _entrypoint(path: Path) -> Path:
        entrypoint = path / "unrpyc.py" if path.is_dir() else path
        if not entrypoint.is_file() or entrypoint.name.casefold() != "unrpyc.py":
            raise DecompilationError(f"无效的 unrpyc 入口：{entrypoint}")
        return entrypoint

    def _download(self) -> Path:
        request = urllib.request.Request(
            UNRPYC_ARCHIVE_URL,
            headers={"User-Agent": f"RenWeave/{UNRPYC_VERSION}"},
        )
        payload = bytearray()
        with urllib.request.urlopen(request, timeout=120) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > MAX_TOOL_DOWNLOAD_BYTES:
                    raise DecompilationError("unrpyc 下载包超过安全大小限制")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != UNRPYC_ARCHIVE_SHA256:
            raise DecompilationError(
                f"unrpyc 下载包哈希不匹配：期望 {UNRPYC_ARCHIVE_SHA256}，实际 {digest}"
            )

        self.tools_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".unrpyc-", dir=self.tools_root))
        try:
            self._extract_verified(bytes(payload), staging)
            if not (staging / "unrpyc.py").is_file():
                raise DecompilationError("unrpyc 下载包缺少 unrpyc.py")
            atomic_write_json(staging / "renweave-source.json", {
                "version": UNRPYC_VERSION,
                "commit": UNRPYC_COMMIT,
                "archive_url": UNRPYC_ARCHIVE_URL,
                "archive_sha256": UNRPYC_ARCHIVE_SHA256,
                "tree_sha256": self._tree_digest(staging),
            })
            if self.install_dir.exists():
                raise DecompilationError(f"unrpyc 安装目录已存在但不完整：{self.install_dir}")
            os.replace(staging, self.install_dir)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return self.install_dir / "unrpyc.py"

    def _validate_install(self, entrypoint: Path) -> Path:
        metadata_path = self.install_dir / "renweave-source.json"
        if not metadata_path.is_file():
            raise DecompilationError(f"unrpyc 安装缺少来源清单：{metadata_path}")
        metadata = read_json(metadata_path)
        if (
            metadata.get("commit") != UNRPYC_COMMIT
            or metadata.get("archive_sha256") != UNRPYC_ARCHIVE_SHA256
            or metadata.get("tree_sha256") != self._tree_digest(self.install_dir)
        ):
            raise DecompilationError(f"unrpyc 安装内容校验失败：{self.install_dir}")
        return entrypoint

    @staticmethod
    def _tree_digest(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file() or path.name == "renweave-source.json":
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    @staticmethod
    def _extract_verified(payload: bytes, destination: Path) -> None:
        total = 0
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise DecompilationError(f"unrpyc 下载包包含符号链接：{info.filename}")
                parts = PurePosixPath(info.filename).parts
                if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
                    raise DecompilationError(f"unrpyc 下载包包含不安全路径：{info.filename}")
                relative = Path(*parts[1:])
                target = destination / relative
                total += info.file_size
                if total > MAX_TOOL_EXTRACTED_BYTES:
                    raise DecompilationError("unrpyc 解压内容超过安全大小限制")
                atomic_write_bytes(target, archive.read(info))


class UnrpycDecompiler:
    def __init__(
        self,
        entrypoint: str | Path,
        *,
        python_executable: str | Path | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        self.entrypoint = UnrpycToolManager._entrypoint(Path(entrypoint).expanduser().resolve())
        self.python_executable = str(Path(python_executable or sys.executable).resolve())
        self.timeout_seconds = timeout_seconds

    def version(self) -> str:
        process = subprocess.run(
            [*self._base_command(), "--version"],
            cwd=self.entrypoint.parent,
            env=self._subprocess_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(self.timeout_seconds, 60),
            check=False,
        )
        if process.returncode != 0:
            raise DecompilationError(f"无法启动 unrpyc：{process.stderr[-2000:]}")
        return process.stdout.strip()

    def decompile(
        self,
        source_roots: list[str | Path],
        output_root: str | Path,
    ) -> DecompilationManifest:
        root = Path(output_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[Path, Path, Path]] = []
        skipped: list[str] = []
        output_roots: list[str] = []

        for ordinal, raw_source_root in enumerate(source_roots):
            source_root = Path(raw_source_root).expanduser().resolve()
            if not source_root.is_dir():
                continue
            candidates: list[tuple[Path, Path, str, bytes]] = []
            root_digest = hashlib.sha256(str(source_root).encode("utf-8"))
            for compiled in sorted(source_root.rglob("*"), key=lambda item: item.as_posix().casefold()):
                target_suffix = COMPILED_SUFFIXES.get(compiled.suffix.casefold())
                if target_suffix is None or not compiled.is_file():
                    continue
                relative = compiled.relative_to(source_root)
                if (source_root / relative.with_suffix(target_suffix)).is_file():
                    skipped.append(str(compiled))
                    continue
                payload = compiled.read_bytes()
                root_digest.update(relative.as_posix().encode("utf-8"))
                root_digest.update(hashlib.sha256(payload).digest())
                candidates.append((compiled, relative, target_suffix, payload))
            if not candidates:
                continue
            destination_root = root / f"root-{ordinal}-{root_digest.hexdigest()[:12]}"
            output_roots.append(str(destination_root))
            for compiled, relative, target_suffix, payload in candidates:
                staged_compiled = destination_root / relative
                atomic_write_bytes(staged_compiled, payload)
                staged.append((compiled, staged_compiled, staged_compiled.with_suffix(target_suffix)))

        if not staged:
            return DecompilationManifest(
                schema_version=1,
                tool=str(self.entrypoint),
                tool_version=UNRPYC_VERSION,
                output_roots=output_roots,
                skipped_with_source=skipped,
                files=[],
            )

        base_command = [*self._base_command(), "--clobber"]
        if (os.cpu_count() or 1) > 1:
            base_command.extend(["--processes", "1"])
        environment = self._subprocess_environment()
        stdout_parts = []
        stderr_parts = []
        returncode = 0
        for batch in self._command_batches(base_command, [path for _source, path, _output in staged]):
            try:
                process = subprocess.run(
                    batch,
                    cwd=self.entrypoint.parent,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise DecompilationError(f"RPYC 反编译超过 {self.timeout_seconds} 秒") from exc
            stdout_parts.append(process.stdout)
            stderr_parts.append(process.stderr)
            returncode = process.returncode
            if returncode != 0:
                break

        files = []
        missing = []
        for source, _compiled, output in staged:
            if not output.is_file():
                missing.append(str(source))
                continue
            payload = output.read_bytes()
            files.append(DecompiledFile(
                source_path=str(source),
                relative_path=output.relative_to(root).as_posix(),
                output_path=str(output),
                sha256=hashlib.sha256(payload).hexdigest(),
            ))
        manifest = DecompilationManifest(
            schema_version=1,
            tool=str(self.entrypoint),
            tool_version=UNRPYC_VERSION,
            output_roots=output_roots,
            skipped_with_source=skipped,
            files=files,
            stdout="".join(stdout_parts)[-12000:],
            stderr="".join(stderr_parts)[-12000:],
        )
        if returncode != 0 or missing:
            detail = f"退出码 {returncode}"
            if missing:
                detail += f"，{len(missing)} 个文件未生成源码"
            raise DecompilationError(
                f"unrpyc 反编译未完整成功：{detail}\n{manifest.stderr[-2000:]}",
                manifest=manifest,
            )
        return manifest

    def _base_command(self) -> list[str]:
        return [
            self.python_executable,
            "-I",
            "-c",
            ISOLATED_BOOTSTRAP,
            str(self.entrypoint.parent),
            str(self.entrypoint),
        ]

    @staticmethod
    def _subprocess_environment() -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.casefold() in {
                "comspec", "path", "pathext", "systemdrive", "systemroot", "temp", "tmp", "windir"
            }
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    @staticmethod
    def _command_batches(base: list[str], paths: list[Path], limit: int = 24000) -> list[list[str]]:
        batches: list[list[str]] = []
        current = list(base)
        current_length = sum(len(item) + 3 for item in current)
        for path in paths:
            value = str(path)
            added = len(value) + 3
            if len(current) > len(base) and current_length + added > limit:
                batches.append(current)
                current = list(base)
                current_length = sum(len(item) + 3 for item in current)
            current.append(value)
            current_length += added
        if len(current) > len(base):
            batches.append(current)
        return batches
