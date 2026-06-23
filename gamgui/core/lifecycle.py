"""Offboarding routine — the ordered sequence of steps applied to a departing user.

Each step reuses an existing connector mutation. The "timer" is step 7: a reminder event on the
manager's calendar, so there is NO app-side scheduler or persisted state. Building the step list is
pure (and testable); the web route executes it as a guarded, progress-tracked BatchJob.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Awaitable, Callable, List

DEFAULT_SUBJECT = "No longer with the company"
DEFAULT_MESSAGE = "Thank you for your message. This person is no longer with the company; please reach out to our main contact."


@dataclass
class OffboardStep:
    key: str
    label: str
    summary: str  # human description shown in the preview
    action: Callable[[object], Awaitable]  # conn -> awaitable returning a ChangeResult-like object


def build_offboard_steps(
    user: str, manager: str, subject: str, message: str, days: int, today: date
) -> List[OffboardStep]:
    """Turn the offboard parameters into the ordered step list (the user's exact sequence)."""
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
                     "Turn on the auto-reply on the mailbox",
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
                     f"Add a calendar reminder on {manager} for {due.isoformat()} to confirm deletion",
                     lambda c: c.add_calendar_event(
                         manager, reminder_summary, due.isoformat(),
                         (due + timedelta(days=1)).isoformat(), description=reminder_desc)),
    ]
