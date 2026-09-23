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
    "PYTHONPATH": "/tmp/evil",
    "PYTHONHOME": "/tmp/evil",
    "_MEIPASS2": "/tmp/evil",
    "GAMGUI_GAM_BINARY": "/tmp/evil-gam",
    "GAMCFGSECTION": "other",
    "GAM_CSV_OUTPUT_QUOTE_CHAR": "'",
    "SSL_CERT_FILE": "/tmp/evil.pem",
    "GAMCFGDIR": "/tmp/not-the-ephemeral-dir",
}


@pytest.mark.parametrize("frozen", [False, True])
async def test_gam_inherits_only_the_allowlisted_environment(vault, tmp_path, monkeypatch, frozen):
    # A real child process reports what it received: /usr/bin/env stands in for gam.
    import sys
    from pathlib import Path

    from gamgui.core.gam.runner import ENV_ALLOWLIST, MOCK_ENV

    for k, v in HOSTILE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("GAM_MOCK_FIXTURES", "/tmp/fixtures")
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    env_runner = GAMRunner(vault=vault, gam_binary=Path("/usr/bin/env"), base_dir=tmp_path, timeout=15)
    res = await env_runner.run_in_cfgdir(tmp_path, ["-0"])
    env = dict(kv.split("=", 1) for kv in res.stdout.split("\0") if kv)

    allowed = ENV_ALLOWLIST | {"GAMCFGDIR", "GAM_NO_UPDATE_CHECK"} | (set() if frozen else MOCK_ENV)
    assert set(env) <= allowed, set(env) - allowed
    assert env["GAMCFGDIR"] == str(tmp_path)  # ours, never the launch environment's
    assert env["LANG"] == "en_US.UTF-8" and "PATH" in env
    assert ("GAM_MOCK_FIXTURES" in env) is not frozen  # test-only variables never reach the .app's gam


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
