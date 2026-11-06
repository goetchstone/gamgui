"""The incident report (back-port plan E8): each number with the list behind it, every step taken with what shows it, and one document in text, Markdown and PDF. Ported from SwiftGamGui's IncidentReportTests, on
the reader's own made-up incident, then the screen and the export on the mock."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
import zlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import unescape

import pytest

from gamgui.core.incident import reader, report_pdf, runs
from gamgui.core.incident.report import Change, Evidence, Report, cell_csv, evidence_about, got_it_table, make, markdown_escape, markdown_of, span, text_of
from gamgui.core.incident.store import Note, Record, Review

from .test_incident_reader import ALICE, BOB, CAROL, DANA, PEOPLE, REACH_EVENTS, at, done, minute
from .test_incident_respond import ON_INCIDENT, _open_new, _pull, client, state  # noqa: F401 - the fixtures

BASE = Record("2026-10-20-abcdef", "example.com", "Carol's account taken over", at(90), "Pat", "A Google alert", (Note(at(95), "Called the bank."),))


def reading() -> reader.Reading:
    """carol@ locked by alice@ at minute 40, dana@ only reset, erin@ never: on the reader's incident with the phish and bob@'s warning picked."""
    events = [*REACH_EVENTS, done("CHANGE_PASSWORD", minute=40), done("RESET_SIGNIN_COOKIES", minute=41), done("CHANGE_PASSWORD", to=DANA, minute=42)]
    return reader.read(events, reader.Context(domains={"example.com"}, people=dict(PEOPLE), warnings={"phish-1@mail.example.com": {"warn-1@mail.example.com"}}))


DIRECTORY = {CAROL: (True, False), DANA: (False, False), ALICE: (False, False), BOB: (True, False)}


def report(changes=(), directory=DIRECTORY, record: Record = BASE, read=None, **kept) -> Report:
    return make(read or reading(), record, list(changes), [Evidence("Incident logs 2026-10-19 1500", "Over the last 3 days.")], directory, at(120), "pat@example.com", minute, **kept)


# --- what's counted --------------------------------------------------------------------------------------------------------------------


def test_everyone_it_got_to_once_each():
    got = reader.got_it(reading().reaches)
    assert [g.address for g in got] == ["pat@partner.example", ALICE, DANA, "ap&ar@vendor.example", "team@example.com", BOB], "guest@ bounced: it didn't get it"
    pat = got[0]
    assert pat.emails == 2 and pat.subjects == ("Shared file", "Re") and not pat.inside and not pat.received
    assert pat.to_warn and not pat.warned, "warned of the phish, not of the Re after it"
    alice = next(g for g in got if g.address == ALICE)
    assert alice.received and alice.warned and not alice.to_warn and alice.emails == 1
    table = got_it_table(got, DIRECTORY)
    assert table.columns[0] == "Email" and len(table.rows) == 6 and all(len(r) == len(table.columns) for r in table.rows)
    assert table.rows[2][12] == "off" and table.rows[0][12] == "", "2SV only for a person here, with the directory read"


def test_each_number_has_the_list_behind_it():
    made = report()
    assert [c.id for c in made.counts] == ["accounts", "notLocked", "accounts2SV", "emails", "gotIt", "gotItOutside", "toWarn", "warned", "contacted", "notContacted", "reached2SV", "pulled", "actions", "toDo"]
    accounts = made.count("accounts").table
    assert [r[0] for r in accounts.rows] == [CAROL, DANA, "erin@example.com"] and [r[1] for r in accounts.rows] == ["locked", "partly locked", "not locked"]
    assert [r[5] for r in accounts.rows] == ["on", "off", ""], "erin@ isn't in the directory: never \"on\" for what isn't known"
    assert made.number("notLocked") == 2 and made.number("accounts2SV") == 1
    assert made.number("emails") == 2 and [r[4] for r in made.count("emails").table.rows] == ["6", "1"], "a row each email, how many it got to"
    assert made.number("gotIt") == 6 and made.number("gotItOutside") == 2
    assert made.number("toWarn") == 5 and made.number("warned") == 1, "pat@ is still to warn of the Re"
    assert made.number("contacted") == 6 and made.number("notContacted") == 0
    assert made.number("reached2SV") == 2, "alice@ and dana@, people here it got to"
    assert len(made.reached.rows) == 8 and any(r[2] == "guest@example.org" and r[4] == "bounced" for r in made.reached.rows), "the full record keeps the bounce"


def test_with_the_directory_unread_nothing_is_said_of_two_step():
    unread = report(directory=None)
    assert unread.count("accounts2SV") is None and unread.count("reached2SV") is None
    assert [r[5] for r in unread.count("accounts").table.rows] == ["", "", ""], "never \"on\" when it isn't known"


def test_a_locked_account_without_two_step_still_reads_off():
    made = report(directory={CAROL: (False, True), DANA: (True, False), ALICE: (False, True)})
    assert made.count("accounts").table.rows[0][5] == "off (suspended)" and made.number("accounts2SV") == 1, "it was off when the attacker got in"
    assert made.number("reached2SV") == 0, "the next accounts at risk are active ones"


def test_every_step_taken_says_what_shows_it():
    made = report(changes=[Change(at(40), "Reset password", CAROL, ALICE, True), Change(at(44), "Sign out", "Dana@Example.com", ALICE, False), Change(at(45), "Suspend user", "zed@example.com", ALICE, True),
                           Change(at(-60), "Reset password", CAROL, ALICE, True), Change(at(46), "Reset password", CAROL, "", None)])
    assert [a.at for a in made.actions] == sorted(a.at for a in made.actions)
    assert any(a.what == "Password reset by alice@example.com" and a.account == CAROL and a.source == "Google's Admin log" for a in made.actions)
    assert any(a.what == "Reset password by alice@example.com" and a.source == "This app's audit log" for a in made.actions)
    assert any(a.what == "Sign out (failed) by alice@example.com" and a.account == "Dana@Example.com" for a in made.actions)
    assert any(a.what == "Reset password (no result recorded)" for a in made.actions)
    assert not any(a.account == "zed@example.com" for a in made.actions), "zed@ isn't in the incident"
    assert sum(a.source == "This app's audit log" for a in made.actions) == 3, "never one from before the attack"
    assert any(a.source == "Google's Gmail log" and a.what.startswith("Warning “Don't open “Shared file”” sent by bob@example.com: 2 of those") for a in made.actions)
    assert made.actions[-1].what == "Called the bank." and made.actions[-1].source == "A note on the incident"
    assert made.number("actions") == len(made.actions) - 3, "a step that failed, one with no result, and a note are listed, and aren't steps taken"
    assert "Listed too: 2 that failed or showed no result, and 1 note." in made.count("actions").about
    assert len(made.count("actions").table.rows) == len(made.actions)


def test_the_times_to_detect_and_contain():
    made = report()
    assert made.first_sign == at(8) and made.contained is None, "dana@ is only partly locked and erin@ not at all"
    assert span(82 * 60) == "1 hour 22 minutes" and span(3 * 86_400 + 7_200 + 59) == "3 days 2 hours" and span(20) == "under a minute"


def test_contained_only_once_every_account_touched_is_locked():
    events = [*REACH_EVENTS, done("CHANGE_PASSWORD", minute=40), done("RESET_SIGNIN_COOKIES", minute=41), *(done(name, to=who, minute=m) for name, who, m in (
        ("CHANGE_PASSWORD", DANA, 42), ("RESET_SIGNIN_COOKIES", DANA, 43), ("CHANGE_PASSWORD", "erin@example.com", 44), ("RESET_SIGNIN_COOKIES", "erin@example.com", 45)))]
    made = report(read=reader.read(events, reader.Context(domains={"example.com"}, people=dict(PEOPLE))))
    assert made.number("notLocked") == 0 and made.contained == at(45)


# --- the document ------------------------------------------------------------------------------------------------------------------


def test_the_report_as_text():
    text = text_of(report().blocks(minute, "America/New_York", [("evidence/Incident logs 2026-10-19 1500/about.txt", "ab12")]))
    headings = ["AT A GLANCE", "WHERE IT STARTED", "WHAT WAS DONE", "THE ATTACKER'S EMAILS, AND WHO THEY REACHED", "WHERE EACH ACCOUNT STANDS", "STILL TO DO", "TIMELINE", "NOTES",
                "POST-INCIDENT REVIEW: LESSONS LEARNED AND THE PLAN GOING FORWARD", "EVIDENCE"]
    places = [text.index(f"\n{h}\n") for h in headings]
    assert places == sorted(places)
    assert text.startswith("INCIDENT REPORT: Carol's account taken over\nDomain: example.com\n")
    assert "Times are in America/New_York." in text and "Time to detect (first sign to opened): 1 hour 22 minutes" in text
    assert "Contained: not yet" in text and "Got an email of the attacker's: 6\n" in text and "Bounced" not in text
    assert "Not written yet. For the review meeting, go through:\n1. What happened" in text
    assert "4. Who it got to and whether they were warned: 6 addresses (2 outside), 5 still to warn, 6 contacted from here after." in text
    assert "ab12  evidence/Incident logs 2026-10-19 1500/about.txt" in text and "• Incident logs 2026-10-19 1500: Over the last 3 days." in text


def test_a_written_review_goes_in_as_typed():
    record = replace(BASE, title="T", opened_by="", reported="", notes=(), review=Review(severity="High", meeting=at(200), cause="A phishing page.", plan="Enforce 2SV — Pat\n\n Train — Sam "))
    text = text_of(report(record=record, directory=None).blocks(minute))
    assert "Severity: High" in text and "Review meeting: minute 200" in text and "How they got in: A phishing page." in text
    assert "The plan going forward:\n• Enforce 2SV — Pat\n• Train — Sam\n" in text and "The review meeting's agenda:\n1. What happened" in text


def test_the_summary_heads_it_and_alone_isn_t_a_review():
    record = replace(BASE, review=Review(summary="Carol was phished.\n\nEveryone it got to was warned."))
    text = text_of(report(record=record).blocks(minute))
    assert "\nSUMMARY\n=======\nCarol was phished.\nEveryone it got to was warned.\n" in text
    assert "only the summary, the notes, the review and what was marked done by hand are typed." in text and "Not written yet. For the review meeting" in text
    assert "SUMMARY" not in text_of(report().blocks(minute)), "none written: no section"


def test_markdown_every_piece_is_its_words():
    assert markdown_escape("[Click](https://evil.example) ![x](y) <b>hi</b> *a* _b_ `c` | d $1 & e") == r"\[Click\]\(https://evil.example\) \!\[x\]\(y\) \<b\>hi\</b\> \*a\* \_b\_ \`c\` \| d \$1 \& e"
    assert markdown_escape("# Not a heading\nnor this") == r"\# Not a heading nor this"
    assert markdown_escape("12. not a list") == r"12\. not a list" and markdown_escape("- nor this") == r"\- nor this" and markdown_escape("= nor this") == r"\= nor this"
    assert markdown_escape("    nor code: Message-ID 1.5") == "nor code: Message-ID 1.5"


def test_the_report_as_markdown():
    text = markdown_of(report().blocks(minute, "", [("evidence/a`b/about.txt", "ab12")]))
    assert text.startswith("# Incident report: Carol's account taken over\n\nDomain: example.com\n\n")
    assert "\n\n## What was done\n\n- minute " in text and "\n\n| when | account | who | what |\n| --- | --- | --- | --- |\n| minute " in text
    assert "\n\n```\nab12  evidence/a`b/about.txt\n```\n" in text and "“Shared file”" in text
    fence = markdown_of(report().blocks(minute, "", [("a```b", "ab12")]))
    assert "\n````\nab12  a```b\n````\n" in fence, "nothing in it can close the fence"


def test_a_subject_an_attacker_wrote_is_never_markup_in_the_markdown():
    events = [e for e in REACH_EVENTS]
    hostile = [replace(e, parameters={**e.parameters, "message_info.subject": "![x](https://evil.example) # <img src=x>"}) if e.parameters.get("message_info.subject") == "Shared file" else e for e in events]
    text = markdown_of(report(read=reader.read(hostile, reader.Context(domains={"example.com"}, people=dict(PEOPLE)))).blocks(minute))
    assert "![x](" not in text and not re.search(r"(?<!\\)<img", text) and r"\!\[x\]\(https://evil.example\)" in text


def test_a_step_marked_done_by_hand_is_said_as_typed_never_as_an_email_pulled():
    base = reading()
    task = next(t for t in base.to_do if t.tickable)
    ticked = base.ticking([reader.Ticked(task.kind, task.words, task.basis, "Called them.", at(100), "pat@example.com")])
    made = report(read=ticked, directory=None)
    assert any(a.source == "Marked done on the incident" and a.what == f"Done by hand (by pat@example.com): {task.words} How: Called them." for a in made.actions)
    assert made.number("byHand") == 1 and made.number("pulled") == 0 and made.number("toDo") == len(base.to_do) - 1


@pytest.mark.parametrize("picked,says", [("inv", "The email that started it"), ("gone", "isn't in the logs read"), (None, "Not found yet")])
def test_where_it_started_is_said(picked, says):
    email = reader.WayInEmail("inv", "billing@vendor.example", "Invoice", at(1), (CAROL,))
    way_in = reader.WayIn(CAROL, at(8), (), 0, email if picked == "inv" else None, "gone" if picked == "gone" else None)
    made = replace(report(), way_in=way_in)
    assert says in made.way_in_words(minute)


def test_a_csv_cell_the_attacker_wrote_is_inert_and_a_time_is_iso():
    table = got_it_table([reader.GotIt("=cmd()@example.com", False, False, False, at(1), ("=SUM(A1)",), 1, True, False, None)], None)
    rows = list(csv.reader(io.StringIO(table.csv_text())))
    assert rows[1][0] == "'=cmd()@example.com" and rows[1][5] == "'=SUM(A1)" and rows[1][3] == cell_csv(at(1)) and rows[1][3].endswith("Z")


def test_what_a_saved_read_was():
    assert evidence_about("Incident logs exported x.\nOver the 3 days up to y.\nAccounts: a@b.c.\nFrom IP address: any.\n\nlogin-log.csv: the log.\n") == "Over the 3 days up to y. Accounts: a@b.c. From IP address: any."


# --- the PDF --------------------------------------------------------------------------------------------------------------------------------


def _pdf_pages(data: bytes):
    return [zlib.decompress(m) .decode("latin-1") for m in re.findall(rb"stream\n(.*?)\nendstream", data, re.S)]


def _pdf_text(data: bytes) -> str:
    return "\n".join(re.sub(r"\\([()\\])", r"\1", m) for page in _pdf_pages(data) for m in re.findall(r"\(((?:[^()\\]|\\.)*)\) Tj", page))


def test_the_pdf_says_what_the_text_says():
    blocks = report().blocks(minute, "America/New_York", [("evidence/x/about.txt", "ab12")])
    data = report_pdf.pdf_of(blocks)
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    text = _pdf_text(data)
    assert "Carol's account taken over" in text and "What was done" in text and "ab12  evidence/x/about.txt" in text and "Called the bank." in text
    order = [text.index(h) for h in ("At a glance", "What was done", "Where each account stands", "Evidence")]
    assert order == sorted(order), "it reads in order"


def test_the_pdf_s_structure_is_sound():
    data = report_pdf.pdf_of(report().blocks(minute))
    xref = int(re.search(rb"startxref\n(\d+)\n", data).group(1))
    assert data[xref:xref + 4] == b"xref"
    entries = re.findall(rb"(\d{10}) 00000 n ", data[xref:])
    assert entries
    for number, entry in enumerate(entries, 1):
        offset = int(entry)
        assert data[offset:].startswith(b"%d 0 obj" % number), number
    count = int(re.search(rb"/Count (\d+)", data).group(1))
    assert count == len(_pdf_pages(data)) >= 1


def test_a_long_report_runs_to_more_pages_and_stays_on_the_page():
    data = report_pdf.pdf_of(report(record=replace(BASE, notes=tuple(Note(at(95), f"Note {i}: " + "word " * 40 + "x" * 200) for i in range(200)))).blocks(minute))
    pages = _pdf_pages(data)
    assert len(pages) > 3
    for page in pages:
        for x, y in re.findall(r"Tf ([\d.]+) ([\d.]+) Td", page):
            assert report_pdf.MARGIN - 13 <= float(x) <= report_pdf.PAGE_WIDTH and report_pdf.MARGIN <= float(y) <= report_pdf.PAGE_HEIGHT


def test_text_an_attacker_wrote_can_t_reach_the_file_s_structure():
    hostile = ") Tj ET endstream endobj 9 0 obj << /Type /Action >> \\ (\x00\n"
    data = report_pdf.pdf_of(report(record=replace(BASE, title=hostile)).blocks(minute))
    assert b"/Type /Action" not in data
    assert "\\)" in "\n".join(_pdf_pages(data)) and "/Type /Action" in _pdf_text(data), "it's only ever a string on the page"


def test_a_character_the_pdf_can_t_show_is_said():
    data = report_pdf.pdf_of(report(record=replace(BASE, title="Счёт taken over")).blocks(minute))
    assert "?" in _pdf_text(data) and "read as" in _pdf_text(data).replace("\n", " ")
    assert "can't be shown" not in _pdf_text(report_pdf.pdf_of(report().blocks(minute))).replace("\n", " "), "a question mark that's really there isn't one"


# --- the audit's changes -------------------------------------------------------------------------------------------------------------


def test_the_audit_s_changes_stop_at_the_attack_and_leave_out_phishing(tmp_path):
    from gamgui.core.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("reset_password", target=CAROL, ok=True)
    audit.record("trash_message", target=CAROL, ok=True)
    audit.record("sign_out", target=DANA, ok=False)
    audit.record("no_target", ok=True)
    audit.record("sensitive_read", target=CAROL, ok=True)
    found, cut_at = runs.changes_since(audit.path, datetime.now(timezone.utc) - timedelta(hours=1), lambda a: a.upper())
    assert [(c.what, c.target, c.ok) for c in found] == [("SIGN_OUT", DANA, False), ("RESET_PASSWORD", CAROL, True)] and cut_at is None, "a read changes nothing"
    assert runs.changes_since(audit.path, datetime.now(timezone.utc) + timedelta(hours=1), str) == ([], None)


def test_an_audit_log_read_only_so_far_says_how_far(tmp_path):
    from gamgui.core.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl")
    for _ in range(3):
        audit.record("reset_password", target=CAROL, ok=True)
    found, cut_at = runs.changes_since(audit.path, datetime.now(timezone.utc) - timedelta(hours=1), str, limit=2)
    assert len(found) == 2 and cut_at is not None


# --- the screen, on the mock ------------------------------------------------------------------------------------------------------------


EVERYONE = f"{CAROL},{ALICE},{BOB}"


def _ready(client, state):  # noqa: F811
    record = _open_new(client, state)
    _pull(client, state, accounts=EVERYONE)
    return record


def test_it_asks_for_an_incident_then_its_logs(client, state):  # noqa: F811
    assert "Keep or open an incident" in client.get("/incident/report").text
    _open_new(client, state)
    assert "Pull the logs</a> for this incident" in client.get("/incident/report").text


def test_each_number_opens_its_list_with_a_csv(client, state):  # noqa: F811
    _ready(client, state)
    page = unescape(client.get("/incident/report").text)
    assert "Got an email of the attacker's" in page and 'aria-current="page"' in page and "Export report" in page
    for count in ("gotIt", "accounts", "actions", "toDo"):
        assert f"/incident/report/list?id={count}" in page
    listed = unescape(client.get("/incident/report/list?id=gotIt").text)
    assert "pat@partner.example" in listed and "/incident/report/list.csv?id=gotIt" in listed and "Each address an email of the attacker's got to" in listed
    r = client.get("/incident/report/list.csv?id=gotIt")
    assert r.status_code == 200 and "who-got-it.csv" in r.headers["Content-Disposition"] and r.text.startswith("Email,Where,")
    assert "isn't in the report" in unescape(client.get("/incident/report/list?id=nothing").text)
    assert client.get("/incident/report/list.csv?id=nothing").status_code == 404


def test_a_long_list_is_drawn_bounded(client, state, monkeypatch):  # noqa: F811
    from gamgui.web.routes import incident_report

    _ready(client, state)
    monkeypatch.setattr(incident_report, "_SHOWN", 1)
    listed = client.get("/incident/report/list?id=gotIt").text
    assert "The first 1. The CSV has them all." in listed and listed.count("<tr") == 2


def test_the_review_is_kept_and_goes_in_the_report(client, state):  # noqa: F811
    record = _ready(client, state)
    page = unescape(client.post("/incident/report/review", data={"incident": record.id, "kind": "Unauthorized access", "severity": "High", "meeting": "2026-11-01T09:00", "attendees": "Pat, Sam", "cause": "A phishing page.", "went_well": "Fast.",
                                                                  "improve": "2SV.", "plan": "Enforce 2SV — Pat", "summary": "Carol was phished."}).text)
    held = state.incident_store.get(record.id).review
    assert held.kind == "Unauthorized access" and held.plan == "Enforce 2SV — Pat" and held.meeting is not None and held.summary == "Carol was phished."
    assert "Carol was phished." in page and "A phishing page." in page


@pytest.mark.parametrize("data,says", [({"meeting": "0001-01-01"}, "isn't one an incident can keep"), ({"meeting": "not a date"}, "isn't one an incident can keep"), ({"kind": "x" * 101}, "type and severity")])
def test_a_review_that_can_t_be_kept_is_said(client, state, data, says):  # noqa: F811
    record = _ready(client, state)
    assert says in unescape(client.post("/incident/report/review", data={"incident": record.id, **data}).text)
    assert state.incident_store.get(record.id).review == Review()


def test_the_review_needs_an_incident_open(client, state):  # noqa: F811
    assert "Open an incident first" in client.post("/incident/report/review", data={"kind": "x"}).text


def test_it_s_made_once_until_what_it_s_made_of_changes(client, state, monkeypatch):  # noqa: F811
    from gamgui.web.routes import incident_report

    record = _ready(client, state)
    real, calls = incident_report.make, []
    monkeypatch.setattr(incident_report, "make", lambda *args: (calls.append(1), real(*args))[1])
    client.get("/incident/report")
    client.get("/incident/report/list?id=gotIt")
    assert len(calls) == 1
    state.incident_store.add_note(record.id, "Told the insurer", datetime.now(timezone.utc))
    assert "Told the insurer" in client.get("/incident/report").text and len(calls) == 2


def test_what_a_person_typed_is_only_ever_text(client, state):  # noqa: F811
    record = _ready(client, state)
    state.incident_store.add_note(record.id, "<script>x</script>", datetime.now(timezone.utc))
    assert "<script>x" not in client.get("/incident/report").text


# The export


def _export(client) -> zipfile.ZipFile:  # noqa: F811
    made = client.post("/incident/report/export").text
    assert "Save the export" in made, made
    r = client.get(unescape(re.search(r'href="(/incident/report/export\.zip[^"]*)"', made).group(1)))
    assert r.status_code == 200, r.text
    assert r.headers["Content-Disposition"].startswith("attachment; filename=incident-report-2026-")
    return zipfile.ZipFile(io.BytesIO(r.content))


def test_the_export_holds_the_report_each_list_and_the_evidence(client, state):  # noqa: F811
    _ready(client, state)
    archive = _export(client)
    names = set(archive.namelist())
    assert {"Incident report.pdf", "Incident report.md", "Incident report.txt", "who-got-it.csv", "who-it-reached.csv", "emails.csv", "accounts.csv", "steps-taken.csv", "still-to-do.csv", "emails-pulled.csv", "timeline.csv", "evidence-sha256.txt"} <= names
    assert any(n.startswith("evidence/") and n.endswith("/about.txt") for n in names)
    assert archive.read("Incident report.pdf").startswith(b"%PDF-") and archive.read("Incident report.md").startswith(b"# Incident report:") and archive.read("Incident report.txt").startswith(b"INCIDENT REPORT:")


def test_each_evidence_file_s_fingerprint_shows_it_unchanged(client, state):  # noqa: F811
    record = _ready(client, state)
    archive = _export(client)
    lines = archive.read("evidence-sha256.txt").decode().strip().split("\n")
    assert lines
    for line in lines:
        digest, name = line.split("  ", 1)
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    assert lines[0].split("  ")[0] in archive.read("Incident report.txt").decode(), "listed in the report too"
    saved = {f"evidence/{path}": data for path, data, _ in state.incident_store.evidence_files(record.id)}
    assert saved and all(archive.read(name) == data for name, data in saved.items()), "byte for byte"


def test_evidence_that_can_t_be_read_is_said_on_the_screen_and_leaves_nothing(client, state, monkeypatch, tmp_path):  # noqa: F811
    import tempfile

    from gamgui.core.incident.store import IncidentStore, Problem

    _ready(client, state)

    def unreadable(self, record_id):
        raise Problem("Couldn't read the evidence.")
        yield

    monkeypatch.setattr(IncidentStore, "evidence_files", unreadable)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    page = unescape(client.post("/incident/report/export").text)
    assert "wasn't exported: Couldn't read the evidence." in page and not list(tmp_path.glob("gamgui-report-*")) and state.incident_export is None


def test_a_linked_saved_read_stops_the_export_rather_than_being_passed_over(client, state, tmp_path):  # noqa: F811
    record = _ready(client, state)
    evidence = tmp_path / "incidents" / record.id / "evidence"
    (tmp_path / "elsewhere").mkdir()
    (evidence / "linked").symlink_to(tmp_path / "elsewhere")
    assert "wasn't exported" in unescape(client.post("/incident/report/export").text)
    assert ("linked", "") in state.incident_store.saved_about(record.id)


def test_a_stray_file_beside_the_saved_reads_is_neither_listed_nor_exported(client, state, tmp_path):  # noqa: F811
    record = _ready(client, state)
    (tmp_path / "incidents" / record.id / "evidence" / "stray.txt").write_text("not evidence")
    assert "stray.txt" not in [n for n, _ in state.incident_store.saved_about(record.id)]
    assert not any("stray" in n for n in _export(client).namelist())


def test_the_export_is_private_one_use_and_goes_when_saved(client, state, monkeypatch, tmp_path):  # noqa: F811
    import stat
    import tempfile

    _ready(client, state)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    made = client.post("/incident/report/export").text
    held = list(tmp_path.glob("gamgui-report-*.zip"))
    assert len(held) == 1 and stat.S_IMODE(held[0].stat().st_mode) == 0o600
    link = unescape(re.search(r'href="(/incident/report/export\.zip[^"]*)"', made).group(1))
    assert client.get("/incident/report/export.zip?file=wrong").status_code == 404 and held[0].exists()
    assert client.get(link).status_code == 200 and not held[0].exists()
    assert client.get(link).status_code == 404, "one use"


def test_exporting_again_leaves_only_the_newest(client, state, monkeypatch, tmp_path):  # noqa: F811
    import tempfile

    _ready(client, state)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    (tmp_path / "gamgui-report-stranded.zip").write_text("left by a crash")
    client.post("/incident/report/export")
    client.post("/incident/report/export")
    assert len(list(tmp_path.glob("gamgui-report-*.zip"))) == 1 and not (tmp_path / "gamgui-report-stranded.zip").exists()


def test_a_failure_making_the_export_leaves_nothing(client, state, monkeypatch, tmp_path):  # noqa: F811
    import tempfile

    from gamgui.web.routes import incident_report

    _ready(client, state)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def breaks(*args):
        raise ValueError("whatever it was")

    monkeypatch.setattr(incident_report.report_pdf, "pdf_of", breaks)
    with pytest.raises(ValueError):
        client.post("/incident/report/export")
    assert not list(tmp_path.glob("gamgui-report-*"))


def test_export_needs_a_report(client, state):  # noqa: F811
    assert "Open an incident" in client.post("/incident/report/export").text
    assert client.get("/incident/report/export.zip?file=x").status_code == 404


def test_the_export_says_when_it_was_made(client, state, monkeypatch):  # noqa: F811
    from gamgui.web.routes import incident_report

    _ready(client, state)
    client.get("/incident/report")
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    key, made = state.incident_report
    state.incident_report = (key, replace(made, made=old))
    text = _export(client).read("Incident report.txt").decode()
    assert "Report made 2026-01-01" not in text and incident_report._local(old) not in text.split("with GamGUI")[0]


def test_everyone_it_got_to_is_on_respond_to_copy_or_export(client, state):  # noqa: F811
    _pull(client, state, accounts=EVERYONE)
    page = unescape(client.get("/incident/logs/reading?part=respond", headers=ON_INCIDENT).text)
    assert "Everyone it got to:" in page and "Copy addresses" in page and "Export for import" in page and "pat@partner.example" in page
    r = client.get("/incident/logs/got-it.csv")
    assert r.status_code == 200 and r.text.startswith("Email,") and "who-got-it" in r.headers["Content-Disposition"]
    assert "guest@example.org" not in r.text, "if it bounced, they did not get it"


# What the report's review proved: it can't read complete when it isn't


def _say(client) -> str:  # noqa: F811
    return unescape(client.get("/incident/report").text)


def test_it_says_what_it_rests_on(client, state):  # noqa: F811
    _ready(client, state)
    page = _say(client)
    assert "What this report can't show" in page and "The logs read: " in page and f"for {CAROL}, {ALICE}, {BOB}" in page
    text = _export(client).read("Incident report.txt").decode()
    assert "\nWHAT THIS REPORT CAN'T SHOW\n" in text and text.index("WHAT THIS REPORT CAN'T SHOW") < text.index("AT A GLANCE")


def test_a_log_that_stopped_short_or_wasn_t_read_is_said(client, state, gam_state):  # noqa: F811
    from gamgui.core.incident import pull

    (gam_state / "nolog").mkdir()
    (gam_state / "nolog" / "token").write_text("")
    _ready(client, state)
    held = state.incident_pull
    login = held.log("login")
    state.incident_pull = replace(held, logs=[pull.Log.read("login", login.rows, login.hours, True) if log.application == "login" else log for log in held.logs], from_evidence=False)
    state.incident_reading = None
    page = _say(client)
    assert "stopped short: Older events past the cap" in page and "wasn't read:" in page and "isn't saved with the incident" in page


def test_a_report_without_gmail_never_reads_as_no_email_sent(client, state):  # noqa: F811
    _open_new(client, state)
    _pull(client, state, "login", "admin", accounts=CAROL)
    page = _say(client)
    assert "Without the Gmail log, who the attacker emailed" in page and "Not pulled, so nothing from them is here" in page


def test_a_group_not_read_and_a_saved_read_that_can_t_be_are_said(client, state):  # noqa: F811
    from gamgui.core.incident.groups import GroupsRead

    _ready(client, state)
    state.incident_groups = GroupsRead(problems={"dana@example.com": "GAM finds no group at that address"}, nameless={"team@example.com": 2}, asked={"dana@example.com"})
    state.incident_saved_unreadable = ["Broken read"]
    page = _say(client)
    assert "The group dana@example.com counts on the warning's send" in page and "2 of team@example.com's members have no address" in page and "Saved reads that couldn't be read, so aren't counted: Broken read." in page


def test_lists_that_can_t_be_everyone_say_so_in_their_names(client, state):  # noqa: F811
    _ready(client, state)
    _say(client)
    state.incident_groups = None
    from gamgui.core.incident.groups import GroupsRead

    state.incident_groups = GroupsRead(problems={"dana@example.com": "no"})
    names = _export(client).namelist()
    assert "who-got-it-partial.csv" in names and "who-it-reached-partial.csv" in names
    assert "-partial" in client.get("/incident/logs/got-it.csv").headers["Content-Disposition"]


def test_with_no_evidence_it_doesn_t_claim_any():
    made = make(reading(), BASE, [], [], None, at(120), "", minute, saved=False)
    text = text_of(made.blocks(minute))
    assert "from a read of the logs that isn't saved with the incident." in text and "No read of the logs is saved with this incident." in text and "is in the evidence folder" not in text


def test_the_response_is_never_cut_from_what_was_done():
    from .test_incident_reader import ATTACKER, event

    attack = [e for i in range(260) for e in (event("login", CAROL, "login_success", {"is_suspicious": "true", "login_type": "google_password"}, ip=ATTACKER, flagged=True, minute=i),
                                              event("token", CAROL, "authorize", {"client_id": "x", "app_name": "App"}, ip=ATTACKER, minute=i))]
    read = reader.read([*attack, done("CHANGE_PASSWORD", minute=400), done("RESET_SIGNIN_COOKIES", minute=401)], reader.Context(domains={"example.com"}, people=dict(PEOPLE)))
    made = report(read=read)
    assert any("Password reset" in a.what and a.source == "Google's Admin log" for a in made.actions), "a long attack must not push the lock out"
    assert made.timeline_cut > 0
    text = text_of(made.blocks(minute))
    assert f"{made.timeline_cut} more of the attacker's events are in the saved logs" in text


def test_a_line_break_never_forges_a_heading_in_the_text():
    made = report(record=replace(BASE, notes=(Note(at(95), "fine\nSTILL TO DO\n===========\nNothing."),)))
    assert "\nSTILL TO DO\n===========\nNothing." not in text_of(made.blocks(minute))


def test_a_long_word_wraps_fast_and_stays_on_the_page():
    import time

    started = time.monotonic()
    data = report_pdf.pdf_of(report(record=replace(BASE, notes=(Note(at(95), "x" * 20_000),))).blocks(minute))
    assert time.monotonic() - started < 2
    assert "x" * 20 in _pdf_text(data)


def test_a_wide_character_never_runs_off_the_page():
    assert report_pdf._width("—" * 90, 10.5, "F1") >= 945, "counted at least as wide as it is"


def test_each_time_says_its_own_zone():
    from gamgui.web.routes.incident_report import _local

    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d \S+", _local(at(1)))


def test_the_review_is_refused_when_the_form_was_for_another_incident(client, state):  # noqa: F811
    first = _ready(client, state)
    assert f'name="incident" value="{first.id}"' in client.get("/incident/report").text
    second = state.incident_store.create(state.connector.domain, "Second", at(1))
    state.incident_open = second.id
    r = client.post("/incident/report/review", data={"incident": first.id, "cause": "A's cause"})
    assert "for another incident" in unescape(r.text) and r.headers["HX-Retarget"] == "#report-review-said", "what was typed stays where it is"
    assert state.incident_store.get(second.id).review == Review() and state.incident_store.get(first.id).review == Review()


def test_a_review_refused_keeps_what_was_typed(client, state):  # noqa: F811
    record = _ready(client, state)
    r = client.post("/incident/report/review", data={"incident": record.id, "meeting": "0001-01-01", "cause": "typed"})
    assert r.headers["HX-Retarget"] == "#report-review-said" and r.headers["HX-Reswap"] == "innerHTML"


def test_evidence_that_can_t_be_listed_is_said_not_a_500(client, state, tmp_path, monkeypatch):  # noqa: F811
    from gamgui.core.incident.store import IncidentStore

    _ready(client, state)

    def refused(self, record_id):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(IncidentStore, "saved_about", refused)
    r = client.get("/incident/report")
    assert r.status_code == 200 and "The saved reads couldn't be listed: Permission denied" in unescape(r.text)
    assert client.get("/incident/report/list?id=gotIt").status_code == 200


def test_it_never_caches_an_old_reading_under_a_new_key(client, state, monkeypatch):  # noqa: F811
    from gamgui.web.routes import incident_report

    _ready(client, state)
    real = incident_report._directory

    async def cleared_meanwhile(st):
        st.incident_reading = None
        return await real(st)

    monkeypatch.setattr(incident_report, "_directory", cleared_meanwhile)
    assert client.get("/incident/report").status_code == 200, "a read landing during it mustn't break it"


def test_a_pull_going_on_is_said(client, state):  # noqa: F811
    from gamgui.web.jobs import start_job

    _ready(client, state)
    state.incident_pull_job = start_job(state.jobs, 1, kind="incident_logs", title="x").id
    assert "A pull of the logs is going on" in _say(client)
    state.jobs[state.incident_pull_job].finish()
