from __future__ import annotations

from pathlib import Path

from .discovery import ProjectDiscovery
from .models import ProjectIndex
from .parser import RenpyParser


class ProjectIndexer:
    def __init__(self) -> None:
        self.discovery = ProjectDiscovery()
        self.parser = RenpyParser()

    def build(
        self,
        target: str | Path,
        *,
        additional_source_roots: list[str | Path] | None = None,
    ) -> ProjectIndex:
        project = self.discovery.discover(target)
        game_dir = Path(project.game_dir)
        files = []
        scenes = []
        for relative in project.source_scripts:
            script, parsed_scenes = self.parser.parse_file(game_dir / relative, game_dir)
            files.append(script)
            scenes.extend(parsed_scenes)
        for ordinal, raw_root in enumerate(additional_source_roots or []):
            source_root = Path(raw_root).expanduser().resolve()
            if not source_root.is_dir():
                continue
            paths = list(source_root.rglob("*.rpy")) + list(source_root.rglob("*.rpym"))
            for path in sorted(set(paths), key=lambda item: item.as_posix().casefold()):
                script, parsed_scenes = self.parser.parse_file(
                    path,
                    source_root,
                    relative_prefix=f"_archives/{ordinal}",
                )
                files.append(script)
                scenes.extend(parsed_scenes)
        return ProjectIndex(schema_version=6, project=project, files=files, scenes=scenes)
