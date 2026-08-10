from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


LATEST_RELEASE_URL = "https://api.github.com/repos/Mehael-Yeh/RenWeave/releases/latest"
LATEST_TAGS_URL = "https://api.github.com/repos/Mehael-Yeh/RenWeave/tags?per_page=1"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    current_version: str
    latest_version: str
    release_url: str

    @property
    def update_available(self) -> bool:
        return _version_tuple(self.latest_version) > _version_tuple(self.current_version)


def _version_tuple(value: str) -> tuple[int, ...]:
    clean = value.strip().lower().removeprefix("v").split("-", 1)[0]
    parts = []
    for item in clean.split("."):
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_for_updates(current_version: str, *, timeout_seconds: int = 8) -> UpdateResult:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RenWeave-update-check"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(512 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return _check_latest_tag(current_version, timeout_seconds=timeout_seconds)
    if len(raw) > 512 * 1024:
        raise RuntimeError("The update response exceeded the safety limit")
    payload = json.loads(raw.decode("utf-8"))
    latest = str(payload.get("tag_name", "")).strip()
    release_url = str(payload.get("html_url", "")).strip()
    if not latest or not release_url.startswith("https://github.com/"):
        raise RuntimeError("GitHub returned an incomplete release record")
    return UpdateResult(current_version, latest, release_url)


def _check_latest_tag(current_version: str, *, timeout_seconds: int) -> UpdateResult:
    request = urllib.request.Request(
        LATEST_TAGS_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RenWeave-update-check"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise RuntimeError("The update response exceeded the safety limit")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, list) or not payload:
        return UpdateResult(current_version, current_version, "https://github.com/Mehael-Yeh/RenWeave/releases")
    latest = str(payload[0].get("name", "")).strip() if isinstance(payload[0], dict) else ""
    if not latest:
        raise RuntimeError("GitHub returned an incomplete tag record")
    return UpdateResult(
        current_version,
        latest,
        f"https://github.com/Mehael-Yeh/RenWeave/releases/tag/{latest}",
    )
