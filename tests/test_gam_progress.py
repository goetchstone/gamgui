"""GAM's progress lines are never read as the failure: core/gam/errors.py and the mock held to the build.

With show_gettings on (GAM's default) GAM says on stderr what it is getting, before each entity it reads.
errors.py skipped only "Getting all …" and "Got N …", two of its six printers, so a per-entity line
("Getting Reports for all users", "Getting Access Tokens for alice@…") read as an unknown failure. Unknown
outranks not-found, permission-denied and the per-user refusals, so the real failure lost: a refused
`report users` read "GAM failed (unknown, exit=61): Getting Reports for all users" (failure log 2026-10-30,
progress read as the error). Nothing failed, because the mock printed the report's progress on stdout, in
words GAM never writes, and nothing for `deprovision`. Two holds here:

* The build's own. scripts/gam_rc_table.py lists every gamlib.glmsgs message that starts "Getting" or "Got";
  they must be the progress words errors.py skips, so an error GAM words that way is read before a bump is
  trusted. It skips, or fails under EXIT_CODES_REQUIRE_GAM, as tests/test_gam_exit_codes.py does.
* The mock's. A refused report and a deprovision with a refused sign-out print GAM's progress on stderr,
  as GAM does, and keep the real failure's kind and words.
"""
from __future__ import annotations

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.errors import GAMError, GAMErrorKind, _error_lines
from gamgui.core.reports import USAGE_PARAMS

from .test_gam_exit_codes import build_reading


def test_the_builds_progress_words_are_the_ones_skipped():
    words = build_reading("progress")
    assert words == {"GETTING": "Getting", "GETTING_ALL": "Getting all", "GOT": "Got"}, \
        "a gamlib.glmsgs message now starts Getting/Got: read where it is printed, then errors._PROGRESS_LINE"
    # Each printer's shape, from the build's words (gam/__init__.py printGetting*, printGot*).
    printed = [f"{words['GETTING_ALL']} Users, may take some time on a large Google Workspace Account...",
               f"{words['GETTING']} Reports for all users",
               f"{words['GETTING']} Group sales@example.com (1/3)",
               f"{words['GETTING']} Access Tokens for alice@example.com",
               f"{words['GOT']} 3 Users: alice@example.com - carol@example.com",
               f"\r{words['GOT']} 3 Reports for all users on 2026-06-16...\r"]
    assert _error_lines("\n".join(printed)) == []


async def test_a_refused_report_says_why(connector, gam_state):
    (gam_state / "noreports").touch()
    with pytest.raises(GAMError) as ei:
        await connector.usage_report(USAGE_PARAMS)
    err = ei.value
    assert (err.kind, err.kinds, err.exit_code) == (GAMErrorKind.PERMISSION_DENIED, {GAMErrorKind.PERMISSION_DENIED}, 61)
    assert err.message.endswith("User: admin@example.com, Access Forbidden")
    assert "Getting Reports" in err.stderr          # GAM's progress was there; it just isn't the failure


async def test_a_refused_signout_in_deprovision_is_only_that_refusal(runner, domain):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, C.deprovision_user("SIGNOUTFAIL-alice@example.com"))
    err = ei.value
    assert err.kinds == {GAMErrorKind.SCOPE_MISSING}      # not UNKNOWN beside it, which a best-effort caller refuses
    assert err.message.endswith("Sign Out Failed: Not Authorized to access this resource/api")
    assert err.stderr.count("Getting ") == 3
