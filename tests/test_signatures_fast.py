"""Roadmap 5.4: the GAM-native fast path for a signature run, opt-in beside the per-user loop.

One `file - print signature` reads everyone's current signature and each is kept (roadmap 4.1), then one `csv -` sets them
all, the rows on standard input. The rows are held to GAM 7.52.05's own reader (csv.DictReader after sys.stdin.read(), with
the delimiter, quote and no escape character the argv names), and the result is GAM's own "Signature: <u>, Set" line per
person, never the exit code: GAM's csv parent takes none of its children's. tests/fixtures/signatures_fast.csv is the
exact bytes SwiftGamGui must send for the same rows.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from gamgui.core import signatures_fast as fast
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.signature_backups import Entry

from .helpers import gam_writes, wait_for_job
from .test_users_web import _job, _sig_preview, client  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
ALICE, CAROL = "alice@example.com", "carol@example.com"
COMPANY = {"template": "<b>{name}</b>", "scope_type": "company", "scope_value": ""}

FIXTURE_ROWS = [
    (ALICE, '<div dir="ltr">Alice “A” Anders, Café — Sales\r\n\\o/ 100% ~~User~~</div>'),
    (CAROL, "Carol, \"Ops\"\rline two\n"),
]


def _gam_reads(text: str) -> list:
    """What GAM's csv makes of the rows: sys.stdin.read() (universal newlines), then DictReader with the argv's options."""
    stdin = io.TextIOWrapper(io.BytesIO(text.encode("ascii")), encoding="ascii", newline=None).read()
    reader = csv.DictReader(io.StringIO(stdin), delimiter=",", quotechar='"', escapechar=None)
    return [(r["User"], r["signature"]) for r in reader]


@pytest.mark.parametrize("body", [
    'a "quoted", comma', "back\\slash and \\\" escape", "line\r\nbreaks\rand\nmore", "  leading and trailing  ",
    "Café — “smart” ✓", "~~User~~ and ~signature", "%datetime%", "tab\there", "x" * 5000,
])
def test_each_row_reads_back_as_sent(body):
    assert _gam_reads(fast.rows_csv([(ALICE, body), (CAROL, "plain")])) == [(ALICE, fast.as_sent(body)), (CAROL, "plain")]


def test_what_is_sent_is_ascii_with_plain_line_breaks():
    sent = fast.as_sent("Café\r\nline — two\rthree")
    assert sent == "Caf&#233;\nline &#8212; two\nthree" and sent.isascii()


def test_the_rows_match_the_fixture_byte_for_byte():
    assert fast.rows_csv(FIXTURE_ROWS).encode() == (FIXTURES / "signatures_fast.csv").read_bytes()


@pytest.mark.parametrize("body,why", [("é" * 25000, "Too long"),   # 150,000 characters as sent: over GAM's csv field limit
                                      ("", "empty"), ("   \n", "empty"), ("file", "as a file"), ("  HTML_File ", "as a file"),
                                      ("a\0b", "one row")])
def test_a_row_gam_s_csv_would_mishandle_is_refused(body, why):
    assert why in fast.refusal(ALICE, body)


def test_only_gam_s_own_set_line_counts():
    out = ("User: alice@example.com, Signature: alice@example.com, Set\n"
           "User: carol@example.com, Signature: carol@example.com, Set (2/2)\n"
           "User: dave@example.com, Signature: dave@example.com, Set\n"            # not one of ours
           "User: erin@example.com, Signature: frank@example.com, Set\n"
           "User: erin@example.com, SendAs Address: <erin@example.com>, Updated\n")  # not GAM's wording for this
    assert fast.updated(out, [ALICE, CAROL, "erin@example.com", "Alice@Example.com"]) == {ALICE, CAROL, "Alice@Example.com"}


def test_a_read_is_only_who_came_back_whole():
    out = ("User,displayName,isDefault,isPrimary,replyToAddress,sendAsEmail,signature,treatAsAlias,verificationStatus\n"
           "alice@example.com,,True,True,,alice@example.com,\"Best,<br>Alice\",False,accepted\n"
           "carol@example.com,,True,True,,carol@example.com,Carol,False,accepted\n")
    err = "Getting Signature for alice@example.com (1/3)\n"          # carol's row came without GAM saying it read her
    assert fast.read_signatures(out, err, [ALICE, CAROL, "dan@example.com"]) == {ALICE: "Best,<br>Alice"}


# --- the run ---------------------------------------------------------------------------------------------------------

def _preview(client):  # noqa: F811
    page, token = _sig_preview(client, **COMPANY)
    assert token, page
    return page, token


def _fast(client, token, **extra):  # noqa: F811
    r = client.post("/signatures/apply", data={**COMPANY, "confirmed": "1", "preview": token, "fast": "1", **extra})
    job = _job(client, r.text, "/signatures/apply/status")
    wait_for_job(client, job)
    return job


def test_two_gam_calls_set_everyone_and_keep_what_they_had(client, gam_calls, gam_state):  # noqa: F811
    _, token = _preview(client)
    job = _fast(client, token)
    assert (job.kind, job.applied, job.failed_total) == ("signatures_fast", 2, 0)
    assert [c for c in gam_calls() if "signature" in c] == [GAMCommands.print_signatures_from_stdin(),
                                                            GAMCommands.set_signatures_csv()]   # no per-person call
    [kept] = client.app.state.gamgui.signature_backups.runs()
    assert kept.entries == [Entry(ALICE, "Best,<br>Alice", "<b>Alice Anders</b>"),
                            Entry(CAROL, "Carol Clark<br>Operations", "<b>Carol Clark</b>")]
    assert (gam_state / "signatures" / ALICE).read_text() == "<b>Alice Anders</b>"
    [record] = [e for e in client.app.state.gamgui.connector.audit.tail() if e["action"] == "set_signatures_fast"]
    assert record["ok"] is True and record["extra"]["count"] == "2" and record["extra"]["set"] == "2"
    assert "Alice Anders" not in client.app.state.gamgui.connector.audit.path.read_text()   # the bodies went on stdin


def test_someone_not_read_is_not_written(client, gam_calls, gam_state):  # noqa: F811
    (gam_state / "quiet").mkdir()
    (gam_state / "quiet" / ALICE).write_text("")                         # GAM skips her without a word
    _, token = _preview(client)
    job = _fast(client, token)
    assert (job.applied, job.failed_items) == (1, [ALICE]) and "wasn't replaced" in job.failed[0].reason
    assert not (gam_state / "signatures" / ALICE).exists()
    assert [e.email for e in client.app.state.gamgui.signature_backups.runs()[0].entries] == [CAROL]


def test_a_person_gam_doesn_t_say_it_set_is_failed_and_can_be_retried(client, gam_calls, gam_state, monkeypatch):  # noqa: F811
    runner = client.app.state.gamgui.connector.runner
    real = runner.run_authenticated

    async def carol_unsaid(domain, argv, **kw):   # exit 0, and no line for carol: unknown, never "set"
        out = await real(domain, argv, **kw)
        if argv != GAMCommands.set_signatures_csv():
            return out
        return "\n".join(line for line in out.splitlines() if CAROL not in line) + "\n"
    monkeypatch.setattr(runner, "run_authenticated", carol_unsaid)
    _, token = _preview(client)
    job = _fast(client, token)
    assert (job.applied, job.failed_items) == (1, [CAROL]) and job.failed[0].reason == fast.NOT_SAID
    monkeypatch.setattr(runner, "run_authenticated", real)
    retry = client.post("/signatures/retry", data={"job": job.id})
    assert "Applies to <strong>1</strong> user" in retry.text and "one GAM call" not in retry.text   # Retry is per person


def test_it_has_no_stop(client, gam_state):  # noqa: F811
    _, token = _preview(client)
    job = _fast(client, token)
    job.finished = False
    assert not job.can_stop


def test_the_fast_path_needs_its_preview(client, gam_calls):  # noqa: F811
    r = client.post("/signatures/apply", data={**COMPANY, "confirmed": "1", "fast": "1"})
    assert "apply/status" not in r.text and gam_writes(gam_calls()) == []


def test_the_preview_offers_it_only_for_more_than_one(client):  # noqa: F811
    page, _ = _preview(client)
    assert "in one GAM call" in page and "no live ✓/✗ list and no Stop" in page
    one, _ = _sig_preview(client, template="{name}", scope_type="user", scope_value=ALICE)
    assert "sig-fast-btn" not in one


def test_a_failing_exit_partway_still_writes_only_who_was_read(client, gam_calls, gam_state):  # noqa: F811
    # Gmail off for alice: GAM warns and exits 73 for her, and carol's row still comes back. Exit 73 isn't "nobody read",
    # and alice's missing row isn't "no signature" (invariant 10).
    (gam_state / "nogmail").mkdir()
    (gam_state / "nogmail" / ALICE).write_text("")
    _, token = _preview(client)
    job = _fast(client, token)
    assert (job.applied, job.failed_items) == (1, [ALICE])
    assert (gam_state / "signatures" / CAROL).exists() and not (gam_state / "signatures" / ALICE).exists()


def test_a_read_that_fails_outright_writes_nobody(client, gam_calls, gam_state, monkeypatch):  # noqa: F811
    async def broken(*a, **k):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(client.app.state.gamgui.connector.runner, "run_any_exit", broken)
    _, token = _preview(client)
    job = _fast(client, token)
    assert (job.applied, sorted(job.failed_items)) == (0, [ALICE, CAROL])
    assert gam_writes(gam_calls()) == [] and client.app.state.gamgui.signature_backups.runs() == []


def test_a_switch_during_the_read_writes_nothing(client, gam_calls, gam_state, monkeypatch):  # noqa: F811
    # Review finding: the domain switch's Stop landed during the backup read, and the write still ran.
    conn = client.app.state.gamgui.connector
    real = conn.signatures_exact
    jobs = client.app.state.gamgui.jobs

    async def switched_meanwhile(emails):
        read = await real(emails)
        for j in jobs.values():
            j.stop_why, j.cancel_requested = "Stopped: you switched to other.example", True
        return read
    monkeypatch.setattr(conn, "signatures_exact", switched_meanwhile)
    _, token = _preview(client)
    job = _fast(client, token)
    assert gam_writes(gam_calls()) == [] and job.applied == 0
    assert job.error == "Stopped: you switched to other.example — 2 not attempted."
