from __future__ import annotations

import contextlib
import hashlib
import io
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from .io import atomic_write_bytes, atomic_write_json, read_json


UNRPYC_VERSION = "2.0.4"
# Upstream tag v2.0.4 retains the v2.0.3 CLI version string. Keep both values
# explicit so the bundled source remains byte-for-byte identical to upstream.
UNRPYC_CLI_VERSION = "2.0.3"
UNRPYC_COMMIT = "3ae8334ed71a05535927dcc559663d3aca51215b"
UNRPYC_ARCHIVE_URL = f"https://github.com/CensoredUsername/unrpyc/archive/{UNRPYC_COMMIT}.zip"
UNRPYC_ARCHIVE_SHA256 = "36a0e8d05b00939f45c07c7a7d1e7eca37c3b28347d2baea9007ea3b2b5a41b8"
UNRPYC_BUNDLED_TREE_SHA256 = "c828ce5f00af9c962da5ffcf9a230d392d98ee30d9fba0adcba4aad91cb5bf78"
UNRPYC_BUNDLED_FILES = (
    "LICENSE",
    "deobfuscate.py",
    "unrpyc.py",
    "decompiler/__init__.py",
    "decompiler/astdump.py",
    "decompiler/atldecompiler.py",
    "decompiler/magic.py",
    "decompiler/renpycompat.py",
    "decompiler/sl2decompiler.py",
    "decompiler/testcasedecompiler.py",
    "decompiler/translate.py",
    "decompiler/util.py",
)
COMPILED_SUFFIXES = {".rpyc": ".rpy", ".rpymc": ".rpym"}
ISOLATED_BOOTSTRAP = (
    "import runpy,sys;"
    "tool_dir=sys.argv.pop(1);"
    "entry=sys.argv.pop(1);"
    "sys.path.insert(0,tool_dir);"
    "sys.argv[0]=entry;"
    "runpy.run_path(entry,run_name='__main__')"
)
FROZEN_UNRPYC_SWITCH = "--renweave-internal-unrpyc"


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
        self.install_dir = (
            self.tools_root / f"unrpyc-{UNRPYC_VERSION}-{UNRPYC_COMMIT[:8]}-bundled"
        )

    def resolve(
        self,
        explicit_path: str | Path | None = None,
        *,
        allow_download: bool = False,
    ) -> Path:
        # Kept for API compatibility with 1.x callers. RenWeave never downloads
        # executable tools at runtime; the verified tool is part of the package.
        _ = allow_download
        configured = explicit_path or os.environ.get("RENWEAVE_UNRPYC", "")
        if configured:
            return self._entrypoint(Path(configured).expanduser().resolve())
        installed = self.install_dir / "unrpyc.py"
        if installed.is_file():
            return self._validate_install(installed)
        return self._install_bundled()

    @staticmethod
    def _entrypoint(path: Path) -> Path:
        entrypoint = path / "unrpyc.py" if path.is_dir() else path
        if not entrypoint.is_file() or entrypoint.name.casefold() != "unrpyc.py":
            raise DecompilationError(f"无效的 unrpyc 入口：{entrypoint}")
        return entrypoint

    def _install_bundled(self) -> Path:
        self.tools_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".unrpyc-", dir=self.tools_root))
        try:
            bundled = resources.files("renweave").joinpath("_vendor").joinpath("unrpyc")
            for relative_text in UNRPYC_BUNDLED_FILES:
                relative = PurePosixPath(relative_text)
                resource = bundled
                for part in relative.parts:
                    resource = resource.joinpath(part)
                try:
                    payload = resource.read_bytes()
                except (FileNotFoundError, OSError) as exc:
                    raise DecompilationError(
                        f"The RenWeave installation is missing bundled unrpyc file: {relative_text}"
                    ) from exc
                atomic_write_bytes(staging.joinpath(*relative.parts), payload)
            if not (staging / "unrpyc.py").is_file():
                raise DecompilationError("The bundled unrpyc tool is missing its entry point")
            tree_sha256 = self._tree_digest(staging)
            if tree_sha256 != UNRPYC_BUNDLED_TREE_SHA256:
                raise DecompilationError(
                    "The bundled unrpyc files failed their installation integrity check"
                )
            atomic_write_json(staging / "renweave-source.json", {
                "version": UNRPYC_VERSION,
                "commit": UNRPYC_COMMIT,
                "distribution": "bundled",
                "archive_url": UNRPYC_ARCHIVE_URL,
                "archive_sha256": UNRPYC_ARCHIVE_SHA256,
                "tree_sha256": tree_sha256,
            })
            if self.install_dir.exists():
                raise DecompilationError(
                    f"The unrpyc installation directory already exists but is incomplete: {self.install_dir}"
                )
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
            or metadata.get("distribution") != "bundled"
            or metadata.get("archive_sha256") != UNRPYC_ARCHIVE_SHA256
            or metadata.get("tree_sha256") != UNRPYC_BUNDLED_TREE_SHA256
            or metadata.get("tree_sha256") != self._tree_digest(self.install_dir)
        ):
            raise DecompilationError(f"unrpyc 安装内容校验失败：{self.install_dir}")
        return entrypoint

    @staticmethod
    def _tree_digest(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if (
                not path.is_file()
                or path.name == "renweave-source.json"
                or "__pycache__" in path.parts
            ):
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()


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
        if self._uses_frozen_launcher():
            return f"Unrpyc v{UNRPYC_CLI_VERSION}"
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
                if self._uses_frozen_launcher():
                    returncode, stdout, stderr = run_unrpyc_in_process(
                        self.entrypoint,
                        batch[len(self._base_command()):],
                    )
                    process = types.SimpleNamespace(
                        returncode=returncode,
                        stdout=stdout,
                        stderr=stderr,
                    )
                else:
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
        if self._uses_frozen_launcher():
            return [
                self.python_executable,
                FROZEN_UNRPYC_SWITCH,
                str(self.entrypoint.parent),
                str(self.entrypoint),
            ]
        return [
            self.python_executable,
            "-I",
            "-B",
            "-c",
            ISOLATED_BOOTSTRAP,
            str(self.entrypoint.parent),
            str(self.entrypoint),
        ]

    def _uses_frozen_launcher(self) -> bool:
        return bool(getattr(sys, "frozen", False)) and (
            Path(self.python_executable) == Path(sys.executable).resolve()
        )

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
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
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


class SequentialPool:
    """Minimal Pool-compatible executor for the frozen unrpyc child process.

    RenWeave already invokes unrpyc with ``--processes 1``. The upstream tool
    still creates a multiprocessing child for that single worker, which starts
    the frozen RenWeave executable again on Windows. Running the one worker in
    the isolated unrpyc subprocess avoids the duplicate GUI without changing
    the bundled third-party source tree.
    """

    def __init__(self, _processes: int | None = None) -> None:
        pass

    def __enter__(self) -> "SequentialPool":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        return False

    @staticmethod
    def imap(function, iterable, _chunksize: int = 1):
        return map(function, iterable)


def run_unrpyc_in_process(entrypoint: str | Path, arguments: list[str]) -> tuple[int, str, str]:
    """Run one unrpyc batch without spawning the frozen GUI executable again."""
    tool = Path(entrypoint).expanduser().resolve()
    tool_dir = tool.parent
    original_argv = list(sys.argv)
    original_path = list(sys.path)
    original_modules = dict(sys.modules)
    stdout = io.StringIO()
    stderr = io.StringIO()
    multiprocessing_stub = types.ModuleType("multiprocessing")
    multiprocessing_stub.Lock = lambda: object()
    multiprocessing_stub.Pool = SequentialPool
    multiprocessing_stub.cpu_count = lambda: 2
    multiprocessing_stub.current_process = lambda: types.SimpleNamespace(name="MainProcess")
    multiprocessing_stub.freeze_support = lambda: None
    exit_code = 0
    try:
        sys.modules["multiprocessing"] = multiprocessing_stub
        sys.path.insert(0, str(tool_dir))
        sys.argv = [str(tool), *arguments]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(tool), run_name="__main__")
            except SystemExit as exc:
                exit_code = int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
    except BaseException as exc:
        exit_code = 1
        stderr.write(f"{type(exc).__name__}: {exc}\n")
    finally:
        sys.argv = original_argv
        sys.path[:] = original_path
        for name in set(sys.modules) - set(original_modules):
            sys.modules.pop(name, None)
        sys.modules.update(original_modules)
    return exit_code, stdout.getvalue(), stderr.getvalue()
