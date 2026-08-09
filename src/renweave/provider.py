from __future__ import annotations

import json
import os
import time
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
    max_response_bytes: int = 16 * 1024 * 1024
    max_retries: int = 3
    retry_base_seconds: float = 1.0

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
        if not 1024 <= self.max_response_bytes <= 256 * 1024 * 1024:
            raise ValueError("max_response_bytes 必须在 1 KiB 到 256 MiB 之间")
        if not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries 必须在 0 到 10 之间")
        if not 0 <= self.retry_base_seconds <= 60:
            raise ValueError("retry_base_seconds 必须在 0 到 60 秒之间")

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


class OpenAICompatibleGateway:
    def __init__(self, profile: ModelProfile) -> None:
        profile.validate()
        self.profile = profile
        self.requests_attempted = 0
        self.model_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

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
        for attempt in range(self.profile.max_retries + 1):
            self.requests_attempted += 1
            try:
                with urllib.request.urlopen(request, timeout=self.profile.timeout_seconds) as response:
                    raw = response.read(self.profile.max_response_bytes + 1)
                if len(raw) > self.profile.max_response_bytes:
                    raise RuntimeError("模型响应超过 max_response_bytes 安全限制")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("模型 HTTP 响应 JSON 顶层必须是对象")
                self._record_usage(result)
                return result
            except urllib.error.HTTPError as exc:
                detail = exc.read(2001).decode("utf-8", errors="replace")[:2000]
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= self.profile.max_retries:
                    raise RuntimeError(f"模型请求失败：HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt >= self.profile.max_retries:
                    raise RuntimeError(f"模型网络请求失败：{exc.reason}") from exc
            self._retry_delay(attempt)
        raise RuntimeError("模型请求重试耗尽")

    def _retry_delay(self, attempt: int) -> None:
        delay = min(8.0, self.profile.retry_base_seconds * (2 ** attempt))
        if delay > 0:
            time.sleep(delay)

    def _record_usage(self, response: dict[str, Any]) -> None:
        self.model_calls += 1
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            return
        self.prompt_tokens += self._usage_value(usage, "prompt_tokens", "input_tokens")
        self.completion_tokens += self._usage_value(
            usage, "completion_tokens", "output_tokens"
        )

    @staticmethod
    def _usage_value(usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        return 0
