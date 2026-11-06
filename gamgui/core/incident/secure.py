"""Secure an account: the lock and the attacker's changes the operator picked, as steps for one preview.

The person stays employed, so there's no manager and no hand-over. Every step but the picks is a switch, so any of them can run
alone. While the reset runs, every other step needs it to have succeeded; otherwise nothing stops anything: a refused sign-out
mustn't stop forwarding off. Each step is offboarding's ``OffboardStep``, run by ``lifecycle.run_offboard``. Ported from
SwiftGamGui's ``Compromised.steps``.
"""

from __future__ import annotations

from typing import FrozenSet, List, Sequence, Tuple

from ..gam.commands import GAMCommands, is_one_address
from ..lifecycle import STEP_NAMES, OffboardStep
from ..person_reads import Computer
from .check import Draft, Filter, ScheduledSend, scheduled_recipients

SWITCHABLE: Tuple[str, ...] = ("password", "clear_recovery", "revoke", "forward", "imap_off", "pop_off")
# The lock. Clearing recovery is a loss when the values are the person's own, so it waits for the operator to look.
ON_BY_DEFAULT: FrozenSet[str] = frozenset({"password", "revoke", "forward", "imap_off", "pop_off"})
NOTES = {
    "password": "Sets a random password nobody is shown, so the attacker's copy stops working; you give the person a new one "
                "afterwards. Off when you already reset it in the Admin console. While it runs, every other step waits for it to succeed.",
    "clear_recovery": "Removes both, so an attacker's recovery phone or address can't take the account back through “Forgot password?”. "
                      "The old values aren't kept: the person adds their own again.",
    "revoke": "Ends every session and revokes app passwords and connected apps: the person signs in again and re-grants the apps they use.",
    "forward": "Stops automatic forwarding of their mail (harmless if it's already off).",
    "imap_off": "No mail app can download their mail over IMAP. The reset and the sign-out already cut off the ones signed in; the person "
                "can turn IMAP back on in Gmail's settings.",
    "pop_off": "No mail app can download their mail over POP. Nothing in Gmail is deleted; the person can turn POP back on in Gmail's settings.",
}


def _named(subjects: Sequence[str]) -> str:
    """Up to three subjects in quotes, each once, and how many others: what a deleting step names."""
    distinct = list(dict.fromkeys(subjects))
    shown = ", ".join(f"“{s or '(no subject)'}”" for s in distinct[:3])
    return shown + (f" and {len(distinct) - 3} other subjects" if len(distinct) > 3 else "")


def steps(user: str, switched_on: FrozenSet[str], *, filters: Sequence[Filter] = (), delegates: Sequence[str] = (),
          send_as: Sequence[str] = (), auto_reply_off: bool = False, scheduled: Sequence[ScheduledSend] = (),
          drafts: Sequence[Draft] = (), recovery: Tuple[str, str] = ("", ""), computers: Sequence[Tuple[Computer, str]] = ()) -> List[OffboardStep]:
    """Every step in run order, for ``user``, the account's primary address. ValueError when a pick can't be one argv element."""
    if not switched_on <= frozenset(SWITCHABLE):
        raise ValueError(f"only {', '.join(SWITCHABLE)} are switched")
    if not all(is_one_address(d) for d in delegates):
        raise ValueError("each delegate to remove is one email address")
    if not all(s.is_scheduled for s in scheduled):      # whatever the search returned
        raise ValueError("only a message Gmail labels SCHEDULED is deleted as a scheduled send")
    needs: Tuple[str, ...] = ("password",) if "password" in switched_on else ()
    planned: List[OffboardStep] = []

    def add(key: str, label: str, summary: str, action, argv: List[str], requires: Tuple[str, ...] = needs) -> None:
        planned.append(OffboardStep(key, label, summary, action, [argv], requires))

    if "password" in switched_on:
        add("password", STEP_NAMES["password"],
            f"Set {user}'s password to a random one nobody is shown, so the attacker's copy stops working. The person can't sign in "
            f"until you give them a new one.",
            lambda c: c.reset_password(user), GAMCommands.reset_password(user), ())
    if scheduled:       # right after the reset: one comes due while the run goes on, and once sent, its ID may be the Sent copy
        ids = [s.id for s in scheduled]
        people = len(scheduled_recipients(scheduled))
        argv = GAMCommands.delete_messages(user, ids)
        add("scheduled", f"Delete {len(ids)} scheduled email{'' if len(ids) == 1 else 's'}",
            f"Delete {len(ids)} of {user}'s emails scheduled to send ({_named([s.subject for s in scheduled])}), to {people} "
            f"recipient{'' if people == 1 else 's'}, for good, before {'its' if len(ids) == 1 else 'their'} time comes. They can't be restored.",
            lambda c: c.delete_messages(user, ids), argv)
    if "clear_recovery" in switched_on:
        was = " and ".join(w for w in (f"email {recovery[0]}" if recovery[0] else "", f"phone {recovery[1]}" if recovery[1] else "") if w)
        add("clear_recovery", STEP_NAMES["clear_recovery"],
            f"Remove {user}'s recovery email and recovery phone{f' (now {was})' if was else ''}, so “Forgot password?” can't send a code to "
            f"an attacker's. The old values aren't kept.",
            lambda c: c.clear_recovery(user), GAMCommands.clear_recovery(user))
    if "revoke" in switched_on:
        add("revoke", STEP_NAMES["revoke"],
            f"Sign {user} out of every web and device session, delete their app passwords, invalidate their 2-Step Verification backup "
            f"codes and revoke every connected app's access. They sign in again and re-grant the apps they use; 2-Step Verification itself is left on.",
            lambda c: c.revoke_access(user), GAMCommands.deprovision_user(user))
    if "forward" in switched_on:
        add("forward", STEP_NAMES["forward"], f"Turn off automatic forwarding of {user}'s incoming mail (harmless if it's already off).",
            lambda c: c.forward_off(user), GAMCommands.forward_off(user))
    if filters:
        filter_ids = [f.id for f in filters]
        argv = GAMCommands.delete_filters(user, filter_ids)
        words = "; ".join(f"“{f.words}”" for f in filters)
        add("filters", f"Delete {len(filters)} filter{'' if len(filters) == 1 else 's'}",
            f"Delete {user}'s Gmail filter{'' if len(filters) == 1 else 's'} {words}. The whole filter goes.",
            lambda c: c.delete_filters(user, filter_ids), argv)
    for delegate in delegates:
        add("delegate", f"Remove the delegate {delegate}", f"Take {delegate}'s access to {user}'s mailbox away.",
            lambda c, d=delegate: c.remove_delegate(user, d), GAMCommands.remove_delegate(user, delegate))
    for address in send_as:
        add("send_as", f"Remove the send-as address {address}", f"Take {address} off {user}'s “Send mail as” list, so mail can't be sent from it as them.",
            lambda c, a=address: c.delete_send_as(user, a), GAMCommands.delete_send_as(user, address))
    for computer, action in computers:      # block, or take the account off: a name that isn't one GAM can act on never gets here, the screen offering it no choice
        name = computer.device_user
        if action == "block":
            add("computer", f"Block {computer.words}",
                f"Block {user}'s sign-in on {computer.words}, so it can't reach their data. It stays listed as blocked, and approving it in the Admin console undoes it.",
                lambda c, n=name: c.block_device_user(user, n), GAMCommands.block_device_user(name))
        elif action == "delete":
            add("computer", f"Take {user}'s account off {computer.words}",
                f"Take {user}'s account off {computer.words} and revoke its access to their data. It shows again only if someone signs in on it again.",
                lambda c, n=name: c.delete_device_user(user, n), GAMCommands.delete_device_user(name))
        else:
            raise ValueError("a computer is blocked, or the account taken off it")
    if auto_reply_off:
        add("auto_reply", "Turn off the auto-reply",
            f"Turn off {user}'s auto-reply, so nobody who writes to them gets its message. Its subject and text stay in Gmail's settings, off.",
            lambda c: c.clear_vacation(user), GAMCommands.vacation_off(user))
    if drafts:
        draft_ids = [d.id for d in drafts]
        argv = GAMCommands.delete_messages(user, draft_ids)
        add("drafts", f"Delete {len(drafts)} draft{'' if len(drafts) == 1 else 's'}",
            f"Delete {len(drafts)} of {user}'s drafts ({_named([d.subject for d in drafts])}) for good, as Gmail's Discard does, so nobody "
            f"signed in to the account can send them. They can't be restored.",
            lambda c: c.delete_messages(user, draft_ids), argv)
    if "imap_off" in switched_on:
        add("imap_off", STEP_NAMES["imap_off"],
            f"Turn off IMAP for {user}: no mail app can download their mail over IMAP. Their other IMAP settings go back to Gmail's defaults; their mail is unchanged.",
            lambda c: c.imap_off(user), GAMCommands.imap_off(user))
    if "pop_off" in switched_on:
        add("pop_off", STEP_NAMES["pop_off"], f"Turn off POP for {user}: no mail app can download their mail over POP. Nothing in Gmail is deleted.",
            lambda c: c.pop_off(user), GAMCommands.pop_off(user))
    return planned


def what_stops_what(reset_runs: bool) -> str:
    return (f"If “{STEP_NAMES['password']}” fails, nothing after it runs. If any other step fails, it is reported as failed and the rest still run."
            if reset_runs else "If a step fails, it is reported as failed and the rest still run.")
