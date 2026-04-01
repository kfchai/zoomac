"""Credential vault with optional Fernet encryption."""

from __future__ import annotations

import base64
import hashlib
import logging
import warnings
from datetime import datetime, timezone

from zoomac.planner.models import CredentialEntry
from zoomac.planner.store import GoalStore

logger = logging.getLogger(__name__)


class CredentialVault:
    """Encrypted credential storage backed by GoalStore.

    If ``encryption_key`` is provided, values are encrypted with Fernet.
    Otherwise values are stored in plaintext with a warning.
    """

    def __init__(self, store: GoalStore, encryption_key: str | None = None) -> None:
        self._store = store
        self._fernet = None

        if encryption_key:
            try:
                from cryptography.fernet import Fernet

                # Derive a valid Fernet key from the user-provided key
                key_bytes = hashlib.sha256(encryption_key.encode()).digest()
                fernet_key = base64.urlsafe_b64encode(key_bytes)
                self._fernet = Fernet(fernet_key)
            except ImportError:
                warnings.warn(
                    "cryptography package not installed. Credentials stored in plaintext.",
                    stacklevel=2,
                )
        else:
            logger.warning(
                "No ZOOMAC_SECRET_KEY set. Credentials will be stored in plaintext."
            )

    def store(self, key: str, value: str, description: str = "") -> None:
        """Encrypt and store a credential."""
        encrypted = self._encrypt(value)
        existing = self._store.get_credential(key)
        entry = CredentialEntry(
            key=key,
            description=description or (existing.description if existing else key),
            value=encrypted,
            requested_at=existing.requested_at if existing else datetime.now(timezone.utc),
            provided_at=datetime.now(timezone.utc),
        )
        self._store.save_credential(entry)

    def retrieve(self, key: str) -> str | None:
        """Decrypt and return a credential, or None if not found/not provided."""
        entry = self._store.get_credential(key)
        if entry is None or entry.value is None:
            return None
        return self._decrypt(entry.value)

    def request(self, key: str, description: str) -> CredentialEntry:
        """Create a pending credential request. Returns existing if already provided."""
        existing = self._store.get_credential(key)
        if existing and existing.value is not None:
            return existing
        entry = CredentialEntry(key=key, description=description)
        self._store.save_credential(entry)
        return entry

    def list_pending(self) -> list[CredentialEntry]:
        """List credentials requested but not yet provided."""
        return self._store.list_pending_credentials()

    def _encrypt(self, value: str) -> str:
        if self._fernet:
            return self._fernet.encrypt(value.encode()).decode()
        return value

    def _decrypt(self, value: str) -> str:
        if self._fernet:
            return self._fernet.decrypt(value.encode()).decode()
        return value
