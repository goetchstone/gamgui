"""Offboarding routine — the ordered sequence of steps applied to a departing user.

Each step reuses an existing connector mutation. The "timer" is step 7: a reminder event on the
manager's calendar, so there is NO app-side scheduler or persisted state. Building the step list is
pure (and testable); the web route executes it as a guarded, progress-tracked BatchJob.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Awaitable, Callable, List

DEFAULT_SUBJECT = "{employee} is no longer with the company"
DEFAULT_MESSAGE = (
    "Thank you for your email. {employee} is no longer with the company. "
    "For assistance, please reach out to {manager}, who will be glad to help or direct your message "
    "to the appropriate person. We appreciate your understanding."
)


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
    return [
        OffboardStep("password", "Reset password",
                     f"Reset {user}'s password and end sessions (locks sign-in; mailbox stays live)",
                     lambda c: c.reset_password(user)),
        OffboardStep("delegate", "Set delegate",
                     f"Give {manager} delegate access to {user}'s mailbox",
                     lambda c: c.add_delegate(user, manager)),
        OffboardStep("vacation", "Set auto-responder",
                     f"Auto-reply — “{subject}”: {message}",
                     lambda c: c.set_vacation(user, subject, message)),
        OffboardStep("drive", "Transfer Drive & Docs",
                     f"Transfer {user}'s Drive/Docs ownership to {manager}",
                     lambda c: c.transfer_data(user, "drive", manager)),
        OffboardStep("calendar", "Transfer calendars",
                     f"Transfer {user}'s calendars to {manager}",
                     lambda c: c.transfer_data(user, "calendar", manager)),
        OffboardStep("calacls", "Remove from everyone's calendars",
                     f"Remove {user} from other users' calendars",
                     lambda c: c.remove_from_all_calendars(user)),
        OffboardStep("reminder", f"{days}-day reminder for {manager}",
                     f"Add a calendar reminder on {manager}"
                     + (f" (also invites {notify})" if notify else "")
                     + f" for {due.isoformat()} to confirm deletion",
                     lambda c: c.add_calendar_event(
                         manager, reminder_summary, due.isoformat(),
                         (due + timedelta(days=1)).isoformat(), description=reminder_desc, attendee=notify)),
    ]
