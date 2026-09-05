from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from types import SimpleNamespace

from renweave.credentials import SecureCredentialStore
from renweave.gui import RenWeaveDesktopApp, TaskState


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

    window.lift()
    window.attributes("-topmost", True)
    try:
        window.update()
        left = window.winfo_rootx()
        top = window.winfo_rooty()
        right = left + window.winfo_width()
        bottom = top + window.winfo_height()
        if right - left < 100 or bottom - top < 40:
            raise RuntimeError(f"Window is too small to capture: {right - left}x{bottom - top}")
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    finally:
        window.attributes("-topmost", False)
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
            # A withdrawn Tk window reports 1 px canvas dimensions and cannot
            # validate responsive layout. Keep it mapped but fully transparent.
            root.attributes("-alpha", 0.0)
            root.geometry("1240x840+20+20")
            root.deiconify()
        else:
            root.geometry("1240x840+20+20")
            root.deiconify()
        app = RenWeaveDesktopApp(
            root,
            settings_path=Path(directory) / "settings.json",
            credential_store=SecureCredentialStore(backend=MemoryCredentialBackend()),
        )
        app.model.set("visual-smoke-model")
        sample_game = Path(directory) / "SampleGame"
        sample_language = sample_game / "game" / "tl" / "zh_hans"
        sample_language.mkdir(parents=True)
        (sample_language / "script.rpy").write_text(
            'translate zh_hans strings:\n\n    old "Settings"\n    new "设置"\n',
            encoding="utf-8",
        )
        app.project.set(str(sample_game))
        app._inspect_project_selection()
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
            "current_file": "src/plot/chapter_two.rpy",
            "total_files": 28,
            "completed_files": 12,
            "remaining_files": 16,
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
                    root.update()
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
                    if app.next_button is not None:
                        card_right = app.page_card.winfo_rootx() + app.page_card.winfo_width()
                        action_right = app.next_button.winfo_rootx() + app.next_button.winfo_width()
                        if abs(card_right - action_right) > 1:
                            raise RuntimeError(
                                f"Workflow action is not aligned to the card at "
                                f"{width}x{height}, {locale}, step {step}: "
                                f"card_right={card_right}, action_right={action_right}"
                            )
                    if app.back_button is not None and app.next_button is not None:
                        if app.back_button.winfo_width() != app.next_button.winfo_width():
                            raise RuntimeError(
                                f"Footer actions have unequal widths at "
                                f"{width}x{height}, {locale}, step {step}"
                            )
                    if (
                        app.settings_button.winfo_width() != app.language_button.winfo_width()
                        or app.settings_button.winfo_height() != app.language_button.winfo_height()
                    ):
                        raise RuntimeError(
                            f"Header actions have unequal dimensions at {width}x{height}, {locale}"
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
            app._set_locale("zh")
            app.source_language.set("English")
            app.target_language.set("zh_hans")
            app.scope_preview_signature = app._scope_signature()
            app.scope_preview_status = "ready"
            pending_units = [
                {
                    "file": f"game/story/chapter_{index:02d}.rpy",
                    "line": index * 17,
                    "source": f"Incremental source line {index}",
                    "detail": "missing" if index % 2 else "changed",
                }
                for index in range(1, 19)
            ]
            app.scope_preview_inventory = SimpleNamespace(
                total_units=430,
                reusable_units=412,
                model_units=len(pending_units),
                files_scanned=28,
                pending_units=pending_units,
            )
            app.step = 2
            app.worker = None
            app._render()
            root.update()
            capture_window(root, output_dir / "zh-languages-incremental.png")
            app.step = 3
            app.worker = None
            app.show_pending_details.set(True)
            app._render()
            root.update()
            if app.review_details is None or app.review_detail_host is None:
                raise RuntimeError("Incremental review details were not rendered")
            options_left = app.review_options.winfo_rootx()
            details_left = app.review_details.winfo_rootx()
            options_right = options_left + app.review_options.winfo_width()
            details_right = details_left + app.review_details.winfo_width()
            if abs(options_left - details_left) > 1 or abs(options_right - details_right) > 1:
                raise RuntimeError("Incremental review panel does not share the options alignment")
            capture_window(root, output_dir / "zh-review-incremental.png")
            app.content_canvas.yview_moveto(1.0)
            root.update()
            capture_window(root, output_dir / "zh-review-incremental-details.png")

            app._set_locale("en")
            app.status.set("Translating chapter_two")
            app.step = 2
            app.worker = None
            app.use_model_for_translation.set(False)
            app._render()
            hide_provider_picker(app)
            root.update()
            capture_window(root, output_dir / "en-model-blank.png")

            app.use_model_for_translation.set(True)
            app._render()
            hide_provider_picker(app)
            root.update()
            capture_window(root, output_dir / "en-model-using-model.png")

            app.step = 4
            app.last_stage = "paused"
            app._render()
            capture_window(root, output_dir / "en-progress-paused.png")

            app.step = 2
            app._render()
            root.update()
            settings = app._open_settings()
            capture_window(settings.window, output_dir / "en-settings.png")
            settings._close()

            error = app._dialog(
                "Connection failed",
                "The API rejected this request. Check the key and endpoint, then try again.",
                error=True,
            )
            capture_window(error.window, output_dir / "en-error-dialog.png")
            error.window.destroy()

        # Exercise the contradiction-prone states even when screenshots are disabled.
        app.step = 4
        app.translation_started = True
        cases = (
            ("preparing", "created", True, 0, 0, 100, TaskState.PREPARING, None),
            ("translating", "translating", True, 12, 30, 48, TaskState.TRANSLATING, 48),
            ("paused", "paused", False, 12, 30, 100, TaskState.PAUSED, 40),
            ("completed", "complete", False, 30, 30, 100, TaskState.COMPLETED, 100),
            ("failed", "failed", False, 12, 30, 48, TaskState.FAILED, 48),
            ("total-unknown", "translating", True, 2053, 0, 100, TaskState.TRANSLATING, None),
        )
        for name, stage, running, completed, total, raw_percent, expected_state, expected_percent in cases:
            app.worker = SimpleNamespace(is_alive=lambda value=running: value) if running else None
            app.cancel_token = None
            app.last_stage = stage
            if stage not in {"paused", "failed", "complete"}:
                app.last_active_stage = stage
            app.progress_payload = {
                "stage": stage,
                "completed_scenes": completed,
                "total_scenes": total,
                "progress_percent": raw_percent,
            }
            task = app._task_presentation()
            if task.state != expected_state or task.percent != expected_percent:
                raise RuntimeError(f"Task-state mismatch for {name}: {task}")
            app._render()
            root.update_idletasks()
            if total <= 0 and app.progress.winfo_ismapped():
                raise RuntimeError(f"Unknown total exposed a percentage bar for {name}")
            if task.state != TaskState.COMPLETED and app.progress_percent_text.get() == "100%":
                raise RuntimeError(f"Non-completed state exposed 100% for {name}")
            if output_dir is not None:
                capture_window(root, output_dir / f"en-progress-{name}.png")
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
