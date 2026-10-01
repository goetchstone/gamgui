from __future__ import annotations

import sys

import pytest
from keyring.errors import NoKeyringError, PasswordDeleteError

from gamgui.core.secrets.vault import CREDENTIAL_NAMES, InMemoryBackend, SecretsVault, _KeyringBackend


@pytest.fixture
def empty_vault() -> SecretsVault:
    return SecretsVault(backend=InMemoryBackend())


def test_set_get_roundtrip(empty_vault):
    empty_vault.set("a.com", "oauth2", "tok")
    assert empty_vault.get("a.com", "oauth2") == "tok"


def test_unknown_name_raises(empty_vault):
    with pytest.raises(ValueError):
        empty_vault.get("a.com", "not_a_credential")


def test_has_credentials_requires_service_and_oauth(empty_vault):
    assert empty_vault.has_credentials("a.com") is False
    empty_vault.set("a.com", "oauth2service", "{}")
    assert empty_vault.has_credentials("a.com") is False
    empty_vault.set("a.com", "oauth2", "tok")
    assert empty_vault.has_credentials("a.com") is True


def test_domain_index_and_clear(empty_vault):
    empty_vault.set("a.com", "oauth2", "t")
    empty_vault.set("b.com", "oauth2", "t")
    assert empty_vault.list_domains() == ["a.com", "b.com"]
    empty_vault.clear_domain("a.com")
    assert empty_vault.get("a.com", "oauth2") is None
    assert empty_vault.list_domains() == ["b.com"]


def test_get_all_returns_all_names(empty_vault):
    empty_vault.set("a.com", "oauth2", "t")
    allc = empty_vault.get_all("a.com")
    assert set(allc.keys()) == {"client_secrets", "oauth2", "oauth2service"}
    assert allc["oauth2"] == "t"
    assert allc["client_secrets"] is None


class _CountingBackend(InMemoryBackend):
    """In-memory backend that counts reads, to prove the cache avoids Keychain prompts."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get_password(self, service: str, username: str):
        self.reads += 1
        return super().get_password(service, username)


def test_cache_avoids_repeat_backend_reads():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=300)
    v.set("a.com", "oauth2", "tok")   # seeds the cache
    backend.reads = 0
    for _ in range(5):
        assert v.get("a.com", "oauth2") == "tok"
    assert backend.reads == 0          # all served from the session cache -> no repeat Keychain prompts


def test_clear_cache_forces_reread():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=300)
    v.set("a.com", "oauth2", "tok")
    v.clear_cache()                    # explicit "lock"
    backend.reads = 0
    assert v.get("a.com", "oauth2") == "tok"
    assert backend.reads == 1          # re-locked: one fresh backend read


def test_cache_ttl_zero_disables_caching():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=0)
    v.set("a.com", "oauth2", "tok")
    backend.reads = 0
    v.get("a.com", "oauth2")
    v.get("a.com", "oauth2")
    assert backend.reads == 2          # caching disabled: every read hits the backend


def test_delete_invalidates_cache():
    backend = _CountingBackend()
    v = SecretsVault(backend=backend, cache_ttl=300)
    v.set("a.com", "oauth2", "tok")
    v.delete("a.com", "oauth2")
    assert v.get("a.com", "oauth2") is None  # not a stale cached "tok"


@pytest.mark.parametrize("stored, admin", [
    # GAM7 keeps the ID token's claims next to the token (`decoded_id_token`, read from the vendored
    # build's writer); an older file carries them as `id_token`.
    ('{"refresh_token": "r", "decoded_id_token": {"email": "Admin@Example.com", "hd": "example.com"}}',
     "admin@example.com"),
    ('{"refresh_token": "r", "id_token": {"email": "admin@example.com"}}', "admin@example.com"),
    ('{"refresh_token": "r", "decoded_id_token": "{\\"email\\": \\"admin@example.com\\"}"}', "admin@example.com"),
    ('{"refresh_token": "r", "id_token": "eyJhbGciOi.jwt.sig"}', ""),     # an undecoded JWT: not read
    ('{"refresh_token": "r", "decoded_id_token": {"email": "not-an-address"}}', ""),
    ('{"refresh_token": "r"}', ""),
    ("tok", ""),                                                          # not JSON at all
    (None, ""),                                                           # no credential stored
])
def test_oauth_admin_email_reads_only_the_email_claim(empty_vault, stored, admin):
    if stored is not None:
        empty_vault.set("a.com", "oauth2", stored)
    assert empty_vault.oauth_admin_email("a.com") == admin


# --- _KeyringBackend.delete_password: only "no such item" is a no-op ------------------------------
#
# keyring's macOS backend (keyring/backends/macOS/__init__.py) turns every failed SecItemDelete into
# one PasswordDeleteError, raised `from` the Security-API error (keyring/backends/macOS/api.py) whose
# args are (OSStatus, text): an absent item and a denied or locked Keychain differ only in that cause.
# The fake raises exactly that; the macOS-only tests below hold it to the real backend's code.

_ITEM_NOT_FOUND = -25300            # errSecItemNotFound
_DENIED = -128                      # the operator clicked Deny on the Keychain prompt
_INTERACTION_NOT_ALLOWED = -25308   # a locked Keychain and no UI to unlock it
_AUTH_FAILED = -25293               # errSecAuthFailed

_FAILED_DELETES = [_DENIED, _INTERACTION_NOT_ALLOWED, _AUTH_FAILED]

_macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="keyring's macOS backend loads only on macOS")


class _api:
    """keyring.backends.macOS.api's errors: same names, hierarchy and args (that module loads only on macOS)."""

    class Error(Exception):
        pass

    class NotFound(Error):
        pass

    class KeychainDenied(Error):
        pass

    class SecAuthFailure(Error):
        pass


def _keychain_delete_error(status: int) -> PasswordDeleteError:
    """What keyring's macOS backend raises when SecItemDelete returns *status* (api.Error.raise_for_status)."""
    cause: _api.Error
    if status == _ITEM_NOT_FOUND:
        cause = _api.NotFound(status, "Item not found")
    elif status == _DENIED:
        cause = _api.KeychainDenied(status, "Keychain Access Denied")
    elif status in (_AUTH_FAILED, -67030):
        cause = _api.SecAuthFailure(
            status, "Security Auth Failure: make sure executable is signed with codesign util")
    else:
        cause = _api.Error(status, "Unknown Error")
    err = PasswordDeleteError(f"Can't delete password in keychain: {cause}")
    err.__cause__ = cause            # what `raise ... from e` sets
    err.__suppress_context__ = True
    return err


class _FakeKeyring:
    """Stands in for the `keyring` module over the macOS backend: a missing item reads as None, and a
    delete fails the way the real one does (absent -> errSecItemNotFound; listed in `deny` -> that status)."""

    def __init__(self) -> None:
        self.store: dict = {}
        self.deny: dict = {}            # (service, username) -> the OSStatus SecItemDelete returns

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        key = (service, username)
        status = self.deny.get(key, 0 if key in self.store else _ITEM_NOT_FOUND)
        if status:
            raise _keychain_delete_error(status)
        del self.store[key]


def _keyring_backend(keyring_module) -> _KeyringBackend:
    backend = _KeyringBackend()          # imports keyring; touches no Keychain
    backend._keyring = keyring_module
    return backend


def test_keyring_delete_of_an_absent_item_is_a_no_op():
    _keyring_backend(_FakeKeyring()).delete_password("gamgui:a.com", "client_secrets")


@pytest.mark.parametrize("status", _FAILED_DELETES)
def test_keyring_delete_that_fails_raises(status):
    fake = _FakeKeyring()
    fake.store[("gamgui:a.com", "oauth2")] = "tok"
    fake.deny[("gamgui:a.com", "oauth2")] = status
    with pytest.raises(PasswordDeleteError):
        _keyring_backend(fake).delete_password("gamgui:a.com", "oauth2")
    assert fake.store[("gamgui:a.com", "oauth2")] == "tok"


def test_keyring_delete_with_no_keyring_backend_raises():
    from keyring.backends import fail

    with pytest.raises(NoKeyringError):
        _keyring_backend(fail.Keyring()).delete_password("gamgui:a.com", "oauth2")


def test_clear_domain_skips_absent_items():
    fake = _FakeKeyring()
    v = SecretsVault(backend=_keyring_backend(fake), cache_ttl=0)
    v.set_all("a.com", {"oauth2": "tok", "oauth2service": "{}"})       # no client_secrets
    v.clear_domain("a.com")
    assert v.get_all("a.com") == {"client_secrets": None, "oauth2": None, "oauth2service": None}
    assert v.list_domains() == []


@pytest.mark.parametrize("status", _FAILED_DELETES)
def test_clear_domain_that_cannot_delete_raises_and_keeps_the_domain(status):
    fake = _FakeKeyring()
    v = SecretsVault(backend=_keyring_backend(fake), cache_ttl=300)
    v.set_all("a.com", {"oauth2": "tok", "oauth2service": "{}"})
    fake.deny[("gamgui:a.com", "oauth2service")] = status
    with pytest.raises(PasswordDeleteError):
        v.clear_domain("a.com")
    assert v.get("a.com", "oauth2service") == "{}"     # still in the Keychain, and still reported there
    assert v.list_domains() == ["a.com"]                # not dropped from the index while it is
    assert v.get("a.com", "oauth2") == "tok"            # oauth2service goes first: nothing else went


def test_clear_domain_deletes_every_credential_most_dangerous_first():
    from gamgui.core.secrets.vault import _DELETE_ORDER

    assert sorted(_DELETE_ORDER) == sorted(CREDENTIAL_NAMES) and _DELETE_ORDER[0] == "oauth2service"


class _PlainKeyring(_FakeKeyring):
    """A non-macOS backend: a failed delete is a PasswordDeleteError with no Security status behind it."""

    def delete_password(self, service, username):
        if (service, username) not in self.store or (service, username) in self.deny:
            raise PasswordDeleteError("Password not found")
        del self.store[(service, username)]


def test_another_backends_delete_of_an_absent_item_is_a_no_op():
    _keyring_backend(_PlainKeyring()).delete_password("gamgui:a.com", "client_secrets")


def test_another_backends_failed_delete_of_a_present_item_raises():
    fake = _PlainKeyring()
    fake.store[("gamgui:a.com", "oauth2")] = "tok"
    fake.deny[("gamgui:a.com", "oauth2")] = 1
    with pytest.raises(PasswordDeleteError):
        _keyring_backend(fake).delete_password("gamgui:a.com", "oauth2")


@_macos_only
@pytest.mark.parametrize("status", [_ITEM_NOT_FOUND, *_FAILED_DELETES])
def test_fake_raises_what_the_real_macos_backend_raises(status, monkeypatch):
    from keyring.backends import macOS

    monkeypatch.setattr(macOS.api, "SecItemDelete", lambda query: status)   # the OS call: no Keychain
    with pytest.raises(PasswordDeleteError) as real:
        macOS.Keyring().delete_password("gamgui:a.com", "oauth2")
    fake = _keychain_delete_error(status)
    assert type(real.value) is type(fake)
    assert str(real.value) == str(fake)
    assert type(real.value.__cause__).__name__ == type(fake.__cause__).__name__
    assert real.value.__cause__.args == fake.__cause__.args


@_macos_only
@pytest.mark.parametrize("status, raises", [(0, False), (_ITEM_NOT_FOUND, False),
                                            *[(s, True) for s in _FAILED_DELETES]])
def test_keyring_backend_over_the_real_macos_backend(status, raises, monkeypatch):
    from keyring.backends import macOS

    monkeypatch.setattr(macOS.api, "SecItemDelete", lambda query: status)   # the OS call: no Keychain
    backend = _keyring_backend(macOS.Keyring())
    if raises:
        with pytest.raises(PasswordDeleteError):
            backend.delete_password("gamgui:a.com", "oauth2")
    else:
        backend.delete_password("gamgui:a.com", "oauth2")
