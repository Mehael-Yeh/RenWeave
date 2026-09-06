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
from renweave.qt_gui import ModelPickerDialog, QtRenWeaveWindow


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
            self.assertFalse(window.review_details_label.isHidden())

            window._progress_received({"current_operation": "translated script.rpy"})
            window.step = 4
            window._refresh_shell()
            log_edit = window.log_edit
            window._toggle_log()
            self.assertIs(window.log_edit, log_edit)
            self.assertFalse(window.log_edit.isHidden())
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
            self.assertTrue(window.endpoint_edit.isVisible() or not window.isVisible())
            self.assertTrue(window.endpoint_preset_combo.isHidden())
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
                SimpleNamespace(language="zh_hans", display_name="简体中文", script_files=1, compiled_files=0),
            ]
            window._refresh_existing_languages()
            self.assertEqual(
                [window.target_combo.itemText(index) for index in range(window.target_combo.count())].count("简体中文"),
                1,
            )
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
            window._set_target_language_value("zh_hans")
            window._selected_existing_language = "zh_hans"
            window._scope_preview_status = "scanning"
            window._refresh_shell()
            self.assertFalse(window.action_button.isEnabled())
            window._scope_preview_status = "ready"
            window._refresh_shell()
            self.assertTrue(window.action_button.isEnabled())
        finally:
            window.close()

    def test_target_language_starts_blank_and_blocks_languages_continue(self):
        window = QtRenWeaveWindow()
        try:
            window.step = 1
            window._refresh_shell()
            self.assertEqual(window._target_language_value(), "")
            self.assertFalse(window.action_button.isEnabled())
            window._set_target_language_value("简体中文")
            window._refresh_shell()
            self.assertTrue(window.action_button.isEnabled())
        finally:
            window.close()

    def test_model_verification_and_catalog_are_remembered_per_provider(self):
        window = QtRenWeaveWindow()
        try:
            window.step = 2
            window.api_key_edit.setText("secret")
            window.model_edit.setEditText("model-a")
            window._refresh_shell()
            self.assertFalse(window.action_button.isEnabled())

            window._models_loaded(SimpleNamespace(models=("model-a", "model-b"), latency_ms=1))
            window._model_verified(SimpleNamespace(model="model-a", latency_ms=1))
            self.assertTrue(window.action_button.isEnabled())

            window._select_provider(1)
            window._refresh_shell()
            self.assertFalse(window.action_button.isEnabled())

            window._select_provider(0)
            self.assertEqual(
                [window.model_edit.itemText(i) for i in range(window.model_edit.count())],
                ["model-a", "model-b"],
            )
            window._refresh_shell()
            self.assertTrue(window.action_button.isEnabled())

            window.use_model_check.setChecked(False)
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

    def test_review_options_and_path_details_refresh_immediately(self):
        window = QtRenWeaveWindow()
        try:
            window.locale = "zh"
            window._retranslate_ui()
            window.project_edit.setText("C:/Games/Demo")
            window.workspace_edit.setText("C:/Work/Demo")
            window.renpy_sdk_edit.setText("C:/SDK/RenPy")
            window._scope_preview_inventory = SimpleNamespace(model_units=2, reusable_units=1, pending_units=[])
            window._scope_preview_budget = SimpleNamespace(estimated_total_low=1, estimated_total_high=2)
            window._refresh_review_preview()
            self.assertIn("项目：C:/Games/Demo", window.review_details_label.text())
            self.assertIn("工作区：C:/Work/Demo", window.review_details_label.text())
            self.assertIn("SDK：C:/SDK/RenPy", window.review_details_label.text())
            self.assertNotIn("校验后安装", window.review_fact_values[1].text())
            window.install_check.setChecked(True)
            self.assertIn("校验后安装", window.review_fact_values[1].text())
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

    def test_path_actions_copy_and_output_actions_restore_all_targets(self):
        window = QtRenWeaveWindow()
        try:
            window._copy_path("C:/Games/Example")
            self.assertEqual(QApplication.clipboard().text(), "C:/Games/Example")
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "rpy"
                package = Path(directory) / "translation.rpa"
                installed = Path(directory) / "installed"
                output.mkdir()
                package.write_text("package", encoding="utf-8")
                installed.mkdir()
                window._translation_finished(
                    SimpleNamespace(
                        stage=PipelineStage.COMPLETE,
                        to_dict=lambda: {
                            "stage": "complete",
                            "output_dir": str(output),
                            "package_path": str(package),
                            "installed_dir": str(installed),
                        },
                    )
                )
                self.assertTrue(window.progress_open_button.isVisible() or not window.isVisible())
                self.assertTrue(window.progress_open_rpa_button.isVisible() or not window.isVisible())
                self.assertTrue(window.progress_open_install_button.isVisible() or not window.isVisible())
                self.assertIn(str(package), window.progress_output.text())
        finally:
            window.close()

    def test_workspace_log_is_restored_before_a_new_run(self):
        window = QtRenWeaveWindow()
        try:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                (workspace / "logs").mkdir()
                (workspace / "logs" / "renweave.log").write_text("restored event\n", encoding="utf-8")
                window.workspace_edit.setText(str(workspace))
                window._load_workspace_log()
                self.assertIn("restored event", window.log_edit.toPlainText())
        finally:
            window.close()

    def test_provider_endpoint_presets_and_searchable_model_picker(self):
        window = QtRenWeaveWindow()
        try:
            deepseek_index = window.provider_ids.index("deepseek")
            window._select_provider(deepseek_index)
            self.assertEqual(window.endpoint_preset_combo.count(), 2)
            window.endpoint_preset_combo.setCurrentIndex(1)
            self.assertEqual(window.endpoint_edit.text(), "https://api.deepseek.com/v1")

            window._models_loaded(SimpleNamespace(models=("alpha", "beta-vision", "gamma"), latency_ms=4))
            self.assertFalse(window.browse_model_button.isVisible())
            dialog = ModelPickerDialog(window, window._model_catalog_models)
            try:
                dialog.search_edit.setText("vision")
                self.assertEqual(dialog.list_widget.count(), 1)
                self.assertEqual(dialog.list_widget.item(0).text(), "beta-vision")
                dialog.list_widget.setCurrentRow(0)
                dialog._select()
                self.assertEqual(dialog.selected_model, "beta-vision")
            finally:
                dialog.close()
        finally:
            window.close()

    def test_progress_phases_follow_pipeline_stage_and_errors_keep_details(self):
        window = QtRenWeaveWindow()
        try:
            window._progress_received({"stage": "translating", "progress_percent": 45})
            states = [label.property("phase_state") for label in window.progress_phase_labels]
            self.assertEqual(states, ["done", "done", "active", "idle", "idle"])
            window._translation_failed(RuntimeError("provider timeout with request id 42"))
            self.assertTrue(window.progress_error_button.isVisible() or not window.isVisible())
            self.assertIn("request id 42", window._last_error_details)
            window.locale = "zh"
            window._retranslate_ui()
            window.step = 3
            window.review_details_label.setVisible(True)
            window._toggle_review_details()
            window._toggle_review_details()
            self.assertIn("项目：", window.review_details_label.text())
            self.assertNotIn("Project:", window.review_details_label.text())
            self.assertTrue(window.project_edit.toolTip())
        finally:
            window.close()

    def test_model_connection_failure_is_friendly_and_keeps_copyable_details(self):
        window = QtRenWeaveWindow()
        try:
            self.assertFalse(window.model_error_button.isVisible())
            window._model_operation_failed(RuntimeError("401 unauthorized: invalid api key"), loading=True)
            self.assertTrue(window.model_error_button.isVisible() or not window.isVisible())
            self.assertIn("API", window.model_status.text())
            self.assertIn("401 unauthorized", window._last_error_details)
            window.locale = "zh"
            window._retranslate_ui()
            self.assertTrue(window.provider_description_label.text())
            self.assertNotIn("Official", window.provider_description_label.text())
            self.assertIn("API 密钥", window.model_status.text())
        finally:
            window.close()

    def test_game_page_only_exposes_three_path_selectors_and_normalizes_slashes(self):
        window = QtRenWeaveWindow(initial_project="C:\\Games\\Demo", initial_workspace="C:\\Work\\Demo")
        try:
            self.assertEqual(window.project_edit.text(), "C:/Games/Demo")
            self.assertEqual(window.workspace_edit.text(), "C:/Work/Demo")
            self.assertFalse(hasattr(window, "project_copy_button"))
            self.assertFalse(hasattr(window, "project_open_button"))
            self.assertIn("QPushButton#Primary:disabled", window.styleSheet())
        finally:
            window.close()

    def test_blank_review_uses_zero_tokens_and_single_summary_card(self):
        window = QtRenWeaveWindow()
        try:
            window.locale = "zh"
            window._blank_translation_mode = True
            window._scope_preview_inventory = SimpleNamespace(model_units=0, reusable_units=3, pending_units=[])
            window._scope_preview_budget = SimpleNamespace(estimated_total_low=12, estimated_total_high=34)
            window._refresh_review_preview()
            self.assertEqual(window.review_fact_values[0].text(), "—")
            self.assertEqual(window.budget_label.text(), "0 Token")
            self.assertEqual(window.budget_note.text(), "生成空白翻译不产生Token消耗")
            self.assertFalse(window.review_details_label.isHidden())
            self.assertIsNone(window.review_details_toggle)
        finally:
            window.close()

    def test_progress_hides_bar_percentage_and_keeps_log_visible_without_toggle(self):
        window = QtRenWeaveWindow()
        try:
            self.assertFalse(window.progress_bar.isTextVisible())
            self.assertTrue(window.log_edit.isVisible() or not window.isVisible())
            self.assertIsNone(window.log_toggle)
            window._toggle_log()
            self.assertFalse(window.log_edit.isHidden())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
