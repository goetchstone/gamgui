"""A deterministic, never-wrong gloss for a command's "what it does".

The catalog ships verified, hand-or-LLM-authored descriptions in `command_catalog.json`. This module
is the *fallback*: when a command has no vendored description, it composes a plain-English one-liner
straight from the parsed grammar (action verb + object + scope). It only restates what the syntax
already says, so it can never mislead — at worst it's terse.
"""

from __future__ import annotations

import re

from .models import CatalogCommand
from .nouns import NOUNS   # generated GAM noun glossary (verified clear phrases)

# verb → the action phrase that reads naturally in front of an object
_ACTION = {
    "print": "List", "list": "List", "show": "Show", "info": "Show details for",
    "get": "Download", "report": "Report on", "whatis": "Identify",
    "checkconnection": "Check the connection to Google", "version": "Show the GAM version",
}

# Generic nouns whose meaning is command-specific — the glossary glossed each from a single example,
# so pin the common ones to their plain sense instead.
_OVERRIDE = {"users": "users", "user": "a user", "groups": "groups", "group": "a group"}

# GAM's terser nouns → a clearer phrase (only the common/opaque ones; everything else is humanized)
_NOUN = {
    "cros": "ChromeOS devices", "crostelemetry": "ChromeOS device telemetry",
    "ou": "an organizational unit", "ous": "organizational units", "ous": "organizational units",
    "sakeys": "service-account keys", "svcaccts": "service accounts", "svcacct": "a service account",
    "vaultmatters": "Vault matters", "vaultholds": "Vault holds", "vaultexports": "Vault exports",
    "calendaracls": "calendar sharing (ACLs)", "drivefileacls": "Drive file sharing (ACLs)",
    "shareddrives": "shared drives", "shareddrive": "a shared drive", "teamdrives": "shared drives",
    "delegates": "mailbox delegates", "forwardingaddresses": "forwarding addresses",
    "sendas": "send-as addresses", "filters": "Gmail filters", "labels": "Gmail labels",
    "datatransfers": "data-transfer jobs", "aliases": "email aliases", "asps": "app passwords",
    "tokens": "OAuth tokens", "admins": "admin role assignments", "adminroles": "admin roles",
    "privileges": "admin privileges", "schemas": "custom user schemas", "browsers": "managed browsers",
}

_PLACEHOLDER = re.compile(r"<[^>]+>")
_PLAIN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _humanize(token: str) -> str:
    low = token.lower()
    word = _OVERRIDE.get(low) or NOUNS.get(low) or _NOUN.get(low)   # overrides, then verified glossary
    if word:
        return word
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", token).replace("_", " ").replace("-", " ")
    return spaced.lower().strip()


_NO_OBJECT = {"version", "checkconnection"}   # verbs that take no object noun


def _object_phrase(cmd: CatalogCommand) -> str:
    """The noun the command acts on — the token immediately after the verb (GAM's `verb noun …`)."""
    if cmd.verb in _NO_OBJECT:
        return ""
    toks = cmd.raw_syntax.split()
    after = toks[toks.index(cmd.verb) + 1:] if cmd.verb in toks else toks[1:]
    if not after:
        return ""
    core = after[0].strip("[]").split("|")[0]   # first alternative of e.g. "delegates|delegate"
    return _humanize(core) if _PLAIN.match(core) else ""   # a placeholder/entity → no separate noun


def _scope(raw: str, obj: str) -> str:
    toks = raw.split()
    head = toks[1] if len(toks) > 1 else ""
    if "UserTypeEntity" in head or head in ("<UserItem>", "[<UserItem>]"):
        return "for a user"
    if "CrOSTypeEntity" in head and "chrome" not in obj.lower():  # don't say "ChromeOS … ChromeOS"
        return "for a ChromeOS device"
    return ""


def gloss(cmd: CatalogCommand) -> str:
    """A one-line fallback description derived only from the command's grammar."""
    action = _ACTION.get(cmd.verb, (cmd.verb or "Run").capitalize())
    obj = _object_phrase(cmd)
    scope = _scope(cmd.raw_syntax, obj)
    if "user" in obj.lower():   # avoid "a user's mailbox … for a user"
        scope = ""
    parts = [action]
    if obj:
        parts.append(obj)
    if scope:
        parts.append(scope)
    sentence = " ".join(parts).strip()
    return (sentence[:1].upper() + sentence[1:] + ".") if sentence else ""
