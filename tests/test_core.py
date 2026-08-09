from __future__ import annotations

import json
import hashlib
import io
import pickle
import sys
import tempfile
import unittest
import zlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import urllib.error

from renweave.context import ContextPlanner
from renweave.build_validation import (
    GeneratedScriptValidator,
    RenpyBuildValidator,
    RenpyEngineValidator,
    RenpySdk,
)
from renweave.decompiler import (
    UNRPYC_ARCHIVE_SHA256,
    UNRPYC_COMMIT,
    DecompilationError,
    UnrpycDecompiler,
    UnrpycToolManager,
)
from renweave.emitter import RenpyTranslationEmitter, TranslationConflict
from renweave.gui import (
    STAGE_LABELS,
    RenWeaveDesktopApp,
    TranslationRequest,
    execute_translation,
)
from renweave.indexer import ProjectIndexer
from renweave.installer import TranslationInstaller
from renweave.knowledge import DeterministicKnowledgeBuilder
from renweave.narrative import NarrativeKnowledgeSynthesizer
from renweave.packaging import TranslationPackager
from renweave.pipeline import PipelineStage, RenWeavePipeline
from renweave.provider import ModelProfile, OpenAICompatibleCatalog
from renweave.refinement import GlobalTranslationRefiner
from renweave.rpa import RpaArchive, RpaError, RpaWriter, UnsafeArchivePath, script_member
from renweave.validation import TranslationValidator


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

    def test_public_api_exposes_pipeline_and_model_profile(self) -> None:
        import renweave

        self.assertEqual(renweave.__version__, "1.0.0")
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
            install=True,
        )
        result = execute_translation(request, pipeline_factory=FakePipeline)
        self.assertEqual(result.stage, PipelineStage.COMPLETE)
        self.assertEqual(captured["args"][2], "Português do Brasil")
        self.assertEqual(captured["args"][3].api_key, "memory-only-secret")
        self.assertTrue(captured["kwargs"]["install"])
        self.assertNotIn("memory-only-secret", provider.read_text(encoding="utf-8"))
        self.assertFalse(workspace.exists())

    def test_desktop_progress_labels_cover_every_pipeline_stage(self) -> None:
        self.assertEqual({str(stage) for stage in PipelineStage} - set(STAGE_LABELS), set())

    def test_desktop_window_constructs_with_one_click_controls(self) -> None:
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
            app = RenWeaveDesktopApp(
                root,
                initial_project=str(self.root),
                initial_workspace=str(Path(self.temp.name) / "visual-workspace"),
            )
            root.update_idletasks()
            self.assertEqual(app.start_button.cget("text"), "开始一键翻译")
            self.assertGreaterEqual(root.minsize()[0], 760)
            self.assertEqual(app.source_language.get(), "auto")
            self.assertEqual(app.status.get(), "就绪：选择项目与模型后即可开始")
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
        self.assertEqual(state.build_validation_status, "passed")
        self.assertEqual(state.engine_validation_status, "skipped")
        self.assertTrue((workspace / "build-validation.json").is_file())
        self.assertEqual(state.total_model_calls, 3)
        self.assertEqual(state.total_prompt_tokens, 30)
        self.assertEqual(state.total_completion_tokens, 15)
        self.assertEqual(Path(state.installed_dir), (self.game / "tl" / "es_es").resolve())
        self.assertTrue((self.game / "tl" / "es_es" / "script.rpy").is_file())
        self.assertTrue((workspace / "install.json").is_file())

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
        self.assertEqual(json.loads(verify_request.data)["model"], "translator-pro")

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
        self.assertEqual(manifest["tool_version"], "2.0.2")

    def test_unrpyc_download_can_be_disabled(self) -> None:
        manager = UnrpycToolManager(Path(self.temp.name) / "empty-tools")
        with self.assertRaises(DecompilationError):
            manager.resolve(allow_download=False)

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

    def test_unrpyc_tool_archive_rejects_path_traversal(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("unrpyc-root/../escape.py", "bad")
        with self.assertRaises(DecompilationError):
            UnrpycToolManager._extract_verified(
                payload.getvalue(),
                Path(self.temp.name) / "unsafe-tool",
            )

    def test_cached_unrpyc_install_detects_tampering(self) -> None:
        manager = UnrpycToolManager(Path(self.temp.name) / "verified-tools")
        manager.install_dir.mkdir(parents=True)
        (manager.install_dir / "unrpyc.py").write_text("print('ok')\n", encoding="utf-8")
        dependency = manager.install_dir / "decompiler.py"
        dependency.write_text("VALUE = 1\n", encoding="utf-8")
        (manager.install_dir / "renweave-source.json").write_text(
            json.dumps({
                "commit": UNRPYC_COMMIT,
                "archive_sha256": UNRPYC_ARCHIVE_SHA256,
                "tree_sha256": manager._tree_digest(manager.install_dir),
            }),
            encoding="utf-8",
        )
        self.assertTrue(manager.resolve(allow_download=False).is_file())
        dependency.write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaises(DecompilationError):
            manager.resolve(allow_download=False)

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
