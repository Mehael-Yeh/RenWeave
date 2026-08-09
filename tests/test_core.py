from __future__ import annotations

import json
import hashlib
import pickle
import tempfile
import unittest
import zlib
from pathlib import Path

from renweave.context import ContextPlanner
from renweave.emitter import RenpyTranslationEmitter, TranslationConflict
from renweave.indexer import ProjectIndexer
from renweave.installer import TranslationInstaller
from renweave.knowledge import DeterministicKnowledgeBuilder
from renweave.pipeline import PipelineStage, RenWeavePipeline
from renweave.provider import ModelProfile
from renweave.rpa import RpaArchive, UnsafeArchivePath, script_member
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
            def chat(self, messages, *, temperature=0.2):
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
        state = RenWeavePipeline(workspace).translate(
            self.root,
            "en",
            "es-ES",
            profile,
            gateway=FakeGateway(),
            install=True,
        )
        self.assertEqual(state.stage, PipelineStage.COMPLETE)
        self.assertEqual(state.renpy_language, "es_es")
        self.assertEqual(len(state.completed_scene_ids), 3)
        self.assertEqual(state.failed_scene_ids, [])
        self.assertTrue(Path(state.output_dir, "script.rpy").is_file())
        self.assertTrue(Path(state.output_dir, "strings.rpy").is_file())
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


if __name__ == "__main__":
    unittest.main()
