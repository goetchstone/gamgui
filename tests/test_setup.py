from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gamgui.core import setup as setup_mod
from gamgui.core.gam.commands import EXPECTED_GAM_VERSION
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import FILENAMES, InMemoryBackend, SecretsVault
from gamgui.core.setup import SetupService, _extract_auth_url, _parse_check


@pytest.fixture
def bounded_home(tmp_path, monkeypatch):
    """Make ``tmp_path`` an allowed import root, the way the ``ctx`` fixture in test_setup_web does.

    An import is bounded to home plus ``$GAMCFGDIR`` (SetupService.allowed_roots), and pytest's
    tmp_path lives under /private/var/folders — genuinely outside home, hence correctly refused. So
    tmp_path *becomes* home for the test: the folders below it are then really under home, and the
    app's own data dir stays out of the operator's real one. $GAMCFGDIR starts unset so the default
    roots are what gets exercised.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GAMCFGDIR", raising=False)
    return tmp_path


def _write_config(d: Path, with_client_id: bool = True) -> None:
    (d / "client_secrets.json").write_text('{"installed": {"client_id": "x"}}')
    (d / "oauth2.txt").write_text("admin-refresh-token")
    svc = {"type": "service_account", "private_key": "k"}
    if with_client_id:
        svc["client_id"] = "123456789.apps.googleusercontent.com"
    (d / "oauth2service.json").write_text(json.dumps(svc))


def _svc(vault: SecretsVault, tmp_path: Path) -> SetupService:
    return SetupService(vault, GAMRunner(vault, gam_binary=tmp_path / "no-binary"))


# `inspect` reports what a folder holds, and it is bounded exactly like the import (an unbounded
# presence oracle for arbitrary paths is the bug H6 describes) — so these use `bounded_home`, which
# makes tmp_path the home dir and therefore an allowed root.
def test_inspect_detects_required_files(tmp_path, bounded_home):
    _write_config(tmp_path)
    insp = _svc(SecretsVault(InMemoryBackend()), tmp_path).inspect(tmp_path)
    assert insp.files["oauth2service"] is True
    assert insp.has_required is True


def test_inspect_missing_is_not_ready(tmp_path, bounded_home):
    (tmp_path / "client_secrets.json").write_text("{}")  # only the non-critical file
    insp = _svc(SecretsVault(InMemoryBackend()), tmp_path).inspect(tmp_path)
    assert insp.any_present is True
    assert insp.has_required is False


def test_import_dir_populates_vault(tmp_path, bounded_home):
    _write_config(tmp_path)
    vault = SecretsVault(InMemoryBackend())
    imported = _svc(vault, tmp_path).import_dir(tmp_path, "ex.com")
    assert set(imported) == set(FILENAMES.keys())
    assert vault.has_credentials("ex.com")
    assert "service_account" in (vault.get("ex.com", "oauth2service") or "")


def test_import_from_managed_dir_wipes_plaintext(tmp_path, monkeypatch, bounded_home):
    # Security: after credentials land in the Keychain, the managed staging files are destroyed —
    # no persistent plaintext DWD key on disk.
    _write_config(tmp_path)
    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    monkeypatch.setattr(svc, "managed_setup_dir", lambda: tmp_path)  # treat tmp_path as OUR dir
    imported = svc.import_dir(tmp_path, "ex.com")
    assert set(imported) == set(FILENAMES.keys())
    assert vault.has_credentials("ex.com")                       # safely in the Keychain
    for fname in FILENAMES.values():
        assert not (tmp_path / fname).exists()                   # …and wiped from disk


def test_import_from_managed_dir_wipes_plaintext_via_a_variant_spelling(tmp_path, monkeypatch, bounded_home):
    # Security regression guard: the wipe must key off filesystem identity, not the spelling. A
    # case-variant path to OUR OWN staging dir used to compare unequal (resolve() does not correct
    # case), so the import succeeded and the three plaintext credential files stayed on disk —
    # readable by any same-UID process with no Keychain prompt. Same dir, different spelling, still
    # wiped.
    _write_config(tmp_path)
    variant = str(tmp_path).swapcase()
    if not (os.path.exists(variant) and os.path.samefile(tmp_path, variant)):
        pytest.skip("case-sensitive volume: the variant spelling is not the same directory here")
    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    monkeypatch.setattr(svc, "managed_setup_dir", lambda: tmp_path)  # tmp_path is OUR staging dir
    imported = svc.import_dir(variant, "ex.com")                     # …named the other way
    assert set(imported) == set(FILENAMES.keys())
    assert vault.has_credentials("ex.com")
    for fname in FILENAMES.values():
        assert not (tmp_path / fname).exists()


def test_import_skips_a_credential_file_symlinked_out_of_bounds(tmp_path, bounded_home):
    # Bounding the folder is not enough: the bytes we read have to be in bounds too. `oauth2.txt` as
    # a symlink to /etc/passwd is, by path, a file in an allowed folder — its content must never
    # reach the Keychain.
    _write_config(tmp_path)
    secret = Path("/private/etc/passwd")
    if not secret.is_file():
        pytest.skip("no /private/etc/passwd to point at on this machine")
    (tmp_path / "oauth2.txt").unlink()
    (tmp_path / "oauth2.txt").symlink_to(secret)

    vault = SecretsVault(InMemoryBackend())
    imported = _svc(vault, tmp_path).import_dir(tmp_path, "ex.com")

    assert "oauth2" not in imported                       # skipped, not imported
    assert vault.get("ex.com", "oauth2") is None           # and nothing of /etc landed in the vault
    assert not vault.has_credentials("ex.com")             # oauth2 is required, so not ready
    assert secret.read_text(encoding="utf-8", errors="replace")  # target untouched and still there
    assert (tmp_path / "oauth2.txt").is_symlink()          # we did not follow it destructively
    # The in-bounds files alongside it still import — one bad symlink doesn't poison the folder.
    assert "oauth2service" in imported


# --- the bound has to survive a CONCURRENT attacker, not just a snapshot ----------------------
# Checking a PATH and then opening that PATH is a TOCTOU, and a winnable one: renaming a directory
# component into place in between made GamGUI read out-of-bounds bytes into the Keychain, and — worse
# — zero and unlink a file outside the roots that it had never imported. The import now pins the
# credentials directory to a descriptor, proves THAT is in bounds by walking up from it with
# openat(fd, ".."), and opens each credential file as a single O_NOFOLLOW component relative to it.
# The two helpers below fire the swap at the two instants that matter — a deterministic stand-in for
# winning the race, no 200k-attempt loop required.


def _after_the_bounds_check(monkeypatch, svc, action) -> None:
    """Swap while the folder is still just a name: after ``resolve_dir`` approved it, before the pin."""
    real = svc.resolve_dir

    def wrapper(path):
        p = real(path)
        action()
        return p

    monkeypatch.setattr(svc, "resolve_dir", wrapper)


def _after_the_pin(monkeypatch, action) -> None:
    """Swap once the descriptor is held — the window that used to matter and now provably doesn't."""
    real = setup_mod._pin_bounded_dir
    fired = []

    def wrapper(p, root_ids):
        dir_fd = real(p, root_ids)
        if not fired:
            fired.append(True)
            action()
        return dir_fd

    monkeypatch.setattr(setup_mod, "_pin_bounded_dir", wrapper)


def _outside_dir(factory, contents: str) -> Path:
    d = factory.mktemp("out-of-bounds")
    for fname in FILENAMES.values():
        (d / fname).write_text(contents)
    return d


def test_a_swap_between_the_bounds_check_and_the_read_lands_nothing(
    tmp_path, monkeypatch, tmp_path_factory
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GAMCFGDIR", raising=False)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _write_config(cfg)
    outside = _outside_dir(tmp_path_factory, "OUT-OF-BOUNDS")

    def win_the_race() -> None:
        cfg.rename(tmp_path / "cfg-real")                    # the approved dir steps aside…
        cfg.symlink_to(outside, target_is_directory=True)    # …a link out of bounds takes its name

    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    _after_the_bounds_check(monkeypatch, svc, win_the_race)

    # The pin lands on the attacker's directory, and walking up from that descriptor finds no root, so
    # the import refuses outright rather than reading anything.
    with pytest.raises(ValueError, match="can't be read right now"):
        svc.import_dir(cfg, "ex.com")
    assert not vault.has_credentials("ex.com")
    for name in FILENAMES:
        assert vault.get("ex.com", name) is None
    for fname in FILENAMES.values():                         # and the attacker's files are untouched
        assert (outside / fname).read_text() == "OUT-OF-BOUNDS"


def test_a_swap_after_the_pin_cannot_redirect_the_read(tmp_path, monkeypatch, tmp_path_factory):
    # Same swap, one instant later. A descriptor names an inode, so the reads still come from the
    # directory that was proven in bounds — the attacker's rename simply has nothing left to bite on.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GAMCFGDIR", raising=False)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _write_config(cfg)
    outside = _outside_dir(tmp_path_factory, "OUT-OF-BOUNDS")

    def win_the_race() -> None:
        cfg.rename(tmp_path / "cfg-real")
        cfg.symlink_to(outside, target_is_directory=True)

    _after_the_pin(monkeypatch, win_the_race)
    vault = SecretsVault(InMemoryBackend())
    imported = _svc(vault, tmp_path).import_dir(cfg, "ex.com")

    assert set(imported) == set(FILENAMES)
    assert vault.get("ex.com", "oauth2") == "admin-refresh-token"     # the real, in-bounds bytes
    for name in FILENAMES:
        assert "OUT-OF-BOUNDS" not in (vault.get("ex.com", name) or "")
    for fname in FILENAMES.values():
        assert (outside / fname).read_text() == "OUT-OF-BOUNDS"       # untouched


def test_a_symlinked_credential_file_is_not_followed_even_in_bounds(tmp_path, bounded_home):
    # O_NOFOLLOW closes the last door on the read: the fixed credential names are never followed as
    # links, so there is no target left to re-point between the check and the open. That the target
    # here is itself in bounds is deliberate — the tightening is unconditional, and no real GAM setup
    # symlinks these files.
    _write_config(tmp_path)
    target = tmp_path / "kept" / "oauth2.txt"
    target.parent.mkdir()
    target.write_text("in-bounds, but reached through a link")
    (tmp_path / "oauth2.txt").unlink()
    (tmp_path / "oauth2.txt").symlink_to(target)

    vault = SecretsVault(InMemoryBackend())
    imported = _svc(vault, tmp_path).import_dir(tmp_path, "ex.com")

    assert "oauth2" not in imported
    assert vault.get("ex.com", "oauth2") is None
    assert target.read_text() == "in-bounds, but reached through a link"   # left alone
    assert "oauth2service" in imported                       # the ordinary files still import


def test_the_wipe_never_destroys_a_file_it_did_not_import(tmp_path, monkeypatch, tmp_path_factory):
    # The destructive half of the same race: swapping our managed staging dir for a symlink to
    # somebody else's directory made GamGUI zero and unlink files there — files it had never imported,
    # outside the roots, while the real plaintext credentials survived elsewhere.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GAMCFGDIR", raising=False)
    staging = tmp_path / "staging"
    staging.mkdir()
    _write_config(staging)
    theirs = _outside_dir(tmp_path_factory, "NOT OURS")

    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    monkeypatch.setattr(svc, "managed_setup_dir", lambda: staging)

    def win_the_race(p, dir_fd=None) -> bool:
        staging.rename(tmp_path / "staging-real")
        staging.symlink_to(theirs, target_is_directory=True)
        return True                     # …and claim the dir is ours, so the wipe pass does run

    monkeypatch.setattr(svc, "_is_managed", win_the_race)
    imported = svc.import_dir(staging, "ex.com")

    assert set(imported) == set(FILENAMES)                   # the reads happened before the swap
    for fname in FILENAMES.values():
        assert (theirs / fname).is_file()                    # nothing of theirs unlinked…
        assert (theirs / fname).read_text() == "NOT OURS"    # …or zeroed
    for fname in FILENAMES.values():                         # our own staged files are what went
        assert not (tmp_path / "staging-real" / fname).exists()


def test_the_wipe_only_touches_the_inodes_it_imported(tmp_path, monkeypatch, bounded_home):
    # Same rule, one step tighter: a DIFFERENT file — in bounds, in our own staging dir — that takes
    # the credential name after the read is still not the file we imported, so it survives.
    staging = tmp_path / "staging"
    staging.mkdir()
    _write_config(staging)
    decoy = tmp_path / "decoy.txt"
    decoy.write_text("keep me")

    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    monkeypatch.setattr(svc, "managed_setup_dir", lambda: staging)

    def swap_then_claim(p, dir_fd=None) -> bool:
        os.replace(decoy, staging / "oauth2.txt")
        return True

    monkeypatch.setattr(svc, "_is_managed", swap_then_claim)
    assert set(svc.import_dir(staging, "ex.com")) == set(FILENAMES)

    assert (staging / "oauth2.txt").read_text() == "keep me"        # not zeroed, not unlinked
    assert not (staging / "oauth2service.json").exists()           # the real staged files went
    assert not (staging / "client_secrets.json").exists()


def test_an_unreadable_credential_file_is_skipped_not_raised(tmp_path, bounded_home):
    # chmod 000 used to escape as PermissionError -> HTTP 500. It is not something the operator can
    # fix from the wizard mid-import, so it is skipped and the rest still imports.
    _write_config(tmp_path)
    locked = tmp_path / "client_secrets.json"
    os.chmod(locked, 0o000)
    if os.access(locked, os.R_OK):
        pytest.skip("this user can read anything (root?), so chmod 000 proves nothing")
    try:
        vault = SecretsVault(InMemoryBackend())
        imported = _svc(vault, tmp_path).import_dir(tmp_path, "ex.com")
        assert "client_secrets" not in imported
        assert set(imported) == {"oauth2", "oauth2service"}
        assert vault.has_credentials("ex.com")
    finally:
        os.chmod(locked, 0o600)          # so pytest's tmp_path cleanup can remove it


def test_a_credential_file_that_vanishes_after_the_check_is_skipped(tmp_path, monkeypatch, bounded_home):
    # FileNotFoundError at the open (the file was there when the folder was checked) used to escape as
    # a 500 too. Skip it, import the rest.
    _write_config(tmp_path)

    def vanish() -> None:
        (tmp_path / "client_secrets.json").unlink()

    _after_the_pin(monkeypatch, vanish)
    vault = SecretsVault(InMemoryBackend())
    imported = _svc(vault, tmp_path).import_dir(tmp_path, "ex.com")
    assert "client_secrets" not in imported
    assert vault.has_credentials("ex.com")           # the others were unaffected


def test_import_from_user_dir_leaves_files(tmp_path, bounded_home):
    # A user-chosen dir (e.g. their own ~/.gam) is never wiped — we only clean our own staging dir.
    _write_config(tmp_path)
    svc = _svc(SecretsVault(InMemoryBackend()), tmp_path)  # managed dir is the real app dir, != tmp_path
    svc.import_dir(tmp_path, "ex.com")
    assert (tmp_path / "oauth2service.json").exists()


def test_dwd_details_extracts_client_id(tmp_path, bounded_home):
    _write_config(tmp_path)
    vault = SecretsVault(InMemoryBackend())
    svc = _svc(vault, tmp_path)
    svc.import_dir(tmp_path, "ex.com")
    assert svc.dwd_details("ex.com")["client_id"] == "123456789.apps.googleusercontent.com"


def test_setup_commands_shape(tmp_path):
    info = _svc(SecretsVault(InMemoryBackend()), tmp_path).setup_commands("admin@ex.com")
    cmds = info["commands"]
    assert "GAMCFGDIR" in info["env"]
    # `create project` takes the admin; oauth create / svcacct must NOT (that bug dropped oauth2.txt)
    assert any("create project admin@ex.com" in c for c in cmds)
    assert any(c.endswith("oauth create") for c in cmds)
    assert any(c.endswith("create svcacct") for c in cmds)
    # oauth create (writes oauth2.txt) must come before svcacct
    order = [i for i, c in enumerate(cmds) if "oauth create" in c or "create svcacct" in c]
    assert cmds[order[0]].endswith("oauth create")


def test_parse_check_pulls_pass_fail():
    out = "System time status: PASS\nSome scope: FAIL\nno colon line\n"
    lines = _parse_check(out)
    assert ("System time status", "PASS") in lines
    assert ("Some scope", "FAIL") in lines
    assert len(lines) == 2


def test_parse_check_handles_scope_table_format():
    # GAM's real `check serviceaccount` output: "<scope-url>   FAIL (n/m)"
    out = (
        "Domain-wide Delegation authentication:, User: a@e.com, Scopes: 2\n"
        "  https://mail.google.com/                         FAIL (1/2)\n"
        "  https://www.googleapis.com/auth/calendar         PASS (2/2)\n"
    )
    lines = _parse_check(out)
    assert ("https://mail.google.com/", "FAIL") in lines
    assert ("https://www.googleapis.com/auth/calendar", "PASS") in lines


def test_extract_auth_url():
    out = "Some scopes FAILED!\nplease go to:\n    https://gam-shortn.appspot.com/qhhmzr\nthen retry"
    assert _extract_auth_url(out) == "https://gam-shortn.appspot.com/qhhmzr"
    assert _extract_auth_url("all good, no link") == ""


async def test_engine_version(runner, vault):
    assert EXPECTED_GAM_VERSION in await SetupService(vault, runner).engine_version()


async def test_engine_version_warning_silent_when_matched(runner, vault):
    # The mock reports EXPECTED_GAM_VERSION -> no warning.
    assert await SetupService(vault, runner).engine_version_warning() == ""


async def test_engine_version_warning_on_mismatch(runner, vault, monkeypatch):
    svc = SetupService(vault, runner)

    async def fake_version() -> str:
        return "GAM 9.99.99 - mock"

    monkeypatch.setattr(svc, "engine_version", fake_version)
    warning = await svc.engine_version_warning()
    assert "9.99.99" in warning and EXPECTED_GAM_VERSION in warning  # fail-soft: warns, never blocks


async def test_verify_passes_with_mock(runner, vault, domain):
    result = await SetupService(vault, runner).verify(domain, "admin@example.com")
    assert result.ok is True
    assert any(status == "PASS" for _, status in result.lines)


async def test_verify_without_credentials(runner):
    empty = SecretsVault(InMemoryBackend())
    result = await SetupService(empty, runner).verify("nope.com", "a@nope.com")
    assert result.ok is False
    assert "No credentials" in result.summary
