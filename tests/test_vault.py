from __future__ import annotations

import pytest

from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault


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
