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
