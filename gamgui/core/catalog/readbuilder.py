"""Generic, injection-safe builder for READ-ONLY catalog commands.

The curated overlay (`catalog.py`) hand-models a handful of high-value commands with friendly slots.
This module makes *every other* read-only GAM command runnable without hand-modeling each one: it
turns a command's grammar line into (a) typed slots for its required arguments and (b) a template
that assembles an argv list — literal keywords are fixed, slot values are dropped in as single
elements (so a value can never split into extra arguments). Optional `[...]` grammar groups are
omitted; the command runs with just its required arguments.

Safety: this is only ever attached to `RiskLevel.READ_ONLY` commands (callers gate on it), the
template's literal tokens come from the vendored grammar (not user input), and every user value lands
as exactly one argv element. It emits no verb of its own, so it can never turn a read into a write.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Tuple

from .models import CommandSlot, SlotKind

# A literal keyword (`print`, `course-participants`, `3lo`) or an alternation of them
# (`print|show`, `students|teachers`) — GAM keywords may contain hyphens and lead with a digit.
_LITERAL = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_LITERAL_ALT = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\|[a-z0-9][a-z0-9_-]*)+$")
_PLACEHOLDER = re.compile(r"<[^>]+>")

# Template parts (see `make_build`): ("lit", token) | ("req", key) | ("optpair", key, prefix).
TemplatePart = Tuple
_USER_PREFIX = "user"
_CROS_PREFIX = "cros"


def _humanize(placeholder: str) -> str:
    """`<RoleItem>` → "Role", `<EmailAddress>` → "Email address", `<ChatSpace>` → "Chat space"."""
    name = placeholder.strip("<>[]").split("|")[0]
    for suffix in ("TypeEntity", "Entity", "List", "Item"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).strip()
    return (spaced[:1].upper() + spaced[1:]) if spaced else placeholder


def _slot_kind(core: str) -> SlotKind:
    low = core.lower()
    if "email" in low:
        return SlotKind.EMAIL
    if "group" in low:
        return SlotKind.GROUP
    if "user" in low:
        return SlotKind.USER
    return SlotKind.TEXT


def parse_read_template(raw_syntax: str) -> Tuple[List[CommandSlot], List[TemplatePart]]:
    """Parse a `gam …` read line into (slots, argv-template).

    Walks the grammar tokens tracking `[...]` depth. Required (depth-0) tokens become either fixed
    literals or slots; optional groups are dropped — except a leading user/device entity, which is
    kept as an optional slot so the command can still be scoped to someone."""
    tokens = raw_syntax.split()
    if tokens and tokens[0] == "gam":
        tokens = tokens[1:]

    slots: List[CommandSlot] = []
    template: List[TemplatePart] = []
    used_labels: Dict[str, int] = {}
    depth = 0
    next_key = 0

    def add_slot(label: str, kind: SlotKind, required: bool, placeholder: str) -> str:
        nonlocal next_key
        key = f"a{next_key}"
        next_key += 1
        n = used_labels.get(label, 0) + 1   # disambiguate repeats ("User", "User 2")
        used_labels[label] = n
        slots.append(CommandSlot(key=key, label=label if n == 1 else f"{label} {n}",
                                 kind=kind, required=required, placeholder=placeholder))
        return key

    for pos, tok in enumerate(tokens):
        start_depth = depth
        depth += tok.count("[") - tok.count("]")
        optional = start_depth > 0 or tok.startswith("[")
        core = tok.strip("[]")
        if not core:
            continue
        # A single, self-contained optional token like `[<UserItem>]` or `[<UserTypeEntity>]`.
        self_contained_opt = (tok.startswith("[") and tok.endswith("]")
                              and tok.count("[") == 1 and tok.count("]") == 1)

        # A user/device entity expands to "user <x>" / "cros <x>". `<UserTypeEntity>`/`<CrOSTypeEntity>`
        # always; a bare leading `<UserItem>` is the operate-on user too. Only handle it when it's
        # required or a self-contained optional — never when buried inside a `[data <…>]` flag group.
        is_user = "UserTypeEntity" in core or (core == "<UserItem>" and pos == 0)
        is_cros = "CrOSTypeEntity" in core and "UserTypeEntity" not in core
        if (is_user or is_cros) and (not optional or self_contained_opt):
            prefix = _USER_PREFIX if is_user else _CROS_PREFIX
            label, kind = ("User", SlotKind.TARGET_USER) if is_user else ("Device", SlotKind.TEXT)
            key = add_slot(label, kind, required=not optional,
                           placeholder="user@domain" if is_user else "<CrOSDeviceID>")
            if optional:
                template.append(("optpair", key, prefix))
            else:
                template.append(("lit", prefix))
                template.append(("req", key))
            continue

        # A single-token optional positional like `info user [<UserItem>]` — keep as an optional slot
        # so the command can still be targeted. Multi-token optional flag groups are dropped below.
        if optional and self_contained_opt and ")" not in core:
            m = _PLACEHOLDER.search(core)
            if m:
                key = add_slot(_humanize(m.group()), _slot_kind(m.group()), required=False, placeholder=m.group())
                template.append(("opt", key))
            continue
        if optional:
            continue  # drop optional flags/fields — the command runs on its required args

        # Required (depth-0) tokens.
        if ")" in tok:
            continue  # a trailing alternative / group fragment — drop (avoids garbage + double slots)
        m = _PLACEHOLDER.search(core)
        if m:  # a placeholder, possibly wrapped in `<A>|(…` junk → one clean slot from the first <…>
            key = add_slot(_humanize(m.group()), _slot_kind(m.group()), required=True, placeholder=m.group())
            template.append(("req", key))
        elif "(" in core:
            continue  # a paren-group fragment with no placeholder
        elif _LITERAL.match(core) or _LITERAL_ALT.match(core):
            template.append(("lit", core.split("|")[0]))  # first alternative is the canonical form
        # else: unrecognized punctuation — skip rather than guess.

    return slots, template


def make_build(template: List[TemplatePart]) -> Callable[[Dict[str, str]], List[str]]:
    """Return a build(slot_values) → argv closure for a parsed template (injection-safe by design)."""
    def build(values: Dict[str, str]) -> List[str]:
        argv: List[str] = []
        for part in template:
            kind = part[0]
            if kind == "lit":
                argv.append(part[1])
            elif kind == "req":
                argv.append((values.get(part[1]) or "").strip())
            elif kind == "opt":
                value = (values.get(part[1]) or "").strip()
                if value:
                    argv.append(value)
            elif kind == "optpair":
                value = (values.get(part[1]) or "").strip()
                if value:
                    argv.extend([part[2], value])
        return argv
    return build
