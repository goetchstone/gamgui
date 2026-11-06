"""Saved offboarding routines: the whole form but the two addresses and the "already done" ticks, which are one leaver's.

Picking one fills the form and nothing else, so the plan is always the form's and the lock can't be left out by a template.
The file is the operator's: written atomically ``0600`` in a ``0700`` folder, and one it can't read, or holding a template
that couldn't run, is moved aside whole and the starters shown, never saved over. One set for every domain, as the signature
templates are. Ported from SwiftGamGui's ``Offboarding.Template`` and ``OffboardTemplateStore``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from .lifecycle import ADDABLE_STEPS, DEFAULT_MESSAGE, DEFAULT_SUBJECT, OPTIONAL_STEPS, RUN_ORDER, ROUTINE, STEP_NAMES
from .onboarding import looks_like_email
from .paths import app_data_dir

MIN_DAYS, MAX_DAYS = 1, 36_500
FILE_VERSION = 2


@dataclass(frozen=True)
class OffboardTemplate:
    leave_out: FrozenSet[str] = frozenset()
    add: FrozenSet[str] = frozenset()
    subject: str = DEFAULT_SUBJECT     # as typed: an empty one runs the default text, as the form's does
    message: str = DEFAULT_MESSAGE
    days: int = 30
    invitee: str = ""                  # also invited to the reminder

    @property
    def runs(self) -> List[str]:
        """The steps it runs, in run order, by name."""
        keys = (set(ROUTINE) | self.add) - self.leave_out
        return [STEP_NAMES[key] for key in RUN_ORDER if key in keys]

    @property
    def problem(self) -> str:
        """Why it can't be saved, in words; "" when it can."""
        for key in RUN_ORDER:
            if key in self.leave_out and key not in OPTIONAL_STEPS:
                if key in ADDABLE_STEPS:
                    return f"“{STEP_NAMES[key]}” isn't left out: it runs only when added."
                return f"“{STEP_NAMES[key]}” always runs: the reset, the sign-out and forwarding off lock the account."
        if unknown := (self.leave_out | self.add) - set(RUN_ORDER):
            return f"“{sorted(unknown)[0]}” isn't an offboarding step."
        for key in RUN_ORDER:
            if key in self.add and key not in ADDABLE_STEPS:
                return f"“{STEP_NAMES[key]}” is one of the routine's steps: it isn't added, it runs unless left out."
        if not MIN_DAYS <= self.days <= MAX_DAYS:
            return f"The reminder is between {MIN_DAYS} and {MAX_DAYS:,} days out."
        invitee = self.invitee.strip()
        if invitee and not looks_like_email(invitee):
            return f"The invitee “{invitee}” isn't one email address. Enter one, like it@example.com, or leave it empty."
        return ""

    def as_json(self) -> Dict[str, Any]:
        return {"leave_out": sorted(self.leave_out), "add": sorted(self.add), "subject": self.subject, "message": self.message,
                "days": self.days, "invitee": self.invitee}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "OffboardTemplate":
        """ValueError for a record that isn't one. Version 1's has no `add`, and reads as adding nothing."""
        leave_out, add, days = data.get("leave_out", []), data.get("add", []), data.get("days", 30)
        subject, message, invitee = data.get("subject", DEFAULT_SUBJECT), data.get("message", DEFAULT_MESSAGE), data.get("invitee", "")
        if (not isinstance(leave_out, list) or not isinstance(add, list) or not isinstance(days, int) or isinstance(days, bool)
                or not all(isinstance(v, str) for v in (*leave_out, *add, subject, message, invitee))):
            raise ValueError("not a template")
        return cls(frozenset(leave_out), frozenset(add), subject, message, days, invitee)


# The templates on first use, all the operator's to change or delete. The form opens as it always has, all eight steps,
# until the operator picks another default. "Hand-over" is the routine without the domain-wide calendar sweep; "Lock only" is
# the lock alone, for a leaver whose hand-over waits. None adds a step.
STARTERS: Tuple[Tuple[str, OffboardTemplate], ...] = (
    ("All eight steps", OffboardTemplate()),
    ("Hand-over", OffboardTemplate(leave_out=frozenset({"calacls"}))),
    ("Lock only", OffboardTemplate(leave_out=frozenset(OPTIONAL_STEPS))),
)
STARTER_DEFAULT = "All eight steps"


def default_store_path() -> Path:
    return app_data_dir() / "offboarding-templates.json"


@dataclass
class OffboardTemplateStore:
    path: Path = field(default_factory=default_store_path)
    set_aside: Optional[Path] = None     # where an unreadable file went: said on the screen

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._templates, self.default = self._load()

    def _starters(self) -> Tuple[Dict[str, OffboardTemplate], str]:
        return dict(STARTERS), STARTER_DEFAULT

    def _load(self) -> Tuple[Dict[str, OffboardTemplate], str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._starters()
        except OSError:
            return self._move_aside()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") not in (1, FILE_VERSION) or not isinstance(data.get("templates"), dict):
                raise ValueError("not a templates file")
            templates = {str(name): OffboardTemplate.from_json(t) for name, t in data["templates"].items()}
            if any(t.problem for t in templates.values()) or not templates:
                raise ValueError("a template that couldn't run")
            default = data.get("default")
            return templates, default if default in templates else next(iter(templates))
        except (ValueError, TypeError, AttributeError):
            return self._move_aside()

    def _move_aside(self) -> Tuple[Dict[str, OffboardTemplate], str]:
        aside = self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
        try:
            os.replace(self.path, aside)
            self.set_aside = aside
        except OSError:
            pass
        return self._starters()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        data = {"version": FILE_VERSION, "default": self.default,
                "templates": {name: t.as_json() for name, t in self._templates.items()}}
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".offboarding-templates-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def names(self) -> List[str]:
        return sorted(self._templates, key=str.lower)

    def get(self, name: str) -> Optional[OffboardTemplate]:
        return self._templates.get(name)

    @property
    def opening(self) -> OffboardTemplate:
        return self._templates[self.default]

    def save(self, name: str, template: OffboardTemplate) -> None:
        """ValueError, in words, for what can't be saved."""
        name = name.strip()
        if not name:
            raise ValueError("Give the template a name.")
        if template.problem:
            raise ValueError(template.problem)
        self._templates[name] = template
        self._save()

    def delete(self, name: str) -> None:
        """ValueError for the last one: the form needs something to open on."""
        if name not in self._templates:
            return
        if len(self._templates) == 1:
            raise ValueError("That's the last template: keep one for the form to open on.")
        del self._templates[name]
        if self.default == name:
            self.default = self.names()[0]
        self._save()

    def set_default(self, name: str) -> None:
        if name in self._templates:
            self.default = name
            self._save()
