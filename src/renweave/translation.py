from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .context import SceneContext
from .provider import OpenAICompatibleGateway


SYSTEM_PROMPT = """You are RenWeave's scene-level game localization engine.
Translate the complete Ren'Py scene into the requested target language.
Preserve every text id, placeholder, interpolation, and Ren'Py text tag byte-for-byte.
Use the surrounding scene and character evidence to preserve relationships, callbacks, tone, jokes, and subtext.
Do not invent facts or make a relationship more or less intimate than the source scene.
Return one JSON object with a `translations` array. Each item must contain exactly `id` and `text`.
"""

REPAIR_PROMPT = """You repair a small set of invalid Ren'Py translations.
Return one JSON object with a `translations` array containing exactly the requested text ids.
Fix only the listed validation problems. Preserve every placeholder, interpolation, and Ren'Py text tag byte-for-byte.
Do not add explanations or markdown.
"""


@dataclass(slots=True)
class SceneTranslation:
    scene_id: str
    translations: dict[str, str]
    raw_response: dict[str, Any]


class SceneTranslator:
    def __init__(self, gateway: OpenAICompatibleGateway) -> None:
        self.gateway = gateway

    def translate(
        self,
        context: SceneContext,
        target_language: str,
        *,
        source_language: str = "auto",
    ) -> SceneTranslation:
        user_payload = {
            "source_language": source_language,
            "target_language": target_language,
            "scene": context.to_dict(),
        }
        response = self.gateway.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ])
        payload = self._response_payload(response)
        translations = self._translations(payload)
        return SceneTranslation(context.scene_id, translations, response)

    def repair(
        self,
        context: SceneContext,
        target_language: str,
        current: dict[str, str],
        issues: dict[str, list[str]],
        *,
        source_language: str = "auto",
    ) -> dict[str, str]:
        lines = []
        for line in context.lines:
            text_id = str(line["id"])
            if text_id not in issues:
                continue
            lines.append({
                "id": text_id,
                "source": line["source"],
                "current_translation": current.get(text_id, ""),
                "validation_errors": issues[text_id],
                "tags": line.get("tags", []),
                "placeholders": line.get("placeholders", []),
            })
        if not lines:
            return {}
        response = self.gateway.chat([
            {"role": "system", "content": REPAIR_PROMPT},
            {"role": "user", "content": json.dumps({
                "source_language": source_language,
                "target_language": target_language,
                "scene_id": context.scene_id,
                "lines": lines,
            }, ensure_ascii=False)},
        ])
        return self._translations(self._response_payload(response))

    @staticmethod
    def _response_payload(response: dict[str, Any]) -> dict[str, Any]:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("模型响应缺少 choices[0].message.content") from exc
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise ValueError("模型响应 content 不是 JSON 对象或字符串")
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
            if cleaned.lstrip().lower().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3]
        payload = json.loads(cleaned.strip())
        if not isinstance(payload, dict):
            raise ValueError("模型响应 JSON 顶层必须是对象")
        return payload

    @staticmethod
    def _translations(payload: dict[str, Any]) -> dict[str, str]:
        rows = payload.get("translations", [])
        if not isinstance(rows, list):
            raise ValueError("模型响应 translations 必须是数组")
        return {
            str(row["id"]): str(row["text"])
            for row in rows
            if isinstance(row, dict) and "id" in row and "text" in row
        }
