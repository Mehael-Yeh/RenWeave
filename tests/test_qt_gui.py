import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from renweave.decompiler import run_unrpyc_in_process
from renweave.pipeline import PipelineStage
from renweave.qt_gui import QtRenWeaveWindow


class QtFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_shell_keeps_five_page_instances_and_switches_in_place(self):
        window = QtRenWeaveWindow()
        try:
            self.assertEqual(len(window.pages), 5)
            page_widgets = [page.scroll for page in window.pages]
            window.step = 3
            window._refresh_shell()
            window.step = 4
            window._refresh_shell()
            self.assertEqual([page.scroll for page in window.pages], page_widgets)
            self.assertIs(window.stack.currentWidget(), page_widgets[4])
        finally:
            window.close()

    def test_frontend_does_not_load_heavy_qt_modules(self):
        self.assertNotIn("PySide6.QtWebEngineCore", sys.modules)
        self.assertNotIn("PySide6.QtMultimedia", sys.modules)
        self.assertNotIn("PySide6.QtQuick", sys.modules)

    def test_unrpyc_isolation_survives_pyside_import_hook(self):
        entrypoint = (
            Path(__file__).parents[1]
            / "src"
            / "renweave"
            / "_vendor"
            / "unrpyc"
            / "unrpyc.py"
        )
        returncode, stdout, stderr = run_unrpyc_in_process(entrypoint, ["--version"])
        self.assertEqual(returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "Unrpyc v2.0.3")

    def test_review_and_progress_modules_toggle_in_place(self):
        window = QtRenWeaveWindow()
        try:
            window.show()
            window._scope_preview_inventory = SimpleNamespace(
                model_units=2,
                reusable_units=8,
                pending_units=[{"file": "script.rpy", "line": 12, "source": "Hello"}],
            )
            window._scope_preview_budget = SimpleNamespace(
                estimated_total_low=100,
                estimated_total_high=200,
            )
            window.step = 3
            window._refresh_shell()
            window._refresh_review_preview()
            pending_details = window.pending_details
            review_details = window.review_details_label
            window._toggle_pending_details()
            window._toggle_review_details()
            self.assertIs(window.pending_details, pending_details)
            self.assertIs(window.review_details_label, review_details)
            self.assertTrue(window.pending_details.isVisible())
            self.assertTrue(window.review_details_label.isVisible())

            window._progress_received({"current_operation": "translated script.rpy"})
            window.step = 4
            window._refresh_shell()
            log_edit = window.log_edit
            window._toggle_log()
            self.assertIs(window.log_edit, log_edit)
            self.assertTrue(window.log_edit.isVisible())
            self.assertIn("translated script.rpy", window.log_edit.toPlainText())
        finally:
            window.close()

    def test_locale_switch_retranslates_mounted_widgets(self):
        window = QtRenWeaveWindow()
        try:
            window.locale = "en"
            window._retranslate_ui()
            page_widgets = [page.scroll for page in window.pages]
            window._toggle_locale()
            self.assertEqual(window.locale, "zh")
            self.assertEqual(window.pages[0].title.text(), "游戏")
            self.assertEqual(window.action_button.text(), "继续")
            self.assertEqual(window.back_button.text(), "返回")
            self.assertEqual([page.scroll for page in window.pages], page_widgets)
            window._toggle_locale()
            self.assertEqual(window.pages[0].title.text(), "Game")
            self.assertEqual(window.action_button.text(), "Continue")
        finally:
            window.close()

    def test_project_revision_invalidates_pending_inspection(self):
        window = QtRenWeaveWindow()
        try:
            window.project_edit.setText("first")
            first_revision = window._project_revision
            window._project_validation_state = "pending"
            window._inspection_revision = first_revision
            window._inspection_value = "first"
            window.project_edit.setText("second")
            self.assertGreater(window._project_revision, first_revision)
            self.assertEqual(window._project_validation_state, "pending")
            self.assertFalse(window.action_button.isEnabled())
            window._project_inspected((object(), [], None), first_revision, "first")
            self.assertIsNone(window._discovered_project)
        finally:
            window.close()

    def test_project_selection_suggests_a_workspace(self):
        initial_project = str(Path("Games") / "Example")
        window = QtRenWeaveWindow(initial_project=initial_project)
        try:
            workspace = Path(window.workspace_edit.text())
            self.assertEqual(workspace.name, "Example")
            self.assertEqual(workspace.parent.name, "RenWeaveWork")
            self.assertTrue(window._workspace_auto_generated)
            custom_workspace = str(Path("Custom") / "Workspace")
            window.workspace_edit.setText(custom_workspace)
            window.workspace_edit.textEdited.emit(window.workspace_edit.text())
            window.project_edit.setText(str(Path("Games") / "Other"))
            self.assertEqual(window.workspace_edit.text(), custom_workspace)
        finally:
            window.close()

    def test_model_controls_have_selection_state_and_runtime_icon(self):
        window = QtRenWeaveWindow()
        try:
            self.assertFalse(window.windowIcon().isNull())
            window._select_provider(0)
            self.assertEqual(window.endpoint_edit.text(), "https://api.openai.com/v1")
            self.assertEqual(window.model_edit.__class__.__name__, "QComboBox")
            window.provider_buttons[0].click()
            self.assertEqual(window.provider_combo.currentIndex(), 0)
            self.assertTrue(window.provider_buttons[0].isChecked())
            self.assertFalse(any(button.isChecked() for button in window.provider_buttons[1:]))
            window.model_edit.addItems(["model-a", "model-b"])
            window.model_edit.setCurrentText("model-b")
            self.assertEqual(window.model_edit.currentText(), "model-b")
        finally:
            window.close()

    def test_all_combo_boxes_share_the_same_field_height_and_arrow_theme(self):
        window = QtRenWeaveWindow()
        try:
            combos = [window.source_combo, window.target_combo, window.provider_combo,
                      window.model_edit, window.reasoning_combo]
            self.assertTrue(all(combo.minimumHeight() == 34 for combo in combos))
            self.assertTrue(all(combo.maximumHeight() == 34 for combo in combos))
            self.assertIn("QComboBox::down-arrow", window.styleSheet())
            self.assertIn("chevron-down.svg", window.styleSheet())
        finally:
            window.close()

    def test_loaded_models_select_an_actual_catalog_item_and_reasoning_is_saved(self):
        window = QtRenWeaveWindow()
        try:
            window.model_edit.clear()
            window.model_edit.setEditText("")
            window._model_by_identity.clear()
            window._models_loaded(SimpleNamespace(models=("model-a", "model-b"), latency_ms=12))
            self.assertEqual(window.model_edit.currentIndex(), 0)
            self.assertEqual(window.model_edit.currentText(), "model-a")
            window.reasoning_combo.setCurrentIndex(window.reasoning_combo.findData("high"))
            self.assertEqual(window._profile().reasoning_level, "high")
        finally:
            window.close()

    def test_model_page_starts_without_a_model_and_shows_api_key(self):
        window = QtRenWeaveWindow()
        try:
            self.assertEqual(window.model_edit.currentText(), "")
            self.assertEqual(window.api_key_edit.echoMode(), window.api_key_edit.EchoMode.Password)
            window.api_key_edit.setText("secret")
            window.api_key_toggle.click()
            self.assertEqual(window.api_key_edit.echoMode(), window.api_key_edit.EchoMode.Normal)
            window.api_key_toggle.click()
            self.assertEqual(window.api_key_edit.echoMode(), window.api_key_edit.EchoMode.Password)
            self.assertTrue(window.use_model_hint.text())
        finally:
            window.close()

    def test_settings_restore_key_storage_and_persist_without_secret(self):
        window = QtRenWeaveWindow()
        try:
            class FakeCredentialStore:
                def set(self, *_args):
                    return None

                def delete(self, *_args):
                    return None

            with tempfile.TemporaryDirectory() as directory:
                window._settings_path = Path(directory) / "settings.json"
                window._credential_store = FakeCredentialStore()
                window._set_key_storage("memory")
                window.api_key_edit.setText("session-secret")
                window._save_api_key()
                window._save_settings()
                self.assertEqual(window._key_storage, "memory")
                self.assertEqual(window._settings_path.read_text(encoding="utf-8").find("session-secret"), -1)
            window._forget_api_key()
            self.assertEqual(window.api_key_edit.text(), "")
        finally:
            window.close()

    def test_existing_language_selection_is_cancelled_when_languages_change(self):
        window = QtRenWeaveWindow()
        try:
            window.existing_languages = [
                SimpleNamespace(language="zh_hans", script_files=1, compiled_files=0),
            ]
            window._refresh_existing_languages()
            window._start_scope_preview = lambda *_args: None
            window._select_existing_language("zh_hans")
            self.assertEqual(window._selected_existing_language, "zh_hans")
            self.assertTrue(window.existing_language_controls["zh_hans"].isChecked())
            window.source_combo.setCurrentText("English")
            self.assertIsNone(window._selected_existing_language)
            self.assertFalse(window.existing_language_controls["zh_hans"].isChecked())
        finally:
            window.close()

    def test_existing_language_reuse_must_finish_before_languages_continue(self):
        window = QtRenWeaveWindow()
        try:
            window.step = 1
            window._selected_existing_language = "zh_hans"
            window._scope_preview_status = "scanning"
            window._refresh_shell()
            self.assertFalse(window.action_button.isEnabled())
            window._scope_preview_status = "ready"
            window._refresh_shell()
            self.assertTrue(window.action_button.isEnabled())
        finally:
            window.close()

    def test_rpa_option_controls_install_visibility(self):
        window = QtRenWeaveWindow()
        try:
            window.show()
            window.step = 3
            window._refresh_shell()
            self.assertTrue(window.install_check.isVisible())
            window.generate_rpa_check.setChecked(False)
            self.assertFalse(window.install_check.isVisible())
            self.assertFalse(window.install_check.isEnabled())
            window.generate_rpa_check.setChecked(True)
            self.assertTrue(window.install_check.isVisible())
            self.assertTrue(window.install_check.isEnabled())
        finally:
            window.close()

    def test_model_continue_is_blocked_without_a_selected_model(self):
        window = QtRenWeaveWindow()
        try:
            window.locale = "en"
            window._retranslate_ui()
            window.step = 2
            window.model_edit.setEditText("")
            window._refresh_shell()
            self.assertFalse(window.action_button.isEnabled())
        finally:
            window.close()

    def test_review_continue_only_enters_progress_page(self):
        window = QtRenWeaveWindow()
        try:
            window.locale = "en"
            window._retranslate_ui()
            window.step = 3
            window._scope_preview_status = "ready"
            window._refresh_shell()
            window._continue()
            self.assertEqual(window.step, 4)
            self.assertFalse(window._translation_started)
            self.assertEqual(window.action_button.text(), "Start translation")
        finally:
            window.close()

    def test_paused_translation_is_presented_as_resumable(self):
        window = QtRenWeaveWindow()
        try:
            window.step = 4
            window._scope_preview_status = "ready"
            window._translation_started = True
            window._translation_finished(
                SimpleNamespace(
                    stage=PipelineStage.PAUSED,
                    to_dict=lambda: {
                        "stage": "paused",
                        "completed_scene_ids": ["scene-1"],
                    },
                )
            )
            self.assertEqual(window._last_stage, "paused")
            self.assertFalse(window._translation_started)
            self.assertEqual(window.action_button.text(), "Resume translation")
            self.assertIn("paused", window.progress_heading.text().casefold())
        finally:
            window.close()

    def test_navigation_is_disabled_while_translation_is_running(self):
        window = QtRenWeaveWindow()
        try:
            window.step = 4
            window._translation_started = True
            window._refresh_shell()
            self.assertFalse(window.back_button.isEnabled())
            self.assertFalse(any(button.isEnabled() for button in window.nav_buttons))
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
