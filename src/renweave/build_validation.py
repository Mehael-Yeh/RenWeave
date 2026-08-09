from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .emitter import BuildManifest
from .io import atomic_write_bytes, atomic_write_text
from .parser import _quoted_segments


TRANSLATE_HEADER_RE = re.compile(r"^translate\s+([^\W\d]\w*)\s+([^\W\d]\w*)\s*:$", re.UNICODE)


@dataclass(slots=True, frozen=True)
class BuildValidationIssue:
    code: str
    file: str
    line: int
    message: str


@dataclass(slots=True)
class EngineValidation:
    status: str
    sdk_root: str = ""
    command: list[str] = field(default_factory=list)
    return_code: int | None = None
    output: str = ""


@dataclass(slots=True)
class BuildValidationReport:
    schema_version: int
    passed: bool
    static_passed: bool
    engine: EngineValidation
    issues: list[BuildValidationIssue]
    staged_project: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class RenpySdk:
    root: Path
    command: tuple[str, ...]


class RenpySdkLocator:
    """Locate a usable Ren'Py SDK without invoking a game executable."""

    def resolve(
        self,
        explicit: str | Path | None = None,
        *,
        project_root: str | Path | None = None,
    ) -> RenpySdk | None:
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        elif os.environ.get("RENWEAVE_RENPY_SDK"):
            candidates.append(Path(os.environ["RENWEAVE_RENPY_SDK"]).expanduser())
        if project_root:
            project = Path(project_root).expanduser().resolve()
            candidates.extend((project, project.parent))
        for candidate in candidates:
            sdk = self._candidate(candidate.resolve())
            if sdk is not None:
                return sdk
        if explicit:
            raise ValueError(f"指定路径不是可用的 Ren'Py SDK：{Path(explicit).expanduser()}")
        return None

    @staticmethod
    def _candidate(path: Path) -> RenpySdk | None:
        root = path.parent if path.is_file() and path.name.casefold() == "renpy.py" else path
        entrypoint = root / "renpy.py"
        if not entrypoint.is_file():
            return None
        windows_runners = (
            root / "lib" / "py3-windows-x86_64" / "python.exe",
            root / "lib" / "py3-windows-arm64" / "python.exe",
            root / "lib" / "py3-windows-i686" / "python.exe",
        )
        for runner in windows_runners:
            if runner.is_file():
                return RenpySdk(root, (str(runner), str(entrypoint)))
        shell_runner = root / "renpy.sh"
        if shell_runner.is_file():
            return RenpySdk(root, (str(shell_runner),))
        return None


class GeneratedScriptValidator:
    """Strictly validate the small RenWeave-generated Ren'Py grammar."""

    def validate(self, build: BuildManifest) -> list[BuildValidationIssue]:
        issues: list[BuildValidationIssue] = []
        identifiers: set[str] = set()
        root = Path(build.output_dir).expanduser().resolve()
        expected_prefix = Path("game", "tl", build.renpy_language)
        for emitted in build.files:
            relative = Path(emitted.relative_path)
            try:
                local = relative.relative_to(expected_prefix)
            except ValueError:
                issues.append(BuildValidationIssue(
                    "PATH_OUTSIDE_LANGUAGE", emitted.relative_path, 0,
                    "构建文件不在目标语言目录中",
                ))
                continue
            path = root.joinpath(local).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                issues.append(BuildValidationIssue(
                    "PATH_TRAVERSAL", emitted.relative_path, 0, "构建文件逃逸目标语言目录",
                ))
                continue
            if not path.is_file() or path.is_symlink():
                issues.append(BuildValidationIssue(
                    "FILE_MISSING", emitted.relative_path, 0, "构建文件缺失或为符号链接",
                ))
                continue
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != emitted.sha256:
                issues.append(BuildValidationIssue(
                    "HASH_MISMATCH", emitted.relative_path, 0, "构建文件与清单哈希不一致",
                ))
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(BuildValidationIssue(
                    "INVALID_UTF8", emitted.relative_path, 0, "生成脚本不是 UTF-8",
                ))
                continue
            issues.extend(self._validate_text(
                emitted.relative_path,
                text,
                build.renpy_language,
                identifiers,
                emitted.dialogue_blocks,
                emitted.string_entries,
            ))
        return issues

    def _validate_text(
        self,
        filename: str,
        text: str,
        language: str,
        identifiers: set[str],
        expected_dialogue: int,
        expected_strings: int,
    ) -> list[BuildValidationIssue]:
        issues: list[BuildValidationIssue] = []
        if "\0" in text:
            issues.append(BuildValidationIssue("NUL_BYTE", filename, 0, "脚本包含 NUL 字符"))
        if not text.startswith("# Generated by RenWeave."):
            issues.append(BuildValidationIssue("HEADER_MISSING", filename, 1, "缺少 RenWeave 生成标记"))
        mode = ""
        dialogue_statements = 0
        string_entries = 0
        current_dialogue_statements = 0
        waiting_for_new = False

        def finish_dialogue(line_number: int) -> None:
            nonlocal current_dialogue_statements
            if mode == "dialogue" and current_dialogue_statements != 1:
                issues.append(BuildValidationIssue(
                    "DIALOGUE_BODY_COUNT", filename, line_number,
                    "每个翻译对话块必须恰好包含一条译文语句",
                ))
            current_dialogue_statements = 0

        lines = text.splitlines()
        for number, line in enumerate(lines, start=1):
            if "\t" in line[:len(line) - len(line.lstrip())]:
                issues.append(BuildValidationIssue("TAB_INDENT", filename, number, "缩进不得使用制表符"))
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):
                finish_dialogue(number)
                match = TRANSLATE_HEADER_RE.fullmatch(line)
                if match is None:
                    issues.append(BuildValidationIssue(
                        "INVALID_TOP_LEVEL", filename, number, "只允许标准 translate 块",
                    ))
                    mode = ""
                    continue
                block_language, identifier = match.groups()
                if block_language != language:
                    issues.append(BuildValidationIssue(
                        "LANGUAGE_MISMATCH", filename, number, "translate 块语言与构建目标不一致",
                    ))
                if identifier != "strings":
                    if identifier in identifiers:
                        issues.append(BuildValidationIssue(
                            "DUPLICATE_TRANSLATION_ID", filename, number, f"翻译 ID 重复：{identifier}",
                        ))
                    identifiers.add(identifier)
                    dialogue_statements += 1
                    mode = "dialogue"
                else:
                    mode = "strings"
                waiting_for_new = False
                continue
            if not line.startswith("    ") or line.startswith("     "):
                issues.append(BuildValidationIssue(
                    "INVALID_INDENT", filename, number, "生成语句必须使用四空格缩进",
                ))
                continue
            statement = line[4:]
            literals = _quoted_segments(statement)
            if mode == "dialogue":
                current_dialogue_statements += 1
                if not literals:
                    issues.append(BuildValidationIssue(
                        "DIALOGUE_LITERAL_MISSING", filename, number, "对话译文语句缺少有效字符串",
                    ))
            elif mode == "strings":
                keyword = "new " if waiting_for_new else "old "
                if not statement.startswith(keyword) or len(literals) != 1:
                    issues.append(BuildValidationIssue(
                        "INVALID_STRING_PAIR", filename, number, f"字符串块期待 {keyword.strip()} 语句",
                    ))
                if waiting_for_new:
                    string_entries += 1
                waiting_for_new = not waiting_for_new
            else:
                issues.append(BuildValidationIssue(
                    "STATEMENT_OUTSIDE_BLOCK", filename, number, "缩进语句位于 translate 块之外",
                ))
        finish_dialogue(len(lines) + 1)
        if waiting_for_new:
            issues.append(BuildValidationIssue(
                "INCOMPLETE_STRING_PAIR", filename, len(lines), "最后一个 old 字符串缺少 new 译文",
            ))
        if dialogue_statements != expected_dialogue:
            issues.append(BuildValidationIssue(
                "DIALOGUE_COUNT_MISMATCH", filename, 0,
                f"清单记录 {expected_dialogue} 个对话块，脚本包含 {dialogue_statements} 个",
            ))
        if string_entries != expected_strings:
            issues.append(BuildValidationIssue(
                "STRING_COUNT_MISMATCH", filename, 0,
                f"清单记录 {expected_strings} 个字符串条目，脚本包含 {string_entries} 个",
            ))
        return issues


class RenpyEngineValidator:
    def validate(
        self,
        sdk: RenpySdk,
        project_dir: Path,
        *,
        timeout_seconds: int = 180,
    ) -> EngineValidation:
        save_dir = project_dir / "saves"
        save_dir.mkdir(parents=True, exist_ok=True)
        command = [
            *sdk.command,
            "--savedir", str(save_dir),
            str(project_dir),
            "compile",
            "--keep-orphan-rpyc",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=sdk.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return EngineValidation(
                status="failed",
                sdk_root=str(sdk.root),
                command=command,
                output=str(exc)[:20000],
            )
        output = (completed.stdout + "\n" + completed.stderr).strip()[:20000]
        return EngineValidation(
            status="passed" if completed.returncode == 0 else "failed",
            sdk_root=str(sdk.root),
            command=command,
            return_code=completed.returncode,
            output=output,
        )


class RenpyBuildValidator:
    def validate(
        self,
        build: BuildManifest,
        validation_root: str | Path,
        *,
        sdk_path: str | Path | None = None,
        project_root: str | Path | None = None,
        require_engine: bool = False,
    ) -> BuildValidationReport:
        issues = GeneratedScriptValidator().validate(build)
        if issues:
            return BuildValidationReport(1, False, False, EngineValidation("not_run"), issues)
        staged = self._stage(build, Path(validation_root).expanduser().resolve())
        try:
            sdk = RenpySdkLocator().resolve(sdk_path, project_root=project_root)
        except ValueError as exc:
            issues.append(BuildValidationIssue("RENPY_SDK_INVALID", "", 0, str(exc)))
            return BuildValidationReport(
                schema_version=1,
                passed=False,
                static_passed=True,
                engine=EngineValidation("failed", output=str(exc)),
                issues=issues,
                staged_project=str(staged),
            )
        if sdk is None:
            engine = EngineValidation("missing" if require_engine else "skipped")
            if require_engine:
                issues.append(BuildValidationIssue(
                    "RENPY_SDK_MISSING", "", 0,
                    "要求 Ren'Py 引擎验证，但未发现 SDK；请使用 --renpy-sdk 指定",
                ))
        else:
            engine = RenpyEngineValidator().validate(sdk, staged)
            if engine.status != "passed":
                issues.append(BuildValidationIssue(
                    "RENPY_COMPILE_FAILED", "", 0, "隔离项目未通过 Ren'Py compile",
                ))
        return BuildValidationReport(
            schema_version=1,
            passed=not issues,
            static_passed=True,
            engine=engine,
            issues=issues,
            staged_project=str(staged),
        )

    @staticmethod
    def _stage(build: BuildManifest, validation_root: Path) -> Path:
        material = "\n".join(
            f"{item.relative_path}\0{item.sha256}" for item in build.files
        ).encode("utf-8")
        fingerprint = hashlib.sha256(material).hexdigest()[:16]
        project = validation_root / fingerprint
        game = project / "game"
        atomic_write_text(game / "script.rpy", "label start:\n    return\n")
        language_root = Path(build.output_dir).expanduser().resolve()
        prefix = Path("game", "tl", build.renpy_language)
        for emitted in build.files:
            local = Path(emitted.relative_path).relative_to(prefix)
            atomic_write_bytes(
                game / "tl" / build.renpy_language / local,
                (language_root / local).read_bytes(),
            )
        return project
