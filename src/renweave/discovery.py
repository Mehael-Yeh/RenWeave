from __future__ import annotations

from pathlib import Path

from .models import ProjectInfo, relative_posix


class ProjectDiscoveryError(RuntimeError):
    pass


class ProjectDiscovery:
    def discover(self, target: str | Path) -> ProjectInfo:
        path = Path(target).expanduser().resolve()
        project_root, game_dir = self._resolve(path)
        source_scripts = self._files_many(game_dir, ("*.rpy", "*.rpym"), exclude_tl=True)
        compiled_scripts = self._files_many(game_dir, ("*.rpyc", "*.rpymc"), exclude_tl=True)
        archives = self._files(game_dir, "*.rpa", exclude_tl=False)
        if not source_scripts and not compiled_scripts and not archives:
            raise ProjectDiscoveryError(
                f"未找到可处理的 Ren'Py 脚本或归档：{game_dir}"
            )
        tl_dir = game_dir / "tl"
        languages = sorted(
            child.name for child in tl_dir.iterdir() if child.is_dir()
        ) if tl_dir.is_dir() else []
        return ProjectInfo(
            project_root=str(project_root),
            game_dir=str(game_dir),
            name=project_root.name,
            renpy_version=self._version(project_root),
            source_scripts=[relative_posix(item, game_dir) for item in source_scripts],
            compiled_scripts=[relative_posix(item, game_dir) for item in compiled_scripts],
            archives=[relative_posix(item, game_dir) for item in archives],
            translation_languages=languages,
        )

    def _resolve(self, path: Path) -> tuple[Path, Path]:
        if not path.exists():
            raise ProjectDiscoveryError(f"路径不存在：{path}")
        if path.is_file():
            if path.suffix.lower() not in {".exe", ".sh", ".py", ".app"}:
                raise ProjectDiscoveryError(f"无法从该文件识别 Ren'Py 游戏：{path}")
            path = path.parent
        if path.name.casefold() == "game" and path.is_dir():
            return path.parent, path
        if (path / "game").is_dir():
            return path, path / "game"
        if (
            any(path.glob("*.rpy"))
            or any(path.glob("*.rpym"))
            or any(path.glob("*.rpyc"))
            or any(path.glob("*.rpymc"))
            or any(path.glob("*.rpa"))
        ):
            return path.parent, path
        raise ProjectDiscoveryError(f"未找到 game 目录或 Ren'Py 脚本：{path}")

    @staticmethod
    def _files(game_dir: Path, pattern: str, *, exclude_tl: bool) -> list[Path]:
        result = []
        for item in game_dir.rglob(pattern):
            if exclude_tl and "tl" in {part.casefold() for part in item.relative_to(game_dir).parts[:-1]}:
                continue
            result.append(item)
        return sorted(result, key=lambda item: item.as_posix().casefold())

    @classmethod
    def _files_many(
        cls,
        game_dir: Path,
        patterns: tuple[str, ...],
        *,
        exclude_tl: bool,
    ) -> list[Path]:
        result = []
        for pattern in patterns:
            result.extend(cls._files(game_dir, pattern, exclude_tl=exclude_tl))
        return sorted(set(result), key=lambda item: item.as_posix().casefold())

    @staticmethod
    def _version(project_root: Path) -> str:
        candidates = (
            project_root / "renpy" / "version.txt",
            project_root / "renpy" / "__init__.py",
        )
        for candidate in candidates:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                cleaned = line.strip()
                if cleaned and ("Ren'Py" in cleaned or "version" in cleaned.casefold()):
                    return cleaned[:160]
            if text.strip():
                return text.strip().splitlines()[0][:160]
        return "unknown"
