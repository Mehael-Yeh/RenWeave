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
        content = response["choices"][0]["message"]["content"]
        payload = json.loads(content) if isinstance(content, str) else content
        rows = payload.get("translations", [])
        translations = {
            str(row["id"]): str(row["text"])
            for row in rows
            if isinstance(row, dict) and "id" in row and "text" in row
        }
        return SceneTranslation(context.scene_id, translations, response)
