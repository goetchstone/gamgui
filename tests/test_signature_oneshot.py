"""The Users page's signature set keeps the old one first (roadmap 4.1), as a Signatures run does: read exactly, kept on
disk, then replaced. One that can't be read or kept isn't replaced."""

from __future__ import annotations

from html import unescape

from gamgui.core import signature_backups as sb
from gamgui.core.gam.commands import GAMCommands

from .helpers import gam_writes
from .test_users_web import client  # noqa: F401

ALICE = "alice@example.com"


def _set(client, body="<b>New</b>"):  # noqa: F811
    return client.post("/users/signature", data={"email": ALICE, "signature": body, "html": "on"})


def test_the_old_signature_is_kept_before_it_is_replaced(client, gam_calls):  # noqa: F811
    r = _set(client)
    assert "Signature updated. The old one is kept" in unescape(r.text)
    calls = gam_calls()
    assert calls.index(GAMCommands.print_signature(ALICE)) < calls.index(GAMCommands.set_signature(ALICE, "<b>New</b>"))
    [kept] = client.app.state.gamgui.signature_backups.runs()
    assert kept.what == ALICE and kept.entries == [sb.Entry(ALICE, "Best,<br>Alice", "<b>New</b>")]


def test_a_signature_that_cant_be_read_isnt_replaced(client, gam_calls, gam_state):  # noqa: F811
    (gam_state / "quiet").mkdir()
    (gam_state / "quiet" / ALICE).write_text("")
    r = _set(client)
    assert "Couldn't read their current signature, so it wasn't replaced" in unescape(r.text)
    store = client.app.state.gamgui.signature_backups
    assert gam_writes(gam_calls()) == [] and (store is None or store.runs() == [])   # nothing to keep, nothing started


def test_a_copy_that_cant_be_kept_replaces_nothing(client, gam_calls, monkeypatch):  # noqa: F811
    def full(self, email, previous, new):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sb.RunWriter, "keep", full)
    r = _set(client)
    assert "Couldn't keep a copy of their current signature on this Mac, so it wasn't replaced" in unescape(r.text)
    assert gam_writes(gam_calls()) == []
