from __future__ import annotations

import json
import os
import pwd
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core import setup as setup_mod
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.setup import SetupService, _root_is_sane
from gamgui.web.server import AppState, create_app

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    # An import is bounded to the home dir plus $GAMCFGDIR (see SetupService.allowed_roots), so make
    # tmp_path *be* home for the duration: the folders these tests build are then genuinely "under
    # home", and the app's own data dir (~/Library/Application Support/GamGUI) stays out of the real
    # one. $GAMCFGDIR starts unset so the default roots are exercised.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GAMCFGDIR", raising=False)
    vault = SecretsVault(InMemoryBackend())
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    state = AppState(vault=vault, runner=runner, audit_domain="", connector=None, token="t")
    client = TestClient(create_app(state))
    client.get("/?token=t")  # establish the token cookie
    return client, tmp_path, vault, state


def test_setup_page_renders(ctx):
    client = ctx[0]
    r = client.get("/setup")
    assert r.status_code == 200
    assert "Connect Google Workspace" in r.text


def test_import_shows_dwd_and_stores_creds(ctx):
    client, base, vault, _ = ctx
    cfg = base / "cfg"
    cfg.mkdir()
    (cfg / "oauth2.txt").write_text("tok")
    (cfg / "oauth2service.json").write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))
    r = client.post("/setup/import", data={"domain": "ex.com", "admin": "a@ex.com", "config_dir": str(cfg)})
    assert r.status_code == 200
    assert "CID.apps" in r.text                 # DWD client id surfaced
    assert "copyEl(" in r.text                   # client id has a copy button
    assert vault.has_credentials("ex.com")


def test_import_requires_fields(ctx):
    client = ctx[0]
    r = client.post("/setup/import", data={"domain": "", "admin": "", "config_dir": ""})
    assert "domain" in r.text.lower()


def test_import_leaves_a_user_chosen_dirs_plaintext_alone(ctx):
    # The post-import wipe applies ONLY to our own managed staging dir. A folder the operator
    # picked — their own GAM install — keeps its files; we don't destroy someone else's config.
    client, base, vault, _ = ctx
    cfg = base / "their-gam"
    cfg.mkdir()
    (cfg / "oauth2.txt").write_text("tok")
    (cfg / "oauth2service.json").write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))
    r = client.post("/setup/import", data={"domain": "ex.com", "admin": "a@ex.com", "config_dir": str(cfg)})
    assert r.status_code == 200
    assert vault.has_credentials("ex.com")
    assert (cfg / "oauth2.txt").read_text() == "tok"
    assert (cfg / "oauth2service.json").is_file()


# --- the config_dir a form supplies has to be a real directory, inside bounded roots ----------
# The operator choosing the folder is the feature, so there's no allow-list of exact paths — but the
# folder has to sit under the home dir or $GAMCFGDIR, and it has to be an existing directory: a path
# that isn't is refused with a message, not reported as an empty import.


def _svc(state) -> SetupService:
    return SetupService(state.vault, state.runner)


def test_import_rejects_nonexistent_dir(ctx):
    _, base, vault, state = ctx
    with pytest.raises(ValueError, match="No such folder"):
        _svc(state).import_dir(base / "typo-not-here", "ex.com")
    assert not vault.has_credentials("ex.com")


def test_import_rejects_a_file(ctx):
    _, base, vault, state = ctx
    f = base / "oauth2service.json"
    f.write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))
    with pytest.raises(ValueError, match="not a folder"):
        _svc(state).import_dir(f, "ex.com")     # the file itself, not its parent
    assert not vault.has_credentials("ex.com")
    assert f.is_file()                          # and nothing of theirs was touched


def test_import_rejects_blank_dir(ctx):
    _, _, vault, state = ctx
    with pytest.raises(ValueError, match="Choose the folder"):
        _svc(state).import_dir("   ", "ex.com")
    assert not vault.has_credentials("ex.com")


# The three above call the service directly. These go over HTTP, because a rejected path has to
# reach the operator as a message they can act on — raising into the route turns a typo into a 500.
def test_import_route_reports_a_bad_path_instead_of_crashing(ctx):
    client, base, vault, _ = ctx
    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(base / "typo-not-here")})
    assert r.status_code == 200
    assert "No such folder" in r.text
    assert not vault.has_credentials("ex.com")


def test_import_route_reports_a_file_instead_of_crashing(ctx):
    client, base, vault, _ = ctx
    f = base / "oauth2.txt"
    f.write_text("tok")
    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(f)})
    assert r.status_code == 200
    assert "not a folder" in r.text
    assert not vault.has_credentials("ex.com")
    assert f.is_file()


def test_resolve_dir_accepts_a_real_dir_and_expands_home(ctx):
    _, base, _, state = ctx
    nested = base / "cfg" / "sub" / ".."
    (base / "cfg" / "sub").mkdir(parents=True)
    assert _svc(state).resolve_dir(nested) == (base / "cfg").resolve()
    assert _svc(state).resolve_dir("~").is_dir()


# --- bounded roots: home + $GAMCFGDIR ---------------------------------------------------------
# `import_dir` reads credential files from an operator-named folder, so the folder is only allowed to
# be inside the home dir or whatever $GAMCFGDIR points at. Everything candidate_dirs() offers is one
# of those; nothing else is.


def _write_creds(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "oauth2.txt").write_text("tok")
    (d / "oauth2service.json").write_text(json.dumps({"client_id": "CID.apps", "type": "service_account"}))


def test_import_accepts_a_dir_under_home(ctx):
    _, base, vault, state = ctx           # base is home for this test (see the ctx fixture)
    cfg = base / "elsewhere-under-home" / "gam"
    _write_creds(cfg)
    assert _svc(state).import_dir(cfg, "ex.com")
    assert vault.has_credentials("ex.com")


def test_import_accepts_the_managed_setup_dir(ctx):
    # The dir our own fresh-setup flow writes into has to keep importing — it lives under
    # ~/Library/Application Support, i.e. inside home.
    _, _, vault, state = ctx
    svc = _svc(state)
    managed = svc.managed_setup_dir()
    _write_creds(managed)
    assert svc.resolve_dir(managed) == managed.resolve()
    assert svc.import_dir(managed, "ex.com")
    assert vault.has_credentials("ex.com")


def test_import_rejects_a_dir_outside_home_until_gamcfgdir_points_at_it(ctx, monkeypatch, tmp_path_factory):
    # The removable-volume case, and the whole point of the design: a service-account key kept on an
    # encrypted volume is off-limits until the operator names it via $GAMCFGDIR.
    _, _, vault, state = ctx
    volume = tmp_path_factory.mktemp("encrypted-volume")   # a sibling of home, not under it
    _write_creds(volume)

    with pytest.raises(ValueError, match="outside the places"):
        _svc(state).import_dir(volume, "ex.com")
    assert not vault.has_credentials("ex.com")

    monkeypatch.setenv("GAMCFGDIR", str(volume))
    assert _svc(state).import_dir(volume, "ex.com")
    assert vault.has_credentials("ex.com")
    assert (volume / "oauth2.txt").read_text() == "tok"    # not ours to wipe


def test_import_rejects_traversal_that_escapes_home(ctx, tmp_path_factory):
    # Starts under home, climbs out. Resolving before the bounds check is what catches it.
    _, base, vault, state = ctx
    outside = tmp_path_factory.mktemp("not-home")
    _write_creds(outside)
    sneaky = base / ".." / outside.name
    with pytest.raises(ValueError, match="outside the places"):
        _svc(state).import_dir(sneaky, "ex.com")
    assert not vault.has_credentials("ex.com")


def test_import_rejects_a_symlink_under_home_pointing_outside(ctx, tmp_path_factory):
    _, base, vault, state = ctx
    outside = tmp_path_factory.mktemp("off-limits")
    _write_creds(outside)
    link = base / "shortcut"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the places"):
        _svc(state).import_dir(link, "ex.com")
    assert not vault.has_credentials("ex.com")


def test_blank_gamcfgdir_does_not_widen_the_roots(ctx, monkeypatch, tmp_path_factory):
    # A blank/whitespace value must be ignored, not resolved into "/" (which would allow everything).
    _, _, vault, state = ctx
    monkeypatch.setenv("GAMCFGDIR", "   ")
    svc = _svc(state)
    assert all(str(r) != "/" for r in svc.allowed_roots())
    outside = tmp_path_factory.mktemp("still-outside")
    with pytest.raises(ValueError, match="outside the places"):
        svc.import_dir(outside, "ex.com")
    with pytest.raises(ValueError, match="outside the places"):
        svc.resolve_dir("/etc")
    assert not vault.has_credentials("ex.com")


def test_case_variant_spelling_is_not_spuriously_rejected(ctx):
    # macOS volumes usually fold case, and resolve() does NOT correct the case that was typed: a
    # folder entered as /users/x/gam is the same folder as /Users/x/gam and must import.
    _, base, vault, state = ctx
    cfg = base / "gam-cfg"
    _write_creds(cfg)
    swapped = str(cfg).swapcase()
    if not (os.path.exists(swapped) and os.path.samefile(cfg, swapped)):
        pytest.skip("case-sensitive volume: the variant spelling is not the same directory here")
    assert _svc(state).import_dir(swapped, "ex.com")
    assert vault.has_credentials("ex.com")


def test_unicode_normalization_variant_of_home_is_accepted(ctx, monkeypatch):
    # An accented home folder ("josé") spelled in the other normalization form (NFD vs NFC) is the
    # SAME directory — os.path.samefile says so — so it must not lock the operator out of their own
    # home. Byte-comparing the strings gets this wrong; comparing (st_dev, st_ino) gets it right.
    _, base, vault, state = ctx
    nfc, nfd = unicodedata.normalize("NFC", "josé"), unicodedata.normalize("NFD", "josé")
    home_nfc, home_nfd = base / nfc, base / nfd
    home_nfc.mkdir()
    if not (os.path.exists(home_nfd) and os.path.samefile(home_nfc, home_nfd)):
        pytest.skip("this volume distinguishes NFC from NFD: the two spellings are not one dir")
    monkeypatch.setenv("HOME", str(home_nfd))    # home, decomposed
    cfg = home_nfc / "gam"                       # a folder under it, composed
    _write_creds(cfg)
    assert _svc(state).import_dir(cfg, "ex.com")
    assert vault.has_credentials("ex.com")


def test_data_volume_spelling_of_home_is_accepted(ctx):
    # /Users is an APFS *firmlink*, not a symlink, so resolve() does not collapse
    # /System/Volumes/Data/Users/x to /Users/x — but it IS home, and must import.
    _, base, vault, state = ctx
    data = Path("/System/Volumes/Data")
    if not data.is_dir():
        pytest.skip("no /System/Volumes/Data on this machine")
    firm_home = data / str(base).lstrip("/")
    if not (firm_home.exists() and os.path.samefile(firm_home, base)):
        pytest.skip("home is not reachable through the data volume here")
    cfg = base / "cfg-under-firmlinked-home"
    _write_creds(cfg)
    assert _svc(state).import_dir(data / str(cfg).lstrip("/"), "ex.com")
    assert vault.has_credentials("ex.com")


def test_overbroad_gamcfgdir_does_not_expose_system_config(ctx, monkeypatch):
    # $GAMCFGDIR is operator-supplied and a few values quietly widen the bound to the whole machine:
    # "/", "/private" (where /etc lives), "/etc/.." and "/System/Volumes/Data" (the data volume,
    # which reaches /private through a firmlink). Each must be dropped quietly — a bad $GAMCFGDIR
    # simply doesn't widen the roots, and home keeps working.
    _, base, vault, state = ctx
    svc = _svc(state)
    home = Path.home().resolve()
    cfg = base / "still-fine"
    _write_creds(cfg)

    spellings_of_etc = ["/etc", "/private/etc"]
    if Path("/System/Volumes/Data/private/etc").exists():
        spellings_of_etc.append("/System/Volumes/Data/private/etc")

    for bad in ("/", "/private", "/etc/..", "/System/Volumes/Data"):
        monkeypatch.setenv("GAMCFGDIR", bad)
        assert svc.allowed_roots() == [home]        # nothing was added
        for etc in spellings_of_etc:
            with pytest.raises(ValueError, match="outside the places"):
                svc.resolve_dir(etc)
        assert svc.import_dir(cfg, "ex.com")        # …and home still imports
    assert vault.has_credentials("ex.com")


# --- what makes a $GAMCFGDIR root acceptable: an existing DIRECTORY, above nothing that matters ---
# The old rule only asked "does this root put /etc in reach?", and only along the short path — so
# /System and /System/Volumes sailed through (they reach /etc through the data-volume firmlink),
# a FILE could be a root (its own inode matched itself, and a credential symlink to it imported its
# bytes), and /Users, /var and /Library handed over another user's home and /private/var/root.


def test_gamcfgdir_must_be_an_existing_directory(ctx, monkeypatch, tmp_path_factory):
    _, base, vault, state = ctx
    svc = _svc(state)
    home = Path.home().resolve()
    volume = tmp_path_factory.mktemp("volume")
    keyfile = volume / "not-a-folder.txt"
    keyfile.write_text("secret bytes")

    monkeypatch.setenv("GAMCFGDIR", str(keyfile))
    assert svc.allowed_roots() == [home]            # a file never becomes a root…
    with pytest.raises(ValueError, match="outside the places"):
        svc.resolve_dir(volume)                     # …so its folder is still off-limits
    with pytest.raises(ValueError, match="outside the places"):
        svc.resolve_dir(keyfile)                     # nor is the file itself importable

    # …and the route that used to work: a credential symlink pointing at the file-as-root.
    cfg = base / "cfg"
    _write_creds(cfg)
    (cfg / "oauth2.txt").unlink()
    (cfg / "oauth2.txt").symlink_to(keyfile)
    imported = svc.import_dir(cfg, "ex.com")
    assert "oauth2" not in imported
    assert vault.get("ex.com", "oauth2") is None
    assert keyfile.read_text() == "secret bytes"    # and it was not touched

    if Path("/etc/passwd").is_file():               # the real thing, same rule
        monkeypatch.setenv("GAMCFGDIR", "/etc/passwd")
        assert svc.allowed_roots() == [home]


@pytest.mark.parametrize(
    "bad", ["/", "/private", "/etc/..", "/etc", "/var", "/usr", "/Library", "/dev",
            "/System", "/System/Volumes", "/System/Volumes/Data"],
)
def test_overbroad_gamcfgdir_values_are_skipped_quietly(ctx, monkeypatch, bad):
    _, base, vault, state = ctx
    svc = _svc(state)
    home = Path.home().resolve()
    if not Path(bad).is_dir():
        pytest.skip(f"no {bad} on this machine")
    cfg = base / "still-fine"
    _write_creds(cfg)

    monkeypatch.setenv("GAMCFGDIR", bad)
    assert svc.allowed_roots() == [home]            # quiet skip: the bound is not widened
    for etc in ("/etc", "/private/etc", "/System/Volumes/Data/private/etc"):
        if Path(etc).exists():                      # every spelling of /etc stays out of reach
            with pytest.raises(ValueError, match="outside the places"):
                svc.resolve_dir(etc)
    assert svc.import_dir(cfg, "ex.com")            # …and home still imports
    assert vault.has_credentials("ex.com")


def test_gamcfgdir_above_home_is_skipped(ctx, monkeypatch, tmp_path_factory):
    # The rule that does most of the work: a root must not be an ANCESTOR of home. Here $GAMCFGDIR is
    # one level up from home — it holds credentials, but allowing it would hand over every sibling
    # home directory, which is what "/Users" does on a real Mac.
    _, _, vault, state = ctx
    outer = tmp_path_factory.mktemp("Users-like")
    home = outer / "me"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GAMCFGDIR", str(outer))
    _write_creds(outer)
    svc = _svc(state)

    assert svc.allowed_roots() == [home.resolve()]
    with pytest.raises(ValueError, match="outside the places"):
        svc.import_dir(outer, "ex.com")
    assert not vault.has_credentials("ex.com")


def test_gamcfgdir_of_users_is_skipped_because_home_sits_under_it(ctx, monkeypatch):
    # The same rule against the real thing: on a stock Mac $HOME is /Users/<me>, so /Users is an
    # ancestor of home and must not become a root — that is another operator's home.
    _, _, _, state = ctx
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if Path("/Users") not in real_home.parents:
        pytest.skip("this account's home does not live under /Users")
    monkeypatch.setenv("HOME", str(real_home))       # the operator's actual home, as in production
    monkeypatch.setenv("GAMCFGDIR", "/Users")
    svc = _svc(state)
    assert svc.allowed_roots() == [real_home]
    with pytest.raises(ValueError, match="outside the places"):
        svc.resolve_dir("/Users/definitely-not-me")


def test_a_mounted_volume_style_root_outside_home_still_works(ctx, monkeypatch, tmp_path_factory):
    # The case the escape hatch exists for, and the thing the sanity rules must NOT break: a
    # service-account key on an encrypted stick at /Volumes/GAMKEY is above neither home nor any
    # system directory. A mount point is just a directory to every check here, so a real directory
    # outside home stands in for one.
    _, _, vault, state = ctx
    svc = _svc(state)
    volume = tmp_path_factory.mktemp("GAMKEY")
    _write_creds(volume)

    monkeypatch.setenv("GAMCFGDIR", str(volume))
    assert _root_is_sane(volume.resolve(), Path.home().resolve()) is True
    assert volume.resolve() in svc.allowed_roots()
    assert svc.import_dir(volume, "ex.com")
    assert vault.has_credentials("ex.com")
    assert (volume / "oauth2.txt").read_text() == "tok"          # not ours to wipe
    # …and the wizard offers it, consistently with the import accepting it
    assert any(Path(d.path).resolve() == volume.resolve() for d in svc.candidate_dirs())


# --- home is inherently allowed: the roots must never come back empty -------------------------


def test_home_is_allowed_even_when_the_sanity_rules_would_drop_it(ctx, monkeypatch, tmp_path_factory):
    # In a launchd/daemon context $HOME can be "/" — a value rule (b) drops on sight. Applying the
    # sanity rules to home as well emptied the root list, and an empty root list refuses EVERY path,
    # including our own managed staging dir: the wizard could no longer import the credentials it had
    # just walked the operator through creating.
    _, _, _, state = ctx
    svc = _svc(state)
    monkeypatch.setenv("HOME", "/")
    assert not _root_is_sane(Path("/"), Path("/"))            # the rules would indeed drop it…
    assert svc.allowed_roots() == [Path("/")]                 # …but home is a root regardless
    anywhere = tmp_path_factory.mktemp("under-the-root")
    assert svc.resolve_dir(anywhere) == anywhere.resolve()    # so nothing is refused outright


def test_no_determinable_home_reports_instead_of_crashing(ctx, monkeypatch):
    # Path.home() raises RuntimeError when neither $HOME nor the password database can answer. That
    # used to escape as an HTTP 500 through three different paths.
    client, base, vault, state = ctx

    def no_home(*_args, **_kwargs):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    svc = _svc(state)
    assert svc.allowed_roots() == []                         # nothing is in bounds any more…
    with pytest.raises(ValueError, match="outside the places"):
        svc.import_dir(base, "ex.com")                       # …but that is a ValueError, not a crash
    assert svc.candidate_dirs() == []

    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(base)})
    assert r.status_code == 200 and "outside the places" in r.text
    assert client.get("/setup").status_code == 200            # and the wizard still renders
    assert not vault.has_credentials("ex.com")


def test_a_tilde_path_with_no_home_is_a_message_not_a_500(ctx, monkeypatch):
    # The other RuntimeError source: expanding "~" with no determinable home. Stubbed at
    # Path.expanduser, which is where pathlib raises it.
    client, _, _, state = ctx

    def no_home(*_args, **_kwargs):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", no_home)
    with pytest.raises(ValueError, match="can't be expanded"):
        _svc(state).import_dir("~/.gam", "ex.com")
    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": "~/.gam"})
    assert r.status_code == 200 and "be expanded" in r.text       # (the apostrophe is escaped)


# --- the import route renders filesystem trouble instead of a 500 -----------------------------


def test_an_unreadable_credential_file_does_not_500(ctx):
    client, base, vault, _ = ctx
    cfg = base / "cfg"
    _write_creds(cfg)
    locked = cfg / "client_secrets.json"
    locked.write_text("{}")
    os.chmod(locked, 0o000)
    if os.access(locked, os.R_OK):
        pytest.skip("this user can read anything (root?), so chmod 000 proves nothing")
    try:
        r = client.post("/setup/import", data={
            "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(cfg)})
        assert r.status_code == 200
        assert vault.has_credentials("ex.com")                # the readable files still imported
        assert vault.get("ex.com", "client_secrets") is None
    finally:
        os.chmod(locked, 0o600)


@pytest.mark.parametrize(
    "exc, fragment",
    [
        (PermissionError(13, "Permission denied"), "Permission denied"),
        (OSError(5, "Input/output error"), "Input/output error"),
        (RuntimeError("Could not determine home directory."), "home directory"),
    ],
)
def test_import_route_renders_filesystem_and_home_failures(ctx, monkeypatch, exc, fragment):
    client, base, vault, _ = ctx

    def boom(self, path, domain):
        raise exc

    monkeypatch.setattr(SetupService, "import_dir", boom)
    r = client.post("/setup/import", data={
        "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(base)})
    assert r.status_code == 200
    assert fragment in r.text
    assert not vault.has_credentials("ex.com")


def test_import_route_still_surfaces_programming_errors(ctx, monkeypatch):
    # The handler is deliberately narrow: a bug is not an operator-facing condition and must not be
    # dressed up as one.
    client, base, _, _ = ctx

    def boom(self, path, domain):
        raise TypeError("this is a bug, not a bad path")

    monkeypatch.setattr(SetupService, "import_dir", boom)
    with pytest.raises(TypeError):
        client.post("/setup/import", data={
            "domain": "ex.com", "admin": "a@ex.com", "config_dir": str(base)})


# --- inspect() is bounded too: no presence oracle, no offers the import refuses ---------------


def test_inspect_is_bounded_like_the_import(ctx, tmp_path_factory):
    # inspect() answered "is there an oauth2service.json here?" for ANY path while import_dir refused
    # the same path — an unbounded presence oracle for arbitrary locations.
    _, base, _, state = ctx
    svc = _svc(state)
    outside = tmp_path_factory.mktemp("not-in-bounds")
    _write_creds(outside)
    assert svc.inspect(outside).any_present is False
    assert svc.inspect("/etc").any_present is False
    inside = base / "cfg"
    _write_creds(inside)
    assert svc.inspect(inside).has_required is True          # in bounds, and still honest


def test_candidate_dirs_never_offers_what_the_import_would_refuse(ctx, monkeypatch, tmp_path_factory):
    # $GAMCFGDIR one level above home: it holds credentials, but as a root it is an ancestor of home,
    # so it is dropped — and the wizard must not put an Import button on it either.
    _, _, _, state = ctx
    outer = tmp_path_factory.mktemp("above-home")
    home = outer / "me"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GAMCFGDIR", str(outer))
    _write_creds(outer)
    svc = _svc(state)

    assert svc.inspect(outer).any_present is False
    assert [d.path for d in svc.candidate_dirs()] == []
    with pytest.raises(ValueError, match="outside the places"):
        svc.import_dir(outer, "ex.com")


def test_every_offered_candidate_dir_actually_imports(ctx, monkeypatch, tmp_path_factory):
    # The consistency property itself, over the whole offer list.
    _, _, vault, state = ctx
    volume = tmp_path_factory.mktemp("GAMKEY")
    _write_creds(volume)
    monkeypatch.setenv("GAMCFGDIR", str(volume))
    svc = _svc(state)
    _write_creds(Path.home() / ".gam")
    _write_creds(svc.managed_setup_dir())

    offered = svc.candidate_dirs()
    assert len(offered) == 3                     # $GAMCFGDIR, ~/.gam, our staging dir
    for d in offered:
        assert svc.import_dir(d.path, "ex.com"), d.path
    assert vault.has_credentials("ex.com")


def test_out_of_bounds_message_is_truthful_about_gamcfgdir(ctx):
    # The old message said "point $GAMCFGDIR at it and relaunch GamGUI" — but the .app has no
    # LSEnvironment in gamgui.spec, so an app launched from Finder never sees shell variables. Lead
    # with the advice that always works, and keep the caveat attached to the other one.
    _, _, _, state = ctx
    with pytest.raises(ValueError) as exc:
        _svc(state).resolve_dir("/etc")
    msg = str(exc.value)
    assert "home folder" in msg
    assert msg.index("home folder") < msg.index("GAMCFGDIR")   # the working advice comes first
    assert "same terminal session" in msg and "Finder" in msg


def test_verify_activates_connector(ctx):
    client, _, vault, state = ctx
    vault.set_all("ex.com", {"oauth2": "tok", "oauth2service": json.dumps({"client_id": "x"})})
    r = client.post("/setup/verify", data={"domain": "ex.com", "admin": "a@ex.com"})
    assert r.status_code == 200
    assert "connected" in r.text.lower()
    assert state.connector is not None and state.audit_domain == "ex.com"


def test_fresh_shows_commands(ctx):
    client = ctx[0]
    r = client.post("/setup/fresh", data={"domain": "ex.com", "admin": "a@ex.com"})
    assert "create project" in r.text
    assert "GAMCFGDIR" in r.text
    assert "copyEl(" in r.text  # each command has a copy button
