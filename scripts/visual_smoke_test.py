from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from types import SimpleNamespace

from renweave.credentials import SecureCredentialStore
from renweave.gui import ModelPickerDialog, RenWeaveDesktopApp


class MemoryCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def capture_window(window, path: Path) -> None:
    from PIL import ImageGrab

    window.update()
    left = window.winfo_rootx()
    top = window.winfo_rooty()
    right = left + window.winfo_width()
    bottom = top + window.winfo_height()
    if right - left < 100 or bottom - top < 40:
        raise RuntimeError(f"Window is too small to capture: {right - left}x{bottom - top}")
    image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


def hide_provider_picker(app: RenWeaveDesktopApp) -> None:
    """Keep transient-state captures focused when the model page is mounted."""
    provider_grid = getattr(app, "provider_grid", None)
    provider_more = getattr(app, "provider_more_button", None)
    provider_description = getattr(app, "provider_description", None)
    if provider_grid is not None and provider_grid.winfo_exists():
        provider_grid.grid_remove()
    if provider_more is not None and provider_more.winfo_exists():
        provider_more.master.grid_remove()
    if provider_description is not None and provider_description.winfo_exists():
        provider_description.master.grid_remove()


def verify_layout(output_dir: Path | None = None) -> None:
    import tkinter as tk

    with tempfile.TemporaryDirectory(prefix="renweave-visual-") as directory:
        root = tk.Tk()
        if output_dir is None:
            root.withdraw()
        else:
            root.geometry("1240x840+20+20")
            root.deiconify()
        app = RenWeaveDesktopApp(
            root,
            settings_path=Path(directory) / "settings.json",
            credential_store=SecureCredentialStore(backend=MemoryCredentialBackend()),
        )
        app.connection_state = "verified"
        app.connection_detail = {"model": "visual-smoke-model", "latency": 20}
        app.model.set("visual-smoke-model")
        app.project.set(str(Path(directory) / "SampleGame"))
        app.workspace.set(str(Path(directory) / "workspace"))
        app.source_language.set("English")
        app.target_language.set("日本語")
        app.last_stage = "translating"
        app.progress_payload = {
            "stage": "translating",
            "progress_percent": 48,
            "completed_scenes": 12,
            "total_scenes": 30,
            "eta_seconds": 540,
            "total_model_calls": 14,
            "total_prompt_tokens": 3200,
            "total_completion_tokens": 900,
            "estimated_total_tokens_low": 12000,
            "estimated_total_tokens_high": 18000,
            "usage_reporting_status": "reported",
            "current_scene_label": "chapter_two",
        }

        checked = 0
        for width, height in ((1240, 840), (1060, 720), (900, 640)):
            root.geometry(f"{width}x{height}+20+20")
            for locale in ("en", "zh"):
                app._set_locale(locale)
                app.status.set(
                    "Translating chapter_two"
                    if locale == "en"
                    else "正在翻译 chapter_two"
                )
                for step in range(5):
                    app.step = step
                    app.worker = SimpleNamespace(is_alive=lambda: True) if step == 4 else None
                    app._render()
                    root.update_idletasks()
                    app._sync_content_layout()
                    root.update_idletasks()
                    canvas_width = app.content_canvas.winfo_width()
                    content_width = app.content.winfo_width()
                    if content_width > canvas_width + 1:
                        raise RuntimeError(
                            f"Horizontal overflow at {width}x{height}, {locale}, step {step}: "
                            f"content={content_width}, canvas={canvas_width}"
                        )
                    if app.next_button is not None and app.next_button.winfo_reqheight() < 34:
                        raise RuntimeError(
                            f"Workflow action is too small at {width}x{height}, {locale}, step {step}"
                        )
                    if output_dir is not None:
                        root.update()
                        capture_window(
                            root,
                            output_dir / f"{locale}-{width}x{height}-step-{step + 1}.png",
                        )
                    checked += 1

                app.step = 0
                app._render()
                stable_page = app.page
                app._on_root_configure(SimpleNamespace(widget=root, width=width))
                app._render_responsive()
                if app.page is not stable_page:
                    raise RuntimeError(
                        f"Responsive update rebuilt the page at {width}x{height}, {locale}"
                    )
        if output_dir is not None:
            root.geometry("1240x840+20+20")
            root.update()
            app._set_locale("en")
            app.status.set("Translating chapter_two")
            app.step = 0
            app.worker = None
            app.connection_state = "connecting"
            app.connection_detail = {}
            app._render()
            hide_provider_picker(app)
            root.update()
            capture_window(root, output_dir / "en-model-connecting.png")

            app.connection_state = "failed"
            app.connection_detail = {"message": "Authentication failed. Check the key and try again."}
            app._render()
            hide_provider_picker(app)
            root.update()
            capture_window(root, output_dir / "en-model-retry.png")

            app.step = 4
            app.last_stage = "paused"
            app._render()
            capture_window(root, output_dir / "en-progress-paused.png")

            app.step = 0
            app.connection_state = "verified"
            app.connection_detail = {"model": "visual-smoke-model", "latency": 20}
            app._render()
            root.update()
            settings = app._open_settings()
            capture_window(settings.window, output_dir / "en-settings.png")
            settings._close()

            app.model_choices = ("provider/model-small", "provider/model-large")
            picker = ModelPickerDialog(app)
            capture_window(picker.window, output_dir / "en-model-picker.png")
            picker.window.destroy()

            error = app._dialog(
                "Connection failed",
                "The API rejected this request. Check the key and endpoint, then try again.",
                error=True,
            )
            capture_window(error.window, output_dir / "en-error-dialog.png")
            error.window.destroy()
        root.destroy()
    screenshot_note = f" and wrote screenshots to {output_dir}" if output_dir else ""
    print(
        f"Verified {checked} desktop page layouts without horizontal overflow or resize rebuilds"
        f"{screenshot_note}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    arguments = parser.parse_args()
    verify_layout(arguments.output_dir)
