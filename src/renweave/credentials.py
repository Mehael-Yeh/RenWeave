from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


class CredentialBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialStorageError(RuntimeError):
    pass


def credential_account(provider_id: str, base_url: str) -> str:
    identity = f"{provider_id.strip().casefold()}\n{base_url.strip().rstrip('/').casefold()}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"provider:{provider_id.strip() or 'custom'}:{digest}"


@dataclass(slots=True)
class SecureCredentialStore:
    """Store API keys in the operating system's encrypted credential service."""

    backend: CredentialBackend | None = None
    service_name: str = "RenWeave API Credentials"

    def _backend(self) -> CredentialBackend:
        if self.backend is not None:
            return self.backend
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - packaging installs keyring
            raise CredentialStorageError("The secure credential component is not installed") from exc
        return keyring

    def get(self, provider_id: str, base_url: str) -> str:
        try:
            return self._backend().get_password(
                self.service_name, credential_account(provider_id, base_url)
            ) or ""
        except Exception as exc:
            raise CredentialStorageError(f"Could not read the encrypted credential: {exc}") from exc

    def set(self, provider_id: str, base_url: str, secret: str) -> None:
        if not secret:
            return
        try:
            self._backend().set_password(
                self.service_name, credential_account(provider_id, base_url), secret
            )
        except Exception as exc:
            raise CredentialStorageError(f"Could not save the encrypted credential: {exc}") from exc

    def delete(self, provider_id: str, base_url: str) -> None:
        try:
            self._backend().delete_password(
                self.service_name, credential_account(provider_id, base_url)
            )
        except Exception as exc:
            # A missing item is not a failure from the user's perspective.
            if type(exc).__name__ != "PasswordDeleteError" and "not found" not in str(exc).casefold():
                raise CredentialStorageError(f"Could not delete the encrypted credential: {exc}") from exc
