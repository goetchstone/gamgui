"""GAM's framing of a refused command is never read as the failure: core/gam/errors.py and the mock held to the build.

Before a usage error, and before ending a command on an entity that doesn't exist, GAM echoes the command line
with the bad argument marked ("Command: gam … >>>bad<<< …"); after a usage error it says where the syntax and
the wiki are ("Help: …"). errors.py read those as error lines. The echo is unknown, which outranks not-found, so
a transfer to an owner who isn't a user read "GAM failed (unknown, exit=56): Command: gam create datatransfer …";
and every usage error's message was the wiki's address (failure log 2026-10-31). Nothing failed, because the
mock's usage error printed no help lines and its transfer reported a missing owner without the echo. Two holds:

* The build's own. scripts/gam_rc_table.py reads the echo's text (glclargs CommandLineWithBadArgumentMarked)
  and the help messages; lines built from them must be skipped. It skips, or fails under
  EXIT_CODES_REQUIRE_GAM, as tests/test_gam_exit_codes.py does.
* The mock's. A transfer to a missing owner and a usage error print GAM's framing and keep the real failure's
  kind and words.
"""
from __future__ import annotations

from gamgui.core.gam.errors import GAMErrorKind, _error_lines

from .test_gam_exit_codes import build_reading


def test_the_builds_framing_is_skipped():
    framing = build_reading("framing")
    echo = framing["echo"]
    assert echo[:2] == ["Command: ", " >>>"], "GAM's command echo changed: read glclargs at the new tag"
    messages = framing["messages"]
    assert messages == {"HELP_SYNTAX": "Help: Syntax in file {0}\n", "HELP_WIKI": "Help: Documentation is at {0}\n",
                        "NESTED_LOOP_CMD_NOT_ALLOWED": "Command can not be nested."}, \
        "a gamlib.glmsgs message now starts Command/Help: read where it is printed, then errors._FRAMING_LINE"
    marked = f"{echo[0]}gam create datatransfer{echo[1]}nobody@example.com<<< drive alice@example.com all"
    help_lines = [messages["HELP_SYNTAX"].format("/Applications/GamGUI.app/Contents/Resources/gam7/GamCommands.txt"),
                  messages["HELP_WIKI"].format("https://github.com/GAM-team/GAM/wiki")]
    assert _error_lines("\n".join([marked, *help_lines])) == []
    assert _error_lines(messages["NESTED_LOOP_CMD_NOT_ALLOWED"])      # an error that starts "Command", not framing


async def test_a_transfer_to_an_owner_who_is_not_a_user_says_who(connector):
    res = await connector.transfer_data("leaver@example.com", "drive", "nonexistent-mgr@example.com")
    assert not res.ok and res.kind is GAMErrorKind.NOT_FOUND
    assert res.remediation == "The requested user, group, or resource was not found."
    assert res.detail.endswith("ERROR: User: nonexistent-mgr@example.com, Does not exist")


async def test_a_usage_error_is_its_error_line_not_the_help(connector):
    res = await connector.transfer_data("leaver@example.com", "nosuchservice", "mgr@example.com")
    assert not res.ok and res.kind is GAMErrorKind.UNKNOWN
    assert res.detail.endswith("ERROR: Invalid choice (nosuchservice): Expected <calendar|datastudio|lookerstudio|"
                               "data studio|googledrive|gdrive|drive|drive and docs>")
