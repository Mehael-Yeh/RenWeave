from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .models import ProjectIndex


TAG_RE = re.compile(r"\{/?[A-Za-z][^{}]*\}")
PLACEHOLDER_RE = re.compile(r"\[[^\[\]\r\n]+\]|%(?:\([^)]+\))?[#0 +\-]?[0-9]*(?:\.[0-9]+)?[a-zA-Z]")


@dataclass(slots=True, frozen=True)
class ValidationIssue:
    code: str
    text_id: str
    message: str


@dataclass(slots=True)
class ValidationReport:
    expected: int
    received: int
    issues: list[ValidationIssue]

    @property
    def passed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "expected": self.expected,
            "received": self.received,
            "passed": self.passed,
            "issues": [asdict(issue) for issue in self.issues],
        }


class TranslationValidator:
    def validate_scene(
        self,
        index: ProjectIndex,
        scene_id: str,
        translations: dict[str, str],
    ) -> ValidationReport:
        scene = next((item for item in index.scenes if item.id == scene_id), None)
        if scene is None:
            raise KeyError(f"未知场景：{scene_id}")
        issues: list[ValidationIssue] = []
        expected_ids = {unit.id for unit in scene.text_units}
        for missing in sorted(expected_ids - translations.keys()):
            issues.append(ValidationIssue("MISSING_TRANSLATION", missing, "模型未返回该文本"))
        for extra in sorted(translations.keys() - expected_ids):
            issues.append(ValidationIssue("UNKNOWN_TEXT_ID", extra, "模型返回了当前场景不存在的文本 ID"))
        for unit in scene.text_units:
            if unit.id not in translations:
                continue
            translated = translations[unit.id]
            if not translated.strip() and unit.source.strip():
                issues.append(ValidationIssue("EMPTY_TRANSLATION", unit.id, "译文为空"))
            source_tags = sorted(TAG_RE.findall(unit.source))
            target_tags = sorted(TAG_RE.findall(translated))
            if source_tags != target_tags:
                issues.append(ValidationIssue("TAG_MISMATCH", unit.id, "Ren'Py 文本标签不一致"))
            source_placeholders = sorted(PLACEHOLDER_RE.findall(unit.source))
            target_placeholders = sorted(PLACEHOLDER_RE.findall(translated))
            if source_placeholders != target_placeholders:
                issues.append(ValidationIssue("PLACEHOLDER_MISMATCH", unit.id, "变量或占位符不一致"))
        return ValidationReport(len(expected_ids), len(translations), issues)
