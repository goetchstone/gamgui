"""Reports' mail review (roadmap 3.2): forwarding that is on, and mail delegates, across every active account.

GAM announces each account it reads on stderr, warns by name for one it can't, and skips a refused token without a word
(printShowForward, printShowDelegates, read from the vendored 7.52.03 build). Every account it listed is accounted for, and
one that wasn't read never counts as "forwards nothing" (invariant 10): tested on the clean exit, a per-account warning, a
silent skip, an exit that isn't a per-account one and a run with no table.
"""

from __future__ import annotations

from html import unescape

import pytest

from gamgui.core import mail_review
from gamgui.core.gam.errors import GAMError
from gamgui.core.gam.runner import RunResult
from gamgui.core.phishing import Incomplete

from .helpers import wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

ALICE, CAROL = "alice@example.com", "carol@example.com"


# --- reading GAM's lines ------------------------------------------------------------------------------------------------

def test_every_account_announced_is_read():
    a = mail_review.account_for("Got 2 Users...\r\nGetting Forward Enabled for a@x.com (1/2)\n"
                                "Getting Forward Enabled for b@x.com (2/2)\n")
    assert (a.listed, a.read, a.unread, a.skipped, a.complete) == (2, 2, [], 0, True)


def test_an_account_gam_warned_about_is_unread_with_its_reason():
    a = mail_review.account_for("Getting all Delegates for a@x.com (1/2)\nUser: a@x.com, Gmail Service/App not enabled (1/2)\n"
                                "Getting all Delegates for b@x.com (2/2)\nUser: b@x.com, Delegate Failed: boom (2/2)\n")
    assert a.read == 0 and not a.complete
    assert a.unread == [mail_review.Unread("a@x.com", mail_review.GMAIL_OFF), mail_review.Unread("b@x.com", mail_review.NOT_READ)]


def test_an_account_gam_skipped_without_a_word_is_counted_never_read():
    a = mail_review.account_for("Got 3 Users...\nGetting Forward Enabled for a@x.com (1/3)\n")
    assert (a.listed, a.read, a.skipped, a.complete) == (3, 1, 2, False)


def test_a_run_that_announced_nothing_is_no_answer():
    assert mail_review.account_for("") is None and mail_review.account_for("ERROR: something\n") is None


def test_only_forwarding_that_is_on_is_listed():
    rows = [{"User": "b@x.com", "forwardEnabled": "True", "forwardTo": "t@y.org", "disposition": "archive"},
            {"User": "a@x.com", "forwardEnabled": "False", "forwardTo": "", "disposition": ""}]
    assert mail_review.forwarding_from(rows) == [mail_review.Forwarding("b@x.com", "t@y.org", "archive")]


# --- the connector, against the mock that fails as GAM fails --------------------------------------------------------------

async def test_the_clean_read(connector):
    review = await connector.mail_review()
    assert review.forwarding == [mail_review.Forwarding(CAROL, "carol.home@example.org", "leaveInInbox")]
    assert [(d.user, d.delegate) for d in review.delegations] == [
        (ALICE, "assistant@example.com"), (ALICE, "backup@example.com"), (CAROL, "helpdesk@example.com")]
    assert review.forwarding_accounts.read == review.forwarding_accounts.listed == 2 and review.complete


async def test_gmail_off_for_one_account_keeps_the_others_and_names_it(connector, gam_state):
    (gam_state / "nogmail").mkdir()
    (gam_state / "nogmail" / CAROL).write_text("")
    review = await connector.mail_review()
    assert review.forwarding == [] and review.forwarding_accounts.read == 1
    assert review.forwarding_accounts.unread == [mail_review.Unread(CAROL, mail_review.GMAIL_OFF)]
    assert [d.user for d in review.delegations] == [ALICE, ALICE] and not review.complete


async def test_an_account_skipped_without_a_word_is_never_read_as_not_forwarding(connector, gam_state):
    (gam_state / "quiet").mkdir()
    (gam_state / "quiet" / CAROL).write_text("")
    review = await connector.mail_review()
    assert review.forwarding == [] and review.forwarding_accounts.skipped == 1 and not review.complete


async def test_an_exit_that_isnt_a_per_account_warning_raises(connector, monkeypatch):
    async def refused(domain, argv, timeout=None):
        return RunResult(stdout="", stderr="ERROR: 403: Not Authorized to access this resource/api\n", returncode=2)

    monkeypatch.setattr(connector.runner, "run_any_exit", refused)
    with pytest.raises(GAMError):
        await connector.mail_review()


@pytest.mark.parametrize("stdout, stderr", [
    ("", "Getting Forward Enabled for alice@example.com (1/2)\n"),              # announced, but no table came
    ("User,forwardEnabled,forwardTo,disposition\n", ""),                         # a table, but no account announced
])
async def test_a_run_missing_its_table_or_its_accounts_is_incomplete(connector, monkeypatch, stdout, stderr):
    async def partial(domain, argv, timeout=None):
        return RunResult(stdout=stdout, stderr=stderr, returncode=0)

    monkeypatch.setattr(connector.runner, "run_any_exit", partial)
    with pytest.raises(Incomplete):
        await connector.mail_review()


# --- the Reports panel ------------------------------------------------------------------------------------------------

def _read(client):  # noqa: F811
    client.post("/reports/mail-review/read")
    st = client.app.state.gamgui
    wait_for_job(client, st.jobs[st.mail_review_job_id])
    return unescape(client.get("/reports/mail-review").text)


def test_reports_offers_the_read_and_never_reads_by_itself(client, gam_calls):  # noqa: F811
    assert 'hx-get="/reports/mail-review"' in client.get("/reports").text
    page = client.get("/reports/mail-review").text
    assert "Read mail settings" in page and "filters" in page
    assert not [a for a in gam_calls() if a[:2] == ["all", "users"]]


def test_the_panel_lists_each_and_opens_the_person(client):  # noqa: F811
    page = _read(client)
    assert "Forwarding: read 2 of 2 accounts" in page and "→ carol.home@example.org (leaveInInbox)" in page
    assert '/users/detail?email=carol%40example.com' in page and "<helpdesk@example.com>" in page
    assert "Delegates: read 2 of 2 accounts" in page and "doesn’t cover Gmail filters that forward" in page


def test_the_panel_says_what_wasnt_read_and_never_none(client, gam_state):  # noqa: F811
    (gam_state / "quiet").mkdir()
    for user in (ALICE, CAROL):
        (gam_state / "quiet" / user).write_text("")
    page = _read(client)
    # Nothing was read: the panel says so, never "none of the accounts forwards".
    assert "Forwarding: read 0 of 2 accounts" in page and "GAM skipped 2 accounts without a word" in page
    assert "None of the" not in page


def test_a_skipped_account_is_said(client, gam_state):  # noqa: F811
    (gam_state / "quiet").mkdir()
    (gam_state / "quiet" / CAROL).write_text("")
    page = _read(client)
    assert "GAM skipped 1 account without a word" in page and "Forwarding: read 1 of 2 accounts" in page


def test_a_connector_switch_drops_the_review(client):  # noqa: F811
    _read(client)
    st = client.app.state.gamgui
    assert st.mail_review is not None
    st.activate(st.connector)
    assert st.mail_review is None and st.mail_review_job_id == ""
