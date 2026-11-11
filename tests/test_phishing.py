"""Phishing (back-port plan D1-D3): reading GAM's dry run and Spam read, and the run they lead to. The cases are SwiftGamGui's,
which learned each from GAM's source. Trash by query is argv-identical to its live runs; Spam hasn't run live in either app."""

from __future__ import annotations

import ast
import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gamgui.core import phishing
from gamgui.core.audit import AuditLog
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.commands import GAMCommands, MessageId, id_list
from gamgui.core.gam.errors import GAMError
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.phishing import EmailSearch, Found, Incomplete, addresses_in, parse_message_ids, read_copies, read_recipients, read_spam_copies
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.web.server import AppState, create_app

from .helpers import OAUTH2_FAKE, TEST_HOSTS, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
DOMAIN = "example.com"
PHISH = "CAPh1sh+Notice=0001@mail.example.com"
M = MessageId(PHISH)


# --- D1: a Message-ID only ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [PHISH, f"<{PHISH}>", f"Message-ID: <{PHISH}>", f"message-id:{PHISH}", f"rfc822msgid:{PHISH}", f"  <{PHISH}>\n"])
def test_what_an_operator_copies_reads_as_the_id(text):
    assert MessageId.parse(text).id == PHISH


@pytest.mark.parametrize("text,says", [
    ("", "Paste"), ("a b@c.d", "no spaces"), ("{a}@b.c", "{, } or |"), ("a|b@c.d", "{, } or |"), ("a@b@c.d", "exactly one @"), ("nobody", "exactly one @"),
    (".a@b.c", "single dots"), ("a..b@c.d", "single dots"), ("a@b.", "single dots"), ("a,b@c.d", "“,”"), ('a"b@c.d', '“"”'),
    ("a:b@c.d", "“:”"), ("a(b)@c.d", "“(”"), ("a\\b@c.d", "“\\”"), ("é@c.d", "U+00E9"), ("a*@c.d", "“*”"), ("x" * 999 + "@c.d", "longer"),
    ("from:boss@example.com", "“:”"), ("a@b.c OR b@c.d", "no spaces"),
])
def test_anything_else_is_refused_with_what_is_wrong(text, says):
    with pytest.raises(ValueError, match=re.escape(says)):
        MessageId.parse(text)


def test_the_queries_keep_the_senders_sent_copy():
    assert M.query == f"rfc822msgid:{PHISH} -in:sent"
    assert M.any_copy_query == f"rfc822msgid:{PHISH}" and M.spam_query == f"rfc822msgid:{PHISH} in:spam"


def test_several_ids_a_line_each_or_between_commas():
    assert [m.id for m in parse_message_ids("a@b.c\n<d@e.f>,\n\na@b.c")] == ["a@b.c", "d@e.f"]
    assert [m.id for m in parse_message_ids(f"Message-ID:\n<{PHISH}>")] == [PHISH]


def test_a_bad_line_is_named_and_there_is_a_limit():
    with pytest.raises(ValueError, match="Line 2:"):
        parse_message_ids("a@b.c\nnope")
    with pytest.raises(ValueError, match="At most 100"):
        parse_message_ids("\n".join(f"m{i}@b.c" for i in range(101)))


# --- the argvs ----------------------------------------------------------------------------------------------------------


def test_each_argv_is_swift_gamgui_s():
    c = GAMCommands
    assert c.find_message_copies(M) == ["all", "users", "trash", "messages", "query", M.query, "notquick", "max_to_trash", "1"]
    assert c.trash_message("a@e.com", M, 2) == ["user", "a@e.com", "trash", "messages", "query", M.query, "notquick", "max_to_trash", "2", "doit"]
    assert c.find_spam_copies(M) == ["all", "users", "print", "messages", "query", M.spam_query, "includespamtrash", "headers", "message-id", "showlabels"]
    assert c.trash_messages("a@e.com", ["x1", "x2"]) == ["user", "a@e.com", "trash", "messages", "ids", "x1,x2"]


def test_the_dry_run_and_the_spam_read_carry_no_doit():
    assert "doit" not in GAMCommands.find_message_copies(M) and "doit" not in GAMCommands.find_spam_copies(M)


@pytest.mark.parametrize("copies", [0, -1])
def test_no_cap_below_one_since_gam_reads_zero_as_no_limit(copies):
    with pytest.raises(ValueError):
        GAMCommands.trash_message("a@e.com", M, copies)


@pytest.mark.parametrize("ids", [[], ["a,b"], ["a b"], ["file"], ["CSV_File"]])
def test_each_message_id_is_one_element(ids):
    assert id_list(ids) is None
    with pytest.raises(ValueError):
        GAMCommands.trash_messages("a@e.com", ids)


def test_only_a_reviewed_read_may_use_the_any_exit_door():
    # It's how a write could slip past _run_write, so its callers are named here.
    source = (Path(__file__).parent.parent / "gamgui" / "core" / "connectors" / "gam_connector.py").read_text()
    callers = set()
    for fn in ast.walk(ast.parse(source)):
        if isinstance(fn, ast.AsyncFunctionDef):
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "run_any_exit":
                    callers.add(fn.name)
    # _courses_read: `print courses` (a READS builder), whose exit 56 can be only unknown-owner warnings over a whole table.
    assert callers == {"find_message_copies", "find_spam_copies", "mailbox_copy", "script_projects", "_courses_read"}


# --- MessageCopies ------------------------------------------------------------------------------------------------------

GOOD = """Got 4 Users: alice@example.com - dana@example.com
User: alice@example.com, Message: 1, Not Trashed: Use the "doit" argument to perform action (1/4)
User: carol@example.com, Messages: 0, Not Trashed: No Messages matched (2/4)
User: dana@example.com, Messages: 2, Not Trashed: Count 2 exceeds maximum to Trash 1 (3/4)
User: erin@example.com, Gmail Service/App not enabled (4/4)
"""


def test_the_lines_are_the_result_not_the_exit_code():
    copies = read_copies(60, GOOD, ["x"])
    assert [(m.email, m.copies) for m in copies.with_copies] == [("alice@example.com", 1), ("dana@example.com", 2)]
    assert [m.email for m in copies.unsearched] == ["erin@example.com"] and copies.unsearched[0].why_unsearched == "Gmail is off"
    assert not copies.looks_like_missing_scope


def test_a_users_failure_is_that_mailbox_never_clear():
    copies = read_copies(50, "User: a@e.com, Trash Failed: quota\nUser: b@e.com, User Trash Failed: nope\nUser: c@e.com, Some new words\n", ["x"])
    assert [m.failed for m in copies.unsearched] == ["quota", "nope", "Some new words"]


def test_every_mailbox_gmail_off_looks_like_a_missing_scope():
    assert read_copies(73, "User: a@e.com, Gmail Service/App not enabled\n", ["x"]).looks_like_missing_scope


@pytest.mark.parametrize("stderr,says", [
    ("", "no mailbox at all"),
    ("Got 3 Users: a - c\nUser: a@e.com, Messages: 0, Not Trashed: No Messages matched (1/3)\n", "of the 3"),
    ("User: a@e.com, Messages: 0, Not Trashed: No Messages matched (1/2)\n", "of the 2"),
    ("User: a@e.com, Messages: 0, Not Trashed: No Messages matched (1/2)\nUser: b@e.com, Messages: 0, Not Trashed: No Messages matched (2/3)\n", "disagree"),
    ("User: a@e.com, Messages: 0, Not Trashed: No Messages matched\nUser: a@e.com, Messages: 0, Not Trashed: No Messages matched\n", "twice"),
])
def test_a_list_that_did_not_come_back_whole_is_refused(stderr, says):
    with pytest.raises(Incomplete, match=says):
        read_copies(60, stderr, ["x"])


def test_a_domain_past_one_page_of_users_counts_the_last_page():
    # GAM overwrites its page counts on one line (writeGotMessage), so a real run's line holds them all. Split on newlines only,
    # the first page was the count, and no domain over one page could be searched.
    lines = "".join(f"User: u{i}@e.com, Messages: 0, Not Trashed: No Messages matched ({i}/742)\n" for i in range(1, 743))
    stderr = f"Getting all Users, may take some time on a large Google Workspace Account...\n\rGot 500 Users...\rGot 742 Users...\r\n{lines}"
    assert len(read_copies(60, stderr, ["x"]).mailboxes) == 742
    getting = "".join(f"Getting all Messages that match query ((rfc822msgid:x in:spam)) for u{i}@e.com ({i}/742)\n" for i in range(1, 743))
    assert len(read_spam_copies(60, f"\rGot 500 Users...\rGot 742 Users...\r\n{getting}", [], ["x"], M).searched) == 742


def test_a_page_count_past_the_users_reported_is_still_refused():
    with pytest.raises(Incomplete, match="of the 742"):
        read_copies(60, "\rGot 500 Users...\rGot 742 Users...\r\nUser: a@e.com, Messages: 0, Not Trashed: No Messages matched\n", ["x"])


def test_an_exit_code_no_user_sets_is_the_command_failing():
    with pytest.raises(GAMError):
        read_copies(2, "Command: gam x\n\nERROR: Invalid argument\n", ["x"])


def test_an_account_wide_failure_among_the_other_lines_is_the_command_failing():
    with pytest.raises(GAMError):
        read_copies(50, "ERROR: invalid_grant: token has been expired or revoked\nUser: a@e.com, Messages: 0, Not Trashed: No Messages matched\n", ["x"])


# --- SpamCopies ---------------------------------------------------------------------------------------------------------


def _spam_row(user="a@e.com", gmail_id="x1", mid=f"<{PHISH}>", labels="SPAM", count="1"):
    return {"User": user, "id": gmail_id, "Message-ID": mid, "Labels": labels, "LabelsCount": count}


GETTING = "Getting all Messages that match query ((rfc822msgid:x in:spam)) for a@e.com\n"


def test_a_copy_in_spam_is_kept_by_its_id():
    spam = read_spam_copies(0, GETTING, [_spam_row(), _spam_row(gmail_id="x2")], ["x"], M)
    assert spam.mailboxes[0].ids == ("x1", "x2") and spam.total == 2 and spam.searched == ("a@e.com",)


@pytest.mark.parametrize("row", [_spam_row(labels="TRASH"), _spam_row(labels="SENT")])
def test_a_copy_in_trash_or_the_senders_sent_copy_is_not_one_to_trash(row):
    spam = read_spam_copies(0, GETTING, [row], ["x"], M)
    assert not spam.mailboxes and not spam.unsearched


@pytest.mark.parametrize("row", [_spam_row(mid="<other@e.com>"), _spam_row(labels="INBOX"), _spam_row(labels="SPAM My Label", count="2"),
                                 _spam_row(gmail_id="a,b")])
def test_a_copy_not_plainly_this_email_in_spam_leaves_the_mailbox_unsearched(row):
    spam = read_spam_copies(0, GETTING, [row], ["x"], M)
    assert not spam.mailboxes and "by hand" in spam.unsearched[0][1]


def test_a_copy_for_a_user_it_did_not_report_is_refused():
    with pytest.raises(Incomplete, match="didn't report"):
        read_spam_copies(0, GETTING, [_spam_row(user="z@e.com")], ["x"], M)


def test_gmail_off_in_the_spam_read_is_said():
    spam = read_spam_copies(73, "User: a@e.com, Gmail Service/App not enabled\n", [], ["x"], M)
    assert spam.unsearched == (("a@e.com", "Gmail is off"),)


# --- recipients ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("header,addresses,unreadable", [
    ("a@e.com", ["a@e.com"], []),
    ("A <a@e.com>, B <b@e.com>", ["a@e.com", "b@e.com"], []),
    ('"Doe, Jane" <jane@e.org>', ["jane@e.org"], []),
    ("Doe, Jane <jane@e.org>", ["jane@e.org"], []),
    ('"fake@evil.org" <real@e.com>', ["real@e.com"], []),
    ("(fake@evil.org) real@e.com", ["real@e.com"], []),
    ("Team:a@e.com,b@e.com;", ["a@e.com", "b@e.com"], []),
    ("<a@e.com> <b@e.com>", [], ["<a@e.com> <b@e.com>"]),
    ("a@e.com b@e.com", [], ["a@e.com b@e.com"]),
    ("Open <a@e.com", ["a@e.com"], []),
    ("not an address @", [], ["not an address @"]),
    ("", [], []),
])
def test_addresses_are_read_as_rfc_5322_says(header, addresses, unreadable):
    assert addresses_in(header) == (addresses, unreadable)


def test_each_address_once_with_its_header_and_never_delivered_to():
    copy = read_recipients([{"To": "a@e.com", "To 1": "b@e.com", "Cc": "A@e.com, c@e.com", "CC": "d@e.com", "Bcc": "e@e.com", "Delivered-To": "x@e.com",
                             "From": "s@e.com", "Subject": "Hi", "Labels": "SENT INBOX", "Date": "D"}])
    assert copy.recipients[:2] == (("a@e.com", "To"), ("b@e.com", "To")) and copy.has_bcc and copy.sent_by_them
    assert "x@e.com" not in [a for a, _ in copy.recipients] and ("e@e.com", "Bcc") in copy.recipients
    assert sum(a.lower() == "a@e.com" for a, _ in copy.recipients) == 1


def test_no_copy_is_none_and_two_headings_are_said():
    assert read_recipients([]) is None
    two = read_recipients([{"To": "a@e.com", "From": "x", "Subject": "1"}, {"To": "a@e.com", "From": "y", "Subject": "2"}])
    assert len(two.headings) == 2


def test_outside_comes_first_and_unreadable_is_kept():
    copy = read_recipients([{"To": "in@example.com, out@evil.org, a@e.com b@e.com"}])
    assert phishing.recipients_rows(copy, ["example.com"]) == [
        ["out@evil.org", "outside", "To"], ["in@example.com", "inside", "To"], ["a@e.com b@e.com", "check by hand", "To"]]


# --- Found and the steps ------------------------------------------------------------------------------------------------


def _search(message=M, copies=(("a@e.com", 1),), spam=(), unsearched=(), spam_unsearched=(), spam_none=False):
    boxes = tuple(phishing.Mailbox(e, copies=n) for e, n in copies) + tuple(phishing.Mailbox(e, failed="no") for e in unsearched)
    spam_read = None if spam_none else phishing.SpamCopies(tuple(phishing.SpamMailbox(e, ids) for e, ids in spam), tuple((e, "no") for e in spam_unsearched),
                                                           tuple(e for e, _ in spam))
    return phishing.EmailSearch(message, phishing.MessageCopies(boxes), spam_read, "" if not spam_none else "failed")


def test_a_mailbox_with_copies_in_both_places_has_two_steps():
    found = phishing.Found([_search(copies=(("a@e.com", 2),), spam=(("a@e.com", ("x1",)),))])
    steps = phishing.trash_steps(found)
    assert [(s.email, s.copies, s.in_spam) for s in steps] == [("a@e.com", 2, False), ("a@e.com", 1, True)] and found.step_count == 2


def test_found_nowhere_with_something_unsearched_is_unsure_never_clear():
    clear = _search(copies=())
    unsure = _search(MessageId("u@e.com"), copies=(), unsearched=("z@e.com",))
    no_spam_read = _search(MessageId("v@e.com"), copies=(), spam_none=True)
    found = phishing.Found([clear, unsure, no_spam_read])
    assert found.nobody_has == [clear] and found.unsure == [unsure, no_spam_read] and found.with_copies == []


def test_a_line_neither_reader_knows_is_unsure_never_clear():
    # The Spam read counts a mailbox once its "Getting all" line came: a failure after it without "User: " was read as nothing.
    copies = read_copies(60, "User: a@e.com, Messages: 0, Not Trashed: No Messages matched\n", ["x"])
    spam = read_spam_copies(60, "\rGot 1 User...\r\nGetting all Messages that match query ((rfc822msgid:a@b.c in:spam)) for a@e.com\n"
                                "ERROR: 500: backendError - Unknown Error\n", [], ["x"], M)
    search = EmailSearch(M, copies, spam)
    assert search.gam_also_said == ("ERROR: 500: backendError - Unknown Error",) and not search.searched_everywhere
    found = Found([search])
    assert found.unsure == [search] and found.nobody_has == []


def test_gam_also_said_is_on_the_screen(client, state):
    copies = read_copies(60, "User: a@e.com, Messages: 0, Not Trashed: No Messages matched\nA line nobody knows\n", ["x"])
    state.phishing_found = Found([EmailSearch(M, copies, read_spam_copies(60, GETTING, [], ["x"], M))])
    page = client.get("/phishing").text
    assert "GAM also said" in page and "A line nobody knows" in page and "every active mailbox and its Spam searched" not in page


def test_the_csv_says_not_searched_never_a_zero():
    rows = phishing.copies_rows(_search(copies=(("a@e.com", 1),), unsearched=("z@e.com",), spam=(("a@e.com", ("x1",)),), spam_unsearched=("q@e.com",)))
    assert rows[0] == ["a@e.com", "copies", "1", "1", ""] and rows[1][1] == "not searched: no" and rows[1][3] == "not searched"
    assert rows[2][0] == "q@e.com" and rows[2][3] == "not searched: no"


# --- through the page and the mock --------------------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": OAUTH2_FAKE, "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    return AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t")


@pytest.fixture
def client(state):
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _find(client, state, messages=PHISH) -> str:
    client.post("/phishing/find", data={"messages": messages})
    job = state.jobs[state.phishing_job_id]
    wait_for_job(client, job)
    return unescape(client.get(f"/phishing/find/status?job={job.id}").text)


def _token(html):
    m = re.search(r'"preview": "([A-Za-z0-9_\-]+)"', unescape(html))
    return m.group(1) if m else ""


def _trashes(gam_calls):
    return [a for a in gam_calls() if "trash" in a and "doit" in a or a[2:5] == ["trash", "messages", "ids"]]


def test_the_page_and_nav_offer_it(client):
    page = client.get("/phishing").text
    assert "Find copies" in page and 'href="/phishing"' in page


def test_find_reads_and_changes_nothing(client, state, gam_calls):
    page = _find(client, state)
    assert "alice@example.com" in page and "Trash the copies" in page
    assert ["all", "users", "trash", "messages", "query", M.query, "notquick", "max_to_trash", "1"] in gam_calls()
    assert GAMCommands.find_spam_copies(M) in gam_calls()
    assert not [a for a in gam_calls() if "doit" in a]


def test_trash_runs_one_step_per_mailbox_from_its_preview(client, state, gam_calls, tmp_path):
    _find(client, state)
    found = state.phishing_found
    count = found.step_count
    confirm = client.post("/phishing/trash/preview").text
    assert f"Type {count}" in confirm and _trashes(gam_calls) == []
    r = client.post("/phishing/trash/apply", data={"confirmed": "1", "preview": _token(confirm), "confirm_count": str(count)})
    assert r.status_code == 200
    job = next(j for j in state.jobs.values() if j.kind == "phishing")
    wait_for_job(client, job)
    assert job.finished and job.total == count and len(_trashes(gam_calls)) == count
    assert state.phishing_found is None       # its counts are from before the run
    log = (tmp_path / "audit.jsonl").read_text()
    assert PHISH in log and '"trash_message"' in log


def test_the_wrong_count_runs_nothing(client, state, gam_calls):
    _find(client, state)
    confirm = client.post("/phishing/trash/preview").text
    refused = client.post("/phishing/trash/apply", data={"confirmed": "1", "preview": _token(confirm), "confirm_count": "999"}).text
    assert "type" in refused.lower() and _trashes(gam_calls) == []


def test_a_token_runs_once_and_a_tenant_switch_spends_it(client, state, gam_calls, tmp_path):
    _find(client, state)
    token = _token(client.post("/phishing/trash/preview").text)
    state.activate(GAMConnector(runner=state.runner, domain="other.example", audit=AuditLog(tmp_path / "o.jsonl")))
    refused = client.post("/phishing/trash/apply", data={"confirmed": "1", "preview": token, "confirm_count": "1"}).text
    assert "expired" in refused or "changed" in refused
    assert _trashes(gam_calls) == [] and state.phishing_found is None


def test_a_preview_from_an_older_find_is_refused_however_python_reuses_its_memory(client, state, gam_calls):
    # A preview once bound to id(found), which Python reuses as soon as the old Found is freed.
    _find(client, state)
    old = _token(client.post("/phishing/trash/preview").text)
    state.phishing_found = None
    _find(client, state)
    refused = client.post("/phishing/trash/apply", data={"confirmed": "1", "preview": old, "confirm_count": "2"}).text
    assert "find again" in unescape(refused) and not [a for a in gam_calls() if a[0] == "user" and a[2:4] == ["trash", "messages"]]


def test_find_and_the_confirm_step_show_what_the_id_matched(client, state):
    page = unescape(_find(client, state))
    search = state.phishing_found.searches[0]
    assert search.sample and search.sample[1].subject and search.sample[1].subject in page and "check this is the email" in page
    assert search.sample[1].subject in unescape(client.post("/phishing/trash/preview").text)


def test_a_sample_that_can_t_be_read_is_said_and_blocks_nothing(client, state, monkeypatch):
    async def locked(email, message):
        raise PermissionError("the Keychain said no")

    monkeypatch.setattr(state.connector, "mailbox_copy", locked)
    page = unescape(_find(client, state))
    assert "Couldn't read a copy to show what this ID matched" in page and state.phishing_found.step_count > 0


def test_a_sender_written_subject_is_only_ever_text(client, state):
    copies = read_copies(60, "User: a@e.com, Message: 1, Not Trashed: Use the \"doit\" argument to perform action\n", ["x"])
    copy = read_recipients([{"Subject": "<script>x</script>", "From": "<img src=x>", "Labels": "INBOX"}])
    state.phishing_found = Found([EmailSearch(M, copies, sample=("a@e.com", copy))])
    page = client.get("/phishing").text
    assert "<script>x</script>" not in page and "<img src=x>" not in page and "&lt;script&gt;x" in page


def test_recipients_that_fail_without_a_gam_error_are_said(client, state, monkeypatch):
    async def locked(email, message):
        raise RuntimeError("no binary")

    monkeypatch.setattr(state.connector, "mailbox_copy", locked)
    r = client.post("/phishing/recipients", data={"sender": "alice@example.com", "message": PHISH})
    assert r.status_code == 200 and "no binary" not in r.text


def test_nothing_to_trash_says_so(client):
    assert "No mailbox has a copy" in client.post("/phishing/trash/preview").text


def test_a_bad_id_reaches_no_gam(client, gam_calls):
    page = client.post("/phishing/find", data={"messages": "from:boss@example.com"}).text
    assert "can&#39;t contain" in page or "can't contain" in page
    assert not [a for a in gam_calls() if "trash" in a]


def test_recipients_read_the_senders_copy_and_never_claim_everyone(client, gam_calls):
    page = unescape(client.post("/phishing/recipients", data={"sender": "carol@example.com", "message": PHISH}).text)
    assert "Outside" in page and "This may not be everyone" in page and "Email Log Search" in page
    assert not [a for a in gam_calls() if "doit" in a]
    csv = client.get("/phishing/recipients.csv")
    assert csv.status_code == 200 and "address,where,header" in csv.text


def test_recipients_with_no_copy_say_so(client):
    page = unescape(client.post("/phishing/recipients", data={"sender": "alice@example.com", "message": "nope-nothing@mail.example.com"}).text)
    assert "no copy of this email left" in page


def test_the_copies_csv_is_inert_and_whole(client, state):
    _find(client, state)
    text = client.get("/phishing/copies.csv").text
    assert text.startswith("message_id,mailbox,found,copies,in_spam,outcome") and PHISH in text.splitlines()[1] or text.splitlines()[1].startswith("'")


def test_an_email_that_cannot_be_searched_is_said_beside_the_rest(client, state, monkeypatch):
    conn = state.connector
    real = conn.find_message_copies

    async def one_fails(message):
        if message.id == "bad@mail.example.com":
            raise Incomplete("GAM reported 1 of the 2 users it searched.")
        return await real(message)

    monkeypatch.setattr(conn, "find_message_copies", one_fails)
    page = _find(client, state, f"bad@mail.example.com\n{PHISH}")
    assert "Couldn't search <bad@mail.example.com>" in page and "alice@example.com" in page


def test_a_read_that_ends_after_a_switch_is_not_kept(client, state, tmp_path):
    client.post("/phishing/find", data={"messages": PHISH})
    job = state.jobs[state.phishing_job_id]
    state.activate(GAMConnector(runner=state.runner, domain="other.example", audit=AuditLog(tmp_path / "o.jsonl")))
    wait_for_job(client, job)
    assert state.phishing_found is None and "Not kept" in job.error
