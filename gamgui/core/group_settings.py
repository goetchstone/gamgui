"""A group's settings (roadmap 4.4): seven of Google's, in words, read with `info group` and changed with `update group`.

Who can post, who can join, who can see the members, members from outside, moderation, the web, the archive: the ones an
admin is asked about. Each is read again before a change is held and again at the click, and only the settings that change
go in the argv, so Google keeps the rest as they are.

A setting the read didn't give is unknown, never a default (invariant 10): GAM skips an abuse@ or postmaster@ group's
settings without a word, and a read missing any of them offers nothing. A value Google gives that isn't one offered here is
shown as Google's, and left alone.

Google refuses some combinations itself. GAM prints that as `Group: <g>, Update Failed: <Google's words>` (exit 50), and the
screen says it in Google's words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .gam.commands import GROUP_SETTINGS

# Each setting's name on the screen, and each value in words: Google's Groups Settings API reference
# (developers.google.com/workspace/admin/groups-settings/v1/reference/groups), shortened.
WORDS: Dict[str, Tuple[str, Dict[str, str]]] = {
    "whocanpostmessage": ("Who can post", {
        "anyone_can_post": "Anyone, outside the organization too", "all_in_domain_can_post": "Anyone in the organization",
        "all_members_can_post": "Members", "all_managers_can_post": "Managers and owners",
        "all_owners_can_post": "Owners", "none_can_post": "Nobody"}),
    "whocanjoin": ("Who can join", {
        "anyone_can_join": "Anyone, outside the organization too", "all_in_domain_can_join": "Anyone in the organization",
        "can_request_to_join": "People can ask to join", "invited_can_join": "Only people invited"}),
    "whocanviewmembership": ("Who can see the members", {
        "all_in_domain_can_view": "Anyone in the organization", "all_members_can_view": "Members",
        "all_managers_can_view": "Managers and owners", "all_owners_can_view": "Owners"}),
    "allowexternalmembers": ("Members from outside the organization", {"true": "Allowed", "false": "Not allowed"}),
    "messagemoderationlevel": ("Messages held for a moderator", {
        "moderate_none": "None", "moderate_new_members": "From new members", "moderate_non_members": "From non-members",
        "moderate_all_messages": "All of them"}),
    "allowwebposting": ("Posting from the Groups website", {"true": "Allowed", "false": "Not allowed: email only"}),
    "isarchived": ("Conversation history", {"true": "Kept", "false": "Not kept"}),
}

_REFUSED = re.compile(r"Group: [^\n]*?, Update Failed: (.+)$", re.M)   # a result's detail leads with "GAM failed (…): "


@dataclass(frozen=True)
class Setting:
    key: str                       # GAM's word for it: `update group <g> <key> <value>`
    label: str
    google: str                    # Google's value as read
    value: Optional[str]           # as GAM is given it, or None: Google's value isn't one offered here
    choices: Tuple[Tuple[str, str], ...]

    @property
    def shown(self) -> str:
        return WORDS[self.key][1].get(self.value or "", f"Google's value: {self.google}")


@dataclass(frozen=True)
class Change:
    key: str
    old: str                       # Google's value when previewed, as read (compared again at the click)
    new: str                       # as GAM is given it

    @property
    def label(self) -> str:
        return WORDS[self.key][0]

    @property
    def before(self) -> str:
        return WORDS[self.key][1].get(self.old.lower(), f"Google's value: {self.old}")

    @property
    def after(self) -> str:
        return WORDS[self.key][1][self.new]


def _google(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def read(record: Mapping[str, Any]) -> Optional[List[Setting]]:
    """Each offered setting as the record has it, or None when any is missing: unread, never a default."""
    out = []
    for key, (field, values) in GROUP_SETTINGS.items():
        if record.get(field) in (None, ""):
            return None
        google = _google(record[field])
        out.append(Setting(key, WORDS[key][0], google, google.lower() if google.lower() in values else None,
                           tuple((v, WORDS[key][1][v]) for v in values)))
    return out


def changes(now: List[Setting], wanted: Mapping[str, str]) -> List[Change]:
    """What the form asks for that differs from the read, in the read's order. A value that isn't one offered raises."""
    out = []
    for s in now:
        new = (wanted.get(s.key) or "").strip()
        if not new:
            continue
        if new not in GROUP_SETTINGS[s.key][1]:
            raise ValueError(f"{new!r} isn't a value offered for {s.label}")
        if new != s.value:
            out.append(Change(s.key, s.google, new))
    return out


def moved(held: List[Change], now: List[Setting]) -> List[str]:
    """The held changes whose setting no longer reads as it did at the preview: changed elsewhere since."""
    current = {s.key: s.google for s in now}
    return [c.label for c in held if current.get(c.key, "").lower() != c.old.lower()]


def google_said(detail: str) -> str:
    """Google's words for a refused update, from GAM's `Group: <g>, Update Failed: <why>`, or ""."""
    found = _REFUSED.findall(detail or "")
    return found[-1].strip() if found else ""
