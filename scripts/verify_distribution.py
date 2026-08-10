from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

from renweave.decompiler import UNRPYC_BUNDLED_FILES


def _require_members(artifact: Path, names: set[str], prefix: str) -> None:
    required = {
        f"{prefix}src/renweave/_vendor/unrpyc/{relative}"
        if prefix
        else f"renweave/_vendor/unrpyc/{relative}"
        for relative in UNRPYC_BUNDLED_FILES
    }
    missing = sorted(required - names)
    if missing:
        raise RuntimeError(
            f"{artifact.name} is missing bundled unrpyc files: {', '.join(missing)}"
        )


def verify(directory: Path) -> None:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("Expected exactly one wheel and one source distribution")

    with zipfile.ZipFile(wheels[0]) as archive:
        _require_members(wheels[0], set(archive.namelist()), "")

    with tarfile.open(sdists[0], "r:gz") as archive:
        names = set(archive.getnames())
        roots = {name.split("/", 1)[0] for name in names if "/" in name}
        if len(roots) != 1:
            raise RuntimeError(f"Could not determine source root in {sdists[0].name}")
        root = next(iter(roots))
        _require_members(sdists[0], names, f"{root}/")
        if f"{root}/THIRD_PARTY_NOTICES.md" not in names:
            raise RuntimeError(f"{sdists[0].name} is missing THIRD_PARTY_NOTICES.md")

    print(f"Verified bundled offline tools in {wheels[0].name} and {sdists[0].name}")


if __name__ == "__main__":
    verify(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve())
