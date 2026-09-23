"""Offboarding routine — the ordered sequence of steps applied to a departing user.

Each step reuses an existing connector mutation. The "timer" is the last step: a reminder event on
the manager's calendar, so there is NO app-side scheduler or persisted state. Building the step list
is pure (and testable); the web route executes it as a guarded, progress-tracked BatchJob.
"""

from __future__ import annotations

import html
import re
import shlex
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .audit import redact_argv
from .gam.commands import GAMCommands
from .gam.models import GAMUser
from .gam.runner import DOMAIN_WIDE_TIMEOUT

DEFAULT_SUBJECT = "{employee} is no longer with the company"
DEFAULT_MESSAGE = (
    "Thank you for your email. {employee} is no longer with the company. "
    "For assistance, please reach out to {manager}, who will be glad to help or direct your message "
    "to the appropriate person. We appreciate your understanding."
)


# The combined <DataTransferServiceList>: ONE transfer, one argv element (a second same-user one 409s).
TRANSFER_SERVICES = "drive,calendar"
# Every Drive file the leaver owns, private and shared: left to the API's default, files they had
# shared might stay behind — and be lost when the account is deleted.
TRANSFER_PRIVACY = "all"

# What each step needs to have succeeded before it runs — a failed step stops the steps that rely on
# it rather than half-offboarding the account (the runbook's "When a step fails" table):
#   - the reset gates everything: nothing announces the departure or moves data while the account can
#     still sign in, and a first-step failure usually means every later step would fail too;
#   - the delegate is the first write to the manager, who also receives the transfer and the
#     reminder, so it gates the rest;
#   - the reminder asks the manager to approve deletion, and deleting before the transfer loses the
#     leaver's files for good — so no transfer, no reminder.
# Revoking access (sessions, app passwords, tokens), turning off forwarding, the auto-reply and the
# calendar sweep gate nothing: their failure is reported (✗, and the run isn't "complete") and the
# routine goes on. The revoke and the forwarding run straight after the reset, before anything is
# handed over, so a failed delegate can't stop them; they don't gate the hand-over either — the reset
# has already stopped new sign-ins, and the likeliest cause (a missing scope) would otherwise strand
# the mailbox with no delegate.
REQUIRES: Dict[str, Tuple[str, ...]] = {
    "password": (),
    "revoke": ("password",),
    "forward": ("password",),
    "delegate": ("password",),
    "vacation": ("password", "delegate"),
    "transfer": ("password", "delegate"),
    "calacls": ("password", "delegate"),
    "reminder": ("password", "delegate", "transfer"),
}
# The steps as the form's "already done" boxes name them (a re-run skips a ticked step).
STEP_NAMES = {"password": "Reset password", "revoke": "Revoke access & sign out", "forward": "Turn off forwarding",
              "delegate": "Set delegate", "vacation": "Auto-reply", "transfer": "Transfer Drive & Calendar",
              "calacls": "Remove from everyone's calendars", "reminder": "Manager reminder"}

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


def autoreply_html(text: str) -> str:
    """The auto-reply text as the HTML body GAM sends (`html`), so senders read what the preview shows.

    In HTML a raw line break is only a space, and GAM turns just the two characters ``\\n`` into
    ``<br/>`` (setVacation) — so each line break becomes ``<br/>``, ``&``/``<``/``>`` are escaped (the
    operator typed text, not markup) and a backslash is ``&#92;`` (a typed ``\\n`` stays text)."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "<br/>".join(html.escape(line, quote=False).replace("\\", "&#92;") for line in lines)


@dataclass
class AddressCheck:
    """The directory's verdict on an offboarding's two addresses: errors block it, warnings are shown."""
    user: Optional[GAMUser] = None
    manager: Optional[GAMUser] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def check_addresses(directory: Sequence[GAMUser], user: str, manager: str) -> AddressCheck:
    """Find the departing user and the manager in the directory, by primary address.

    Unknown is an error: a typo'd manager would have the mailbox, Drive and reminder handed to nobody
    after the password was already reset. So is an alias (the calendar sweep matches ACLs by primary
    address) and the same person twice (GAM refuses a transfer to oneself)."""
    by_email = {u.primary_email.lower(): u for u in directory}
    by_alias = {a.lower(): u for u in directory for a in u.aliases}
    check = AddressCheck()

    def find(addr: str, role: str) -> Optional[GAMUser]:
        found = by_email.get(addr.lower())
        if found is None:
            owner = by_alias.get(addr.lower())
            check.errors.append(
                f"{addr} is an alias of {owner.primary_email} — enter the primary address as the {role}."
                if owner else
                f"{addr} isn't in the directory — check the {role}'s address (an account created in the "
                f"last few minutes shows after Users → Refresh).")
        return found

    check.user, check.manager = find(user, "departing user"), find(manager, "manager")
    leaver, mgr = check.user, check.manager
    if leaver and mgr and leaver.primary_email.lower() == mgr.primary_email.lower():
        check.errors.append("The departing user and the manager are the same account — enter the manager "
                            "who takes over the mailbox and files.")
        return check
    if leaver and leaver.is_admin:
        check.warnings.append(
            f"{leaver.primary_email} is a super admin. Offboarding doesn't remove the role — revoke it in the "
            f"Admin console. If GamGUI is connected as this account, “{STEP_NAMES['revoke']}” deletes GamGUI's "
            f"own authorization (its OAuth token) and the later steps fail.")
    elif leaver and leaver.is_delegated_admin:
        check.warnings.append(f"{leaver.primary_email} holds a delegated admin role — offboarding doesn't "
                              f"remove it; revoke it in the Admin console.")
    if leaver and leaver.suspended:
        check.warnings.append(f"{leaver.primary_email} is already suspended — the mailbox steps (delegate, "
                              f"auto-reply) and “{STEP_NAMES['revoke']}” may fail for a suspended account.")
    if mgr and mgr.suspended:
        check.warnings.append(f"The manager {mgr.primary_email} is suspended — the delegate, the Drive & "
                              f"Calendar transfer and the reminder all go to this account and will likely fail.")
    return check


@dataclass
class OffboardStep:
    key: str
    label: str
    summary: str  # human description shown in the preview
    action: Callable[[object], Awaitable]  # conn -> awaitable returning a ChangeResult-like object
    # The exact argv(s) ``action`` runs, in order, for the preview. Built from the same GAMCommands
    # builders and values as the connector call (test_offboard_preview_commands_are_what_runs).
    commands: List[List[str]] = field(default_factory=list)
    # Keys of the steps that must have succeeded first; otherwise this one is not run (REQUIRES).
    requires: Tuple[str, ...] = ()

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
    body = autoreply_html(message)   # what's sent; the preview and the step summary show `message`
    due = today + timedelta(days=days)
    reminder_summary = f"Offboarding {user}: confirm with IT whether to delete the account"
    reminder_desc = (
        f"{user} was offboarded on {today.isoformat()} (password reset; data + calendars transferred to "
        f"{manager}). When you're sure it's safe, tell IT to delete the account."
    )
    start, end = due.isoformat(), (due + timedelta(days=1)).isoformat()
    steps = [
        OffboardStep("password", "Reset password",
                     f"Set {user}'s password to a random one nobody is shown, so the old password stops "
                     f"working (the mailbox stays live)",
                     lambda c: c.reset_password(user),
                     [GAMCommands.reset_password(user)]),
        OffboardStep("revoke", STEP_NAMES["revoke"],
                     f"Sign {user} out of every web and device session, delete their app passwords, "
                     f"invalidate their 2-Step Verification backup codes and revoke every connected app's "
                     f"access (OAuth tokens). 2-Step Verification itself is left on",
                     lambda c: c.revoke_access(user),
                     [GAMCommands.deprovision_user(user)]),
        # Always, not only when the preview saw it on: the leaver can switch it on until the sign-out.
        OffboardStep("forward", STEP_NAMES["forward"],
                     f"Turn off automatic forwarding of {user}'s incoming mail (harmless if it's already off). "
                     f"Gmail filters that forward mail are not changed",
                     lambda c: c.forward_off(user),
                     [GAMCommands.forward_off(user)]),
        OffboardStep("delegate", "Set delegate",
                     f"Give {manager} delegate access to {user}'s mailbox",
                     lambda c: c.add_delegate(user, manager),
                     [GAMCommands.add_delegate(user, manager)]),
        OffboardStep("vacation", "Set auto-responder",
                     f"Auto-reply — “{subject}”: {message}",
                     lambda c: c.set_vacation(user, subject, body),
                     [GAMCommands.set_vacation(user, subject, body)]),
        OffboardStep("transfer", "Transfer Drive & Calendar ownership",
                     f"Transfer {user}'s Drive/Docs (private and shared files) and calendars to {manager}",
                     lambda c: c.transfer_data(user, TRANSFER_SERVICES, manager, privacy=TRANSFER_PRIVACY),
                     [GAMCommands.create_datatransfer(user, TRANSFER_SERVICES, manager, privacy=TRANSFER_PRIVACY)]),
        OffboardStep("calacls", "Remove from everyone's calendars",
                     f"Remove {user} from other users' calendars — one domain-wide call that visits "
                     f"every user, so it can take many minutes (stopped and reported failed after "
                     f"{DOMAIN_WIDE_TIMEOUT / 60:g} min)",
                     lambda c: c.remove_from_all_calendars(user),
                     [GAMCommands.remove_all_calendar_acls(user)]),
        OffboardStep("reminder", f"{days}-day reminder for {manager}",
                     f"Add a calendar reminder on {manager}"
                     + (f" (also invites {notify}, who gets an invitation email)" if notify else "")
                     + f" for {due.isoformat()} to confirm deletion",
                     lambda c: c.add_calendar_event(
                         manager, reminder_summary, start, end, description=reminder_desc, attendee=notify),
                     [GAMCommands.add_calendar_event(
                         manager, reminder_summary, start, end, description=reminder_desc, attendee=notify)]),
    ]
    for step in steps:
        step.requires = REQUIRES[step.key]
    return steps
