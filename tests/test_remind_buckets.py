"""Reports' No 2SV / No recovery → a reminder email (roadmap 3.3, D3): previewed from a fresh directory read, the exact
text held with the people, one audited email each, and text edited after the preview refused."""

from __future__ import annotations

import re
from html import unescape

from gamgui.core import reports
from gamgui.core.connectors.base import ChangePreview, ChangeResult, ConnectorID, RiskLevel
from gamgui.core.gam.models import GAMUser

from .helpers import gam_writes, wait_for_job
from .test_users_web import client  # noqa: F401  (the fixture)

FORM = {"key": "no_2sv", "subject": "Turn on 2SV", "message": "Hello <b>you</b>,\nplease turn it on."}


def _sent(gam_calls):
    return [a for a in gam_writes(gam_calls()) if a[:1] == ["sendemail"]]


def _preview(client, **form):  # noqa: F811
    r = client.post("/reports/remind/preview", data={**FORM, **form})
    token = re.search(r'"preview": "([^"]+)"', r.text)
    return unescape(r.text), (token.group(1) if token else "")


def _send(client, token, **form):  # noqa: F811
    r = client.post("/reports/remind/apply", data={**FORM, "confirmed": "1", "preview": token, **form})
    job = next((j for j in client.app.state.gamgui.jobs.values() if j.kind == "remind"), None)
    if job is not None:
        wait_for_job(client, job)
    return r, job


def _bucket(client, key):  # noqa: F811
    page = client.get("/reports").text
    card = page.split(f'<div id="report-{key}"', 1)[1].split('<div id="report-', 1)[0]
    return sorted(re.findall(r"— ([^<\s]+@[^<\s]+)\s*</span>", card))


def test_the_text_is_the_operators_own_words_escaped_with_its_line_breaks():
    assert reports.reminder_html("Hi <b>x</b> & co\nline two\n") == "Hi &lt;b&gt;x&lt;/b&gt; &amp; co<br>line two"


def test_only_the_no_2sv_and_no_recovery_cards_offer_it(client):  # noqa: F811
    page = client.get("/reports").text
    assert set(re.findall(r'id="remind-(\w+)-form"', page)) <= set(reports.REMINDERS)
    assert 'id="remind-no_2sv-form"' in page and "myaccount.google.com/security" in page


def test_the_preview_reads_again_lists_the_bucket_and_sends_nothing(client, gam_calls):  # noqa: F811
    expected = _bucket(client, "no_2sv")
    reads = len([a for a in gam_calls() if a[:2] == ["print", "users"]])
    page, token = _preview(client)
    assert len([a for a in gam_calls() if a[:2] == ["print", "users"]]) == reads + 1
    assert token and all(f"— {e}" in page for e in expected) and _sent(gam_calls) == []


def test_send_emails_each_person_the_held_text_through_the_chokepoint(client, gam_calls):  # noqa: F811
    expected = _bucket(client, "no_2sv")
    _, token = _preview(client)
    _, job = _send(client, token)
    assert job is not None and job.applied == len(expected) > 0
    sent = _sent(gam_calls)
    assert sorted(a[2] for a in sent) == expected
    assert sent[0][3:] == ["subject", "Turn on 2SV", "message", "Hello &lt;b&gt;you&lt;/b&gt;,<br>please turn it on.", "html"]
    audit = [(e["action"], e["ok"]) for e in client.app.state.gamgui.connector.audit.tail()]
    assert audit.count(("send_reminder_email", True)) == len(expected)
    again, _ = _send(client, token)
    assert "expired or was already run" in again.text and len(_sent(gam_calls)) == len(expected)   # single use


def test_text_edited_after_the_preview_is_refused_never_sent(client, gam_calls):  # noqa: F811
    _, token = _preview(client)
    r, job = _send(client, token, message="Something else.")
    assert "message changed after the preview" in r.text and job is None and _sent(gam_calls) == []


def test_an_empty_subject_or_message_previews_nothing(client):  # noqa: F811
    for blank in ({"subject": " "}, {"message": ""}):
        page, token = _preview(client, **blank)
        assert "Write a subject and a message first" in page and not token


def test_a_bucket_that_gets_no_reminder_is_refused(client):  # noqa: F811
    page, token = _preview(client, key="inactive")
    assert "Not a list that gets a reminder" in page and not token


def test_a_directory_that_couldnt_be_read_again_offers_nothing(client, monkeypatch):  # noqa: F811
    async def refused(force=False, stale_ok=False):
        raise OSError("LEAK-MARKER")

    monkeypatch.setattr(client.app.state.gamgui, "users", refused)
    page, token = _preview(client)
    assert "Couldn't read the directory again" in page and not token and "LEAK-MARKER" not in page


def test_more_than_25_need_the_count_typed(client, gam_calls, monkeypatch):  # noqa: F811
    many = [GAMUser(primary_email=f"p{i:02}@example.com", last_login_time="2026-11-18T00:00:00Z") for i in range(30)]

    async def thirty(force=False, stale_ok=False):
        return many

    monkeypatch.setattr(client.app.state.gamgui, "users", thirty)
    page, token = _preview(client)
    assert 'name="confirm_count"' in page
    r, job = _send(client, token, confirm_count="29")
    assert "type 30 to confirm" in r.text and job is None and _sent(gam_calls) == []


def test_a_failed_send_is_listed_with_why(client, monkeypatch):  # noqa: F811
    conn = client.app.state.gamgui.connector
    expected = _bucket(client, "no_2sv")

    async def refused(to, subject, body):
        preview = ChangePreview(connector_id=ConnectorID.GOOGLE_WORKSPACE, target=to, summary="x", risk=RiskLevel.LOW)
        return ChangeResult(preview=preview, ok=False, detail="ERROR: 400: Invalid to header", remediation="Check the address.")

    monkeypatch.setattr(conn, "send_reminder_email", refused)
    _, token = _preview(client)
    _, job = _send(client, token)
    page = unescape(client.get("/reports/remind/status", params={"job": job.id}).text)
    assert job.failed_items == expected and "Sent 0 of" in page and "Check the address." in page and "Invalid to header" in page
