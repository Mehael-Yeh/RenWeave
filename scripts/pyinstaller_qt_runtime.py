"""Make the bundled PySide6 DLL directories visible before QtCore imports."""

from __future__ import annotations

import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    dll_directories = (
        bundle_root,
        bundle_root / "PySide6",
        bundle_root / "shiboken6",
    )
    for directory in dll_directories:
        if directory.is_dir() and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(directory))
    os.environ.setdefault("QT_PLUGIN_PATH", str(bundle_root / "PySide6" / "plugins"))
