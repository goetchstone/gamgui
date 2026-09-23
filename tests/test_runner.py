from __future__ import annotations

import pytest

from gamgui.core.gam.commands import EXPECTED_GAM_VERSION, GAMCommands
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.runner import GAMRunner


async def test_version(runner):
    assert EXPECTED_GAM_VERSION in await runner.version()


async def test_run_authenticated_reads_users(runner, domain):
    out = await runner.run_authenticated(domain, GAMCommands.print_users())
    assert "alice@example.com" in out


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("notfound", GAMErrorKind.NOT_FOUND),
        ("scope", GAMErrorKind.SCOPE_MISSING),
        ("rate", GAMErrorKind.RATE_LIMITED),
        ("auth", GAMErrorKind.AUTH_EXPIRED),
    ],
)
async def test_error_classification(runner, domain, kind, expected):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, ["MOCKFAIL", kind])
    assert ei.value.kind == expected
    assert ei.value.remediation  # human guidance present


async def test_missing_binary_raises(vault, tmp_path):
    r = GAMRunner(vault=vault, gam_binary=tmp_path / "does-not-exist", base_dir=tmp_path)
    assert r.binary_exists() is False
    with pytest.raises(RuntimeError):
        await r.version()


async def test_timeout_kills_gam_wipes_the_config_and_frees_the_write_lock(runner, domain, tmp_path):
    # A stalled API call: the runner must kill the process (not leave it holding plaintext
    # credentials), still wipe the ephemeral GAMCFGDIR, and not strand the serialize lock.
    import os

    pidfile = tmp_path / "gam.pid"
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, ["MOCKSLEEP", "30", str(pidfile)], timeout=0.5, serialize=True)
    assert ei.value.kind is GAMErrorKind.TIMEOUT and ei.value.exit_code is None
    assert "timed out after 0.5s and was stopped" in str(ei.value)   # says it was cut off, not just failed
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)          # killed and reaped
    assert list(tmp_path.glob("gamcfg-*")) == []       # credentials wiped despite the timeout
    await runner.run_authenticated(domain, GAMCommands.signout_user("a@e.com"), serialize=True)


async def test_cancel_kills_gam_wipes_the_config_and_frees_the_write_lock(runner, domain, tmp_path):
    # Quitting the app cancels an in-flight job task (server._lifespan): CancelledError is a
    # BaseException, so it skipped the timeout's kill and left gam running with the credentials it
    # had already loaded while the dir was wiped under it (review F3).
    import asyncio
    import os

    from .helpers import wait_until

    pidfile = tmp_path / "gam.pid"
    task = asyncio.create_task(runner.run_authenticated(
        domain, ["MOCKSLEEP", "30", str(pidfile)], serialize=True))
    await wait_until(lambda: pidfile.exists() and pidfile.read_text().strip())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task                                     # the cancellation still propagates
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)          # killed and reaped, not left running
    assert list(tmp_path.glob("gamcfg-*")) == []       # credentials wiped (invariant #4)
    await runner.run_authenticated(domain, GAMCommands.signout_user("a@e.com"), serialize=True)


async def test_oauth_token_write_back_through_a_real_run(runner, vault, domain, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_REFRESH", "1")
    before = vault.get(domain, "oauth2")
    await runner.run_authenticated(domain, GAMCommands.set_suspended("a@e.com", False), serialize=True)
    after = vault.get(domain, "oauth2")
    assert after != before
    assert "refreshed" in after


# Launch-environment variables that must never reach the process holding the plaintext credentials.
HOSTILE_ENV = {
    "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
    "DYLD_LIBRARY_PATH": "/tmp/evil",
    "PYTHONPATH": "/tmp/evil",
    "PYTHONHOME": "/tmp/evil",
    "_MEIPASS2": "/tmp/evil",
    "GAMGUI_GAM_BINARY": "/tmp/evil-gam",
    "GAMCFGSECTION": "other",
    "GAM_CSV_OUTPUT_QUOTE_CHAR": "'",
    "SSL_CERT_FILE": "/tmp/evil.pem",
    "GAMCFGDIR": "/tmp/not-the-ephemeral-dir",
}

# What gam may inherit, written out here rather than read from runner.py: a test that computed its
# expectation from ENV_ALLOWLIST passed an edit adding PYTHONPATH, DYLD_*, GAMCFGSECTION or
# SSL_CERT_FILE to the allowlist. A new variable GAM genuinely needs is added in both places.
EXPECTED_PASSTHROUGH = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
}
SET_BY_THE_RUNNER = {"GAMCFGDIR", "GAM_NO_UPDATE_CHECK"}
EXPECTED_MOCK_ONLY = {"GAM_MOCK_FIXTURES", "GAM_MOCK_REFRESH", "GAM_MOCK_ARGV_LOG", "GAM_MOCK_STATE"}


def test_the_env_allowlist_is_the_reviewed_one():
    from gamgui.core.gam.runner import ENV_ALLOWLIST, MOCK_ENV

    assert set(ENV_ALLOWLIST) == EXPECTED_PASSTHROUGH
    assert set(MOCK_ENV) == EXPECTED_MOCK_ONLY


async def _child_env(vault, tmp_path, monkeypatch, frozen, child, argv):
    """Run ``child`` as gam under a hostile launch environment; return (result, the env it saw)."""
    import sys

    for k, v in HOSTILE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("GAM_MOCK_FIXTURES", "/tmp/fixtures")
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    env_runner = GAMRunner(vault=vault, gam_binary=child, base_dir=tmp_path, timeout=15)
    res = await env_runner.run_in_cfgdir(tmp_path, argv)
    return res, dict(kv.split("=", 1) for kv in res.stdout.split("\0") if kv)


@pytest.mark.parametrize("frozen", [False, True])
async def test_gam_inherits_only_the_allowlisted_environment(vault, tmp_path, monkeypatch, frozen):
    # A real child process reports exactly what it received: /usr/bin/env stands in for gam.
    from pathlib import Path

    _, env = await _child_env(vault, tmp_path, monkeypatch, frozen, Path("/usr/bin/env"), ["-0"])
    allowed = EXPECTED_PASSTHROUGH | SET_BY_THE_RUNNER | (set() if frozen else EXPECTED_MOCK_ONLY)
    assert set(env) <= allowed, set(env) - allowed
    assert not (set(HOSTILE_ENV) - {"GAMCFGDIR"}) & set(env)
    assert env["GAMCFGDIR"] == str(tmp_path)  # ours, never the launch environment's
    assert env["LANG"] == "en_US.UTF-8" and "PATH" in env
    assert ("GAM_MOCK_FIXTURES" in env) is not frozen  # test-only variables never reach the .app's gam


@pytest.mark.parametrize("frozen", [False, True])
async def test_no_dyld_variable_reaches_gam(vault, tmp_path, monkeypatch, frozen):
    # /usr/bin/env can't show DYLD_*: it is a SIP-protected platform binary, so macOS strips them
    # before it runs. The venv's Python is not, so it sees any that the runner passes (-I: a leaked
    # PYTHONHOME can't stop it starting). A leaked DYLD_INSERT_LIBRARIES aborts it: returncode != 0.
    import sys
    from pathlib import Path

    code = "import os, sys; sys.stdout.write('\\0'.join(f'{k}={v}' for k, v in os.environ.items()))"
    res, env = await _child_env(vault, tmp_path, monkeypatch, frozen, Path(sys.executable), ["-I", "-c", code])
    assert res.returncode == 0, res.stderr
    assert not {k for k in env if k.startswith("DYLD_")}, env
    assert not (set(HOSTILE_ENV) - {"GAMCFGDIR"}) & set(env)


def test_binary_override_is_ignored_in_the_packaged_app(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    from gamgui.core.gam.runner import GAM_BINARY_ENV, locate_gam_binary

    monkeypatch.setenv(GAM_BINARY_ENV, "/tmp/evil-gam")
    assert locate_gam_binary() == Path("/tmp/evil-gam")  # a source checkout honors it
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert locate_gam_binary() == tmp_path / "resources" / "gam7" / "gam"


def test_strip_cfgdir_noise_removes_gam_init_banner():
    from pathlib import Path

    from gamgui.core.gam.runner import strip_cfgdir_noise

    cfg = Path("/var/run/gamcfg-xyz")
    out = (
        f"Created: {cfg}/gamcache\n"
        f"Config File: {cfg}/gam.cfg, Initialized\n"
        "User: x@e.com, Vacation:\n  Enabled: True\n"
    )
    cleaned = strip_cfgdir_noise(out, cfg)
    assert "gamcache" not in cleaned and "Initialized" not in cleaned
    assert cleaned == "User: x@e.com, Vacation:\n  Enabled: True"  # only the real data survives


def test_strip_cfgdir_noise_keeps_unrelated_output():
    from pathlib import Path

    from gamgui.core.gam.runner import strip_cfgdir_noise

    out = "primaryEmail\na@e.com\nb@e.com"
    assert strip_cfgdir_noise(out, Path("/var/run/gamcfg-xyz")) == out  # untouched when dir not present
