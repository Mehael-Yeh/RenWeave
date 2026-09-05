from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

from renweave import __version__
from renweave.decompiler import (
    FROZEN_UNRPYC_SWITCH,
    UNRPYC_CLI_VERSION,
    UnrpycDecompiler,
    UnrpycToolManager,
    run_unrpyc_in_process,
)


SELF_TEST_SWITCH = "--renweave-self-test"


def _restore_stream(name: str, descriptor: int) -> None:
    if getattr(sys, name) is not None:
        return
    try:
        stream = open(
            descriptor,
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
            closefd=False,
        )
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    setattr(sys, name, stream)


def _run_unrpyc(arguments: list[str]) -> int:
    if len(arguments) < 2:
        return 2
    _restore_stream("stdout", 1)
    _restore_stream("stderr", 2)
    entrypoint = Path(arguments[1]).resolve()
    result, stdout, stderr = run_unrpyc_in_process(entrypoint, arguments[2:])
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    return result


def _run_self_test(output_path: str) -> int:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"status": "started"}) + "\n", encoding="utf-8")
    try:
        entrypoint = UnrpycToolManager(output.parent / "tools").resolve()
        decompiler = UnrpycDecompiler(entrypoint)
        command = [*decompiler._base_command(), "--version"]
        process = subprocess.run(
            command,
            cwd=entrypoint.parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        payload = {
            "status": "complete",
            "renweave_version": __version__,
            "unrpyc_version": decompiler.version(),
            "unrpyc_subprocess_version": process.stdout.strip(),
            "unrpyc_subprocess_returncode": process.returncode,
            "bundled_tool": entrypoint.is_file(),
        }
        expected = f"Unrpyc v{UNRPYC_CLI_VERSION}"
        succeeded = (
            process.returncode == 0
            and process.stdout.strip() == expected
            and payload["unrpyc_version"] == expected
            and payload["bundled_tool"]
        )
    except BaseException as exc:
        payload = {
            "status": "failed",
            "renweave_version": __version__,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        succeeded = False
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0 if succeeded else 1


def main() -> int:
    arguments = sys.argv[1:]
    if arguments and arguments[0] == FROZEN_UNRPYC_SWITCH:
        result = _run_unrpyc(arguments[1:])
        if getattr(sys, "frozen", False):
            # A windowed one-file child can retain PyInstaller runtime threads
            # after runpy returns even though all RPY files are closed. Flush
            # captured diagnostics, then terminate this dedicated tool process
            # so the parent translation pipeline can continue.
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except (AttributeError, OSError):
                    pass
            os._exit(result)
        return result
    if len(arguments) == 2 and arguments[0] == SELF_TEST_SWITCH:
        return _run_self_test(arguments[1])
    from renweave.qt_gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
