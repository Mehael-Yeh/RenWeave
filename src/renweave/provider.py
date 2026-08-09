from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .io import read_json


def response_json(response: dict[str, Any]) -> dict[str, Any]:
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


@dataclass(slots=True)
class ModelProfile:
    name: str
    model: str
    base_url: str
    api_key_env: str = ""
    api_key: str = ""
    timeout_seconds: int = 120
    context_window: int = 0
    supports_json: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "ModelProfile":
        payload = read_json(Path(path))
        if payload.get("kind", "openai_compatible") != "openai_compatible":
            raise ValueError("当前里程碑仅支持 openai_compatible 配置")
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("模型配置缺少 name")
        if not self.model.strip():
            raise ValueError("模型配置缺少 model")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须是 HTTP(S) 地址")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于零")

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


class OpenAICompatibleGateway:
    def __init__(self, profile: ModelProfile) -> None:
        profile.validate()
        self.profile = profile

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> dict[str, Any]:
        endpoint = self.profile.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.profile.supports_json:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        key = self.profile.resolved_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.profile.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"模型请求失败：HTTP {exc.code}: {detail}") from exc
