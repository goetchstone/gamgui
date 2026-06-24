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
from .models import Catalog, CatalogCommand, CommandSlot, SlotKind
from .parser import parse_grammar

GROUP_ROLES = ["member", "manager", "owner"]
TRANSFER_SERVICES = ["drive", "calendar"]
FORWARD_ACTIONS = list(GAMCommands.FORWARD_ACTIONS)


def _resource(name: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # frozen .app
        return Path(meipass) / "resources" / "gam7" / name
    return Path(__file__).resolve().parents[2] / "resources" / "gam7" / name


def _slot(key: str, label: str, kind: SlotKind, **kw) -> CommandSlot:
    return CommandSlot(key=key, label=label, kind=kind, **kw)


def _cmd(cid, category, subcategory, name, risk, slots, build, raw) -> CatalogCommand:
    return CatalogCommand(
        id=cid, category=category, subcategory=subcategory, name=name, raw_syntax=raw,
        verb=name.split()[0].lower(), risk=risk, buildable=True, slots=slots, build=build,
    )


def _curated() -> List[CatalogCommand]:
    U = SlotKind.TARGET_USER
    return [
        _cmd("build.set_signature", "Users", "Gmail - Signature", "Set Gmail signature", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("signature", "Signature (HTML)", SlotKind.TEXT)],
             lambda s: GAMCommands.set_signature(s["email"], s.get("signature", ""), html=True),
             "gam user <email> signature <html>"),
        _cmd("build.add_delegate", "Users", "Gmail - Delegates", "Add mailbox delegate", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("delegate", "Delegate", SlotKind.EMAIL)],
             lambda s: GAMCommands.add_delegate(s["email"], s.get("delegate", "")),
             "gam user <email> add delegate <delegate>"),
        _cmd("build.remove_delegate", "Users", "Gmail - Delegates", "Remove mailbox delegate", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("delegate", "Delegate", SlotKind.EMAIL)],
             lambda s: GAMCommands.remove_delegate(s["email"], s.get("delegate", "")),
             "gam user <email> delete delegate <delegate>"),
        _cmd("build.set_vacation", "Users", "Gmail - Vacation", "Set vacation auto-reply", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("subject", "Subject", SlotKind.TEXT),
              _slot("message", "Message", SlotKind.TEXT)],
             lambda s: GAMCommands.set_vacation(s["email"], s.get("subject", ""), s.get("message", ""), html=True),
             "gam user <email> vacation on subject <s> message <m>"),
        _cmd("build.vacation_off", "Users", "Gmail - Vacation", "Turn off vacation auto-reply", RiskLevel.LOW,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.vacation_off(s["email"]),
             "gam user <email> vacation off"),
        _cmd("build.print_delegates", "Users", "Gmail - Delegates", "List mailbox delegates", RiskLevel.READ_ONLY,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.print_delegates(s["email"]),
             "gam user <email> print delegates"),
        _cmd("build.add_forwarding_address", "Users", "Gmail - Forwarding", "Add a forwarding address", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("address", "Forward to", SlotKind.EMAIL)],
             lambda s: GAMCommands.add_forwarding_address(s["email"], s.get("address", "")),
             "gam user <email> add forwardingaddress <address>"),
        _cmd("build.set_forward", "Users", "Gmail - Forwarding", "Turn on forwarding", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("address", "Forward to", SlotKind.EMAIL),
              _slot("action", "Keep original as", SlotKind.CHOICE, choices=FORWARD_ACTIONS, default="keep")],
             lambda s: GAMCommands.set_forward(s["email"], s.get("address", ""), s.get("action") or "keep"),
             "gam user <email> forward on <action> <address>"),
        _cmd("build.forward_off", "Users", "Gmail - Forwarding", "Turn off forwarding", RiskLevel.LOW,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.forward_off(s["email"]),
             "gam user <email> forward off"),
        _cmd("build.print_forwarding", "Users", "Gmail - Forwarding", "List forwarding addresses", RiskLevel.READ_ONLY,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.print_forwarding_addresses(s["email"]),
             "gam user <email> print forwardingaddresses"),
        _cmd("build.create_alias", "Aliases", "", "Add an alias to a user", RiskLevel.LOW,
             [_slot("alias", "New alias address", SlotKind.EMAIL), _slot("email", "User", U)],
             lambda s: GAMCommands.create_user_alias(s.get("alias", ""), s["email"]),
             "gam create alias <alias> user <email>"),
        _cmd("build.delete_alias", "Aliases", "", "Remove an alias", RiskLevel.LOW,
             [_slot("alias", "Alias address", SlotKind.EMAIL)],
             lambda s: GAMCommands.delete_alias(s.get("alias", "")),
             "gam delete alias <alias>"),
        _cmd("build.add_group_member", "Groups", "", "Add member to group", RiskLevel.LOW,
             [_slot("group", "Group", SlotKind.GROUP), _slot("member", "Member", SlotKind.USER),
              _slot("role", "Role", SlotKind.CHOICE, choices=GROUP_ROLES, default="member")],
             lambda s: GAMCommands.add_group_member(s["group"], s.get("member", ""), s.get("role") or "member"),
             "gam update group <group> add <role> <member>"),
        _cmd("build.remove_group_member", "Groups", "", "Remove member from group", RiskLevel.LOW,
             [_slot("group", "Group", SlotKind.GROUP), _slot("member", "Member", SlotKind.USER)],
             lambda s: GAMCommands.remove_group_member(s["group"], s.get("member", "")),
             "gam update group <group> remove <member>"),
        _cmd("build.set_organization", "Users", "Profile", "Set title / department", RiskLevel.LOW,
             [_slot("email", "User", U), _slot("title", "Title", SlotKind.TEXT, required=False),
              _slot("department", "Department", SlotKind.TEXT, required=False)],
             lambda s: GAMCommands.update_organization(s["email"], s.get("title", ""), s.get("department", "")),
             "gam update user <email> organization title <t> department <d> primary"),
        _cmd("build.reset_password", "Users", "", "Reset password (random)", RiskLevel.LOW,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.reset_password(s["email"]),
             "gam update user <email> password random changepassword off"),
        _cmd("build.transfer_data", "Data Transfers", "", "Transfer Drive/Calendar ownership", RiskLevel.LOW,
             [_slot("old_owner", "From user", U),
              _slot("service", "Service", SlotKind.CHOICE, choices=TRANSFER_SERVICES, default="drive"),
              _slot("new_owner", "To user", SlotKind.USER)],
             lambda s: GAMCommands.create_datatransfer(s["old_owner"], s.get("service") or "drive", s.get("new_owner", "")),
             "gam create datatransfer <old> <service> <new>"),
        _cmd("build.suspend_user", "Users", "", "Suspend account", RiskLevel.DESTRUCTIVE,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.set_suspended(s["email"], True),
             "gam update user <email> suspended on"),
        _cmd("build.delete_user", "Users", "", "Delete account", RiskLevel.DESTRUCTIVE,
             [_slot("email", "User", U)],
             lambda s: GAMCommands.delete_user(s["email"]),
             "gam delete user <email>"),
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


def load_catalog() -> Catalog:
    shallow, version = _load_shallow()
    return Catalog(commands=_curated() + shallow, version=version)
