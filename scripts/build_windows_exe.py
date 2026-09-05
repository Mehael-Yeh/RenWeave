from __future__ import annotations

import json
import os
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path

from packaging.version import InvalidVersion, Version
from PIL import Image, ImageDraw
from PyInstaller.__main__ import run as run_pyinstaller


ROOT = Path(__file__).resolve().parents[1]


def requested_version() -> str:
    raw = os.environ.get("RENWEAVE_BUILD_VERSION", "").strip()
    if not raw:
        raise RuntimeError("RENWEAVE_BUILD_VERSION is required")
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:
        raise RuntimeError(f"Invalid PEP 440 release version: {raw!r}") from exc
    if str(parsed) != raw:
        raise RuntimeError(
            f"Use the canonical PEP 440 version {str(parsed)!r} instead of {raw!r}"
        )
    return raw


def require_matching_package_metadata(version: str) -> None:
    try:
        actual = installed_version("renweave")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Install RenWeave with the action-provided version before building the executable"
        ) from exc
    if actual != version:
        raise RuntimeError(
            f"Installed RenWeave metadata is {actual!r}, expected {version!r}. "
            "Reinstall the package with SETUPTOOLS_SCM_PRETEND_VERSION set."
        )


def write_version_resource(path: Path, version: str) -> None:
    parsed = Version(version)
    release = (*parsed.release[:4], 0, 0, 0, 0)[:4]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={release!r},
    prodvers={release!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'RenWeave contributors'),
         StringStruct('FileDescription', 'RenWeave context-aware RenPy translator'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'RenWeave'),
         StringStruct('LegalCopyright', 'GPL-3.0-or-later'),
         StringStruct('OriginalFilename', 'RenWeave.exe'),
         StringStruct('ProductName', 'RenWeave'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


def write_app_icon(path: Path) -> None:
    size = 256
    image = Image.new("RGBA", (size, size), "#0B1020")
    draw = ImageDraw.Draw(image)
    scale = size // 32
    bars = (
        (5, 6, 10, 24, "#20B8CD"),
        (11, 10, 16, 28, "#5B5CE2"),
        (17, 6, 22, 24, "#8B8CF6"),
        (23, 10, 28, 28, "#20B8CD"),
    )
    for left, top, right, bottom, color in bars:
        draw.rectangle(
            (left * scale, top * scale, right * scale - 1, bottom * scale - 1),
            fill=color,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def build_with_isolated_native_path(arguments: list[str]) -> None:
    """Prevent unrelated Poppler ICU DLLs from shadowing Qt's dependencies."""
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        entry for entry in original_path.split(os.pathsep) if "poppler" not in entry.casefold()
    )
    try:
        run_pyinstaller(arguments)
    finally:
        os.environ["PATH"] = original_path


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("The standalone executable must be built on Windows")
    version = requested_version()
    require_matching_package_metadata(version)
    build_root = ROOT / "build" / "windows"
    release_root = ROOT / "release"
    version_resource = build_root / "version_info.txt"
    app_icon = build_root / "renweave.ico"
    name = f"RenWeave-{version}-windows-x64"
    output = release_root / f"{name}.exe"
    if build_root.exists():
        shutil.rmtree(build_root)
    release_root.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    write_version_resource(version_resource, version)
    write_app_icon(app_icon)
    interface_mode = (
        "--console" if os.environ.get("RENWEAVE_BUILD_CONSOLE") == "1" else "--windowed"
    )
    build_with_isolated_native_path([
        "--noconfirm",
        "--clean",
        "--onefile",
        interface_mode,
        "--noupx",
        "--name", name,
        "--paths", str(ROOT / "src"),
        "--collect-data", "renweave",
        "--add-data", (
            f"{ROOT / 'src' / 'renweave' / '_vendor' / 'unrpyc'}"
            f"{os.pathsep}renweave/_vendor/unrpyc"
        ),
        "--copy-metadata", "renweave",
        "--hidden-import", "pickletools",
        "--hidden-import", "keyring.backends.Windows",
        "--runtime-hook", str(ROOT / "scripts" / "pyinstaller_qt_runtime.py"),
        "--version-file", str(version_resource),
        "--icon", str(app_icon),
        "--distpath", str(release_root),
        "--workpath", str(build_root / "work"),
        "--specpath", str(build_root / "spec"),
        str(ROOT / "packaging" / "windows_entry.py"),
    ])
    if not output.is_file() or output.stat().st_size < 1_000_000:
        raise RuntimeError(f"PyInstaller did not create a valid executable: {output}")
    manifest = {
        "version": version,
        "artifact": output.name,
        "size": output.stat().st_size,
    }
    (build_root / "artifact.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
