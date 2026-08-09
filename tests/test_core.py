from __future__ import annotations

import json
import pickle
import tempfile
import unittest
import zlib
from pathlib import Path

from renweave.context import ContextPlanner
from renweave.indexer import ProjectIndexer
from renweave.knowledge import DeterministicKnowledgeBuilder
from renweave.pipeline import PipelineStage, RenWeavePipeline
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


if __name__ == "__main__":
    unittest.main()
