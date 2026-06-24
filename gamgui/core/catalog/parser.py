"""Shallow parser for GAM's `GamCommands.txt` grammar → categorized `CatalogCommand`s.

Browse-only by design: it gives every command a category (from the `# ` section headers) and an
*inferred* risk (from the verb), so the catalog can show all ~1,000 commands. Only the curated
overlay (in `catalog.py`) is runnable.
"""

from __future__ import annotations

import re
from typing import List, Optional

from ..connectors.base import Capability, RiskLevel
from .models import CatalogCommand

# Verb → risk. The grammar often prefixes an entity (`gam <UserTypeEntity> delete delegate …`,
# `gam all users delete calendaracls …`), so risk is taken from the FIRST recognized verb token,
# not token 2.
_READ = {"print", "show", "info", "list", "get", "report", "whatis", "checkconnection", "version"}
_DESTRUCTIVE = {"delete", "remove", "wipe", "suspend", "deprovision", "purge", "empty", "trash"}
_LOW = {
    "add", "update", "create", "set", "modify", "move", "copy", "sync", "transfer", "import",
    "rotate", "clear", "enable", "disable", "reenable", "send", "append", "insert", "replace",
    "unsuspend", "undelete", "accept", "reject", "approve", "hide", "unhide", "archive", "unarchive",
    "upload", "download", "issue", "reset", "generate", "signout", "watch", "claim", "release",
    "select", "use", "cancel", "createcontactgroup",
}
_KNOWN = _READ | _DESTRUCTIVE | _LOW
_PLAIN_WORD = re.compile(r"^[a-z][a-z0-9_]*$")


def _alts(tok: str) -> List[str]:
    return [a.strip().lower() for a in tok.split("|") if a.strip()]


def _verb_risk(verb_token: str) -> RiskLevel:
    risk = RiskLevel.LOW
    for a in _alts(verb_token):
        if a in _DESTRUCTIVE:
            return RiskLevel.DESTRUCTIVE
        if a in _READ:
            risk = min(risk, RiskLevel.READ_ONLY) if risk != RiskLevel.LOW else RiskLevel.READ_ONLY
    return risk


def _find_verb(after: List[str]):
    """Return (verb_token, index, uncertain) — first token whose alt is a known verb."""
    for i, tok in enumerate(after[:6]):
        if any(a in _KNOWN for a in _alts(tok)):
            return tok, i, False
    return (after[0] if after else ""), 0, True


def _capability(category: str) -> Optional[Capability]:
    c = category.lower()
    if "group" in c:
        return Capability.GROUPS
    if "gmail" in c or "calendar" in c:
        return Capability.MAIL
    if "chrome" in c or "device" in c or "mobile" in c:
        return Capability.MDM
    if c in ("users", "organizational units", "aliases", "administrators", "schemas", "domains"):
        return Capability.DIRECTORY
    return None


def _is_header(line: str) -> bool:
    # A real section header ("# Users - Gmail - Delegates"), not a continuation comment ("#   [opt]").
    return line.startswith("# ") and len(line) > 2 and not line[2].isspace()


def _split_header(line: str):
    text = line[2:].strip()
    parts = text.split(" - ")
    return parts[0].strip(), " - ".join(p.strip() for p in parts[1:])


def parse_grammar(text: str) -> List[CatalogCommand]:
    out: List[CatalogCommand] = []
    category, subcategory = "Other", ""
    for lineno, raw in enumerate(text.splitlines()):
        if _is_header(raw):
            category, subcategory = _split_header(raw)
            continue
        if not raw.startswith("gam "):
            continue
        toks = raw.split()
        after = toks[1:]
        if not after:
            continue
        verb_tok, vi, uncertain = _find_verb(after)
        verb = _alts(verb_tok)[0] if _alts(verb_tok) else verb_tok.lower()
        risk = RiskLevel.LOW if uncertain else _verb_risk(verb_tok)
        noun = after[vi + 1] if vi + 1 < len(after) else ""
        noun = noun if _PLAIN_WORD.match(noun or "") else ""
        name = (f"{verb} {noun}".strip() or raw[4:]).capitalize()
        out.append(CatalogCommand(
            id=f"raw.{lineno}", category=category, subcategory=subcategory, name=name,
            raw_syntax=raw.strip(), verb=verb, risk=risk, capability=_capability(category),
            buildable=False, uncertain=uncertain,
        ))
    return out
