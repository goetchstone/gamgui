"""A temporary password (back-port plan C1): made at the preview and held server-side, run only from its confirm step, and
shown once, masked everywhere else. New to both apps: offline only until it runs on a throwaway user."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import ADMIN, OAUTH2_FAKE, TEST_HOSTS

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
CAROL = "carol@example.com"
SECRET = "Xk7m-Qp9r-2Tzv"


@pytest.fixture
def audit_path(tmp_path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def client(tmp_path, monkeypatch, audit_path):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    monkeypatch.setattr("gamgui.web.routes.users.generate_temp_password", lambda: SECRET)
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(audit_path))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _token(html: str) -> str:
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(html))
    return m.group(1) if m else ""


def _preview(client, email=CAROL) -> str:
    return client.post("/users/password/preview", data={"email": email}).text


def _apply(client, token, email=CAROL, **form):
    return client.post("/users/password/apply", data={"email": email, "confirmed": "1", "preview": token, **form}).text


def _writes(gam_calls):
    return [argv for argv in gam_calls() if argv[:2] == ["update", "user"]]


def test_the_argv_is_swift_gamgui_s():
    assert GAMCommands.set_password(CAROL, SECRET) == ["update", "user", CAROL, "password", SECRET, "changepassword", "on"]


def test_the_confirm_step_shows_the_command_masked_and_runs_nothing(client, gam_calls):
    page = _preview(client)
    assert SECRET not in page and "password &#39;********&#39; changepassword on" in page
    assert _writes(gam_calls) == []


def test_it_runs_once_from_its_preview_and_shows_the_password_once(client, gam_calls):
    card = _apply(client, _token(_preview(client)))
    assert _writes(gam_calls) == [GAMCommands.set_password(CAROL, SECRET)]
    assert card.count(f">{SECRET}<") == 1 and f"Temporary password: {SECRET}" in card     # the sheet, and its Copy text
    assert "Temporary password set for carol@example.com." in card


def test_a_token_runs_once(client, gam_calls):
    token = _token(_preview(client))
    _apply(client, token)
    again = _apply(client, token)
    assert "expired or was already run" in again and SECRET not in again and len(_writes(gam_calls)) == 1


def test_a_preview_for_one_person_can_t_run_on_another(client, gam_calls):
    refused = _apply(client, _token(_preview(client)), email="bob@example.com")
    assert "changed after the preview" in refused and _writes(gam_calls) == []


def test_a_bare_post_runs_nothing(client, gam_calls):
    assert "expired or was already run" in client.post("/users/password/apply", data={"email": CAROL, "confirmed": "1"}).text
    assert _writes(gam_calls) == []


def test_it_needs_the_confirm_click(client, gam_calls):
    token = _token(_preview(client))
    assert SECRET not in client.post("/users/password/apply", data={"email": CAROL, "preview": token}).text
    assert _writes(gam_calls) == []


def test_each_preview_makes_a_new_password(tmp_path, monkeypatch, client):
    monkeypatch.undo()
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    first = _apply(client, _token(_preview(client)))
    second = _apply(client, _token(_preview(client)))
    pattern = r'font-mono[^>]*>([^<]+)<'
    assert re.search(pattern, first).group(1) != re.search(pattern, second).group(1)


def test_the_audit_and_the_tray_never_hold_it(client, audit_path):
    _apply(client, _token(_preview(client)))
    log = audit_path.read_text()
    assert SECRET not in log and '"set_password"' in log and "redacted" in log
    tray = client.get("/jobs/status").text
    assert SECRET not in tray and "Set a temporary password: carol@example.com" in unescape(tray)
    for job_id in client.app.state.gamgui.jobs:
        assert SECRET not in client.get(f"/jobs/{job_id}").text


def test_a_failure_shows_no_password_and_says_it_is_unchanged(client, audit_path, monkeypatch):
    # GAM echoes the whole command line on a usage error, password and all (mock_gam.sh's usage_error).
    from gamgui.core.gam.errors import GAMError

    runner = client.app.state.gamgui.runner
    real = runner.run_authenticated

    async def fails(domain, argv, **kwargs):
        if argv[:2] == ["update", "user"]:
            raise GAMError.from_run(2, f"Command: gam {' '.join(argv)}\n\nERROR: Invalid argument\n", list(argv))
        return await real(domain, argv, **kwargs)

    monkeypatch.setattr(runner, "run_authenticated", fails)
    page = _apply(client, _token(_preview(client)))
    assert SECRET not in page and "Their password is unchanged" in page
    assert SECRET not in audit_path.read_text()


def test_the_connected_admin_s_is_refused_at_both_steps(client, gam_calls, audit_path):
    page = _preview(client, ADMIN)
    assert not _token(page) and "no way back" in page
    assert _writes(gam_calls) == []
    # A preview made before the admin was known runs nothing either: _run_write asks again.
    held = client.app.state.gamgui.previews.hold("temporary_password", ADMIN.lower(), (ADMIN, SECRET))
    assert SECRET not in _apply(client, held, email=ADMIN)
    assert _writes(gam_calls) == [] and SECRET not in audit_path.read_text()


def test_a_preview_doesn_t_survive_a_tenant_switch(client, gam_calls, tmp_path):
    state = client.app.state.gamgui
    token = _token(_preview(client))
    state.activate(GAMConnector(runner=state.runner, domain="other.example", audit=AuditLog(tmp_path / "o.jsonl")))
    assert SECRET not in _apply(client, token) and _writes(gam_calls) == []


def test_the_security_tab_offers_it(client):
    page = client.get("/users/detail", params={"email": CAROL}).text
    assert 'hx-post="/users/password/preview"' in page and "Set temporary password" in page
    assert SECRET not in page


# --- What an adversarial pass found (back-port plan C1) ---------------------------------------------------------------


def _fail_with(client, monkeypatch, make_error):
    runner = client.app.state.gamgui.runner
    real = runner.run_authenticated

    async def fails(domain, argv, **kwargs):
        if argv[:2] == ["update", "user"]:
            raise make_error(argv)
        return await real(domain, argv, **kwargs)

    monkeypatch.setattr(runner, "run_authenticated", fails)


def test_a_timeout_doesn_t_claim_the_password_is_unchanged(client, monkeypatch):
    # GAM is killed after Google may have taken it, and the password just made is gone.
    from gamgui.core.gam.errors import GAMError, GAMErrorKind

    _fail_with(client, monkeypatch, lambda argv: GAMError(kind=GAMErrorKind.TIMEOUT, exit_code=None, argv=list(argv)))
    page = unescape(_apply(client, _token(_preview(client))))
    assert "unchanged" not in page and "may have been set" in page and "Set a new one" in page


def test_a_failure_around_the_call_doesn_t_claim_it_either(client, monkeypatch):
    _fail_with(client, monkeypatch, lambda argv: OSError("couldn't wipe the folder"))
    page = unescape(_apply(client, _token(_preview(client))))
    assert "unchanged" not in page and "may have been set" in page


def test_gam_s_own_rejection_says_it_is_unchanged(client, monkeypatch):
    from gamgui.core.gam.errors import GAMError

    _fail_with(client, monkeypatch, lambda argv: GAMError.from_run(50, "User: x, Update Failed: Invalid Password\n", list(argv)))
    assert "Their password is unchanged" in _apply(client, _token(_preview(client)))


def test_the_audit_log_failing_after_the_set_still_shows_the_password(client, monkeypatch):
    # The account changed. A 500 here lost the password and the tray said it failed.
    audit = client.app.state.gamgui.connector.audit
    real = audit.record

    def full_disk(*args, **kwargs):
        if kwargs.get("ok") is True:
            raise OSError(28, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(audit, "record", full_disk)
    card = _apply(client, _token(_preview(client)))
    assert f">{SECRET}<" in card and "audit log couldn" in unescape(card) and "No space left" in card
    change = next(iter(client.app.state.gamgui.jobs.values()))
    assert (change.applied, change.failed_total) == (1, 0)


@pytest.mark.parametrize("echo", ["Xk7m-Qp9r", "Qp9r-2Tzv", "XK7M-QP9R-2TZV", "xk7m-qp9r-2tzv", "Xk7m Qp9r 2Tzv", "2Tzv"])
def test_part_of_the_password_in_an_echo_is_masked_too(client, monkeypatch, audit_path, echo):
    from gamgui.core.gam.errors import GAMError

    _fail_with(client, monkeypatch, lambda argv: GAMError.from_run(1, f"ERROR: Invalid password: {echo}\n", list(argv)))
    page = _apply(client, _token(_preview(client)))
    log = audit_path.read_text()
    for group in ("Xk7m", "Qp9r", "2Tzv"):
        for form in (group, group.lower(), group.upper()):
            assert form not in page and form not in log


def test_a_new_preview_replaces_the_older(client, gam_calls):
    older = _token(_preview(client))
    newer = _token(_preview(client))
    assert "expired or was already run" in _apply(client, older) and _writes(gam_calls) == []
    _apply(client, newer)
    assert len(_writes(gam_calls)) == 1


def test_running_one_preview_spends_every_other_of_that_person(client, gam_calls):
    state = client.app.state.gamgui
    token = _token(_preview(client))
    other = state.previews.hold("temporary_password", CAROL, (CAROL, "Aaaa-Bbbb-Cccc"))
    _apply(client, token)
    assert "expired or was already run" in _apply(client, other) and len(_writes(gam_calls)) == 1


def test_another_person_s_preview_is_left_alone(client, gam_calls):
    token = _token(_preview(client))
    other = _token(_preview(client, "bob@example.com"))
    _apply(client, token)
    _apply(client, other, email="bob@example.com")
    assert len(_writes(gam_calls)) == 2
