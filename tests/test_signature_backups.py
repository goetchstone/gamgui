"""Signature backups (roadmap 4.1): each person's signature as it was, read byte for byte and kept on disk before a run
overwrites it. A signature that can't be read isn't overwritten; one that can't be kept stops the run."""

from __future__ import annotations

import os
import stat

import pytest

from gamgui.core import signature_backups as sb
from gamgui.core.gam.commands import GAMCommands
from gamgui.core.gam.errors import GAMError
from gamgui.core.phishing import Incomplete

from .helpers import gam_writes, wait_for_job
from .test_users_web import _job, _sig_apply, _sig_preview, client  # noqa: F401

ALICE, CAROL = "alice@example.com", "carol@example.com"
TRICKY = '  <div dir="ltr">Ünïcode &amp; “quotes”\n\tand a tab</div>\n'   # leading spaces, a trailing newline


# --- the store ----------------------------------------------------------------------------------------------------------

def test_a_run_keeps_each_signature_byte_for_byte_and_privately(tmp_path):
    store = sb.SignatureBackups(tmp_path / "kept")
    run = store.start("sales@example.com")
    run.keep(ALICE, TRICKY, "<b>new</b>")
    run.keep(CAROL, "", "<b>new</b>")          # no signature before: kept as empty, put back as empty
    run.close()
    [loaded] = store.runs()
    assert loaded.what == "sales@example.com" and loaded.entries == [sb.Entry(ALICE, TRICKY, "<b>new</b>"), sb.Entry(CAROL, "", "<b>new</b>")]
    assert stat.S_IMODE(os.stat(tmp_path / "kept").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(run.path).st_mode) == 0o600


def test_only_the_last_runs_are_kept(tmp_path):
    store = sb.SignatureBackups(tmp_path)
    ids = []
    for i in range(sb.KEEP_RUNS + 2):
        run = store.start(f"run {i}")
        run.keep(f"p{i}@example.com", "old", "new")
        ids.append(run.id)
        run.close()
    assert [r.id for r in store.runs()] == sorted(ids, reverse=True)[:sb.KEEP_RUNS]


def test_a_run_that_cant_be_read_whole_is_listed_never_dropped(tmp_path):
    store = sb.SignatureBackups(tmp_path)
    run = store.start("x")
    run.keep(ALICE, "old", "new")
    run.close()
    run.path.write_text(run.path.read_text() + "{not json\n")
    [listed] = store.runs()
    assert listed.id == run.id and listed.what.startswith("unreadable") and listed.entries == []
    with pytest.raises(sb.BackupProblem):
        store.load(run.id)


def test_a_run_still_being_written_is_never_pruned(tmp_path):
    # Review of #163: a long run's file was deleted by five shorter runs started meanwhile, and its later copies went to
    # a deleted inode without an error, so the people it overwrote afterwards had no copy on disk.
    store = sb.SignatureBackups(tmp_path)
    long_run = store.start("the whole company")
    for i in range(sb.KEEP_RUNS + 1):
        short = store.start(f"one person {i}")
        short.keep(f"p{i}@example.com", "old", "new")
        short.close()
    long_run.keep(ALICE, "ORIGINAL", "new")
    assert long_run.path.exists()
    long_run.close()
    assert store.load(long_run.id).entries == [sb.Entry(ALICE, "ORIGINAL", "new")]


def test_runs_that_kept_nobody_dont_push_out_real_copies(tmp_path):
    # Review of #163: five failed attempts (a sign-in expired, every read refused) used up the slots.
    store = sb.SignatureBackups(tmp_path)
    good = store.start("sales")
    good.keep(ALICE, "ORIGINAL", "new")
    good.close()
    for _ in range(sb.KEEP_RUNS + 1):
        store.start("an attempt that kept nobody").close()
    assert [r.id for r in store.runs()] == [good.id]


@pytest.mark.parametrize("bad", ["", "../escape", "a/b", ".hidden", "run\0id"])
def test_only_a_kept_run_s_id_loads(tmp_path, bad):
    with pytest.raises(sb.BackupProblem):
        sb.SignatureBackups(tmp_path).load(bad)


# --- the exact read ----------------------------------------------------------------------------------------------------

async def test_the_read_gives_back_what_was_set_byte_for_byte(connector, gam_state):
    await connector.set_signature(ALICE, TRICKY, html=True)
    assert await connector.signature_exact(ALICE) == TRICKY


async def test_no_signature_reads_as_empty(connector):
    # GAM's titles hold no `signature` column: Gmail leaves an empty one out, so the person's whole row without it is none.
    assert await connector.signature_exact("bob@example.com") == ""


async def test_a_row_cut_short_is_incomplete_never_empty(connector, monkeypatch):
    # Review of #163: a cell missing from the person's row read as None, and None read as "no signature".
    from gamgui.core.gam.runner import RunResult

    async def short(domain, argv, timeout=None):
        return RunResult(stdout="User,displayName,isDefault,isPrimary,replyToAddress,sendAsEmail,signature\n"
                                f"{ALICE},,True,True,,{ALICE}\n", stderr=f"Getting Signatures for {ALICE}\n", returncode=0)

    monkeypatch.setattr(connector.runner, "run_any_exit", short)
    with pytest.raises(Incomplete):
        await connector.signature_exact(ALICE)


async def test_a_read_gam_skipped_without_a_word_is_incomplete_never_empty(connector, gam_state):
    (gam_state / "quiet").mkdir()
    (gam_state / "quiet" / ALICE).write_text("")
    with pytest.raises(Incomplete):
        await connector.signature_exact(ALICE)


async def test_gmail_off_or_no_such_user_raises(connector, gam_state):
    (gam_state / "nogmail").mkdir()
    (gam_state / "nogmail" / ALICE).write_text("")
    with pytest.raises(GAMError):
        await connector.signature_exact(ALICE)
    with pytest.raises(GAMError):
        await connector.signature_exact("nobody@example.com")


# --- a run ------------------------------------------------------------------------------------------------------------

ALICE_AND = {"template": "<b>{name}</b>", "scope_type": "user", "scope_value": ALICE}


def _apply(client, **form):  # noqa: F811
    _, token = _sig_preview(client, **form)
    r = _sig_apply(client, token, **form)
    if "apply/status" not in r.text:
        return r, None
    job = _job(client, r.text, "/signatures/apply/status")
    wait_for_job(client, job)
    return r, job


def test_a_run_keeps_the_old_signature_before_it_writes(client, gam_calls, gam_state):  # noqa: F811
    _, job = _apply(client, **ALICE_AND)
    assert job.applied == 1
    calls = gam_calls()
    assert calls.index(GAMCommands.print_signature(ALICE)) < calls.index(GAMCommands.set_signature(ALICE, "<b>Alice Anders</b>"))
    [kept] = client.app.state.gamgui.signature_backups.runs()
    assert kept.entries == [sb.Entry(ALICE, "Best,<br>Alice", "<b>Alice Anders</b>")]


def test_someone_whose_signature_cant_be_read_isnt_overwritten(client, gam_calls, gam_state):  # noqa: F811
    (gam_state / "quiet").mkdir()
    (gam_state / "quiet" / ALICE).write_text("")
    _, job = _apply(client, **ALICE_AND)
    assert (job.applied, job.failed_items) == (0, [ALICE]) and gam_writes(gam_calls()) == []
    assert "wasn't replaced" in job.failed[0].reason
    assert client.app.state.gamgui.signature_backups.runs() == []     # a run that kept nobody leaves no file


def test_a_copy_that_cant_be_kept_stops_the_run_before_its_write(client, gam_calls, monkeypatch):  # noqa: F811
    def full(self, email, previous, new):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sb.RunWriter, "keep", full)
    _, job = _apply(client, template="{name}", scope_type="company", scope_value="")
    assert gam_writes(gam_calls()) == [] and job.applied == 0
    assert "Couldn't keep a copy of the old signatures on this Mac, so the run stopped" in job.error


def test_no_place_to_keep_copies_replaces_nothing(client, gam_calls, monkeypatch):  # noqa: F811
    def nowhere(self, what):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sb.SignatureBackups, "start", nowhere)
    r, job = _apply(client, **ALICE_AND)
    assert job is None and "nothing was replaced" in r.text and gam_writes(gam_calls()) == []


def test_the_preview_says_what_the_backup_costs(client):  # noqa: F811
    page, _ = _sig_preview(client, **ALICE_AND)
    assert "kept on this Mac first" in page and "two GAM calls a person" in page
