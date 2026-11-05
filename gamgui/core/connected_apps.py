"""The apps people have given access to their account, grouped by app: who granted each, what it may do in
words, and how much that is. Answers "who has which apps and AI agents connected" for the whole tenant.

Pure: the read is ``GAMConnector.connected_apps``. A name that looks like an AI assistant or agent is marked by
its name alone. An app's name is its maker's words, and so can be an attacker's: it is only ever displayed,
autoescaped. Ported from SwiftGamGui's ``GamEngine/ConnectedApps.swift``; its on-device wording isn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class Reach(IntEnum):
    """How much an app may do, broadest first."""

    BROAD = 0    # all of someone's mail or Drive, their Gmail forwarding, admin or Cloud access
    SOME = 1     # reading or sending mail, reading Drive, calendar or contacts
    LITTLE = 2   # signing in, or little else


class Area(Enum):
    """The kinds of broad access, which the summary counts. The value is its words."""

    MAIL = "all of Gmail or reading and changing mail"
    FORWARDING = "Gmail forwarding and delegates"
    DRIVE = "all of Drive"
    ADMIN = "admin access"
    CLOUD = "Google Cloud"


Described = Tuple[str, Reach, Optional[Area]]

_GOOGLE_SCOPE = "https://www.googleapis.com/auth/"
_SCOPES: Dict[str, Described] = {
    "https://mail.google.com/": ("all of Gmail (read, send, delete)", Reach.BROAD, Area.MAIL),
    "https://mail.google.com": ("all of Gmail (read, send, delete)", Reach.BROAD, Area.MAIL),
    "gmail.modify": ("read and change mail", Reach.BROAD, Area.MAIL),
    "gmail.settings.sharing": ("change Gmail forwarding and delegates", Reach.BROAD, Area.FORWARDING),
    "drive": ("all of Drive", Reach.BROAD, Area.DRIVE),
    "cloud-platform": ("Google Cloud", Reach.BROAD, Area.CLOUD),
    "gmail.readonly": ("read mail", Reach.SOME, None),
    "gmail.send": ("send mail", Reach.SOME, None),
    "gmail.compose": ("draft and send mail", Reach.SOME, None),
    "gmail.insert": ("add mail", Reach.SOME, None),
    "gmail.settings.basic": ("change Gmail settings", Reach.SOME, None),
    "gmail.metadata": ("read mail headers", Reach.SOME, None),
    "gmail.labels": ("manage labels", Reach.LITTLE, None),
    "drive.readonly": ("read all of Drive", Reach.SOME, None),
    "drive.file": ("files it made or was given", Reach.LITTLE, None),
    "calendar": ("change calendars", Reach.SOME, None),
    "calendar.events": ("change calendars", Reach.SOME, None),
    "calendar.readonly": ("read calendars", Reach.SOME, None),
    "calendar.events.readonly": ("read calendars", Reach.SOME, None),
    "contacts": ("read contacts", Reach.SOME, None),
    "contacts.readonly": ("read contacts", Reach.SOME, None),
    "contacts.other.readonly": ("read contacts", Reach.SOME, None),
    "directory.readonly": ("read contacts", Reach.SOME, None),
    "openid": ("sign in with Google", Reach.LITTLE, None),
    "email": ("sign in with Google", Reach.LITTLE, None),
    "profile": ("sign in with Google", Reach.LITTLE, None),
    "userinfo.email": ("sign in with Google", Reach.LITTLE, None),
    "userinfo.profile": ("sign in with Google", Reach.LITTLE, None),
}

# Whole words in an app's name that mark an AI assistant or agent: the assistants' and automation tools' names, and
# the words themselves ("Read AI", "Inbox Agent"). Whole words only: "Promotion Manager" isn't Motion.
_AI_WORDS = frozenset({
    "chatgpt", "openai", "gpt", "claude", "anthropic", "copilot", "perplexity", "gemini", "zapier", "n8n",
    "otter", "fireflies", "fathom", "motion", "reclaim", "superhuman", "grammarly", "jasper", "llm",
    "ai", "agent", "agents", "assistant", "bot", "gpts",
})


@dataclass(frozen=True)
class Grant:
    """One person's grant to one app, from GAM's `user,clientId,displayText,anonymous,nativeApp,userKey,scopes`."""

    user: str
    client_id: str
    name: str
    scopes: Tuple[str, ...]


@dataclass(frozen=True)
class ConnectedApp:
    """One app and everyone who granted it."""

    client_id: str
    name: str
    users: Tuple[str, ...]
    access: Tuple[str, ...]     # what it may do, in words, broadest first
    reach: Reach
    areas: Tuple[Area, ...]     # its kinds of broad access, in Area's order
    looks_like_ai: bool

    @property
    def is_broad(self) -> bool:
        return self.reach == Reach.BROAD


def describe(scope: str) -> Described:
    """A scope in words, with how much it reaches and, when that is broad, its kind."""
    short = scope.replace(_GOOGLE_SCOPE, "")
    known = _SCOPES.get(short)
    if known is not None:
        return known
    if short.startswith("admin."):
        return f"admin: {short[6:]}", Reach.BROAD, Area.ADMIN
    if short.startswith("gmail."):
        return f"Gmail: {short[6:]}", Reach.SOME, None
    if short.startswith("drive."):
        return f"Drive: {short[6:]}", Reach.LITTLE, None
    parts = [part for part in short.split("/") if part]
    return (parts[-1] if parts else short), Reach.LITTLE, None


def looks_like_ai(name: str) -> bool:
    """A word of the name is an AI word: "ChatGPT" in "ChatGPT for Gmail", "GPT" in "GPT-4o"."""
    return any(word in _AI_WORDS for word in re.findall(r"[^\W_]+", name.lower()))


def _text(record: Mapping[str, Any], key: str) -> str:
    return str(record.get(key) or "")


def grants_from(records: Iterable[Mapping[str, Any]]) -> List[Grant]:
    """Every grant in GAM's output. Its scopes are joined by GAM's CSV delimiter, a space by default."""
    return [
        Grant(
            user=_text(record, "user"),
            client_id=_text(record, "clientId"),
            name=_text(record, "displayText"),
            scopes=tuple(scope for scope in re.split(r"[\s,]+", _text(record, "scopes")) if scope),
        )
        for record in records
        if _text(record, "clientId")
    ]


def _unique(items: Iterable[str], key=lambda item: item) -> Tuple[str, ...]:
    seen, kept = set(), []
    for item in items:
        if key(item) not in seen:
            seen.add(key(item))
            kept.append(item)
    return tuple(kept)


def apps_from(grants: Sequence[Grant]) -> List[ConnectedApp]:
    """The grants by app: AI assistants and agents first, then the broadest reach, then the most people."""
    by_id: Dict[str, List[Grant]] = {}
    for grant in grants:
        by_id.setdefault(grant.client_id, []).append(grant)
    apps = []
    for client_id, theirs in by_id.items():
        scopes = _unique(scope for grant in theirs for scope in grant.scopes)
        described = sorted((describe(scope) for scope in scopes), key=lambda d: d[1])
        name = theirs[0].name
        areas = {d[2] for d in described if d[2] is not None}
        apps.append(ConnectedApp(
            client_id=client_id,
            name=name or client_id,
            users=_unique((grant.user for grant in theirs), key=str.lower),
            access=_unique(d[0] for d in described),
            reach=described[0][1] if described else Reach.LITTLE,
            areas=tuple(area for area in Area if area in areas),
            looks_like_ai=looks_like_ai(name),
        ))
    return sorted(apps, key=lambda a: (not a.looks_like_ai, a.reach, -len(a.users), a.name.lower()))


def groups_of(apps: Sequence[ConnectedApp]) -> List[Tuple[str, List[ConnectedApp]]]:
    """The list as Reports shows it, leaving out a group with nothing in it."""
    groups = [
        ("AI assistants and agents, by name", [a for a in apps if a.looks_like_ai]),
        ("Broad access", [a for a in apps if not a.looks_like_ai and a.is_broad]),
        ("Other apps", [a for a in apps if not a.looks_like_ai and not a.is_broad]),
    ]
    return [group for group in groups if group[1]]


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _people(apps: Iterable[ConnectedApp]) -> int:
    return len({user.lower() for app in apps for user in app.users})


@dataclass(frozen=True)
class Summary:
    """What every account's apps add up to, counted by code. It holds no app's name or scopes."""

    apps: int
    people: int             # who granted at least one app
    ai: int                 # apps whose names look like an AI assistant's or agent's
    ai_people: int
    broad: int
    ai_broad: int
    areas: Dict[Area, int]  # how many apps have each kind of broad access; an app can have more than one
    one_person: int         # apps only one person granted, and the AI and the broad ones among them
    one_person_ai: int
    one_person_broad: int
    most_people: int        # the most people who granted any one app

    @classmethod
    def of(cls, apps: Sequence[ConnectedApp]) -> "Summary":
        ai = [a for a in apps if a.looks_like_ai]
        lone = [a for a in apps if len(a.users) == 1]
        areas: Dict[Area, int] = {}
        for app in apps:
            for area in app.areas:
                areas[area] = areas.get(area, 0) + 1
        return cls(
            apps=len(apps),
            people=_people(apps),
            ai=len(ai),
            ai_people=_people(ai),
            broad=sum(a.is_broad for a in apps),
            ai_broad=sum(a.is_broad for a in ai),
            areas=areas,
            one_person=len(lone),
            one_person_ai=sum(a.looks_like_ai for a in lone),
            one_person_broad=sum(a.is_broad for a in lone),
            most_people=max((len(a.users) for a in apps), default=0),
        )

    def _kinds(self) -> str:
        """Broad access by kind, after "N apps have broad access": ", to all of Drive" when every one has the
        same kind, else each kind with its count."""
        kinds = [area for area in Area if area in self.areas]
        if len(kinds) == 1:
            return f", to {kinds[0].value}"
        if not kinds:
            return ""
        return ": " + ", ".join(f"{self.areas[area]} to {area.value}" for area in kinds)

    @property
    def sentences(self) -> List[str]:
        """The summary in plain words, a sentence each."""
        if not self.apps:
            return ["No account has given an app access."]
        lines = [f"{_count(self.people, 'person has', 'people have')} given {_count(self.apps, 'app', 'apps')} access."]
        if self.ai:
            lines.append(
                f"{_count(self.ai, 'app looks', 'apps look')} like an AI assistant or agent by name, granted by "
                f"{_count(self.ai_people, 'person', 'people')}; {self.ai_broad or 'none'} with broad access."
            )
        else:
            lines.append("No app looks like an AI assistant or agent by its name.")
        if self.broad:
            lines.append(f"{_count(self.broad, 'app has', 'apps have')} broad access{self._kinds()}.")
        else:
            lines.append("No app has broad access (all of Gmail or Drive, forwarding, admin or Google Cloud).")
        if self.one_person:
            among = []
            if self.one_person_ai:
                among.append(_count(self.one_person_ai, "that looks like AI", "that look like AI"))
            if self.one_person_broad:
                among.append(f"{self.one_person_broad} with broad access")
            tail = ", among them " + " and ".join(among) + "." if among else "."
            lines.append(f"{_count(self.one_person, 'app was', 'apps were')} granted by one person only{tail}")
        lines.append(f"At most {_count(self.most_people, 'person', 'people')} granted any one app.")
        return lines


@dataclass(frozen=True)
class AppsRead:
    """One read as Reports shows it, worked out once: a kept read is shown on every visit."""

    groups: List[Tuple[str, List[ConnectedApp]]]
    sentences: List[str]
    app_count: int
    complete: bool = True   # False: GAM couldn't read at least one account, so its apps aren't here

    @classmethod
    def of(cls, apps: Sequence[ConnectedApp], complete: bool = True) -> "AppsRead":
        return cls(groups_of(apps), Summary.of(apps).sentences, len(apps), complete)


def read_from(records: Iterable[Mapping[str, Any]], complete: bool = True) -> AppsRead:
    return AppsRead.of(apps_from(grants_from(records)), complete)
