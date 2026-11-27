"""GAM says it read a person's signature with "Getting Signatures for <u>": the plural (failure-log 2026-11-27).

printShowSignature calls printGettingEntityItemForWhom(Ent.SIGNATURE, user, i, count), which writes
f'{Msg.GETTING} {Ent.PluralGetting()} {Msg.FOR} {forWhom}', and PluralGetting is Ent.SIGNATURE's first name, 'Signatures'
(glentity.py at the 7.52.05 tag). The app and its mock both said "Getting Signature for", so the mock passed while every
live read, one person's (roadmap 4.1) or many (5.4), would have read as unknown and no signature run would have written
anyone. SwiftGamGui's port caught it.
"""

from __future__ import annotations

import pytest

from gamgui.core import signatures_fast as fast
from gamgui.core.gam.runner import RunResult

ALICE = "alice@example.com"
TITLES = "User,displayName,isDefault,isPrimary,replyToAddress,sendAsEmail,signature,treatAsAlias,verificationStatus\n"
ROW = 'alice@example.com,,True,True,,alice@example.com,"Best,<br>Alice",False,accepted\n'


@pytest.mark.parametrize("line", [f"Getting Signatures for {ALICE}\n", f"Getting Signatures for {ALICE} (1/2)\n"])
def test_gam_s_own_line_says_the_person_was_read(line):
    assert fast.read_signatures(TITLES + ROW, line, [ALICE]) == {ALICE: "Best,<br>Alice"}


async def test_one_person_s_read_takes_gam_s_own_line(connector, monkeypatch):
    async def gam(domain, argv, timeout=None, stdin=None):
        return RunResult(stdout=TITLES + ROW, stderr=f"Getting Signatures for {ALICE}\n", returncode=0)
    monkeypatch.setattr(connector.runner, "run_any_exit", gam)
    assert await connector.signature_exact(ALICE) == "Best,<br>Alice"


def test_the_mock_says_what_gam_says():
    from pathlib import Path
    mock = (Path(__file__).parent / "fixtures" / "mock_gam.sh").read_text()
    assert "Getting Signatures for %s" in mock and "Getting Signature for %s" not in mock
