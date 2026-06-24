"""Data model for the command catalog."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..connectors.base import Capability, RiskLevel


class SlotKind(enum.Enum):
    TARGET_USER = "target_user"  # the primary affected user (drag a person in) — the guard's `target`
    USER = "user"                # another user (drag a person in)
    GROUP = "group"              # a group (drag a group in)
    EMAIL = "email"              # a typed email
    TEXT = "text"                # free text (signature, subject, …)
    CHOICE = "choice"            # one of `choices`


_DRAG_KINDS = {SlotKind.TARGET_USER, SlotKind.USER, SlotKind.GROUP}


@dataclass
class CommandSlot:
    key: str                     # maps to the build() kwarg
    label: str
    kind: SlotKind
    required: bool = True
    choices: Optional[List[str]] = None   # for CHOICE
    default: str = ""
    placeholder: str = ""

    @property
    def is_drop(self) -> bool:
        return self.kind in _DRAG_KINDS


@dataclass
class CatalogCommand:
    id: str
    category: str
    subcategory: str
    name: str
    raw_syntax: str              # the verbatim `gam …` line (browse + copy)
    verb: str
    risk: RiskLevel
    capability: Optional[Capability] = None
    buildable: bool = False
    uncertain: bool = False      # risk inferred from an unknown verb
    slots: List[CommandSlot] = field(default_factory=list)
    # Curated-only; never serialized. Maps a {slot_key: value} dict to an argv list via GAMCommands.
    build: Optional[Callable[[Dict[str, str]], List[str]]] = field(default=None, repr=False)

    @property
    def risk_label(self) -> str:
        return {RiskLevel.READ_ONLY: "read", RiskLevel.LOW: "change", RiskLevel.DESTRUCTIVE: "destructive"}[self.risk]

    def to_json(self) -> dict:
        return {
            "id": self.id, "category": self.category, "subcategory": self.subcategory,
            "name": self.name, "raw_syntax": self.raw_syntax, "verb": self.verb,
            "risk": int(self.risk), "uncertain": self.uncertain,
        }

    @classmethod
    def from_json(cls, d: dict) -> "CatalogCommand":
        return cls(
            id=str(d["id"]), category=str(d.get("category", "Other")),
            subcategory=str(d.get("subcategory", "")), name=str(d.get("name", "")),
            raw_syntax=str(d.get("raw_syntax", "")), verb=str(d.get("verb", "")),
            risk=RiskLevel(int(d.get("risk", 1))), uncertain=bool(d.get("uncertain", False)),
            buildable=False,
        )


@dataclass
class Catalog:
    commands: List[CatalogCommand]
    version: str = ""

    def categories(self) -> List[tuple]:
        """[(category, count)] sorted, with the most-populated areas readable first."""
        counts: Dict[str, int] = {}
        for c in self.commands:
            counts[c.category] = counts.get(c.category, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[0].lower())

    def in_category(self, category: str, q: str = "") -> List[CatalogCommand]:
        ql = (q or "").strip().lower()
        items = [c for c in self.commands if c.category == category]
        if ql:
            items = [c for c in items if ql in c.name.lower() or ql in c.raw_syntax.lower()]
        return sorted(items, key=lambda c: (not c.buildable, c.name.lower()))  # buildable first

    def search(self, q: str, limit: int = 80) -> List[CatalogCommand]:
        ql = (q or "").strip().lower()
        if not ql:
            return []
        items = [c for c in self.commands if ql in c.name.lower() or ql in c.raw_syntax.lower()]
        return sorted(items, key=lambda c: (not c.buildable, c.name.lower()))[:limit]

    def by_id(self, cid: str) -> Optional[CatalogCommand]:
        return next((c for c in self.commands if c.id == cid), None)

    def buildable(self) -> List[CatalogCommand]:
        return [c for c in self.commands if c.buildable]
