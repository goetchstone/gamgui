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
from typing import Dict, Optional, Protocol, Tuple

from .. import clock

# Logical credential name -> the filename GAM expects inside GAMCFGDIR.
FILENAMES: Dict[str, str] = {
    "client_secrets": "client_secrets.json",
    "oauth2": "oauth2.txt",
    "oauth2service": "oauth2service.json",
}
CREDENTIAL_NAMES = tuple(FILENAMES.keys())
# clear_domain's order: the most dangerous first (oauth2service.json can impersonate anyone), so a
# delete that fails part-way never leaves it behind with the lesser credentials already gone.
_DELETE_ORDER = ("oauth2service", "oauth2", "client_secrets")

# Credentials required before GAM can act as the domain (service-account flow).
_REQUIRED = ("oauth2service", "oauth2")

_INDEX_SERVICE = "gamgui"
_INDEX_KEY = "_domains"
# folds_case's throwaway item: mixed case, so its lowercased lookup only matches if the store folds.
_PROBE_SERVICE = "gamgui-probe:CaseProbe"   # outside the gamgui:<domain> namespace
_PROBE_KEY = "probe"


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


# errSecItemNotFound (Security/SecBase.h): the one Keychain status that means "there is no such item".
_ERR_SEC_ITEM_NOT_FOUND = -25300


def _item_not_found(exc: BaseException) -> bool:
    """True when *exc*, a keyring ``PasswordDeleteError``, means the item wasn't there.

    keyring's macOS backend raises that same error for every failed delete: an absent item, a
    denied or locked Keychain, an unsigned binary. It raises it ``from`` the Security-API error,
    whose first arg is the OSStatus, and that status is the only thing that tells them apart.
    """
    cause = exc.__cause__
    return cause is not None and cause.args[:1] == (_ERR_SEC_ITEM_NOT_FOUND,)


class _KeyringBackend:
    """Default backend — lazily imports ``keyring`` so core tests don't require it installed."""

    def __init__(self) -> None:
        import keyring  # noqa: F401  (import-time check that it's available)
        from keyring.errors import PasswordDeleteError

        self._keyring = keyring
        self._delete_error = PasswordDeleteError

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self._keyring.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._keyring.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        # Deleting an absent item is a no-op. Any other failure (a denied or locked Keychain, no
        # keyring backend) propagates: the secret may still be in the Keychain.
        try:
            self._keyring.delete_password(service, username)
        except self._delete_error as exc:
            if _item_not_found(exc):
                return
            # Another keyring backend reports a missing item with no Security status: look again.
            if exc.__cause__ is None and self._keyring.get_password(service, username) is None:
                return
            raise


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
            now = clock.now()
            hit = self._cache.get(key)
            if hit is not None and hit[1] > now:
                self._cache[key] = (hit[0], now + self._cache_ttl)  # sliding: extend on use
                return hit[0]
        value = self.backend.get_password(self._service(domain), name)
        if self._cache_ttl:
            self._cache[key] = (value, clock.now() + self._cache_ttl)
        return value

    def set(self, domain: str, name: str, value: str) -> None:
        _check_name(name)
        self.backend.set_password(self._service(domain), name, value)
        if self._cache_ttl:
            self._cache[(domain, name)] = (value, clock.now() + self._cache_ttl)
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

    def oauth_admin_email(self, domain: str) -> str:
        """The connected admin's address — the ``email`` claim GAM keeps beside the token in
        ``oauth2.txt`` (``decoded_id_token``; ``id_token`` when an older build stored the claims
        there) — lowercased, or "" when it can't be read. Returns that one claim only, parsed in
        memory: the token itself never leaves the vault. An undecoded JWT is not decoded here."""
        try:
            data = json.loads(self.get(domain, "oauth2") or "")
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        for key in ("decoded_id_token", "id_token"):
            claims = data.get(key)
            if isinstance(claims, str):
                try:
                    claims = json.loads(claims)
                except ValueError:
                    continue
            email = claims.get("email") if isinstance(claims, dict) else None
            if isinstance(email, str) and "@" in email and not any(c.isspace() for c in email):
                return email.strip().lower()
        return ""

    def clear_domain(self, domain: str, deleted: Optional[list] = None) -> None:
        # A delete that fails raises before the domain leaves the index: its secrets are still there.
        # ``deleted`` collects each name as it goes (gone or never there), for a caller that records
        # how far a refused removal got.
        for name in _DELETE_ORDER:
            self.delete(domain, name)
            if deleted is not None:
                deleted.append(name)
        self._unregister_domain(domain)

    def forget_domain(self, domain: str) -> None:
        """Drop ``domain`` from the index and the cache, deleting no Keychain item: for a spelling whose
        items the store may share with another (see :meth:`folds_case`)."""
        for name in CREDENTIAL_NAMES:
            self._cache.pop((domain, name), None)
        self._unregister_domain(domain)

    def folds_case(self) -> bool:
        """Whether deleting a service also deletes it under another capitalization. Domains were once
        stored as typed, so ``Example.com`` and ``example.com`` can both be listed; if the store folds
        case, deleting either deletes both. SecItem matching is documented as case-sensitive, but a
        delete of a key that can impersonate anyone isn't left to a document: this stores a throwaway
        item (no secret) under both spellings, deletes the capitalized one and looks for the other —
        the delete itself, not a lookup, is what has to be case-exact."""
        upper, lower = _PROBE_SERVICE, _PROBE_SERVICE.lower()
        try:
            self.backend.set_password(lower, _PROBE_KEY, "probe")
            self.backend.set_password(upper, _PROBE_KEY, "probe")
            self.backend.delete_password(upper, _PROBE_KEY)
            return self.backend.get_password(lower, _PROBE_KEY) is None
        finally:
            self.backend.delete_password(upper, _PROBE_KEY)
            self.backend.delete_password(lower, _PROBE_KEY)

    # --- domain index ------------------------------------------------------------------
    def list_domains(self) -> list:
        raw = self.backend.get_password(_INDEX_SERVICE, _INDEX_KEY)
        try:
            return sorted(json.loads(raw)) if raw else []
        except ValueError:  # json.JSONDecodeError is a subclass of ValueError
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
