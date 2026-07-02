"""GAM command-contract + version-consistency guards.

These catch GAM *syntax/version drift* — the bug class where a GAM upgrade renames or removes a
sub-command and our builders break only against a live tenant. They need no credentials:

* ``test_required_command_tokens_present`` asserts every GAM token our ``GAMCommands`` builders rely on
  still exists in the vendored command reference (``GamCommands.txt``). It SKIPS when the reference
  isn't vendored (fresh clone / clean-room CI), and RUNS in the ``gam-compat`` CI job that fetches the
  real binary — so a renamed/removed command fails the build on the next version bump.
* ``test_pinned_version_consistent`` enforces the single source of truth: ``EXPECTED_GAM_VERSION`` must
  match ``scripts/fetch_gam.sh`` (TAG), the mock, and (if vendored) the ``VERSION`` file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gamgui.core.gam.commands import EXPECTED_GAM_VERSION

ROOT = Path(__file__).resolve().parents[1]
GAM_COMMANDS_REF = ROOT / "gamgui" / "resources" / "gam7" / "GamCommands.txt"

# GAM sub-command tokens our GAMCommands builders depend on today. Add the new token alongside any new
# builder (e.g. add "print cros" when the Chromebook feature lands) so the contract tracks the code.
REQUIRED_TOKENS = [
    "print users",
    "info user",
    "update user",
    "organization",   # update user organization (title/department/store)
    "suspended",      # update user ... suspended on|off
    "signature",
    "delegate",
    "vacation",
    "forwardingaddress",  # gmail forwarding (Builder)
    "print messages",     # mailbox search (Builder: find a message, dump headers)
    "print cros",         # ChromeOS device search (Builder: Find Chromebooks)
    "print filelist",     # Drive file search (Builder: Find a user's Drive files)
    "alias",              # user aliases (Builder)
    "todrive",            # export a print command's CSV to a Google Sheet
    "tduser",             # …in a specific user's Drive
    "print groups",
    "update group",   # add/remove members
    "calendaracls",   # calendar access view/add/remove
    "print resources",  # resource/room calendars
    "print calendars",  # a user's calendars
    "add calendars",    # subscribe a recipient so a shared calendar appears in their sidebar
    "print events",     # event search
    "delete events",    # event deletion
    "remove calendars", # PERMANENT secondary-calendar delete (NOT `delete calendars` = unsubscribe)
    "datatransfer",     # offboarding data transfer
    "add event",        # offboarding manager reminder
    "delete user",      # offboarding final delete
    "create tasklist",  # onboarding runbook -> Google Tasks list
    "create task",      # onboarding runbook -> a task on the list
    "sendemail",        # onboarding welcome email
    "report users",   # usage report
    "serviceaccount", # check serviceaccount (setup verify)
    "formatjson",     # JSON output mode we parse
]


@pytest.mark.skipif(
    not GAM_COMMANDS_REF.exists(),
    reason="vendored GAM command reference not present (clean-room; runs in the gam-compat CI job)",
)
def test_required_command_tokens_present():
    ref = GAM_COMMANDS_REF.read_text(errors="replace")
    missing = [t for t in REQUIRED_TOKENS if t not in ref]
    assert not missing, (
        f"GAM {EXPECTED_GAM_VERSION} command reference is missing tokens our builders rely on: "
        f"{missing}. A GAM upgrade likely renamed/removed a sub-command — update commands.py + this "
        "list together, and re-run the live acceptance pass."
    )


@pytest.mark.skipif(
    not GAM_COMMANDS_REF.exists(),
    reason="vendored GAM command reference not present (clean-room; runs in the gam-compat CI job)",
)
def test_catalog_matches_grammar():
    # The committed command catalog (Builder data) must be regenerated when GAM is bumped:
    # its version + command count must equal a fresh parse of the vendored grammar.
    import json

    from gamgui.core.catalog.parser import parse_grammar

    cat_json = ROOT / "gamgui" / "resources" / "gam7" / "command_catalog.json"
    if not cat_json.exists():
        pytest.skip("command_catalog.json not generated")
    data = json.loads(cat_json.read_text())
    fresh = parse_grammar(GAM_COMMANDS_REF.read_text(errors="replace"))
    assert data["version"] == EXPECTED_GAM_VERSION, "regenerate command_catalog.json (scripts/build_command_catalog.py)"
    assert len(data["commands"]) == len(fresh), "command_catalog.json is stale — regenerate it after the GAM bump"


def test_pinned_version_consistent():
    # Committed sources of the pin must agree. (The vendored VERSION file is checked in the gam-compat
    # CI step instead — after a real fetch — since locally it may be a placeholder.)
    fetch = (ROOT / "scripts" / "fetch_gam.sh").read_text()
    assert f'TAG="v{EXPECTED_GAM_VERSION}"' in fetch, "scripts/fetch_gam.sh TAG must match EXPECTED_GAM_VERSION"

    mock = (ROOT / "tests" / "fixtures" / "mock_gam.sh").read_text()
    assert EXPECTED_GAM_VERSION in mock, "mock_gam.sh must echo EXPECTED_GAM_VERSION"
