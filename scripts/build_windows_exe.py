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
            "Reinstall the package with SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RENWEAVE set."
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
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = image.load()
    top = (91, 92, 226)
    bottom = (28, 184, 205)
    for y in range(size):
        ratio = y / (size - 1)
        color = tuple(round(top[index] * (1 - ratio) + bottom[index] * ratio) for index in range(3))
        for x in range(size):
            pixels[x, y] = (*color, 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((4, 4, 252, 252), radius=56, fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)
    draw.line(
        [(48, 62), (83, 192), (128, 103), (173, 192), (208, 62)],
        fill=(255, 255, 255, 245),
        width=25,
        joint="curve",
    )
    draw.line(
        [(83, 192), (128, 103), (173, 192)],
        fill=(225, 231, 255, 255),
        width=13,
        joint="curve",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


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
    run_pyinstaller([
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
        "--copy-metadata", "sv-ttk",
        "--collect-data", "sv_ttk",
        "--hidden-import", "pickletools",
        "--hidden-import", "keyring.backends.Windows",
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
