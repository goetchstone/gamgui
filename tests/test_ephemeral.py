from __future__ import annotations

import os

import pytest

from gamgui.core.secrets.ephemeral import EphemeralConfig
from gamgui.core.secrets.vault import FILENAMES, InMemoryBackend, SecretsVault


def test_materialize_writes_files_with_restrictive_perms(vault, domain, tmp_path):
    with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
        assert (os.stat(cfgdir).st_mode & 0o777) == 0o700
        for fname in FILENAMES.values():
            f = cfgdir / fname
            assert f.exists(), f"{fname} not materialized"
            assert (os.stat(f).st_mode & 0o777) == 0o600
        saved = cfgdir
    # dir wiped after the block
    assert not saved.exists()


def test_missing_required_credentials_raises(tmp_path):
    empty = SecretsVault(backend=InMemoryBackend())
    with pytest.raises(PermissionError):
        with EphemeralConfig(empty, "x.com", base_dir=tmp_path):
            pass


def test_oauth2_token_write_back(vault, domain, tmp_path):
    # Simulate GAM refreshing the token by rewriting oauth2.txt inside the block.
    with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
        (cfgdir / FILENAMES["oauth2"]).write_text("refreshed-value", encoding="utf-8")
    assert vault.get(domain, "oauth2") == "refreshed-value"


def test_sweep_stale_configs(tmp_path):
    from gamgui.core.secrets.ephemeral import sweep_stale_configs

    old = tmp_path / "gamcfg-old"
    old.mkdir()
    fresh = tmp_path / "gamcfg-fresh"
    fresh.mkdir()
    other = tmp_path / "keep-me"
    other.mkdir()
    past = os.stat(old).st_atime - 3600
    os.utime(old, (past, past))  # backdate so it looks orphaned

    removed = sweep_stale_configs(base_dir=tmp_path, max_age_seconds=600)
    assert removed == 1
    assert not old.exists()      # orphaned -> swept
    assert fresh.exists()        # too recent -> kept
    assert other.exists()        # not a gamcfg-* dir -> untouched


def test_dir_wiped_even_on_exception(vault, domain, tmp_path):
    captured = {}
    with pytest.raises(RuntimeError):
        with EphemeralConfig(vault, domain, base_dir=tmp_path) as cfgdir:
            captured["path"] = cfgdir
            assert cfgdir.exists()
            raise RuntimeError("boom")
    assert not captured["path"].exists()
