"""Catalog loader + the curated *buildable* overlay.

The shallow set (browse-only) comes from the vendored grammar via `parser.py`. The curated set
below is the only runnable surface: each command has typed slots and a `build` callable that returns
an injection-safe argv from a `GAMCommands` static method (never shell-spliced). Risk is authoritative
(it matches the connector's real `_run_write(... RiskLevel.X)`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Tuple

from ..gam.commands import GAMCommands
from ..connectors.base import RiskLevel
from .describe import gloss
from .models import Catalog, CatalogCommand, CommandSlot, SlotKind
from .parser import parse_grammar
from .readbuilder import make_build, parse_read_template

GROUP_ROLES = ["member", "manager", "owner"]
TRANSFER_SERVICES = ["drive", "calendar"]
FORWARD_ACTIONS = list(GAMCommands.FORWARD_ACTIONS)

# Group the ~53 grammar categories into a short, browsable set of areas (display order below).
# Anything unmapped falls through to "Other".
AREA_ORDER = [
    "Users & Identity", "Groups", "Org & Domains", "Calendars", "Drive", "Devices", "Classroom",
    "Security & Access", "Compliance & Audit", "Reporting", "Billing & Licensing", "Messaging",
    "GAM & Meta", "Other",
]
_AREA = {
    "Users & Identity": ["Users", "Aliases", "Schemas", "Administrators", "Customer", "Contacts"],
    "Groups": ["Groups", "Cloud Identity Groups"],
    "Org & Domains": ["Organizational Units", "Domain", "Domains"],
    "Calendars": ["Calendars", "Resource Calendars"],
    "Drive": ["Shared Drives"],
    "Devices": ["ChromeOS Devices", "Mobile Devices", "Cloud Identity Devices", "Printers"],
    "Classroom": ["Classroom", "Classroom User Profiles"],
    "Security & Access": ["Authorization", "Context Aware Access", "Inbound SSO", "Verifications"],
    "Compliance & Audit": ["Vault/Takeout", "Alert Center", "Email Audit Monitor",
                           "Classification Labels", "Cloud Identity Policies"],
    "Reporting": ["Reports", "Analytics Admin"],
    "Billing & Licensing": ["Licenses", "Reseller", "Cloud Channel"],
    "Messaging": ["Chat Bot", "Send Email"],
    "GAM & Meta": ["Meta Commands", "Bulk Processing", "Check connection to Google", "Addresses",
                   "Comment", "Version and Help", "Web Resourses and Sites"],
}
_CATEGORY_TO_AREA = {cat: area for area, cats in _AREA.items() for cat in cats}


def _area_of(category: str) -> str:
    if category in _CATEGORY_TO_AREA:
        return _CATEGORY_TO_AREA[category]
    return "Devices" if category.startswith("Chrome") else "Other"


def _resource(name: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # frozen .app
        return Path(meipass) / "resources" / "gam7" / name
    return Path(__file__).resolve().parents[2] / "resources" / "gam7" / name


def _slot(key: str, label: str, kind: SlotKind, **kw) -> CommandSlot:
    return CommandSlot(key=key, label=label, kind=kind, **kw)


def _cmd(cid, category, subcategory, name, risk, slots, build, raw, desc="") -> CatalogCommand:
    return CatalogCommand(
        id=cid, category=category, subcategory=subcategory, name=name, raw_syntax=raw,
        verb=name.split()[0].lower(), risk=risk, buildable=True, slots=slots, build=build,
        description=desc,
    )


def _curated() -> List[CatalogCommand]:
    U = SlotKind.TARGET_USER
    return [
        _cmd("build.set_signature", "Users", "Gmail - Signature", "Set Gmail signature", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("signature", "Signature (HTML)", SlotKind.TEXT)],
             lambda s: GAMCommands.set_signature(s["email"], s.get("signature", ""), html=True),
             "gam user <email> signature <html>",
             "Replace a user's Gmail signature with the HTML you provide."),
        _cmd("build.add_delegate", "Users", "Gmail - Delegates", "Add mailbox delegate", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("delegate", "Delegate", SlotKind.EMAIL)],
             lambda s: GAMCommands.add_delegate(s["email"], s.get("delegate", "")),
             "gam user <email> add delegate <delegate>",
             "Let another person read and send mail from this user's mailbox."),
        _cmd("build.remove_delegate", "Users", "Gmail - Delegates", "Remove mailbox delegate", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("delegate", "Delegate", SlotKind.EMAIL)],
             lambda s: GAMCommands.remove_delegate(s["email"], s.get("delegate", "")),
             "gam user <email> delete delegate <delegate>",
             "Revoke a delegate's access to this user's mailbox."),
        _cmd("build.set_vacation", "Users", "Gmail - Vacation", "Set vacation auto-reply", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("subject", "Subject", SlotKind.TEXT),
              _slot("message", "Message", SlotKind.TEXT)],
             lambda s: GAMCommands.set_vacation(s["email"], s.get("subject", ""), s.get("message", ""), html=True),
             "gam user <email> vacation on subject <s> message <m>",
             "Turn on the user's vacation auto-responder with a subject and message."),
        _cmd("build.vacation_off", "Users", "Gmail - Vacation", "Turn off vacation auto-reply", RiskLevel.LOW,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.vacation_off(s["email"]),
             "gam user <email> vacation off",
             "Switch off the user's vacation auto-responder."),
        _cmd("build.print_delegates", "Users", "Gmail - Delegates", "List mailbox delegates", RiskLevel.READ_ONLY,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.print_delegates(s["email"]),
             "gam user <email> print delegates",
             "Show everyone who has delegate access to this user's mailbox."),
        _cmd("build.add_forwarding_address", "Users", "Gmail - Forwarding", "Add a forwarding address", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("address", "Forward to", SlotKind.EMAIL)],
             lambda s: GAMCommands.add_forwarding_address(s["email"], s.get("address", "")),
             "gam user <email> add forwardingaddress <address>",
             "Register an address the user is allowed to forward mail to (does not enable forwarding)."),
        _cmd("build.set_forward", "Users", "Gmail - Forwarding", "Turn on forwarding", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("address", "Forward to", SlotKind.EMAIL),
              _slot("action", "Keep original as", SlotKind.CHOICE, choices=FORWARD_ACTIONS, default="keep")],
             lambda s: GAMCommands.set_forward(s["email"], s.get("address", ""), s.get("action") or "keep"),
             "gam user <email> forward on <action> <address>",
             "Auto-forward the user's incoming mail to an address, choosing what to do with the original."),
        _cmd("build.forward_off", "Users", "Gmail - Forwarding", "Turn off forwarding", RiskLevel.LOW,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.forward_off(s["email"]),
             "gam user <email> forward off",
             "Stop auto-forwarding the user's incoming mail."),
        _cmd("build.print_forwarding", "Users", "Gmail - Forwarding", "List forwarding addresses", RiskLevel.READ_ONLY,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.print_forwarding_addresses(s["email"]),
             "gam user <email> print forwardingaddresses",
             "Show the addresses this user may forward mail to."),
        _cmd("build.create_alias", "Aliases", "", "Add an alias to a user", RiskLevel.LOW,
             [_slot("alias", "New alias address", SlotKind.EMAIL), _slot("email", "User", U)],
             lambda s: GAMCommands.create_user_alias(s.get("alias", ""), s["email"]),
             "gam create alias <alias> user <email>",
             "Give the user an additional email address that delivers to their mailbox."),
        _cmd("build.delete_alias", "Aliases", "", "Remove an alias", RiskLevel.LOW,
             [_slot("alias", "Alias address", SlotKind.EMAIL)],
             lambda s: GAMCommands.delete_alias(s.get("alias", "")),
             "gam delete alias <alias>",
             "Remove an email alias so it no longer delivers anywhere."),
        _cmd("build.add_group_member", "Groups", "", "Add member to group", RiskLevel.LOW,
             [_slot("group", "Group", SlotKind.GROUP), _slot("member", "Member", SlotKind.USER),
              _slot("role", "Role", SlotKind.CHOICE, choices=GROUP_ROLES, default="member")],
             lambda s: GAMCommands.add_group_member(s["group"], s.get("member", ""), s.get("role") or "member"),
             "gam update group <group> add <role> <member>",
             "Add someone to a group as a member, manager, or owner."),
        _cmd("build.remove_group_member", "Groups", "", "Remove member from group", RiskLevel.LOW,
             [_slot("group", "Group", SlotKind.GROUP), _slot("member", "Member", SlotKind.USER)],
             lambda s: GAMCommands.remove_group_member(s["group"], s.get("member", "")),
             "gam update group <group> remove <member>",
             "Remove someone from a group."),
        _cmd("build.set_organization", "Users", "Profile", "Set title / department", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("title", "Title", SlotKind.TEXT, required=False),
              _slot("department", "Department", SlotKind.TEXT, required=False)],
             lambda s: GAMCommands.update_organization(s["email"], s.get("title", ""), s.get("department", "")),
             "gam update user <email> organization title <t> department <d> primary",
             "Set the user's job title and department on their directory profile."),
        _cmd("build.reset_password", "Users", "", "Reset password (random)", RiskLevel.LOW,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.reset_password(s["email"]),
             "gam update user <email> password random changepassword off",
             "Set the user's password to a new random value, locking out the old one."),
        _cmd("build.transfer_data", "Data Transfers", "", "Transfer Drive/Calendar ownership", RiskLevel.LOW,
             [_slot("old_owner", "From user", U),
              _slot("service", "Service", SlotKind.CHOICE, choices=TRANSFER_SERVICES, default="drive"),
              _slot("new_owner", "To user", SlotKind.USER)],
             lambda s: GAMCommands.create_datatransfer(s["old_owner"], s.get("service") or "drive", s.get("new_owner", "")),
             "gam create datatransfer <old> <service> <new>",
             "Hand a departing user's Drive or Calendar content to another user."),
        _cmd("build.suspend_user", "Users", "", "Suspend account", RiskLevel.DESTRUCTIVE,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.set_suspended(s["email"], True),
             "gam update user <email> suspended on",
             "Block the user from signing in, without deleting the account (reversible)."),
        _cmd("build.delete_user", "Users", "", "Delete account", RiskLevel.DESTRUCTIVE,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.delete_user(s["email"]),
             "gam delete user <email>",
             "Permanently delete the account (recoverable for ~20 days, then gone)."),
    ]


def _load_shallow() -> Tuple[List[CatalogCommand], str]:
    js = _resource("command_catalog.json")
    if js.exists():
        try:
            data = json.loads(js.read_text())
            return [CatalogCommand.from_json(c) for c in data.get("commands", [])], str(data.get("version", ""))
        except Exception:  # noqa: BLE001 — fall back to a live parse below
            pass
    txt = _resource("GamCommands.txt")
    if txt.exists():
        return parse_grammar(txt.read_text(errors="replace")), ""
    return [], ""


def _make_reads_buildable(commands: List[CatalogCommand]) -> None:
    """Attach a generic, injection-safe builder to every read-only command not already curated.

    Read-only commands can't mutate, so making the whole read surface runnable carries no write risk
    — and argv is still assembled token-by-token (never shell). Gated strictly to READ_ONLY +
    confident risk so a mis-classified verb can never become runnable here."""
    for c in commands:
        if c.buildable or c.risk != RiskLevel.READ_ONLY or c.uncertain:
            continue
        try:
            slots, template = parse_read_template(c.raw_syntax)
        except Exception:  # noqa: BLE001 — leave an un-parseable line browse-only
            continue
        c.slots = slots
        c.build = make_build(template)
        c.buildable = True


def load_catalog() -> Catalog:
    shallow, version = _load_shallow()
    commands = _curated() + shallow
    _make_reads_buildable(commands)
    for c in commands:
        c.area = _area_of(c.category)
        if not c.description:                 # vendored/curated text wins; else a grammar-derived gloss
            c.description = gloss(c)
    return Catalog(commands=commands, version=version)
