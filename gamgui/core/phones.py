"""The phone a signature shows: the primary, else the first of the phones Google keeps as one list.

GAM sends the phones it's given as the list Google keeps in place of the old one, so a phone left out is deleted. A change
therefore sends every other phone back as it is, and one GAM couldn't send back unchanged refuses the change
(``GAMCommands.update_phones``). Pure: the read and the write are on ``GAMConnector``. SwiftGamGui's ``previewPhone``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

from .gam.commands import Phone


def phones_from(record: Mapping[str, Any]) -> List[Phone]:
    """The phones in a user record, as Google keeps them. A record that isn't the person's, or phones that aren't a list of
    phones, are refused: read as "no phones", the change would delete every phone they have."""
    if not record.get("primaryEmail"):
        raise ValueError("GAM's answer wasn't this person's record, so their phones aren't known")
    phones = record.get("phones", [])
    if not isinstance(phones, list) or not all(isinstance(p, dict) for p in phones):
        raise ValueError("GAM's answer didn't list this person's phones as phones, so they aren't known")
    return [
        Phone(type=str(p.get("type") or ""), custom_type=str(p.get("customType") or ""), value=str(p.get("value") or ""),
              primary=bool(p.get("primary")))
        for p in phones
    ]


def shown_index(phones: Sequence[Phone]) -> Optional[int]:
    """Which phone a signature shows."""
    for i, phone in enumerate(phones):
        if phone.primary:
            return i
    return 0 if phones else None


def shown_phone(phones: Sequence[Phone]) -> str:
    i = shown_index(phones)
    return phones[i].value if i is not None else ""


@dataclass(frozen=True)
class PhoneChange:
    """What a preview shows and its confirm step sends: the whole list, in words."""

    phones: List[Phone]
    summary: str
    removes: bool     # the number is gone from Google: destructive, so it needs the Confirm click


def plan_phone_change(current: Sequence[Phone], typed: str, who: str) -> PhoneChange:
    """``typed`` as the phone a signature shows: set for someone with none (a work phone, and the primary), changed in
    place keeping its type, or emptied to remove it. ValueError when nothing would change."""
    phone = typed.strip()
    phones = list(current)
    shown = shown_index(phones)
    was = phones[shown].value if shown is not None else ""
    if phone == was:
        raise ValueError(f"Nothing to remove: {who} has no phone." if not phone else f"Nothing to change: that's already {who}'s phone.")
    if shown is not None and not phone:
        del phones[shown]
        summary = f"Remove {who}'s phone “{was}”"
    elif shown is not None:
        old = phones[shown]
        phones[shown] = Phone(type=old.type, custom_type=old.custom_type, value=phone, primary=old.primary)
        summary = f"Change {who}'s phone from “{was}” to “{phone}”"
    else:
        phones.append(Phone(type="work", value=phone, primary=True))
        summary = f"Set {who}'s work phone to “{phone}”"
    others = len(phones) - (1 if phone else 0)
    if others:
        summary += f", keeping their other {'phone' if others == 1 else f'{others} phones'} as they are"
    return PhoneChange(phones=phones, summary=summary, removes=not phone)
