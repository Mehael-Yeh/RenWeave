from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path

from .models import ProjectInfo
from .rpa import ExtractionManifest, RpaArchive, script_member


@dataclass(slots=True)
class AcquisitionManifest:
    schema_version: int
    project_name: str
    scripts_only: bool
    archives: list[ExtractionManifest]

    @property
    def source_roots(self) -> list[Path]:
        return [Path(item.output_dir) for item in self.archives]

    def to_dict(self) -> dict:
        return asdict(self)


class ArchiveAcquirer:
    def acquire(
        self,
        project: ProjectInfo,
        output_root: str | Path,
        *,
        scripts_only: bool = True,
    ) -> AcquisitionManifest:
        root = Path(output_root).expanduser().resolve()
        game_dir = Path(project.game_dir)
        manifests = []
        for relative in project.archives:
            archive_path = game_dir / relative
            archive_key = relative.replace("\\", "/").replace("/", "__")
            archive_hash = self._sha256_file(archive_path)[:12]
            destination = root / f"{Path(archive_key).stem}-{archive_hash}"
            with RpaArchive(archive_path) as archive:
                manifests.append(archive.extract(
                    destination,
                    include=script_member if scripts_only else None,
                ))
        return AcquisitionManifest(
            schema_version=1,
            project_name=project.name,
            scripts_only=scripts_only,
            archives=manifests,
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as reader:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
