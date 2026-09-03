from __future__ import annotations

import json
import hashlib
import io
import pickle
import re
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import urllib.error

from renweave.context import ContextPlanner
from renweave.credentials import SecureCredentialStore, credential_account
from renweave.build_validation import (
    GeneratedScriptValidator,
    RenpyBuildValidator,
    RenpyEngineValidator,
    RenpySdk,
    RenpySdkLocator,
)
from renweave.decompiler import (
    FROZEN_UNRPYC_SWITCH,
    UNRPYC_BUNDLED_FILES,
    UNRPYC_BUNDLED_TREE_SHA256,
    DecompilationError,
    SequentialPool,
    UnrpycDecompiler,
    UnrpycToolManager,
)
from renweave.emitter import RenpyTranslationEmitter, TranslationConflict
from renweave.existing_translations import ExistingTranslationScanner, discover_existing_languages
from renweave.gui import (
    Metrics,
    STAGE_LABELS,
    RenWeaveDesktopApp,
    TranslationRequest,
    default_desktop_settings_path,
    execute_translation,
)
from renweave.indexer import ProjectIndexer
from renweave.installer import TranslationInstaller
from renweave.knowledge import DeterministicKnowledgeBuilder
from renweave.models import TextChannel
from renweave.narrative import NarrativeKnowledgeSynthesizer
from renweave.packaging import TranslationPackager
from renweave.pipeline import ANALYSIS_SCHEMA_VERSION, PipelineStage, RenWeavePipeline
from renweave.provider import ModelProfile, OpenAICompatibleCatalog, OpenAICompatibleGateway
from renweave.provider_presets import PROVIDER_PRESETS_BY_ID, get_provider_preset
from renweave.runtime import CancellationToken, WorkspaceLease
from renweave.usage import estimate_index_tokens, estimate_project_tokens
from renweave.refinement import GlobalTranslationRefiner
from renweave.rpa import RpaArchive, RpaError, RpaWriter, UnsafeArchivePath, script_member
from renweave.validation import TranslationValidator
from renweave.update_check import check_for_updates


SAMPLE_SCRIPT = '''label start:
    eve happy "Hello [player]{b}friend{/b}!"
    "A quiet morning."
    menu:
        "Go outside" if ready:
            jump outside
    text _("Settings")
    call helper

label outside:
    eve "We made it."
    return

label helper:
    anon "Need any help?"
    return
'''


def write_rpa(path: Path, members: dict[str, bytes], version: str = "3.0") -> None:
    key = 0xDEADBEEF
    header_size = {"2.0": 25, "3.0": 34, "3.2": 43}[version]
    offset = header_size
    payloads = []
    index = {}
    for name, payload in members.items():
        if version == "2.0":
            index[name] = [(offset, len(payload))]
        else:
            index[name] = [(offset ^ key, len(payload) ^ key)]
        payloads.append(payload)
        offset += len(payload)
    serialized = pickle.dumps(index, protocol=2)
    if version == "2.0":
        header = f"RPA-2.0 {offset:016x}\n".encode("ascii")
    elif version == "3.2":
        header = f"RPA-3.2 {offset:016x} 00000000 {key:08x}\n".encode("ascii")
    else:
        header = f"RPA-3.0 {offset:016x} {key:08x}\n".encode("ascii")
    if len(header) != header_size:
        raise AssertionError(len(header))
    path.write_bytes(header + b"".join(payloads) + zlib.compress(serialized))


class CorePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "SampleGame"
        self.game = self.root / "game"
        self.game.mkdir(parents=True)
        (self.game / "script.rpy").write_text(SAMPLE_SCRIPT, encoding="utf-8", newline="\n")
        tl = self.game / "tl" / "zh_hans"
        tl.mkdir(parents=True)
        (tl / "script.rpy").write_text("label translated:\n    eve \"不应索引\"\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_index_builds_scene_graph_and_excludes_tl(self) -> None:
        index = ProjectIndexer().build(self.root)
        self.assertEqual(index.project.source_scripts, ["script.rpy"])
        self.assertEqual(index.project.translation_languages, ["zh_hans"])
        self.assertEqual([scene.label for scene in index.scenes], ["start", "outside", "helper"])
        channels = [str(unit.channel) for unit in index.text_units]
        self.assertEqual(channels.count("dialogue"), 3)
        self.assertEqual(channels.count("narration"), 1)
        self.assertEqual(channels.count("menu"), 1)
        self.assertEqual(channels.count("ui"), 1)
        start = index.scenes[0]
        self.assertEqual([(str(edge.kind), edge.target_label) for edge in start.edges], [
            ("jump", "outside"),
            ("call", "helper"),
            ("fallthrough", "outside"),
        ])

    def test_parser_does_not_treat_ui_syntax_inside_narration_as_a_call(self) -> None:
        (self.game / "quoted_ui_syntax.rpy").write_text(
            'label quoted_ui_syntax:\n    "Use _(\'sample_key\') in source code."\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        scene = next(scene for scene in index.scenes if scene.label == "quoted_ui_syntax")
        self.assertEqual(len(scene.text_units), 1)
        self.assertEqual(scene.text_units[0].channel, TextChannel.NARRATION)
        self.assertEqual(scene.text_units[0].source, "Use _('sample_key') in source code.")

    def test_parser_does_not_translate_internal_condition_literals(self) -> None:
        (self.game / "conditions.rpy").write_text(
            "label conditions:\n"
            "    if relationship < 'romance':\n"
            "        eve \"A real line.\"\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        units = [
            unit for unit in index.text_units
            if unit.location.relative_path == "conditions.rpy"
        ]
        self.assertEqual([unit.source for unit in units], ["A real line."])

    def test_parser_does_not_translate_control_flow_return_values_or_tag_only_text(self) -> None:
        (self.game / "control_values.rpy").write_text(
            "label control_values:\n"
            "    return 'route_key'\n"
            "    style_prefix 'internal_style'\n"
            "    anon '{nw=2}'\n"
            "    anon 'Visible text.'\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        units = [
            unit for unit in index.text_units
            if unit.location.relative_path == "control_values.rpy"
        ]
        self.assertEqual([unit.source for unit in units], ["Visible text."])

    def test_percentage_text_is_not_mistaken_for_printf_placeholder(self) -> None:
        (self.game / "percentage.rpy").write_text(
            "label percentage:\n    anon '75% OFF'\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(unit for unit in index.text_units if unit.source == "75% OFF")
        report = TranslationValidator().validate_scene(
            index,
            unit.scene_id,
            {unit.id: "优惠 75%"},
        )
        self.assertTrue(report.passed)

    def test_parser_excludes_label_free_resource_literals_but_keeps_explicit_ui(self) -> None:
        (self.game / "resources.rpy").write_text(
            "image synthetic composite:\n"
            "    contains:\n"
            "        'synthetic layer name'\n"
            "text _('Synthetic setting')\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        resources = [
            unit for unit in index.text_units if unit.location.relative_path == "resources.rpy"
        ]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].channel, TextChannel.UI)
        self.assertEqual(resources[0].source, "Synthetic setting")

    def test_public_api_exposes_pipeline_and_model_profile(self) -> None:
        import renweave

        self.assertTrue(renweave.__version__)
        self.assertRegex(renweave.__version__, r"^(?:development|\d)")
        self.assertIs(renweave.RenWeavePipeline, RenWeavePipeline)
        self.assertIs(renweave.ModelProfile, ModelProfile)

    def test_knowledge_and_minimal_scene_context(self) -> None:
        index = ProjectIndexer().build(self.root)
        knowledge = DeterministicKnowledgeBuilder().build(index)
        characters = {character.name: character for character in knowledge.characters}
        self.assertEqual(characters["eve"].line_count, 2)
        self.assertEqual(characters["anon"].line_count, 1)
        context = ContextPlanner().build(index, knowledge, index.scenes[1].id)
        self.assertEqual(context.speakers, ("eve",))
        self.assertIn("Hello", context.previous_summary)
        self.assertIn("Need any help", context.next_summary)

    def test_context_uses_control_flow_and_does_not_cross_unrelated_files(self) -> None:
        (self.game / "isolated_a.rpy").write_text(
            'label isolated_a:\n    "Alpha only."\n', encoding="utf-8"
        )
        (self.game / "isolated_b.rpy").write_text(
            'label isolated_b:\n    "Beta only."\n', encoding="utf-8"
        )
        index = ProjectIndexer().build(self.root)
        knowledge = DeterministicKnowledgeBuilder().build(index)
        isolated = next(scene for scene in index.scenes if scene.label == "isolated_a")
        context = ContextPlanner().build(index, knowledge, isolated.id)
        self.assertNotIn("Beta only", context.next_summary)

        start = next(scene for scene in index.scenes if scene.label == "start")
        start_context = ContextPlanner().build(index, knowledge, start.id)
        self.assertIn("We made it", start_context.next_summary)
        self.assertIn("Need any help", start_context.next_summary)

    def test_validation_protects_tags_and_placeholders(self) -> None:
        index = ProjectIndexer().build(self.root)
        scene = index.scenes[0]
        translations = {unit.id: unit.source for unit in scene.text_units}
        report = TranslationValidator().validate_scene(index, scene.id, translations)
        self.assertTrue(report.passed)
        dialogue = scene.text_units[0]
        translations[dialogue.id] = "你好，朋友！"
        report = TranslationValidator().validate_scene(index, scene.id, translations)
        self.assertEqual(
            {issue.code for issue in report.issues},
            {"TAG_MISMATCH", "PLACEHOLDER_MISMATCH"},
        )

    def test_analysis_pipeline_writes_recoverable_artifacts(self) -> None:
        workspace = Path(self.temp.name) / "workspace"
        pipeline = RenWeavePipeline(workspace)
        first_index, _ = pipeline.analyze(self.root)
        second_index, _ = pipeline.analyze(self.root)
        self.assertEqual(first_index.to_dict(), second_index.to_dict())
        self.assertTrue((workspace / "project-index.json").is_file())
        self.assertTrue((workspace / "knowledge.json").is_file())
        state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], str(PipelineStage.KNOWLEDGE_READY))

    def test_desktop_request_uses_any_target_language_and_keeps_key_in_memory(self) -> None:
        provider = Path(self.temp.name) / "desktop-provider.json"
        provider.write_text(json.dumps({
            "kind": "openai_compatible",
            "name": "Desktop Model",
            "model": "translation-model",
            "base_url": "https://example.invalid/v1",
        }), encoding="utf-8")
        workspace = Path(self.temp.name) / "desktop-workspace"
        captured = {}

        class FakePipeline:
            def __init__(self, selected_workspace):
                captured["workspace"] = selected_workspace

            def translate(self, *args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return SimpleNamespace(
                    stage=PipelineStage.COMPLETE,
                    failed_scene_ids=[],
                    error="",
                )

        request = TranslationRequest(
            project=str(self.root),
            workspace=str(workspace),
            provider=str(provider),
            source_language="English",
            target_language="Português do Brasil",
            api_key="memory-only-secret",
            generate_rpa=False,
            install=True,
        )
        result = execute_translation(request, pipeline_factory=FakePipeline)
        self.assertEqual(result.stage, PipelineStage.COMPLETE)
        self.assertEqual(captured["args"][2], "Português do Brasil")
        self.assertEqual(captured["args"][3].api_key, "memory-only-secret")
        self.assertTrue(captured["kwargs"]["install"])
        self.assertFalse(captured["kwargs"]["generate_rpa"])
        self.assertNotIn("memory-only-secret", provider.read_text(encoding="utf-8"))
        self.assertFalse(workspace.exists())

    def test_desktop_progress_labels_cover_every_pipeline_stage(self) -> None:
        self.assertEqual({str(stage) for stage in PipelineStage} - set(STAGE_LABELS), set())

    def test_desktop_settings_path_survives_missing_home_environment(self) -> None:
        fallback = Path(self.temp.name) / "fallback"
        with (
            mock.patch("renweave.gui.os", SimpleNamespace(name="nt", environ={})),
            mock.patch("renweave.gui.Path.home", side_effect=RuntimeError("home unavailable")),
            mock.patch("renweave.gui.tempfile.gettempdir", return_value=str(fallback)),
        ):
            self.assertEqual(
                default_desktop_settings_path(),
                fallback / "AppData" / "Roaming" / "RenWeave" / "settings.json",
            )

    def test_desktop_window_starts_with_model_setup_and_switches_language(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        try:
            root.withdraw()
            settings_path = Path(self.temp.name) / "desktop-settings.json"
            bundled_runner = self.root / "lib" / "py3-windows-x86_64" / "python.exe"
            bundled_runner.parent.mkdir(parents=True)
            bundled_runner.write_bytes(b"runner")
            (self.root / "SampleGame.exe").write_bytes(b"launcher")
            (self.root / "SampleGame.py").write_text("# launcher\n", encoding="utf-8")
            class DesktopCredentialBackend:
                def __init__(self) -> None:
                    self.values = {}

                def get_password(self, service, username):
                    return self.values.get((service, username))

                def set_password(self, service, username, password):
                    self.values[(service, username)] = password

                def delete_password(self, service, username):
                    self.values.pop((service, username), None)

            app = RenWeaveDesktopApp(
                root,
                initial_project=str(self.root),
                initial_workspace=str(Path(self.temp.name) / "visual-workspace"),
                settings_path=settings_path,
                credential_store=SecureCredentialStore(backend=DesktopCredentialBackend()),
            )
            root.update_idletasks()
            app._inspect_project_selection()
            self.assertEqual(app.step, 0)
            self.assertEqual(app.locale.get(), "en")
            self.assertEqual(app.brand_title.cget("text"), "RenWeave")
            self.assertEqual(app.connect_button.cget("text"), "Load available models")
            self.assertEqual(app.selected_provider_id.get(), "openai")
            self.assertEqual(app.key_storage.get(), "secure")
            self.assertFalse(app.update_checks_enabled.get())
            self.assertTrue(app.generate_rpa.get())
            self.assertEqual(app.reasoning_level.get(), "auto")
            self.assertTrue(Path(app.renpy_sdk.get()).samefile(self.root))
            self.assertTrue(app.require_engine.get())
            self.assertEqual([item.language for item in app.existing_languages], ["zh_hans"])
            self.assertEqual(
                tuple(app.reasoning_box.cget("values")),
                ("Automatic (provider default)", "Low", "High", "Maximum"),
            )
            primary_layout = str(app.ttk.Style(root).layout("Primary.TButton"))
            self.assertIn("AccentButton", primary_layout)
            self.assertNotIn("focus", primary_layout.casefold())
            settings_dialog = app._open_settings()
            root.update_idletasks()
            self.assertEqual(settings_dialog.window.title(), "Settings")
            settings_dialog._close()
            endpoint_box = app.endpoint_box
            page = app.page
            scroll_position = app.content_canvas.yview()
            app.api_key.set("provider-specific-secret")
            app._apply_provider_preset("minimax")
            root.update_idletasks()
            self.assertIs(app.endpoint_box, endpoint_box)
            self.assertIs(app.page, page)
            self.assertEqual(app.content_canvas.yview()[0], scroll_position[0])
            self.assertEqual(app.selected_provider_id.get(), "minimax")
            self.assertEqual(app.base_url.get(), "https://api.minimax.io/v1")
            self.assertIn("https://api.minimaxi.com/v1", app.endpoint_box.cget("values"))
            self.assertEqual(app.api_key.get(), "")
            self.assertFalse(app.supports_json.get())
            self.assertEqual(app.next_button.cget("text"), "Continue")
            self.assertEqual(int(app.next_button.cget("width")), Metrics.FOOTER_ACTION_WIDTH)
            self.assertTrue(all(button.winfo_class() == "TButton" for button in app.nav_buttons))
            self.assertEqual(sum(bool(button.grid_info()) for button in app.provider_buttons.values()), 12)
            self.assertTrue(all(button.cget("image") for button in app.provider_buttons.values()))
            self.assertFalse(hasattr(app, "provider_more_button"))
            self.assertIn("custom2", app.provider_buttons)
            self.assertTrue(app.next_button.instate(["disabled"]))
            self.assertEqual(root.minsize()[0], 900)
            self.assertEqual(app.source_language.get(), "auto")
            self.assertEqual(app.status.get(), "Enter an API key, then load available models.")
            self.assertFalse(hasattr(app, "_browse_provider"))
            self.assertEqual(app.language_button.cget("text"), "中文")
            self.assertEqual(app.language_button.cget("style"), "Ghost.TButton")
            self.assertEqual(
                app.settings_button.winfo_reqheight(),
                app.language_button.winfo_reqheight(),
            )
            settings_tooltip = next(
                tooltip
                for tooltip in app._tooltips
                if tooltip.widget is app.settings_button
            )
            self.assertEqual(settings_tooltip.translation_key, "tip.settings")

            def widget_classes(widget):
                classes = [child.winfo_class() for child in widget.winfo_children()]
                for child in widget.winfo_children():
                    classes.extend(widget_classes(child))
                return classes

            self.assertNotIn("TCombobox", widget_classes(app.top))

            app.language_button.invoke()
            root.update_idletasks()
            self.assertEqual(app.locale.get(), "zh")
            self.assertEqual(app.brand_title.cget("text"), "织译")
            self.assertEqual(int(app.brand_title.cget("wraplength")), 188)
            self.assertLessEqual(app.brand_title.winfo_reqwidth(), 192)
            self.assertEqual(app.connect_button.cget("text"), "获取可用模型")
            self.assertEqual(app.language_button.cget("text"), "English")
            self.assertIn("系统加密", app.t(settings_tooltip.translation_key))
            self.assertEqual(app.t("steps.progress"), "翻译")

            app.model.set("translation-model")
            app.connection_state = "verified"
            app.connection_detail = {"model": "translation-model", "latency": 25}
            app._render()
            self.assertFalse(app.next_button.instate(["disabled"]))
            key_entry = app.api_key_entry
            app.api_key.set("edited-in-place")
            self.assertIs(app.api_key_entry, key_entry)
            self.assertEqual(app.connection_state, "changed")
            self.assertTrue(app.next_button.instate(["disabled"]))
            app.connection_state = "verified"
            app._continue()
            self.assertEqual(app.step, 1)
            app._continue()
            self.assertEqual(app.step, 2)
            app.target_language.set("Français")
            app._continue()
            self.assertEqual(app.step, 3)
            self.assertEqual(app.start_button.cget("text"), "开始一键翻译")
            self.assertIsNotNone(app.token_budget)

            def visible_texts(widget):
                texts = []
                for child in widget.winfo_children():
                    try:
                        text = child.cget("text")
                    except tk.TclError:
                        text = ""
                    if text:
                        texts.append(str(text))
                    texts.extend(visible_texts(child))
                return texts

            review_text = "\n".join(visible_texts(app.content))
            self.assertIn("预计 TOKEN 预算", review_text)
            self.assertIn("Token", review_text)
            self.assertIn("生成通过验证的 RPA 归档", review_text)
            self.assertIn("标准 RPY 始终保留", review_text)
            self.assertIn("编译并验证 RPYC", review_text)

            class ActiveWorker:
                @staticmethod
                def is_alive():
                    return True

            app.step = 4
            app.worker = ActiveWorker()
            app.cancel_token = CancellationToken()
            app.last_stage = "translating"
            app.progress_payload = {
                "stage": "translating",
                "progress_percent": 58.5,
                "completed_scenes": 10,
                "total_scenes": 20,
                "eta_seconds": 600,
                "total_model_calls": 12,
                "total_prompt_tokens": 1000,
                "total_completion_tokens": 250,
                "estimated_total_tokens_low": 12000,
                "estimated_total_tokens_high": 18000,
                "usage_reporting_status": "reported",
                "current_scene_label": "chapter_two",
                "current_file": "src/plot/chapter_two.rpy",
                "total_files": 12,
                "completed_files": 7,
                "remaining_files": 5,
            }
            app.status.set("正在翻译")
            app._render()
            progress_page = app.page
            progress_widget = app.progress
            self.assertEqual(float(app.progress["maximum"]), 100)
            self.assertEqual(float(app.progress["value"]), 58.5)
            self.assertEqual(app.pause_button.cget("text"), "安全暂停")
            self.assertEqual(app.progress_runtime_state.cget("text"), "●  程序正在正常运行")
            self.assertEqual(app.progress_stage_counter.cget("text"), "翻译链路第 9/15 阶段")
            self.assertTrue(app._progress_activity_running)
            scrollbars = []

            def collect_scrollbars(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == "TScrollbar":
                        scrollbars.append(child)
                    collect_scrollbars(child)

            collect_scrollbars(app.content)
            self.assertTrue(scrollbars)
            self.assertTrue(all(bar.cget("style") == "Workspace.Vertical.TScrollbar" for bar in scrollbars))
            progress_text = "\n".join(visible_texts(app.content))
            self.assertIn("提供商已报告 1.2K", progress_text)
            self.assertIn("预计项目总量 12K–18K", progress_text)
            self.assertIn("提供商已返回 Token 用量", progress_text)
            updated_payload = dict(app.progress_payload)
            updated_payload.update(progress_percent=61.0, completed_scenes=11)
            app._apply_progress_payload(updated_payload)
            self.assertIs(app.page, progress_page)
            self.assertIs(app.progress, progress_widget)
            self.assertIn("7/12", app.progress_stat_value_labels[1].cget("text"))
            self.assertIn("5", app.progress_stat_value_labels[1].cget("text"))
            self.assertIn("src/plot/chapter_two.rpy", app.progress_stat_value_labels[0].cget("text"))
            app._request_pause()
            self.assertTrue(app.cancel_token.cancelled)
            self.assertEqual(app.status.get(), "正在完成当前安全单元并保存检查点……")
            app._save_desktop_settings()
            saved_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_settings["provider_id"], "minimax")
            self.assertEqual(saved_settings["locale"], "zh")
            self.assertNotIn("api_key", saved_settings)
            self.assertEqual(app._load_desktop_settings()["provider_id"], "minimax")
            app.worker = None
            app.last_stage = "failed"
            app.translation_baseline = app._critical_translation_config()
            app.target_language.set("Deutsch")
            self.assertIn("目标语言", app._critical_config_changes())
            app._render()
            self.assertEqual(app.progress_runtime_state.cget("text"), "●  翻译已停止")
            self.assertFalse(app._progress_activity_running)
            self.assertIsNotNone(app.back_button)
            with mock.patch.object(app, "_dialog") as back_notice:
                app.back_button.invoke()
            self.assertEqual(app.step, 3)
            back_notice.assert_called_once()
            self.assertTrue(back_notice.call_args.kwargs["warning"])
            with mock.patch.object(app, "_dialog") as changed_warning:
                app._start()
            changed_warning.assert_called_once()
            self.assertEqual(changed_warning.call_args.args[0], "翻译关键设置已改变")
            self.assertEqual(changed_warning.call_args.kwargs["confirm_text"], "仍然继续")
            self.assertTrue(callable(changed_warning.call_args.kwargs["on_confirm"]))
            app.narrow_layout = True
            app._render()
            self.assertEqual(int(app.sidebar.cget("width")), Metrics.NARROW_SIDEBAR_WIDTH)
            self.assertEqual(app.brand_title.cget("text"), "织译")
            self.assertTrue(all(button.cget("style").startswith("NavNarrow") for button in app.nav_buttons))
        finally:
            root.destroy()

    def test_analysis_cache_invalidates_when_project_scripts_change(self) -> None:
        workspace = Path(self.temp.name) / "fingerprint-workspace"
        pipeline = RenWeavePipeline(workspace)
        first, _ = pipeline.analyze(self.root)
        with (self.game / "script.rpy").open("a", encoding="utf-8", newline="\n") as writer:
            writer.write('\nlabel added_later:\n    "A newly added line."\n')
        second, _ = pipeline.analyze(self.root)
        self.assertEqual(len(second.scenes), len(first.scenes) + 1)
        self.assertIn("added_later", {scene.label for scene in second.scenes})

    def test_analysis_schema_upgrade_preserves_scene_checkpoints(self) -> None:
        workspace = Path(self.temp.name) / "analysis-upgrade-workspace"
        pipeline = RenWeavePipeline(workspace)
        pipeline.analyze(self.root, source_language="en", target_language="fr")
        state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        state["analysis_schema_version"] = ANALYSIS_SCHEMA_VERSION - 1
        state["completed_scene_ids"] = ["synthetic-checkpoint"]
        (workspace / "state.json").write_text(json.dumps(state), encoding="utf-8")

        upgraded, _ = pipeline.analyze(
            self.root,
            source_language="en",
            target_language="fr",
        )
        saved = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(upgraded.schema_version, ANALYSIS_SCHEMA_VERSION)
        self.assertEqual(saved["analysis_schema_version"], ANALYSIS_SCHEMA_VERSION)
        self.assertEqual(saved["completed_scene_ids"], ["synthetic-checkpoint"])
        self.assertIn(
            "analysis_cache_upgraded",
            (workspace / "logs" / "events.jsonl").read_text(encoding="utf-8"),
        )

    def test_rpa3_safe_selective_extraction(self) -> None:
        archive_path = self.root / "game" / "scripts.rpa"
        script = b'label archived:\n    eve "From archive."\n'
        write_rpa(archive_path, {
            "story/archived.rpy": script,
            "images/large.png": b"not really an image",
        })
        output = Path(self.temp.name) / "unpacked"
        with RpaArchive(archive_path) as archive:
            self.assertEqual(archive.version, "RPA-3.0")
            self.assertEqual(archive.read("story/archived.rpy"), script)
            manifest = archive.extract(output, include=script_member)
        self.assertEqual([item.member_name for item in manifest.files], ["story/archived.rpy"])
        self.assertEqual((output / "story" / "archived.rpy").read_bytes(), script)
        self.assertEqual(len(manifest.files[0].sha256), 64)

    def test_rpa_rejects_path_traversal(self) -> None:
        archive_path = self.root / "game" / "unsafe.rpa"
        write_rpa(archive_path, {"../escape.rpy": b"bad"})
        with self.assertRaises(UnsafeArchivePath):
            RpaArchive(archive_path).open()

    def test_rpa_writer_is_deterministic_and_round_trips(self) -> None:
        first = Path(self.temp.name) / "first.rpa"
        second = Path(self.temp.name) / "second.rpa"
        members = {
            "tl/de/strings.rpy": b"translate de strings:\n",
            "tl/de/story.rpy": b"translate de start_deadbeef:\n    pass\n",
        }
        RpaWriter().write(first, members)
        RpaWriter().write(second, dict(reversed(list(members.items()))))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with RpaArchive(first) as archive:
            self.assertEqual(archive.names(), tuple(sorted(members)))
            self.assertEqual({name: archive.read(name) for name in archive.names()}, members)

    def test_pipeline_indexes_rpy_source_from_archive(self) -> None:
        archive_game = Path(self.temp.name) / "ArchiveOnly" / "game"
        archive_game.mkdir(parents=True)
        write_rpa(archive_game / "scripts.rpa", {
            "story/route.rpy": b'label route:\n    eve "Recovered source."\n',
            "story/route.rpyc": b"compiled-placeholder",
        })
        workspace = Path(self.temp.name) / "archive-workspace"
        index, _ = RenWeavePipeline(workspace).analyze(archive_game.parent)
        self.assertEqual([scene.label for scene in index.scenes], ["route"])
        self.assertTrue(index.files[0].relative_path.startswith("_archives/0/"))
        acquisition = json.loads((workspace / "acquisition.json").read_text(encoding="utf-8"))
        self.assertEqual(len(acquisition["archives"][0]["files"]), 2)

    def test_rpa_legacy_header_variants(self) -> None:
        for version in ("2.0", "3.2"):
            with self.subTest(version=version):
                archive_path = self.root / "game" / f"legacy-{version}.rpa"
                payload = f"archive {version}".encode("ascii")
                write_rpa(archive_path, {"data/value.bin": payload}, version=version)
                with RpaArchive(archive_path) as archive:
                    self.assertEqual(archive.version, f"RPA-{version}")
                    self.assertEqual(archive.read("data/value.bin"), payload)

    def test_emitter_builds_standard_renpy_translation_scripts(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ES: {unit.source}" for unit in index.text_units}
        output = Path(self.temp.name) / "output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "es-ES", output)

        self.assertEqual(manifest.renpy_language, "es_es")
        dialogue = output / "game" / "tl" / "es_es" / "script.rpy"
        strings = output / "game" / "tl" / "es_es" / "strings.rpy"
        self.assertTrue(dialogue.is_file())
        self.assertTrue(strings.is_file())
        canonical = 'eve happy "Hello [player]{b}friend{/b}!"'
        digest = hashlib.md5((canonical + "\r\n").encode("utf-8")).hexdigest()[:8]
        dialogue_text = dialogue.read_text(encoding="utf-8")
        self.assertIn(f"translate es_es start_{digest}:", dialogue_text)
        self.assertIn('eve happy "ES: Hello [player]{b}friend{/b}!"', dialogue_text)
        strings_text = strings.read_text(encoding="utf-8")
        self.assertIn('old "Go outside"', strings_text)
        self.assertIn('new "ES: Go outside"', strings_text)
        self.assertEqual(manifest.translated_units, len(index.text_units))
        self.assertTrue((output / "build.json").is_file())

        packaged = TranslationPackager().package(manifest, output / "packages")
        self.assertTrue(Path(packaged.archive_path).is_file())
        self.assertEqual(len(packaged.archive_sha256), 64)
        with RpaArchive(packaged.archive_path) as archive:
            self.assertEqual(
                archive.names(),
                ("tl/es_es/script.rpy", "tl/es_es/strings.rpy"),
            )

    def test_existing_translation_scanner_detects_complete_and_changed_source(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ZH: {unit.source}" for unit in index.text_units}
        RenpyTranslationEmitter().emit(index, translations, "zh_hans", self.root)

        summaries = discover_existing_languages(self.root)
        self.assertEqual([item.language for item in summaries], ["zh_hans"])
        self.assertGreaterEqual(summaries[0].script_files, 2)
        complete = ExistingTranslationScanner().scan(index, "zh_hans")
        self.assertTrue(complete.complete)
        self.assertEqual(complete.reusable_units, len(index.text_units))
        self.assertEqual(complete.missing_units, 0)

        (self.game / "script.rpy").write_text(
            SAMPLE_SCRIPT.replace("We made it.", "We finally made it."),
            encoding="utf-8",
            newline="\n",
        )
        changed_index = ProjectIndexer().build(self.root)
        changed = ExistingTranslationScanner().scan(changed_index, "zh_hans")
        self.assertFalse(changed.complete)
        self.assertGreaterEqual(changed.missing_units, 1)

    def test_existing_translation_scanner_reuses_unique_source_across_block_types(self) -> None:
        language_file = self.game / "tl" / "zh_hans" / "strings.rpy"
        language_file.write_text(
            'translate zh_hans strings:\n\n'
            '    old "A quiet morning."\n'
            '    new "一个安静的早晨。"\n',
            encoding="utf-8",
            newline="\n",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(item for item in index.text_units if item.source == "A quiet morning.")

        inventory = ExistingTranslationScanner().scan(index, "zh_hans")

        self.assertEqual(
            inventory.translations_by_scene[unit.scene_id][unit.id],
            "一个安静的早晨。",
        )
        self.assertEqual(inventory.exact_source_reused_units, 1)
        self.assertNotIn(unit.id, {item["text_id"] for item in inventory.pending_units})

    def test_existing_translation_scanner_reuses_only_safe_normalized_source(self) -> None:
        original = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ZH: {unit.source}" for unit in original.text_units}
        RenpyTranslationEmitter().emit(original, translations, "zh_hans", self.root)

        for replacement in ("WE MADE IT.", "We, made it."):
            with self.subTest(replacement=replacement):
                (self.game / "script.rpy").write_text(
                    SAMPLE_SCRIPT.replace("We made it.", replacement),
                    encoding="utf-8",
                    newline="\n",
                )
                changed = ProjectIndexer().build(self.root)
                unit = next(item for item in changed.text_units if item.source == replacement)
                inventory = ExistingTranslationScanner().scan(changed, "zh_hans")
                self.assertEqual(
                    inventory.translations_by_scene[unit.scene_id][unit.id],
                    "ZH: We made it.",
                )
                self.assertEqual(inventory.normalized_source_reused_units, 1)

    def test_existing_translation_scanner_does_not_reuse_changed_wording(self) -> None:
        original = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ZH: {unit.source}" for unit in original.text_units}
        RenpyTranslationEmitter().emit(original, translations, "zh_hans", self.root)
        replacement = "We finally made it."
        (self.game / "script.rpy").write_text(
            SAMPLE_SCRIPT.replace("We made it.", replacement),
            encoding="utf-8",
            newline="\n",
        )
        changed = ProjectIndexer().build(self.root)
        unit = next(item for item in changed.text_units if item.source == replacement)
        inventory = ExistingTranslationScanner().scan(changed, "zh_hans")
        self.assertNotIn(unit.id, inventory.translations_by_scene.get(unit.scene_id, {}))
        self.assertIn(unit.id, {item["text_id"] for item in inventory.pending_units})

    def test_incremental_emitter_preserves_existing_scripts_byte_for_byte(self) -> None:
        existing_dir = self.game / "tl" / "zh_hans"
        preserved_path = existing_dir / "custom" / "user_owned.rpy"
        preserved_path.parent.mkdir(parents=True)
        preserved_payload = (
            b"# User-owned translation file.\n"
            b"translate zh_hans python:\n"
            b"    preserved_value = 1\n"
        )
        preserved_path.write_bytes(preserved_payload)
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ZH: {unit.source}" for unit in index.text_units}
        reused = {unit.id for unit in index.text_units}
        output = Path(self.temp.name) / "lossless-incremental-output"

        manifest = RenpyTranslationEmitter().emit(
            index,
            translations,
            "zh_hans",
            output,
            existing_language_dir=existing_dir,
            reused_unit_ids=reused,
        )

        copied = output / "game" / "tl" / "zh_hans" / "custom" / "user_owned.rpy"
        self.assertEqual(copied.read_bytes(), preserved_payload)
        self.assertTrue(any(item.preserved for item in manifest.files))
        self.assertFalse(copied.read_bytes().startswith(b"# Generated by RenWeave."))
        self.assertEqual(GeneratedScriptValidator().validate(manifest), [])

    def test_complete_existing_language_finishes_without_model_calls(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ZH: {unit.source}" for unit in index.text_units}
        RenpyTranslationEmitter().emit(index, translations, "zh_hans", self.root)

        class NoCallGateway:
            model_calls = 0
            prompt_tokens = 0
            completion_tokens = 0
            requests_attempted = 0

            def chat(self, _messages, *, temperature=0.2):
                raise AssertionError("A complete existing language must not call the model")

        state = RenWeavePipeline(Path(self.temp.name) / "existing-complete-workspace").translate(
            self.root,
            "en",
            "zh_hans",
            ModelProfile(name="test", model="fake", base_url="https://example.invalid"),
            gateway=NoCallGateway(),
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertEqual(state.existing_reused_units, len(index.text_units))
        self.assertEqual(state.existing_missing_units, 0)
        self.assertEqual(state.total_model_calls, 0)
        self.assertEqual(state.workflow_mode, "incremental")
        self.assertEqual(state.incremental_units_to_translate, 0)
        self.assertEqual(state.remaining_files, 0)
        report = json.loads(
            (Path(self.temp.name) / "existing-complete-workspace" / "existing-translations.json").read_text(encoding="utf-8")
        )
        self.assertTrue(report["complete"])

    def test_partial_existing_language_requests_only_missing_text(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: f"ZH: {unit.source}" for unit in index.text_units}
        RenpyTranslationEmitter().emit(index, translations, "zh_hans", self.root)
        missing_unit = next(
            unit for unit in index.text_units
            if unit.channel in {TextChannel.DIALOGUE, TextChannel.NARRATION}
        )
        identifier = RenpyTranslationEmitter.dialogue_identifiers(index)[missing_unit.id]
        generated = self.game / "tl" / "zh_hans" / "script.rpy"
        text = generated.read_text(encoding="utf-8")
        block = re.compile(
            rf"(?ms)^translate zh_hans {re.escape(identifier)}:\n.*?(?=^translate zh_hans |\Z)"
        )
        text, removed = block.subn("", text, count=1)
        self.assertEqual(removed, 1)
        generated.write_text(text, encoding="utf-8", newline="\n")

        class MissingOnlyGateway:
            def __init__(self) -> None:
                self.requested: list[set[str]] = []
                self.model_calls = 0
                self.prompt_tokens = 0
                self.completion_tokens = 0
                self.requests_attempted = 0

            def chat(self, messages, *, temperature=0.2):
                request = json.loads(messages[-1]["content"])
                requested = set(request["requested_ids"])
                self.requested.append(requested)
                self.model_calls += 1
                self.requests_attempted += 1
                lines = {line["id"]: line for line in request["scene"]["lines"]}
                rows = [
                    {"id": text_id, "text": f"ZH repaired: {lines[text_id]['source']}"}
                    for text_id in requested
                ]
                return {"choices": [{"message": {"content": json.dumps({"translations": rows})}}]}

        gateway = MissingOnlyGateway()
        state = RenWeavePipeline(Path(self.temp.name) / "existing-partial-workspace").translate(
            self.root,
            "en",
            "zh_hans",
            ModelProfile(name="test", model="fake", base_url="https://example.invalid"),
            gateway=gateway,
            synthesize_knowledge=False,
            refine_translations=False,
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertEqual(gateway.requested, [{missing_unit.id}])
        self.assertEqual(state.existing_missing_units, 1)
        self.assertEqual(state.workflow_mode, "incremental")
        self.assertEqual(state.incremental_units_to_translate, 1)
        self.assertEqual(state.total_model_calls, 1)

    def test_emitter_rejects_conflicting_global_string_translations(self) -> None:
        (self.game / "duplicate.rpy").write_text(
            'label duplicate:\n    menu:\n        "Go outside":\n            return\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        duplicate_units = [unit for unit in index.text_units if unit.source == "Go outside"]
        self.assertEqual(len(duplicate_units), 2)
        translations[duplicate_units[0].id] = "Salir"
        translations[duplicate_units[1].id] = "Ir afuera"
        with self.assertRaises(TranslationConflict):
            RenpyTranslationEmitter().emit(
                index,
                translations,
                "es",
                Path(self.temp.name) / "conflicting-output",
            )

    def test_packager_rejects_script_changed_after_build(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "tampered-package-output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "pt-BR", output)
        script = output / "game" / "tl" / "pt_br" / "script.rpy"
        script.write_text("# tampered after validation\n", encoding="utf-8")
        with self.assertRaises(RpaError):
            TranslationPackager().package(manifest, output / "packages")

    def test_packager_includes_verified_compiled_sidecars(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "compiled-package-output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "de", output)
        compiled_project = Path(self.temp.name) / "compiled-project"
        compiled_root = compiled_project / "game" / "tl" / "de"
        for emitted in manifest.files:
            local = Path(emitted.relative_path).relative_to(Path("game", "tl", "de"))
            target = (compiled_root / local).with_suffix(".rpyc")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic-rpyc")
        packaged = TranslationPackager().package(
            manifest,
            output / "packages",
            compiled_project=compiled_project,
        )
        self.assertTrue(packaged.runtime_ready)
        self.assertEqual(packaged.compiled_members, len(manifest.files))
        with RpaArchive(packaged.archive_path) as archive:
            self.assertTrue(any(name.endswith(".rpyc") for name in archive.names()))

    def test_generated_script_validator_accepts_unicode_language_and_rejects_duplicate_ids(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "unicode-validation-output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "日本語", output)
        self.assertEqual(GeneratedScriptValidator().validate(manifest), [])

        dialogue = output / "game" / "tl" / "日本語" / "script.rpy"
        content = dialogue.read_text(encoding="utf-8")
        header = next(line for line in content.splitlines() if line.startswith("translate "))
        dialogue.write_text(content + f"\n{header}\n\n    \"duplicate\"\n", encoding="utf-8")
        payload = dialogue.read_bytes()
        record = next(item for item in manifest.files if item.relative_path.endswith("script.rpy"))
        object.__setattr__(record, "sha256", hashlib.sha256(payload).hexdigest())
        object.__setattr__(record, "dialogue_blocks", record.dialogue_blocks + 1)
        issues = GeneratedScriptValidator().validate(manifest)
        self.assertIn("DUPLICATE_TRANSLATION_ID", {issue.code for issue in issues})

    def test_renpy_engine_adapter_uses_isolated_project_and_compile_command(self) -> None:
        sdk_root = Path(self.temp.name) / "fake-sdk"
        project = Path(self.temp.name) / "isolated-project"
        (project / "game").mkdir(parents=True)
        sdk_root.mkdir()
        runner = sdk_root / "runner.py"
        runner.write_text(
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "assert '--savedir' in args and 'compile' in args and '--keep-orphan-rpyc' in args\n"
            "project = pathlib.Path(args[args.index('compile') - 1])\n"
            "assert (project / 'game').is_dir()\n",
            encoding="utf-8",
        )
        report = RenpyEngineValidator().validate(
            RenpySdk(sdk_root, (sys.executable, str(runner))),
            project,
        )
        self.assertEqual(report.status, "passed")
        self.assertEqual(report.return_code, 0)

    def test_sdk_locator_accepts_game_bundled_windows_runtime(self) -> None:
        runtime = Path(self.temp.name) / "distributed-runtime"
        runner = runtime / "lib" / "py3-windows-x86_64" / "python.exe"
        runner.parent.mkdir(parents=True)
        runner.write_bytes(b"runner")
        (runtime / "sample.exe").write_bytes(b"launcher")
        (runtime / "sample.py").write_text("# launcher\n", encoding="utf-8")
        located = RenpySdkLocator().resolve(project_root=runtime)
        self.assertIsNotNone(located)
        assert located is not None
        resolved_runtime = runtime.resolve()
        self.assertEqual(
            located.command,
            (
                str(resolved_runtime / "lib" / "py3-windows-x86_64" / "python.exe"),
                str(resolved_runtime / "sample.py"),
            ),
        )

    def test_required_engine_validation_rejects_invalid_sdk_with_report(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "required-engine-output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "ko", output)
        report = RenpyBuildValidator().validate(
            manifest,
            Path(self.temp.name) / "required-engine-validation",
            sdk_path=Path(self.temp.name) / "not-a-renpy-sdk",
            require_engine=True,
        )
        self.assertFalse(report.passed)
        self.assertTrue(report.static_passed)
        self.assertEqual(report.engine.status, "failed")
        self.assertIn("RENPY_SDK_INVALID", {issue.code for issue in report.issues})

    def test_installer_preflights_all_files_before_writing(self) -> None:
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "preflight-output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "it", output)
        destination = self.game / "tl" / "it"
        destination.mkdir(parents=True)
        (destination / "strings.rpy").write_text("# user-owned translation\n", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            TranslationInstaller().install(manifest, self.game)
        self.assertFalse((destination / "script.rpy").exists())

    def test_parser_handles_string_expression_speaker(self) -> None:
        (self.game / "named.rpy").write_text(
            'label named:\n    "Eileen" "Welcome home."\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(unit for unit in index.text_units if unit.source == "Welcome home.")
        self.assertEqual(unit.speaker, "Eileen")
        self.assertEqual(unit.literal_ordinal, 1)
        translations = {item.id: item.source for item in index.text_units}
        translations[unit.id] = "Bienvenida a casa."
        output = Path(self.temp.name) / "named-output"
        RenpyTranslationEmitter().emit(index, translations, "es", output)
        generated = (output / "game" / "tl" / "es" / "named.rpy").read_text(encoding="utf-8")
        self.assertIn('"Eileen" "Bienvenida a casa."', generated)

    def test_parser_does_not_confuse_quoted_menu_condition_with_speaker(self) -> None:
        (self.game / "conditional.rpy").write_text(
            'label conditional:\n    menu:\n        "Choose" if name == "Eileen":\n            return\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(unit for unit in index.text_units if unit.source == "Choose")
        self.assertEqual(str(unit.channel), "menu")
        self.assertEqual(unit.literal_ordinal, 0)
        self.assertEqual(unit.condition, 'name == "Eileen"')

    def test_parser_excludes_code_and_style_string_literals_inside_labels(self) -> None:
        (self.game / "code_literals.rpy").write_text(
            "label code_literals:\n"
            "    $ result = renpy.call_screen('synthetic_screen', value)\n"
            "    style synthetic_style:\n"
            "        outlines ((1, 'abc123', 1, 1),)\n"
            "    narrator 'Actual narration.'\n"
            "    label _('Explicit interface text.')\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        sources = {unit.source for unit in index.text_units}
        self.assertNotIn("synthetic_screen", sources)
        self.assertNotIn("abc123", sources)
        self.assertIn("Actual narration.", sources)
        self.assertIn("Explicit interface text.", sources)

    def test_parser_keeps_renpy_attribute_resets_as_dialogue(self) -> None:
        (self.game / "attribute_reset.rpy").write_text(
            "label attribute_reset:\n"
            "    narrator calm @ -talk 'Synthetic dialogue.'\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(unit for unit in index.text_units if unit.source == "Synthetic dialogue.")
        self.assertEqual(unit.channel, TextChannel.DIALOGUE)
        self.assertEqual(unit.speaker, "narrator")

    def test_extended_menu_syntax_is_emitted_as_string_translation(self) -> None:
        (self.game / "extended_menu.rpy").write_text(
            "label extended_menu:\n"
            "    menu:\n"
            "        'Synthetic choice'(score >= 5, '{icon}'):\n"
            "            return\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(unit for unit in index.text_units if unit.source == "Synthetic choice")
        self.assertEqual(unit.channel, TextChannel.MENU)
        translations = {item.id: item.source for item in index.text_units}
        translations[unit.id] = "Translated choice"
        output = Path(self.temp.name) / "extended-menu-output"
        RenpyTranslationEmitter().emit(index, translations, "es", output)
        generated = (output / "game" / "tl" / "es" / "strings.rpy").read_text(encoding="utf-8")
        self.assertIn('old "Synthetic choice"', generated)
        self.assertIn('new "Translated choice"', generated)
        self.assertNotIn("score >= 5", generated)

    def test_multiline_menu_condition_starts_as_string_translation(self) -> None:
        (self.game / "multiline_menu.rpy").write_text(
            "label multiline_menu:\n"
            "    menu:\n"
            "        'Another choice' if (score >= 5 and\n"
            "                enabled):\n"
            "            return\n",
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        unit = next(unit for unit in index.text_units if unit.source == "Another choice")
        self.assertEqual(unit.channel, TextChannel.MENU)

    def test_emitter_canonicalizes_explicit_say_statement_identifier(self) -> None:
        (self.game / "sayline.rpy").write_text(
            'label sayline:\n    say eve "Explicit syntax."\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "sayline-output"
        RenpyTranslationEmitter().emit(index, translations, "fr", output)
        canonical = 'eve "Explicit syntax."'
        digest = hashlib.md5((canonical + "\r\n").encode("utf-8")).hexdigest()[:8]
        generated = (output / "game" / "tl" / "fr" / "sayline.rpy").read_text(encoding="utf-8")
        self.assertIn(f"translate fr sayline_{digest}:", generated)

    def test_build_validator_accepts_digit_prefixed_translation_hash(self) -> None:
        (self.game / "digit_hash.rpy").write_text(
            'label digit_hash:\n    "Synthetic line."\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: unit.source for unit in index.text_units}
        output = Path(self.temp.name) / "digit-hash-output"
        manifest = RenpyTranslationEmitter().emit(index, translations, "fr", output)
        target = Path(manifest.output_dir) / "digit_hash.rpy"
        text = target.read_text(encoding="utf-8")
        text = re.sub(r"(translate fr )\w+(:)", r"\g<1>1deadbee\2", text, count=1)
        emitted = next(item for item in manifest.files if item.relative_path.endswith("digit_hash.rpy"))
        issues = GeneratedScriptValidator()._validate_text(
            emitted.relative_path,
            text,
            "fr",
            set(),
            emitted.dialogue_blocks,
            emitted.string_entries,
        )
        self.assertEqual(issues, [])

    def test_one_click_pipeline_translates_validates_and_builds(self) -> None:
        class FakeGateway:
            def __init__(self):
                self.model_calls = 0
                self.prompt_tokens = 0
                self.completion_tokens = 0
                self.requests_attempted = 0

            def chat(self, messages, *, temperature=0.2):
                self.model_calls += 1
                self.prompt_tokens += 10
                self.completion_tokens += 5
                self.requests_attempted += 1
                request = json.loads(messages[-1]["content"])
                rows = [
                    {"id": line["id"], "text": f"ES: {line['source']}"}
                    for line in request["scene"]["lines"]
                ]
                return {
                    "choices": [{"message": {"content": json.dumps({"translations": rows})}}]
                }

        workspace = Path(self.temp.name) / "one-click-workspace"
        profile = ModelProfile(name="test", model="fake", base_url="https://example.invalid")
        gateway = FakeGateway()
        state = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "es-ES",
            profile,
            gateway=gateway,
            install=True,
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertEqual(state.renpy_language, "es_es")
        self.assertEqual(len(state.completed_scene_ids), 3)
        self.assertEqual(state.failed_scene_ids, [])
        self.assertTrue(Path(state.output_dir, "script.rpy").is_file())
        self.assertTrue(Path(state.output_dir, "strings.rpy").is_file())
        self.assertTrue(Path(state.package_path).is_file())
        self.assertEqual(len(state.package_sha256), 64)
        self.assertTrue((workspace / "package.json").is_file())
        self.assertTrue(
            json.loads((workspace / "package.json").read_text(encoding="utf-8"))["generated"]
        )
        self.assertEqual(state.build_validation_status, "passed")
        self.assertEqual(state.engine_validation_status, "skipped")
        self.assertTrue((workspace / "build-validation.json").is_file())
        self.assertEqual(state.total_model_calls, 3)
        self.assertEqual(state.total_prompt_tokens, 30)
        self.assertEqual(state.total_completion_tokens, 15)
        self.assertEqual(Path(state.installed_dir), (self.game / "tl" / "es_es").resolve())
        self.assertTrue((self.game / "tl" / "es_es" / "script.rpy").is_file())
        self.assertTrue((workspace / "install.json").is_file())
        usage = json.loads((workspace / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(usage["reporting_status"], "reported")
        self.assertEqual(usage["actual"]["input_tokens"], 30)
        self.assertEqual(usage["actual"]["output_tokens"], 15)
        self.assertEqual(usage["actual"]["total_tokens"], 45)
        self.assertGreater(usage["estimate"]["total_low"], 0)
        self.assertGreaterEqual(usage["estimate"]["total_high"], usage["estimate"]["total_low"])

        rebuilt = RenWeavePipeline(workspace).build(generate_rpa=False)
        rebuilt_state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        package_status = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
        self.assertFalse(rebuilt_state["generate_rpa"])
        self.assertEqual(rebuilt_state["package_path"], "")
        self.assertEqual(rebuilt_state["package_sha256"], "")
        self.assertEqual(rebuilt.archive_path, "")
        self.assertFalse(package_status["generated"])
        self.assertTrue(Path(rebuilt.output_dir, "script.rpy").is_file())
        self.assertTrue(Path(rebuilt.output_dir, "strings.rpy").is_file())

    def test_token_budget_exposes_preflight_and_indexed_ranges(self) -> None:
        preflight = estimate_project_tokens(self.root)
        project_index = ProjectIndexer().build(self.root)
        indexed = estimate_index_tokens(project_index)
        for budget in (preflight, indexed):
            self.assertGreater(budget.source_token_equivalent, 0)
            self.assertGreater(budget.estimated_total_low, 0)
            self.assertGreaterEqual(budget.estimated_total_high, budget.estimated_total_low)
            self.assertGreaterEqual(budget.estimated_input_high, budget.estimated_input_low)
            self.assertGreaterEqual(budget.estimated_output_high, budget.estimated_output_low)
        self.assertEqual(indexed.basis, "indexed_translatable_text")
        self.assertEqual(indexed.confidence, "medium")
        empty = estimate_index_tokens(project_index, set())
        self.assertEqual(empty.estimated_total_low, 0)
        self.assertEqual(empty.estimated_total_high, 0)
        self.assertEqual(empty.source_token_equivalent, 0)
        self.assertEqual(empty.scene_count, 0)
        self.assertEqual(empty.script_count, 0)

    def test_pipeline_repairs_only_invalid_texts(self) -> None:
        class RepairingGateway:
            def __init__(self):
                self.initial_calls = 0
                self.repair_calls = 0

            def chat(self, messages, *, temperature=0.2):
                request = json.loads(messages[-1]["content"])
                if "scene" in request:
                    self.initial_calls += 1
                    lines = request["scene"]["lines"]
                    rows = [
                        {"id": line["id"], "text": f"FR: {line['source']}"}
                        for line in lines
                    ]
                    if self.initial_calls == 1:
                        rows.pop()
                else:
                    self.repair_calls += 1
                    rows = [
                        {"id": line["id"], "text": f"FR: {line['source']}"}
                        for line in request["lines"]
                    ]
                return {
                    "choices": [{"message": {"content": json.dumps({"translations": rows})}}]
                }

        gateway = RepairingGateway()
        state = RenWeavePipeline(Path(self.temp.name) / "repair-workspace").translate(
            self.root,
            "en",
            "fr",
            ModelProfile(name="test", model="fake", base_url="https://example.invalid"),
            gateway=gateway,
            repair_attempts=2,
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertEqual(gateway.initial_calls, 3)
        self.assertEqual(gateway.repair_calls, 1)

    def test_translation_can_pause_after_checkpoint_and_resume_without_repeating_work(self) -> None:
        token = CancellationToken()

        class CancellingGateway:
            def __init__(self, cancel_after_first=False):
                self.calls = 0
                self.cancel_after_first = cancel_after_first

            def chat(self, messages, *, temperature=0.2):
                request = json.loads(messages[-1]["content"])
                self.calls += 1
                rows = [
                    {"id": line["id"], "text": f"FR: {line['source']}"}
                    for line in request["scene"]["lines"]
                ]
                if self.cancel_after_first and self.calls == 1:
                    token.cancel()
                return {"choices": [{"message": {"content": json.dumps({"translations": rows})}}]}

        workspace = Path(self.temp.name) / "resume-workspace"
        profile = ModelProfile(name="resume", model="fake", base_url="https://example.invalid")
        first_gateway = CancellingGateway(cancel_after_first=True)
        paused = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "fr",
            profile,
            gateway=first_gateway,
            cancel_token=token,
            synthesize_knowledge=False,
            refine_translations=False,
        )
        self.assertEqual(paused.stage, PipelineStage.PAUSED)
        self.assertEqual(paused.run_status, "paused")
        self.assertEqual(len(paused.completed_scene_ids), 1)
        self.assertGreater(paused.progress_percent, 32)
        self.assertTrue((workspace / "logs" / "events.jsonl").is_file())
        self.assertIn("run_paused", (workspace / "logs" / "events.jsonl").read_text(encoding="utf-8"))

        second_gateway = CancellingGateway()
        completed = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "fr",
            profile,
            gateway=second_gateway,
            cancel_token=CancellationToken(),
            synthesize_knowledge=False,
            refine_translations=False,
        )
        self.assertEqual(completed.stage, PipelineStage.COMPLETE)
        self.assertEqual(second_gateway.calls, 2)
        self.assertEqual(len(completed.completed_scene_ids), 3)
        self.assertGreaterEqual(completed.resumed_count, 1)
        self.assertEqual(completed.progress_percent, 100)
        self.assertEqual(completed.eta_seconds, 0)

        class NoCallGateway:
            model_calls = 0
            prompt_tokens = 0
            completion_tokens = 0
            requests_attempted = 0

            def chat(self, messages, *, temperature=0.2):
                raise AssertionError("An unchanged completed workspace must not call the model")

        calls_before_rerun = completed.total_model_calls
        refinement_calls_before_rerun = completed.refinement_model_calls
        rerun = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "fr",
            profile,
            gateway=NoCallGateway(),
        )
        self.assertEqual(rerun.stage, PipelineStage.COMPLETE)
        self.assertEqual(rerun.total_model_calls, calls_before_rerun)
        self.assertEqual(rerun.refinement_model_calls, refinement_calls_before_rerun)
        self.assertIn(
            "completed_model_outputs_reused",
            (workspace / "logs" / "events.jsonl").read_text(encoding="utf-8"),
        )

        damaged_scene = completed.completed_scene_ids[0]
        (workspace / "translations" / f"{damaged_scene}.json").write_text(
            '{"scene_id": "damaged", "translations": {}}\n',
            encoding="utf-8",
        )
        repair_gateway = CancellingGateway()
        repaired = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "fr",
            profile,
            gateway=repair_gateway,
            cancel_token=CancellationToken(),
            synthesize_knowledge=False,
            refine_translations=False,
        )
        self.assertEqual(repaired.stage, PipelineStage.COMPLETE)
        self.assertEqual(repair_gateway.calls, 1)
        self.assertIn(
            "checkpoint_rejected",
            (workspace / "logs" / "events.jsonl").read_text(encoding="utf-8"),
        )

    def test_workspace_lease_prevents_concurrent_writers_and_releases_cleanly(self) -> None:
        workspace = Path(self.temp.name) / "locked-workspace"
        with WorkspaceLease(workspace):
            with self.assertRaisesRegex(RuntimeError, "already writing"):
                with WorkspaceLease(workspace):
                    self.fail("a second workspace writer must not acquire the lock")
        with WorkspaceLease(workspace):
            self.assertTrue((workspace / ".renweave-run.lock").is_file())

    def test_cli_interrupt_marks_workspace_paused_and_returns_standard_exit_code(self) -> None:
        from renweave.cli import main as cli_main

        provider = Path(self.temp.name) / "interrupt-provider.json"
        ModelProfile(
            name="interrupt",
            model="fake",
            base_url="https://example.invalid",
        ).save(provider)
        fake_pipeline = mock.Mock()
        fake_pipeline.translate.side_effect = KeyboardInterrupt()
        with mock.patch("renweave.cli.RenWeavePipeline", return_value=fake_pipeline):
            code = cli_main([
                "run",
                str(self.root),
                "--workspace",
                str(Path(self.temp.name) / "interrupt-workspace"),
                "--provider",
                str(provider),
                "--target-language",
                "fr",
            ])
        self.assertEqual(code, 130)
        fake_pipeline.pause.assert_called_once_with("Interrupted from the command line")

    def test_cancellation_is_observed_between_narrative_model_batches(self) -> None:
        with (self.game / "many-scenes.rpy").open("w", encoding="utf-8", newline="\n") as writer:
            for index in range(24):
                writer.write(f'\nlabel extra_{index}:\n    "Story line {index}."\n')
        token = CancellationToken()

        class NarrativeCancellingGateway:
            def __init__(self):
                self.calls = 0
                self.profile = ModelProfile(
                    name="narrative-cancel",
                    model="fake",
                    base_url="https://example.invalid",
                )

            def chat(self, messages, *, temperature=0.2):
                self.calls += 1
                request = json.loads(messages[-1]["content"])
                scene_ids = [str(item["id"]) for item in request.get("scenes", [])]
                token.cancel()
                payload = {
                    "summary": "Partial cached narrative context.",
                    "themes": [],
                    "world_facts": [],
                    "characters": [],
                    "terms": [],
                    "scene_ids": scene_ids,
                }
                return {"choices": [{"message": {"content": json.dumps(payload)}}]}

        gateway = NarrativeCancellingGateway()
        state = RenWeavePipeline(Path(self.temp.name) / "narrative-cancel-workspace").translate(
            self.root,
            "en",
            "fr",
            gateway.profile,
            gateway=gateway,
            cancel_token=token,
        )
        self.assertEqual(state.stage, PipelineStage.PAUSED)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(state.completed_scene_ids, [])
        self.assertGreaterEqual(state.phase_completed, 1)

    def test_scene_exception_is_reported_and_never_marked_complete(self) -> None:
        class FailingGateway:
            def chat(self, messages, *, temperature=0.2):
                raise RuntimeError("temporary model outage")

        workspace = Path(self.temp.name) / "failure-workspace"
        state = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "fr",
            ModelProfile(name="failure", model="fake", base_url="https://example.invalid"),
            gateway=FailingGateway(),
        )
        self.assertEqual(state.stage, PipelineStage.VALIDATED)
        self.assertEqual(len(state.failed_scene_ids), 3)
        self.assertIn("3 个场景翻译失败", state.error)
        report = json.loads(
            (workspace / "reports" / f"{state.failed_scene_ids[0]}.json").read_text(encoding="utf-8")
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"][0]["code"], "SCENE_EXCEPTION")

    def test_model_gateway_retries_transient_http_and_limits_response_size(self) -> None:
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, limit=-1):
                return self.payload if limit < 0 else self.payload[:limit]

        from renweave.provider import OpenAICompatibleGateway

        response = json.dumps({
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }).encode("utf-8")
        throttled = urllib.error.HTTPError(
            "https://example.invalid", 429, "rate limited", {}, io.BytesIO(b"retry")
        )
        profile = ModelProfile(
            name="retry",
            model="retry-model",
            base_url="https://example.invalid/v1",
            max_retries=1,
            retry_base_seconds=0,
            max_response_bytes=1024,
        )
        gateway = OpenAICompatibleGateway(profile)
        with mock.patch(
            "renweave.provider.urllib.request.urlopen",
            side_effect=[throttled, FakeResponse(response)],
        ) as urlopen:
            gateway.chat([{"role": "user", "content": "test"}])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(gateway.requests_attempted, 2)
        self.assertEqual(gateway.model_calls, 1)
        self.assertEqual(gateway.prompt_tokens, 12)
        self.assertEqual(gateway.completion_tokens, 3)

        oversized = OpenAICompatibleGateway(profile)
        with mock.patch(
            "renweave.provider.urllib.request.urlopen",
            return_value=FakeResponse(b"x" * 1025),
        ):
            with self.assertRaisesRegex(RuntimeError, "max_response_bytes"):
                oversized.chat([{"role": "user", "content": "test"}])

    def test_provider_catalog_loads_models_and_verifies_selected_model(self) -> None:
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, limit=-1):
                return self.payload if limit < 0 else self.payload[:limit]

        model_list = json.dumps({
            "data": [{"id": "translator-pro"}, {"id": "translator-mini"}, {"id": "translator-pro"}],
        }).encode("utf-8")
        verification = json.dumps({
            "choices": [{"message": {"content": '{"ok":true}'}}],
        }).encode("utf-8")
        profile = ModelProfile(
            name="Local provider",
            model="",
            base_url="https://models.example/v1/chat/completions",
            api_key="memory-secret",
            max_retries=0,
        )
        catalog = OpenAICompatibleCatalog(profile)
        with mock.patch(
            "renweave.provider.urllib.request.urlopen",
            side_effect=[FakeResponse(model_list), FakeResponse(verification)],
        ) as urlopen:
            result = catalog.list_models()
            checked = catalog.verify_model("translator-pro")

        self.assertEqual(result.models, ("translator-mini", "translator-pro"))
        self.assertEqual(result.endpoint, "https://models.example/v1/models")
        self.assertEqual(checked.model, "translator-pro")
        list_request = urlopen.call_args_list[0].args[0]
        verify_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(list_request.get_method(), "GET")
        self.assertEqual(list_request.get_header("Authorization"), "Bearer memory-secret")
        self.assertEqual(verify_request.get_method(), "POST")
        self.assertEqual(verify_request.full_url, "https://models.example/v1/chat/completions")
        verification_payload = json.loads(verify_request.data)
        self.assertEqual(verification_payload["model"], "translator-pro")
        verification_text = " ".join(
            message["content"] for message in verification_payload["messages"]
        ).casefold()
        self.assertIn("json", verification_text)

    def test_provider_profile_save_excludes_api_key_by_default(self) -> None:
        target = Path(self.temp.name) / "profiles" / "provider.json"
        profile = ModelProfile(
            name="Secure provider",
            model="translation-model",
            base_url="https://models.example/v1/",
            api_key="never-write-this",
        )
        profile.save(target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "openai_compatible")
        self.assertEqual(payload["base_url"], "https://models.example/v1")
        self.assertNotIn("api_key", payload)
        self.assertNotIn("never-write-this", target.read_text(encoding="utf-8"))

    def test_provider_presets_cover_required_official_and_aggregator_apis(self) -> None:
        required = {
            "openai", "google", "anthropic", "deepseek", "minimax", "alibaba",
            "zhipu", "moonshot", "siliconflow", "openrouter", "custom", "custom2",
        }
        self.assertTrue(required.issubset(PROVIDER_PRESETS_BY_ID))
        self.assertEqual(get_provider_preset("openai").base_url, "https://api.openai.com/v1")
        self.assertEqual(
            get_provider_preset("google").base_url,
            "https://generativelanguage.googleapis.com/v1beta/openai",
        )
        self.assertEqual(get_provider_preset("anthropic").base_url, "https://api.anthropic.com/v1")
        self.assertEqual(get_provider_preset("deepseek").base_url, "https://api.deepseek.com")
        self.assertIn("https://api.minimaxi.com/v1", get_provider_preset("minimax").base_urls)
        self.assertEqual(get_provider_preset("alibaba").api_key_env, "DASHSCOPE_API_KEY")
        self.assertEqual(get_provider_preset("zhipu").base_url, "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(get_provider_preset("moonshot").base_url, "https://api.moonshot.cn/v1")
        self.assertEqual(get_provider_preset("siliconflow").base_url, "https://api.siliconflow.cn/v1")
        self.assertTrue(all(not hasattr(preset, "default_models") for preset in PROVIDER_PRESETS_BY_ID.values()))
        self.assertEqual(get_provider_preset("openrouter").category, "aggregator")
        self.assertEqual(get_provider_preset("custom").category, "custom")
        self.assertEqual(get_provider_preset("custom2").category, "custom")
        self.assertEqual(get_provider_preset("custom2").base_url, "http://127.0.0.1:8001/v1")
        self.assertEqual(get_provider_preset("custom2").api_key_env, "RENWEAVE_API_KEY_2")
        with self.assertRaisesRegex(ValueError, "Unknown provider preset"):
            get_provider_preset("missing")

    def test_reasoning_level_uses_provider_specific_payloads(self) -> None:
        cases = {
            "deepseek": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
            "zhipu": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
            "alibaba": {"enable_thinking": True},
            "siliconflow": {"enable_thinking": True, "thinking_budget": 16384},
        }
        for provider_id, expected in cases.items():
            with self.subTest(provider_id=provider_id):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps({
                    "choices": [{"message": {"content": '{"ok":true}'}}]
                }).encode("utf-8")
                profile = ModelProfile(
                    name=provider_id,
                    model="live-model",
                    base_url=get_provider_preset(provider_id).base_url,
                    provider_id=provider_id,
                    supports_json=False,
                    reasoning_level="maximum",
                    max_retries=0,
                )
                with mock.patch("renweave.provider.urllib.request.urlopen", return_value=response) as urlopen:
                    OpenAICompatibleGateway(profile).chat([{"role": "user", "content": "test"}])
                payload = json.loads(urlopen.call_args.args[0].data)
                for key, value in expected.items():
                    self.assertEqual(payload[key], value)

    def test_secure_credential_store_uses_hashed_provider_identity(self) -> None:
        class Backend:
            def __init__(self) -> None:
                self.values = {}

            def get_password(self, service, username):
                return self.values.get((service, username))

            def set_password(self, service, username, password):
                self.values[(service, username)] = password

            def delete_password(self, service, username):
                self.values.pop((service, username), None)

        backend = Backend()
        store = SecureCredentialStore(backend=backend)
        store.set("deepseek", "https://api.deepseek.com", "one-time-secret")
        self.assertEqual(store.get("deepseek", "https://api.deepseek.com"), "one-time-secret")
        account = credential_account("deepseek", "https://api.deepseek.com")
        self.assertNotIn("api.deepseek.com", account)
        store.delete("deepseek", "https://api.deepseek.com")
        self.assertEqual(store.get("deepseek", "https://api.deepseek.com"), "")

    def test_update_check_parses_latest_github_release(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "tag_name": "v9.1.0",
            "html_url": "https://github.com/Mehael-Yeh/RenWeave/releases/tag/v9.1.0",
        }).encode("utf-8")
        with mock.patch("renweave.update_check.urllib.request.urlopen", return_value=response):
            result = check_for_updates("1.7.0")
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "v9.1.0")

    def test_update_check_falls_back_to_tags_when_no_release_exists(self) -> None:
        missing_release = urllib.error.HTTPError(
            "https://api.github.com/repos/Mehael-Yeh/RenWeave/releases/latest",
            404,
            "not found",
            {},
            io.BytesIO(b"{}"),
        )
        tags_response = mock.MagicMock()
        tags_response.__enter__.return_value.read.return_value = json.dumps([
            {"name": "v1.6.0"}
        ]).encode("utf-8")
        with mock.patch(
            "renweave.update_check.urllib.request.urlopen",
            side_effect=[missing_release, tags_response],
        ):
            result = check_for_updates("1.7.0")
        self.assertFalse(result.update_available)
        self.assertEqual(result.latest_version, "v1.6.0")

    def test_provider_profile_round_trips_selected_preset_without_secret(self) -> None:
        target = Path(self.temp.name) / "provider-preset.json"
        profile = ModelProfile(
            name="Google Gemini",
            model="gemini-test",
            base_url=get_provider_preset("google").base_url,
            provider_id="google",
            api_key="memory-only",
            api_key_env="GEMINI_API_KEY",
        )
        profile.save(target)
        loaded = ModelProfile.load(target)
        self.assertEqual(loaded.provider_id, "google")
        self.assertEqual(loaded.api_key_env, "GEMINI_API_KEY")
        self.assertEqual(loaded.resolved_api_key(), "")
        self.assertNotIn("memory-only", target.read_text(encoding="utf-8"))

    def test_provider_catalog_reports_authentication_failure_with_hint(self) -> None:
        denied = urllib.error.HTTPError(
            "https://models.example/v1/models",
            401,
            "unauthorized",
            {},
            io.BytesIO(b"invalid token"),
        )
        profile = ModelProfile(
            name="Protected provider",
            model="",
            base_url="https://models.example/v1",
            max_retries=0,
        )
        with mock.patch("renweave.provider.urllib.request.urlopen", side_effect=denied):
            with self.assertRaisesRegex(RuntimeError, "Check the API key and endpoint"):
                OpenAICompatibleCatalog(profile).list_models()

    def test_run_automatically_synthesizes_narrative_knowledge(self) -> None:
        with (self.game / "script.rpy").open("a", encoding="utf-8", newline="\n") as writer:
            writer.write('\nlabel epilogue:\n    eve "We understand the whole story now."\n')

        class CombinedGateway:
            def __init__(self):
                self.profile = ModelProfile(
                    name="combined",
                    model="fake-combined",
                    base_url="https://example.invalid",
                )

            def chat(self, messages, *, temperature=0.2):
                request = json.loads(messages[-1]["content"])
                if "scene" in request:
                    payload = {"translations": [
                        {"id": line["id"], "text": f"DE: {line['source']}"}
                        for line in request["scene"]["lines"]
                    ]}
                elif "scenes" in request:
                    scene_ids = [item["id"] for item in request["scenes"]]
                    payload = {
                        "summary": "A connected story.",
                        "themes": ["trust"],
                        "world_facts": [{"text": "Events are connected.", "scene_ids": scene_ids}],
                        "characters": [],
                        "terms": [],
                    }
                else:
                    payload = {"world_summary": "A connected story.", "style_guidance": [], "world_facts": []}
                return {"choices": [{"message": {"content": json.dumps(payload)}}]}

        workspace = Path(self.temp.name) / "narrative-run-workspace"
        state = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "de",
            ModelProfile(name="combined", model="fake-combined", base_url="https://example.invalid"),
            gateway=CombinedGateway(),
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertGreaterEqual(state.knowledge_model_calls, 1)
        self.assertTrue((workspace / "narrative-knowledge.json").is_file())

    def test_run_applies_global_refinement_before_build(self) -> None:
        project = Path(self.temp.name) / "RefineGame"
        game = project / "game"
        game.mkdir(parents=True)
        game.joinpath("script.rpy").write_text(
            'label start:\n'
            '    eve "Moon Key"\n'
            '    eve "Second."\n'
            '    eve "Third."\n'
            '    eve "Moon Key"\n'
            '    eve "Fifth."\n'
            '    eve "Sixth."\n',
            encoding="utf-8",
        )

        class PipelineRefinementGateway:
            def __init__(self):
                self.profile = ModelProfile(
                    name="pipeline-refine",
                    model="fake-pipeline-refine",
                    base_url="https://example.invalid",
                )

            def chat(self, messages, *, temperature=0.2):
                request = json.loads(messages[-1]["content"])
                if "candidates" in request:
                    payload = {
                        "corrections": [
                            {"id": item["text_id"], "text": "Mondschlüssel", "reason": "Consistency"}
                            for item in request["candidates"]
                            if item["source"] == "Moon Key"
                        ],
                        "observations": [],
                    }
                else:
                    seen = 0
                    rows = []
                    for line in request["scene"]["lines"]:
                        if line["source"] == "Moon Key":
                            seen += 1
                            translated = "Mondschlüssel" if seen == 1 else "Mond-Schlüssel"
                        else:
                            translated = f"DE: {line['source']}"
                        rows.append({"id": line["id"], "text": translated})
                    payload = {"translations": rows}
                return {"choices": [{"message": {"content": json.dumps(payload)}}]}

        workspace = Path(self.temp.name) / "pipeline-refine-workspace"
        state = RenWeavePipeline(workspace).translate(
            project,
            "en",
            "de",
            ModelProfile(
                name="pipeline-refine",
                model="fake-pipeline-refine",
                base_url="https://example.invalid",
            ),
            gateway=PipelineRefinementGateway(),
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertEqual(state.refinement_changes, 1)
        self.assertTrue((workspace / "refinement.json").is_file())
        generated = Path(state.output_dir, "script.rpy").read_text(encoding="utf-8")
        self.assertEqual(generated.count('eve "Mondschlüssel"'), 2)

    def test_unrpyc_adapter_stages_and_decompiles_without_touching_source(self) -> None:
        compiled_root = Path(self.temp.name) / "compiled"
        compiled_root.mkdir()
        compiled = compiled_root / "route.rpyc"
        compiled.write_bytes(b"compiled-placeholder")
        tool = self._write_fake_unrpyc()
        output = Path(self.temp.name) / "decompiled"
        manifest = UnrpycDecompiler(tool).decompile([compiled_root], output)
        self.assertEqual(len(manifest.files), 1)
        recovered = Path(manifest.files[0].output_path)
        self.assertTrue(recovered.is_file())
        self.assertIn("label recovered", recovered.read_text(encoding="utf-8"))
        self.assertFalse((compiled_root / "route.rpy").exists())

    def test_narrative_knowledge_is_hierarchical_evidenced_and_cached(self) -> None:
        (self.game / "route.rpy").write_text(
            'label route_start:\n    eve "The Moon Key opens North Tower."\n',
            encoding="utf-8",
        )
        index = ProjectIndexer().build(self.root)
        deterministic = DeterministicKnowledgeBuilder().build(index)

        class KnowledgeGateway:
            def __init__(self):
                self.profile = ModelProfile(
                    name="knowledge-test",
                    model="fake-knowledge",
                    base_url="https://example.invalid",
                )
                self.calls = 0

            def chat(self, messages, *, temperature=0.2):
                self.calls += 1
                request = json.loads(messages[-1]["content"])
                if "storylines" in request:
                    scene_ids = [
                        scene_id
                        for item in request["storylines"]
                        for scene_id in item.get("scene_ids", [])
                    ]
                    payload = {
                        "world_summary": "A grounded mystery involving the North Tower.",
                        "style_guidance": ["Dialogue is concise."],
                        "world_facts": [{"text": "The North Tower is locked.", "scene_ids": scene_ids}],
                    }
                else:
                    scene_ids = [item["id"] for item in request["scenes"]]
                    payload = {
                        "summary": f"Storyline {request['storyline']} summary.",
                        "themes": ["mystery"],
                        "world_facts": [
                            {"text": "The Moon Key opens North Tower.", "scene_ids": scene_ids},
                            {"text": "Unsupported invention.", "scene_ids": ["unknown_scene"]},
                        ],
                        "characters": [{
                            "name": "eve",
                            "role": "Investigator",
                            "traits": ["observant"],
                            "voice": ["concise"],
                            "relationships": {"anon": "Offers help"},
                            "scene_ids": scene_ids,
                        }],
                        "terms": [{
                            "source": "Moon Key",
                            "meaning": "A key for North Tower",
                            "guidance": "Translate consistently",
                            "scene_ids": scene_ids,
                        }],
                    }
                return {
                    "choices": [{"message": {"content": json.dumps(payload)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40},
                }

        gateway = KnowledgeGateway()
        cache = Path(self.temp.name) / "knowledge-cache"
        first = NarrativeKnowledgeSynthesizer(gateway, cache).synthesize(
            index,
            deterministic,
            project_fingerprint="fingerprint",
            source_language="en",
        )
        calls_after_first = gateway.calls
        second = NarrativeKnowledgeSynthesizer(gateway, cache).synthesize(
            index,
            deterministic,
            project_fingerprint="fingerprint",
            source_language="en",
        )
        self.assertGreaterEqual(calls_after_first, 3)
        self.assertEqual(gateway.calls, calls_after_first)
        self.assertGreaterEqual(second.usage.cache_hits, 3)
        self.assertEqual(first.world_summary, "A grounded mystery involving the North Tower.")
        self.assertNotIn("Unsupported invention", {fact.text for fact in first.world_facts})
        self.assertEqual(next(item for item in first.characters if item.name == "eve").role, "Investigator")
        route_scene = next(scene for scene in index.scenes if scene.label == "route_start")
        context = ContextPlanner().build(index, deterministic, route_scene.id, first)
        self.assertIn("North Tower", context.world_context)
        self.assertEqual(context.character_profiles[0]["role"], "Investigator")
        self.assertEqual(context.term_hints[0]["source"], "Moon Key")

    def test_global_refinement_reviews_only_risk_candidates_and_caches(self) -> None:
        risk_script = '''label risk:
    eve "Moon Key"
    eve "A second line."
    eve "A third line."
    eve "Moon Key"
    eve "A fifth line."
    eve "A sixth line."
'''
        (self.game / "risk.rpy").write_text(risk_script, encoding="utf-8")
        index = ProjectIndexer().build(self.root)
        translations = {unit.id: f"DE: {unit.source}" for unit in index.text_units}
        repeated = [unit for unit in index.text_units if unit.source == "Moon Key"]
        translations[repeated[0].id] = "Mondschlüssel"
        translations[repeated[1].id] = "Mond-Schlüssel"

        class RefinementGateway:
            def __init__(self):
                self.profile = ModelProfile(
                    name="refine",
                    model="fake-refine",
                    base_url="https://example.invalid",
                )
                self.calls = 0

            def chat(self, messages, *, temperature=0.2):
                self.calls += 1
                request = json.loads(messages[-1]["content"])
                corrections = [
                    {"id": item["text_id"], "text": "Mondschlüssel", "reason": "Consistent term"}
                    for item in request["candidates"]
                    if item["source"] == "Moon Key"
                ]
                return {
                    "choices": [{"message": {"content": json.dumps({
                        "corrections": corrections,
                        "observations": ["Term normalized."],
                    })}}],
                    "usage": {"prompt_tokens": 80, "completion_tokens": 20},
                }

        gateway = RefinementGateway()
        cache = Path(self.temp.name) / "refinement-cache"
        refined, report = GlobalTranslationRefiner(gateway, cache).refine(
            index,
            translations,
            None,
            source_language="en",
            target_language="de",
        )
        calls = gateway.calls
        _refined_again, cached_report = GlobalTranslationRefiner(gateway, cache).refine(
            index,
            translations,
            None,
            source_language="en",
            target_language="de",
        )
        self.assertEqual(gateway.calls, calls)
        self.assertGreaterEqual(cached_report.usage.cache_hits, 1)
        self.assertEqual({refined[unit.id] for unit in repeated}, {"Mondschlüssel"})
        self.assertEqual(len(report.changes), 1)
        self.assertGreaterEqual(report.candidates_reviewed, 2)

    def test_pipeline_automatically_indexes_decompiled_rpyc(self) -> None:
        project = Path(self.temp.name) / "CompiledGame"
        game = project / "game"
        game.mkdir(parents=True)
        (game / "route.rpyc").write_bytes(b"compiled-placeholder")
        workspace = Path(self.temp.name) / "compiled-workspace"
        index, _knowledge = RenWeavePipeline(workspace).analyze(
            project,
            unrpyc_path=self._write_fake_unrpyc(),
            allow_tool_download=False,
        )
        self.assertEqual([scene.label for scene in index.scenes], ["recovered"])
        manifest = json.loads((workspace / "decompilation.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["files"]), 1)
        self.assertEqual(manifest["tool_version"], "2.0.4")

    def test_unrpyc_is_bundled_and_installs_without_network(self) -> None:
        manager = UnrpycToolManager(Path(self.temp.name) / "offline-tools")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used")) as urlopen:
            entrypoint = manager.resolve(allow_download=False)
        urlopen.assert_not_called()
        self.assertTrue(entrypoint.is_file())
        installed_files = {
            path.relative_to(manager.install_dir).as_posix()
            for path in manager.install_dir.rglob("*")
            if path.is_file() and path.name != "renweave-source.json"
        }
        self.assertEqual(installed_files, set(UNRPYC_BUNDLED_FILES))
        metadata = json.loads(
            (manager.install_dir / "renweave-source.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["distribution"], "bundled")
        self.assertEqual(metadata["tree_sha256"], UNRPYC_BUNDLED_TREE_SHA256)
        self.assertEqual(UnrpycDecompiler(entrypoint).version(), "Unrpyc v2.0.3")
        self.assertEqual(manager.resolve(), entrypoint)

    def test_frozen_executable_routes_unrpyc_through_internal_entry(self) -> None:
        entrypoint = UnrpycToolManager(Path(self.temp.name) / "frozen-tools").resolve()
        executable = Path(sys.executable).resolve()
        decompiler = UnrpycDecompiler(entrypoint, python_executable=executable)
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(decompiler.version(), "Unrpyc v2.0.3")
            self.assertEqual(decompiler._base_command(), [
                str(executable),
                FROZEN_UNRPYC_SWITCH,
                str(entrypoint.parent),
                str(entrypoint),
            ])

    def test_sequential_pool_runs_frozen_unrpyc_work_without_spawning(self) -> None:
        with SequentialPool(1) as pool:
            self.assertEqual(list(pool.imap(lambda value: value * 2, [1, 2, 3], 1)), [2, 4, 6])

    def test_frozen_decompiler_runs_batch_in_process_without_relaunching_gui(self) -> None:
        compiled_root = Path(self.temp.name) / "frozen-compiled"
        compiled_root.mkdir()
        (compiled_root / "route.rpyc").write_bytes(b"compiled-placeholder")
        tool = self._write_fake_unrpyc()
        decompiler = UnrpycDecompiler(tool, python_executable=sys.executable)
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch(
            "renweave.decompiler.subprocess.run"
        ) as subprocess_run:
            manifest = decompiler.decompile(
                [compiled_root],
                Path(self.temp.name) / "frozen-output",
            )
        subprocess_run.assert_not_called()
        self.assertEqual(len(manifest.files), 1)
        self.assertTrue(Path(manifest.files[0].output_path).is_file())

    def test_failed_decompilation_keeps_diagnostic_manifest(self) -> None:
        project = Path(self.temp.name) / "BrokenCompiledGame"
        game = project / "game"
        game.mkdir(parents=True)
        (game / "broken.rpyc").write_bytes(b"broken")
        tool_dir = Path(self.temp.name) / "nonproducing-unrpyc"
        tool_dir.mkdir()
        tool = tool_dir / "unrpyc.py"
        tool.write_text("# deliberately produces no output\n", encoding="utf-8")
        workspace = Path(self.temp.name) / "broken-workspace"
        with self.assertRaises(DecompilationError):
            RenWeavePipeline(workspace).decompile(
                project,
                unrpyc_path=tool,
                allow_tool_download=False,
            )
        diagnostics = json.loads((workspace / "decompilation.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostics["files"], [])
        self.assertIn("nonproducing-unrpyc", diagnostics["tool"])

    def test_cached_unrpyc_install_detects_tampering(self) -> None:
        manager = UnrpycToolManager(Path(self.temp.name) / "verified-tools")
        self.assertTrue(manager.resolve().is_file())
        dependency = manager.install_dir / "decompiler" / "util.py"
        dependency.write_text("# tampered\n", encoding="utf-8")
        with self.assertRaises(DecompilationError):
            manager.resolve()

    def _write_fake_unrpyc(self) -> Path:
        tool_dir = Path(self.temp.name) / "fake-unrpyc"
        tool_dir.mkdir(exist_ok=True)
        entrypoint = tool_dir / "unrpyc.py"
        entrypoint.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "for raw in sys.argv[1:]:\n"
            "    path = Path(raw)\n"
            "    if path.suffix.lower() == '.rpyc':\n"
            "        path.with_suffix('.rpy').write_text(\n"
            "            'label recovered:\\n    eve \\\"Recovered dialogue.\\\"\\n', encoding='utf-8')\n"
            "    elif path.suffix.lower() == '.rpymc':\n"
            "        path.with_suffix('.rpym').write_text(\n"
            "            'label recovered_module:\\n    \\\"Recovered module.\\\"\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        return entrypoint


if __name__ == "__main__":
    unittest.main()
