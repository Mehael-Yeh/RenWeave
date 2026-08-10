from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .io import atomic_write_json, read_json
from .provider_presets import PROVIDER_PRESETS_BY_ID


TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}


def response_json(response: dict[str, Any]) -> dict[str, Any]:
    """Extract a JSON object from an OpenAI-compatible chat response."""
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Model response is missing choices[0].message.content") from exc
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("Model response content must be a JSON object or string")
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    payload = json.loads(cleaned.strip())
    if not isinstance(payload, dict):
        raise ValueError("Model response JSON must have an object at the top level")
    return payload


@dataclass(slots=True)
class ModelProfile:
    name: str
    model: str
    base_url: str
    provider_id: str = "custom"
    api_key_env: str = ""
    api_key: str = ""
    timeout_seconds: int = 120
    context_window: int = 0
    supports_json: bool = True
    reasoning_level: str = "auto"
    max_response_bytes: int = 16 * 1024 * 1024
    max_retries: int = 3
    retry_base_seconds: float = 1.0

    @classmethod
    def load(cls, path: str | Path) -> "ModelProfile":
        payload = read_json(Path(path))
        if not isinstance(payload, dict):
            raise ValueError("Provider configuration must be a JSON object")
        if payload.get("kind", "openai_compatible") != "openai_compatible":
            raise ValueError("Only openai_compatible provider configurations are supported")
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in payload.items() if key in allowed})

    def validate_connection(self) -> None:
        """Validate settings needed to contact the API, before a model is selected."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Provider name is required")
        if not isinstance(self.base_url, str) or not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Base URL must be an HTTP(S) address")
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("provider_id is required")
        parsed = urllib.parse.urlsplit(self.base_url)
        if not parsed.netloc:
            raise ValueError("Base URL must include a host")
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not isinstance(self.max_response_bytes, int) or not 1024 <= self.max_response_bytes <= 256 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 KiB and 256 MiB")
        if not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        if not isinstance(self.retry_base_seconds, (int, float)) or not 0 <= self.retry_base_seconds <= 60:
            raise ValueError("retry_base_seconds must be between 0 and 60 seconds")
        if self.reasoning_level not in {"auto", "low", "high", "maximum"}:
            raise ValueError("reasoning_level must be auto, low, high, or maximum")

    def validate(self) -> None:
        self.validate_connection()
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("A model must be selected")

    def resolved_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""

    def save(self, path: str | Path, *, include_api_key: bool = False) -> Path:
        """Persist a reusable profile. API keys stay out of the file by default."""
        self.validate()
        target = Path(path)
        payload = {
            "kind": "openai_compatible",
            "name": self.name.strip(),
            "model": self.model.strip(),
            "base_url": self.base_url.rstrip("/"),
            "provider_id": self.provider_id.strip(),
            "api_key_env": self.api_key_env.strip(),
            "timeout_seconds": self.timeout_seconds,
            "context_window": self.context_window,
            "supports_json": self.supports_json,
            "reasoning_level": self.reasoning_level,
            "max_response_bytes": self.max_response_bytes,
            "max_retries": self.max_retries,
            "retry_base_seconds": self.retry_base_seconds,
        }
        if include_api_key and self.api_key:
            payload["api_key"] = self.api_key
        atomic_write_json(target, payload)
        return target


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    models: tuple[str, ...]
    endpoint: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class ModelVerification:
    model: str
    latency_ms: int


def _api_endpoint(base_url: str, resource: str) -> str:
    """Build an endpoint from API roots or pasted OpenAI endpoint URLs."""
    parsed = urllib.parse.urlsplit(base_url.strip())
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = f"{path}/{resource.lstrip('/')}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class OpenAICompatibleCatalog:
    """Discover and verify models without starting a translation job."""

    def __init__(self, profile: ModelProfile) -> None:
        profile.validate_connection()
        self.profile = profile

    def list_models(self) -> ModelCatalog:
        endpoint = _api_endpoint(self.profile.base_url, "models")
        request = urllib.request.Request(endpoint, headers=self._headers(), method="GET")
        started = time.perf_counter()
        payload = self._request_json(request)
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raise ValueError("The models endpoint response is missing a data list")
        model_ids = {
            item.get("id").strip()
            for item in raw_models
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id").strip()
        }
        if not model_ids:
            raise ValueError("The API returned no selectable models")
        return ModelCatalog(tuple(sorted(model_ids, key=str.casefold)), endpoint, elapsed)

    def verify_model(self, model: str | None = None) -> ModelVerification:
        selected = (model if model is not None else self.profile.model).strip()
        if not selected:
            raise ValueError("Select a model before verifying it")
        verified_profile = replace(self.profile, model=selected)
        started = time.perf_counter()
        response = OpenAICompatibleGateway(verified_profile).chat(
            [
                {"role": "system", "content": "You are an API health check."},
                {"role": "user", "content": 'Return only {"ok":true}.'},
            ],
            # MiniMax and several compatible providers reject a literal zero.
            temperature=0.1,
        )
        # A syntactically valid chat response proves that the selected model can be called.
        if not isinstance(response.get("choices"), list) or not response["choices"]:
            raise ValueError("The selected model returned no choices")
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        return ModelVerification(selected, elapsed)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        key = self.profile.resolved_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _request_json(self, request: urllib.request.Request) -> dict[str, Any]:
        for attempt in range(self.profile.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.profile.timeout_seconds) as response:
                    raw = response.read(self.profile.max_response_bytes + 1)
                if len(raw) > self.profile.max_response_bytes:
                    raise RuntimeError("API response exceeds the max_response_bytes safety limit")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("API response JSON must have an object at the top level")
                return payload
            except urllib.error.HTTPError as exc:
                detail = exc.read(2001).decode("utf-8", errors="replace")[:2000]
                if exc.code not in TRANSIENT_HTTP_STATUSES or attempt >= self.profile.max_retries:
                    hint = " Check the API key and endpoint." if exc.code in {401, 403, 404} else ""
                    raise RuntimeError(f"API request failed: HTTP {exc.code}: {detail}.{hint}") from exc
            except urllib.error.URLError as exc:
                if attempt >= self.profile.max_retries:
                    raise RuntimeError(f"API network request failed: {exc.reason}") from exc
            self._retry_delay(attempt)
        raise RuntimeError("API request retries were exhausted")

    def _retry_delay(self, attempt: int) -> None:
        delay = min(8.0, self.profile.retry_base_seconds * (2**attempt))
        if delay > 0:
            time.sleep(delay)


class OpenAICompatibleGateway:
    def __init__(self, profile: ModelProfile) -> None:
        profile.validate()
        self.profile = profile
        self.requests_attempted = 0
        self.model_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> dict[str, Any]:
        endpoint = _api_endpoint(self.profile.base_url, "chat/completions")
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.profile.supports_json:
            payload["response_format"] = {"type": "json_object"}
        self._apply_reasoning_control(payload)
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
                    raise RuntimeError("Model response exceeds the max_response_bytes safety limit")
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError("Model HTTP response JSON must have an object at the top level")
                self._record_usage(result)
                return result
            except urllib.error.HTTPError as exc:
                detail = exc.read(2001).decode("utf-8", errors="replace")[:2000]
                if exc.code not in TRANSIENT_HTTP_STATUSES or attempt >= self.profile.max_retries:
                    raise RuntimeError(f"Model request failed: HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt >= self.profile.max_retries:
                    raise RuntimeError(f"Model network request failed: {exc.reason}") from exc
            self._retry_delay(attempt)
        raise RuntimeError("Model request retries were exhausted")

    def _apply_reasoning_control(self, payload: dict[str, Any]) -> None:
        """Translate the common UI level into each provider's documented request fields."""
        level = self.profile.reasoning_level
        if level == "auto":
            return
        preset = PROVIDER_PRESETS_BY_ID.get(self.profile.provider_id)
        control = preset.reasoning_control if preset is not None else "effort"
        if control == "none":
            return
        if control == "deepseek":
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = {"low": "low", "high": "high", "maximum": "max"}[level]
            return
        if control == "zhipu":
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = {"low": "low", "high": "high", "maximum": "max"}[level]
            return
        if control == "thinking":
            payload["thinking"] = {"type": "enabled"}
            return
        if control == "toggle":
            payload["enable_thinking"] = True
            return
        if control == "budget":
            payload["enable_thinking"] = True
            payload["thinking_budget"] = {"low": 1024, "high": 8192, "maximum": 16384}[level]
            return
        payload["reasoning_effort"] = {"low": "low", "high": "high", "maximum": "xhigh"}[level]

    def _retry_delay(self, attempt: int) -> None:
        delay = min(8.0, self.profile.retry_base_seconds * (2**attempt))
        if delay > 0:
            time.sleep(delay)

    def _record_usage(self, response: dict[str, Any]) -> None:
        self.model_calls += 1
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            return
        self.prompt_tokens += self._usage_value(usage, "prompt_tokens", "input_tokens")
        self.completion_tokens += self._usage_value(usage, "completion_tokens", "output_tokens")

    @staticmethod
    def _usage_value(usage: dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        return 0
