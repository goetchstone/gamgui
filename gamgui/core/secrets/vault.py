"""Secret storage.

The canonical home for GAM's credentials is the macOS Keychain. We keep three items per
Workspace domain:

* ``client_secrets`` → ``client_secrets.json`` (OAuth client)
* ``oauth2``         → ``oauth2.txt``          (admin refresh token; ≈ admin password)
* ``oauth2service``  → ``oauth2service.json``  (service-account key; can impersonate anyone)

The vault is backend-pluggable so tests (and headless CI) can use an in-memory store instead of
the real Keychain. The default backend uses ``keyring``, which maps to the macOS Keychain.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional, Protocol, Tuple

# Logical credential name -> the filename GAM expects inside GAMCFGDIR.
FILENAMES: Dict[str, str] = {
    "client_secrets": "client_secrets.json",
    "oauth2": "oauth2.txt",
    "oauth2service": "oauth2service.json",
}
CREDENTIAL_NAMES = tuple(FILENAMES.keys())

# Credentials required before GAM can act as the domain (service-account flow).
_REQUIRED = ("oauth2service", "oauth2")

_INDEX_SERVICE = "gamgui"
_INDEX_KEY = "_domains"


class VaultBackend(Protocol):
    """Minimal secret store interface (a subset of keyring's API)."""

    def get_password(self, service: str, username: str) -> Optional[str]: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class InMemoryBackend:
    """Backend for tests — keeps secrets in a dict. Never touches the OS Keychain."""

    def __init__(self) -> None:
        self._store: Dict[str, str] = {}

    @staticmethod
    def _k(service: str, username: str) -> str:
        return f"{service}\x00{username}"

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._store.get(self._k(service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[self._k(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop(self._k(service, username), None)


class _KeyringBackend:
    """Default backend — lazily imports ``keyring`` so core tests don't require it installed."""

    def __init__(self) -> None:
        import keyring  # noqa: F401  (import-time check that it's available)

        self._keyring = keyring

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._keyring.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._keyring.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        try:
            self._keyring.delete_password(service, username)
        except Exception:
            # keyring raises PasswordDeleteError if absent; deleting a missing item is a no-op.
            pass


class SecretsVault:
    # Default lifetime (seconds) for the in-process secret cache: a "session-reuse window" so a
    # burst of gam calls doesn't re-prompt the Keychain on every read. Sliding (extends on use).
    # Override with env GAMGUI_SECRET_CACHE_TTL; set to 0 to disable (re-read the Keychain every call).
    DEFAULT_CACHE_TTL = 300.0

    def __init__(self, backend: Optional[VaultBackend] = None, cache_ttl: Optional[float] = None) -> None:
        self.backend: VaultBackend = backend or _KeyringBackend()
        if cache_ttl is None:
            try:
                cache_ttl = float(os.environ.get("GAMGUI_SECRET_CACHE_TTL", self.DEFAULT_CACHE_TTL))
            except ValueError:
                cache_ttl = self.DEFAULT_CACHE_TTL
        self._cache_ttl = max(0.0, cache_ttl)
        self._cache: Dict[Tuple[str, str], Tuple[Optional[str], float]] = {}

    @staticmethod
    def _service(domain: str) -> str:
        return f"gamgui:{domain}"

    def clear_cache(self) -> None:
        """Forget cached secrets so the next read re-prompts the Keychain (an explicit 'lock')."""
        self._cache.clear()

    # --- single credential -------------------------------------------------------------
    def get(self, domain: str, name: str) -> Optional[str]:
        _check_name(name)
        key = (domain, name)
        if self._cache_ttl:
            now = time.monotonic()
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                self._cache[key] = (hit[0], now + self._cache_ttl)  # sliding: extend on use
                return hit[0]
        value = self.backend.get_password(self._service(domain), name)
        if self._cache_ttl:
            self._cache[key] = (value, time.monotonic() + self._cache_ttl)
        return value

    def set(self, domain: str, name: str, value: str) -> None:
        _check_name(name)
        self.backend.set_password(self._service(domain), name, value)
        if self._cache_ttl:
            self._cache[(domain, name)] = (value, time.monotonic() + self._cache_ttl)
        self._register_domain(domain)

    def delete(self, domain: str, name: str) -> None:
        _check_name(name)
        self.backend.delete_password(self._service(domain), name)
        self._cache.pop((domain, name), None)

    # --- whole credential set ----------------------------------------------------------
    def get_all(self, domain: str) -> Dict[str, Optional[str]]:
        return {name: self.get(domain, name) for name in CREDENTIAL_NAMES}

    def set_all(self, domain: str, creds: Dict[str, str]) -> None:
        for name, value in creds.items():
            if value is not None:
                self.set(domain, name, value)

    def has_credentials(self, domain: str) -> bool:
        return all(self.get(domain, name) for name in _REQUIRED)

    def clear_domain(self, domain: str) -> None:
        for name in CREDENTIAL_NAMES:
            self.delete(domain, name)
        self._unregister_domain(domain)

    # --- domain index ------------------------------------------------------------------
    def list_domains(self) -> list:
        raw = self.backend.get_password(_INDEX_SERVICE, _INDEX_KEY)
        try:
            return sorted(json.loads(raw)) if raw else []
        except (json.JSONDecodeError, ValueError):
            return []

    def _register_domain(self, domain: str) -> None:
        domains = set(self.list_domains())
        if domain not in domains:
            domains.add(domain)
            self.backend.set_password(_INDEX_SERVICE, _INDEX_KEY, json.dumps(sorted(domains)))

    def _unregister_domain(self, domain: str) -> None:
        domains = set(self.list_domains())
        if domain in domains:
            domains.discard(domain)
            self.backend.set_password(_INDEX_SERVICE, _INDEX_KEY, json.dumps(sorted(domains)))


def _check_name(name: str) -> None:
    if name not in CREDENTIAL_NAMES:
        raise ValueError(f"unknown credential name {name!r}; expected one of {CREDENTIAL_NAMES}")
