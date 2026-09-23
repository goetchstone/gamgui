"""Offboarding routine — the ordered sequence of steps applied to a departing user.

Each step reuses an existing connector mutation. The "timer" is the last step: a reminder event on
the manager's calendar, so there is NO app-side scheduler or persisted state. Building the step list
is pure (and testable); the web route executes it as a guarded, progress-tracked BatchJob.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Awaitable, Callable, List, Sequence

from .audit import redact_argv
from .gam.commands import GAMCommands
from .gam.runner import DOMAIN_WIDE_TIMEOUT

DEFAULT_SUBJECT = "{employee} is no longer with the company"
DEFAULT_MESSAGE = (
    "Thank you for your email. {employee} is no longer with the company. "
    "For assistance, please reach out to {manager}, who will be glad to help or direct your message "
    "to the appropriate person. We appreciate your understanding."
)


# The combined <DataTransferServiceList>: ONE transfer, one argv element (a second same-user one 409s).
TRANSFER_SERVICES = "drive,calendar"
# GAM's own password generators (grammar <UserBasicAttribute>): keywords, not secrets, so shown as-is.
_PASSWORD_KEYWORDS = frozenset({"random", "uniquerandom", "blocklogin", "prompt", "uniqueprompt"})


def _quote(arg: str) -> str:
    # shlex.quote spells an apostrophe '"'"' — POSIX double quotes read far better for prose.
    if "'" not in arg:
        return shlex.quote(arg)
    return '"' + re.sub(r'([\\"$`])', r"\\\1", arg) + '"'


def command_line(argv: Sequence[str]) -> str:
    """``argv`` as the gam command the operator reads before a run. Shell-quoted so each element's
    bounds show (a subject with spaces is ONE argument) — display only, never run by a shell. A value
    after a sensitive key is masked as the audit log masks it, except GAM's own password keywords."""
    masked = redact_argv(argv)
    shown = [raw if i and argv[i - 1].lower() == "password" and raw in _PASSWORD_KEYWORDS else m
             for i, (raw, m) in enumerate(zip(argv, masked))]
    return "gam " + " ".join(_quote(a) for a in shown)


def fill_autoreply(text: str, employee: str, manager: str) -> str:
    """Substitute {employee} (the departing person) and {manager}/{contact} (who to reach instead)."""
    return ((text or "")
            .replace("{employee}", employee)
            .replace("{manager}", manager)
            .replace("{contact}", manager))


@dataclass
class OffboardStep:
    key: str
    label: str
    summary: str  # human description shown in the preview
    action: Callable[[object], Awaitable]  # conn -> awaitable returning a ChangeResult-like object
    # The exact argv(s) ``action`` runs, in order, for the preview. Built from the same GAMCommands
    # builders and values as the connector call (test_offboard_preview_commands_are_what_runs).
    commands: List[List[str]] = field(default_factory=list)

    @property
    def command_lines(self) -> List[str]:
        return [command_line(argv) for argv in self.commands]


def build_offboard_steps(
    user: str, manager: str, subject: str, message: str, days: int, today: date,
    notify: str = "", employee_name: str = "", manager_contact: str = "",
) -> List[OffboardStep]:
    """Turn the offboard parameters into the ordered step list (the user's exact sequence).

    ``employee_name`` / ``manager_contact`` are the directory-resolved display forms used only in the
    auto-reply TEXT; the raw ``manager`` email is still what the delegate/transfer/reminder steps act on.
    """
    employee = employee_name or user
    contact = manager_contact or manager
    subject = fill_autoreply(subject, employee, contact)
    message = fill_autoreply(message, employee, contact)
    due = today + timedelta(days=days)
    reminder_summary = f"Offboarding {user}: confirm with IT whether to delete the account"
    reminder_desc = (
        f"{user} was offboarded on {today.isoformat()} (password reset; data + calendars transferred to "
        f"{manager}). When you're sure it's safe, tell IT to delete the account."
    )
    start, end = due.isoformat(), (due + timedelta(days=1)).isoformat()
    return [
        OffboardStep("password", "Reset password",
                     f"Reset {user}'s password and end sessions (locks sign-in; mailbox stays live)",
                     lambda c: c.reset_password(user),
                     [GAMCommands.reset_password(user), GAMCommands.signout_user(user)]),
        OffboardStep("delegate", "Set delegate",
                     f"Give {manager} delegate access to {user}'s mailbox",
                     lambda c: c.add_delegate(user, manager),
                     [GAMCommands.add_delegate(user, manager)]),
        OffboardStep("vacation", "Set auto-responder",
                     f"Auto-reply — “{subject}”: {message}",
                     lambda c: c.set_vacation(user, subject, message),
                     [GAMCommands.set_vacation(user, subject, message)]),
        OffboardStep("transfer", "Transfer Drive & Calendar ownership",
                     f"Transfer {user}'s Drive/Docs and calendars to {manager}",
                     lambda c: c.transfer_data(user, TRANSFER_SERVICES, manager),
                     [GAMCommands.create_datatransfer(user, TRANSFER_SERVICES, manager)]),
        OffboardStep("calacls", "Remove from everyone's calendars",
                     f"Remove {user} from other users' calendars — one domain-wide call that visits "
                     f"every user, so it can take many minutes (stopped and reported failed after "
                     f"{DOMAIN_WIDE_TIMEOUT / 60:g} min)",
                     lambda c: c.remove_from_all_calendars(user),
                     [GAMCommands.remove_all_calendar_acls(user)]),
        OffboardStep("reminder", f"{days}-day reminder for {manager}",
                     f"Add a calendar reminder on {manager}"
                     + (f" (also invites {notify})" if notify else "")
                     + f" for {due.isoformat()} to confirm deletion",
                     lambda c: c.add_calendar_event(
                         manager, reminder_summary, start, end, description=reminder_desc, attendee=notify),
                     [GAMCommands.add_calendar_event(
                         manager, reminder_summary, start, end, description=reminder_desc, attendee=notify)]),
    ]
