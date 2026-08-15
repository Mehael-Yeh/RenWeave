from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from renweave.credentials import SecureCredentialStore
from renweave.gui import RenWeaveDesktopApp


class MemoryCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def verify_layout() -> None:
    import tkinter as tk

    with tempfile.TemporaryDirectory(prefix="renweave-visual-") as directory:
        root = tk.Tk()
        root.withdraw()
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
            root.geometry(f"{width}x{height}")
            for locale in ("en", "zh"):
                app.locale.set(locale)
                for step in range(5):
                    app.step = step
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
        root.destroy()
    print(f"Verified {checked} desktop page layouts without horizontal overflow or resize rebuilds")


if __name__ == "__main__":
    verify_layout()
