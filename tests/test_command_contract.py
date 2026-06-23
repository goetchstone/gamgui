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
    "print groups",
    "update group",   # add/remove members
    "calendaracls",   # calendar access view/add/remove
    "print resources",  # resource/room calendars
    "print calendars",  # a user's calendars
    "print events",     # event search
    "delete events",    # event deletion
    "datatransfer",     # offboarding data transfer
    "add event",        # offboarding manager reminder
    "delete user",      # offboarding final delete
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


def test_pinned_version_consistent():
    # Committed sources of the pin must agree. (The vendored VERSION file is checked in the gam-compat
    # CI step instead — after a real fetch — since locally it may be a placeholder.)
    fetch = (ROOT / "scripts" / "fetch_gam.sh").read_text()
    assert f'TAG="v{EXPECTED_GAM_VERSION}"' in fetch, "scripts/fetch_gam.sh TAG must match EXPECTED_GAM_VERSION"

    mock = (ROOT / "tests" / "fixtures" / "mock_gam.sh").read_text()
    assert EXPECTED_GAM_VERSION in mock, "mock_gam.sh must echo EXPECTED_GAM_VERSION"
