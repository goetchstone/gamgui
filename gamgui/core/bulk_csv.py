"""Change many people from a CSV (roadmap 4.6): an `email` column plus any of title, department, phone and org_unit.

The columns are the ones Reports' fill-in sheets download (``reports.FILL_IN_COLUMNS``), so a sheet filled in comes back as it
went out: `name` is there for whoever fills it in and is not applied, and `location` is said and not applied (a building has to
be one Google knows). **A blank cell leaves that value alone; nothing here clears one.**

Google keeps a person's organizations, and their phones, each as one list that a write replaces whole, so a change sends every
entry back as it was read with only the changed value different (``GAMCommands.update_profile``), and an entry GAM couldn't
send back unchanged refuses that person. The preview plans from the directory read; the run reads each person again right
before their write and changes nothing for them if their organizations, phones or org unit moved since.

The preview's "before" downloads as a CSV of the same columns: the undo. A value that was empty before can't be put back by
it (a blank cell changes nothing), and the preview says so.
"""

from __future__ import annotations

import csv
import io
import json
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import phones as phones_mod
from .bulk import stop_reason, stop_requested
from .csv_safe import csv_safe
from .gam.errors import GAMError
from .gam.commands import ORG_TEXT_FIELDS, ORG_TYPES, GAMCommands, Organization, Phone
from .gam.models import GAMUser
from .onboarding import looks_like_email

COLUMNS = ("title", "department", "phone", "org_unit")      # applied, in this order
HEADER = ("email", *COLUMNS)
NOT_APPLIED = {"name": "is there for whoever fills the sheet in", "location": "isn't set from a CSV: a building has to be one "
               "Google knows, so set it in the Admin console"}
_SAME = {"org unit": "org_unit", "orgunit": "org_unit", "ou": "org_unit", "org_unit_path": "org_unit", "work phone": "phone",
         "work_phone": "phone"}
MAX_BYTES = 1024 * 1024
MAX_ROWS = 2000
_UNGUARD = ("=", "+", "-", "@", "\t", "\r")   # what csv_safe puts a ' before
LABELS = {"title": "Title", "department": "Department", "phone": "Phone", "org_unit": "Org unit"}


@dataclass(frozen=True)
class Row:
    line: int
    email: str
    values: Mapping[str, str]       # only the cells that say something: a blank one leaves the value alone


@dataclass
class Parsed:
    rows: List[Row] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def decode(data: bytes) -> str:
    """The file as text: UTF-8, a byte-order mark dropped (Excel's "CSV UTF-8" writes one). Anything else is refused rather
    than guessed: a wrong guess changes the names it sets."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("The file isn't UTF-8 text. In Excel, save it as “CSV UTF-8”; in Sheets, download it as CSV.") from None


def _column(name: str) -> str:
    key = " ".join((name or "").strip().lower().replace("-", " ").split())
    return _SAME.get(key, key.replace(" ", "_"))


def _cell(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == "'" and value[1] in _UNGUARD:   # csv_safe's guard, as this app writes it
        return value[1:]
    return value


def parse(text: str) -> Parsed:
    """The rows that set something, each refused one said by its line. The whole file is refused when it can't be read."""
    out = Parsed()
    first = text.split("\n", 1)[0]
    delimiter = ";" if ";" in first and "," not in first else ","     # a semicolon is Excel's in some regions
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        records = [(reader.line_num, raw) for raw in reader]
    except csv.Error as exc:
        out.errors.append(f"Line {reader.line_num + 1}: the CSV couldn't be read ({exc}). Nothing is offered: fix the file and "
                          "upload it again.")
        return out
    if not records:
        out.errors.append("The file is empty.")
        return out
    header = [_column(h) for h in records[0][1]]
    if "email" not in header:
        out.errors.append("The CSV needs an `email` column: it says who each row is.")
        return out
    if not set(header) & set(COLUMNS):
        out.errors.append("The CSV has none of title, department, phone or org_unit, so it changes nothing.")
        return out
    for name in sorted(set(header) - set(HEADER)):
        out.notes.append(f"The `{name}` column {NOT_APPLIED.get(name, 'isn’t one this reads')}, so it isn't applied.")
    if len(records) - 1 > MAX_ROWS:
        out.errors.append(f"The CSV has {len(records) - 1} rows, over the {MAX_ROWS} one file takes. Split it.")
        return out
    seen: Dict[str, int] = {}
    nothing = 0
    for line, raw in records[1:]:
        given = {h: (raw[i] if i < len(raw) else "").strip() for i, h in enumerate(header) if h in HEADER}
        cells = {h: _cell(v) for h, v in given.items()}
        if not any(cells.values()):
            continue
        email = cells.get("email", "")
        if not looks_like_email(email):
            out.errors.append(f"Line {line}: “{email}” isn't an email address." if email else f"Line {line}: no email.")
            continue
        if email.lower() in seen:
            out.errors.append(f"Line {line}: {email} is on line {seen[email.lower()]} too. Keep one.")
            continue
        seen[email.lower()] = line
        values = {c: cells[c] for c in COLUMNS if cells.get(c)}
        # A cell starting "=" is a formula a spreadsheet didn't work out; one this app guarded ("'=") is text, as written.
        bad = next((c for c in values if given[c].startswith("=")), None)
        if bad:
            out.errors.append(f"Line {line}: {email}'s {LABELS[bad].lower()} “{values[bad]}” looks like a spreadsheet formula, "
                              "not a value.")
            continue
        bad = next((c for c, v in values.items() if any(unicodedata.category(ch) == "Cc" for ch in v)), None)
        if bad:
            out.errors.append(f"Line {line}: {email}'s {LABELS[bad].lower()} runs over more than one line.")
            continue
        if not values:
            nothing += 1
            continue
        out.rows.append(Row(line, email, values))
    if nothing:
        out.notes.append(f"{nothing} row{'s' if nothing != 1 else ''} set nothing (every cell blank but the email).")
    return out


def organizations_from(record: Mapping[str, Any]) -> List[Organization]:
    """A person's organizations as Google keeps them, each as GAM would send it back. One GAM couldn't send back as it is
    raises ValueError: read as fewer, a write would delete the rest."""
    found = record.get("organizations", [])
    if not isinstance(found, list) or not all(isinstance(o, dict) for o in found):
        raise ValueError("GAM's answer didn't list their organizations as organizations")
    out = []
    for o in found:
        extra = set(o) - set(ORG_TEXT_FIELDS) - {"type", "customType", "fullTimeEquivalent", "primary"}
        if extra:
            raise ValueError(f"an organization of theirs has {', '.join(sorted(extra))}, which GAM wouldn't send back")
        fte = o.get("fullTimeEquivalent")
        if fte is not None and not isinstance(fte, int):
            raise ValueError("an organization's full-time equivalent isn't a number")
        kind = str(o.get("type") or "")
        if kind and kind not in ORG_TYPES and not (kind == "custom" and o.get("customType")):
            raise ValueError(f"an organization's type “{kind}” isn't one GAM sends back")
        out.append(Organization(fields=tuple((k, str(o[k])) for k in ORG_TEXT_FIELDS if o.get(k)), type=kind,
                                custom_type=str(o.get("customType") or ""), full_time_equivalent=fte,
                                primary=bool(o.get("primary"))))
    return out


def _shown(orgs: Sequence[Organization]) -> Optional[int]:
    """Which organization the directory shows a title from: the primary, else the first (models._primary)."""
    for i, o in enumerate(orgs):
        if o.primary:
            return i
    return 0 if orgs else None


def _with(org: Organization, **values: str) -> Organization:
    fields = dict(org.fields)
    fields.update({k: v for k, v in values.items() if v})
    return replace(org, fields=tuple((k, fields[k]) for k in ORG_TEXT_FIELDS if fields.get(k)))


def held(record: Mapping[str, Any]) -> str:
    """What the run compares, as read: the organizations, the phones and the org unit (a missing list is none)."""
    return json.dumps({"organizations": record.get("organizations") or [], "phones": record.get("phones") or [],
                       "orgUnitPath": record.get("orgUnitPath") or "/"}, sort_keys=True)


@dataclass(frozen=True)
class Change:
    email: str                                      # the primary address
    name: str
    before: Mapping[str, str]                       # only the columns that change
    after: Mapping[str, str]
    organizations: Optional[Tuple[Organization, ...]]   # the whole list to send, or None: not sent
    phones: Optional[Tuple[Phone, ...]]
    org_unit: str                                   # where to move them, or ""
    was: str                                        # ``held`` of the record the plan was made from

    @property
    def argv(self) -> List[List[str]]:
        """Every command this change runs, in order: the profile, then the move."""
        out = []
        if self.organizations is not None or self.phones is not None:
            out.append(GAMCommands.update_profile(self.email, self.organizations, self.phones))
        if self.org_unit:
            out.append(GAMCommands.move_user_to_org_unit(self.org_unit, self.email))
        return out


def plan(rows: Sequence[Row], directory: Sequence[GAMUser], units: Sequence[str]) -> Tuple[List[Change], List[str], int]:
    """Each row's change against the directory as read: ``(changes, refusals, unchanged)``. An unknown person, an alias, an
    org unit that isn't one, or a list GAM couldn't send back refuses that row, and says so."""
    by_email = {u.primary_email.lower(): u for u in directory}
    by_alias = {a.lower(): u for u in directory for a in u.aliases}
    changes: List[Change] = []
    errors: List[str] = []
    unchanged = 0
    for row in rows:
        user = by_email.get(row.email.lower())
        if user is None:
            owner = by_alias.get(row.email.lower())
            errors.append(f"Line {row.line}: {row.email} is an alias of {owner.primary_email}: use that address." if owner
                          else f"Line {row.line}: {row.email} isn't an account here.")
            continue
        who, record = user.primary_email, user.raw
        before: Dict[str, str] = {}
        after: Dict[str, str] = {}
        try:
            orgs: Optional[Tuple[Organization, ...]] = None
            title, department = row.values.get("title", ""), row.values.get("department", "")
            if (title and title != user.title) or (department and department != user.department):
                current = organizations_from(record)
                i = _shown(current)
                if i is None:
                    current = [Organization(primary=True)]
                    i = 0
                current[i] = _with(current[i], title=title, department=department)
                orgs = tuple(current)
                for col, new, old in (("title", title, user.title), ("department", department, user.department)):
                    if new and new != old:
                        before[col], after[col] = old, new
            new_phones: Optional[Tuple[Phone, ...]] = None
            if (typed := row.values.get("phone", "")) and typed != user.phone:
                planned = phones_mod.plan_phone_change(phones_mod.phones_from(record), typed, who)
                new_phones = tuple(planned.phones)
                before["phone"], after["phone"] = user.phone, typed
            unit = row.values.get("org_unit", "")
            if unit and unit not in units:
                raise ValueError(f"“{unit}” isn't an org unit here")
            unit = unit if unit and unit != user.org_unit_path else ""
            if unit:
                before["org_unit"], after["org_unit"] = user.org_unit_path, unit
            change = Change(who, user.full_name, before, after, orgs, new_phones, unit, held(record))
            change.argv   # noqa: B018 - every command built now: one GAM couldn't send refuses the row here, not at the run
        except ValueError as exc:
            errors.append(f"Line {row.line}: {who} isn't changed: {exc}. Change them on their page.")
            continue
        if not after:
            unchanged += 1
            continue
        changes.append(change)
    return changes, errors, unchanged


def before_csv(changes: Sequence[Change]) -> str:
    """The undo: each changed person's values as they were, in the columns that change; every other cell blank."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADER)
    for c in changes:
        writer.writerow([csv_safe(c.email), *(csv_safe(c.before.get(col, "")) for col in COLUMNS)])
    return out.getvalue()


def blanks_before(changes: Sequence[Change]) -> int:
    """How many changed values were empty before: the undo file can't put those back."""
    return sum(1 for c in changes for col in c.after if not c.before.get(col))


async def run(job, conn, changes: Sequence[Change], on_changed=None) -> None:
    """Each change in turn into ``job`` (a polled ``BatchJob``): the person read again, then their profile, then their move.
    One whose organizations, phones or org unit moved since the preview isn't changed: a list sent from the old read would
    undo what changed. Stops at Stop, or at a failure every later person would share (``bulk.stop_reason``)."""
    try:
        for c in changes:
            if stop_requested(job):
                break
            job.current = c.email
            ok, kind, why, detail = await _one(conn, c)
            job.record(c.email, ok, why, detail)
            if ok and on_changed is not None:
                on_changed(c)
            if stop := stop_reason(kind, why, job.total - job.done):
                job.error = stop
                break
    except Exception as exc:  # noqa: BLE001 - the loop itself failed: said, never a run that looks finished
        job.error = exc.remediation if isinstance(exc, GAMError) else "Something went wrong talking to GAM."
    finally:
        job.finish()


async def _one(conn, c: Change) -> Tuple[bool, Any, str, str]:
    """``(ok, kind, why, detail)`` for one person."""
    try:
        now = await conn.profile_now(c.email)
    except Exception as exc:  # noqa: BLE001 - unread: nothing changed for them, whatever it was
        return (False, getattr(exc, "kind", None), f"Couldn't read {c.email} again, so nothing changed for them.",
                str(exc) if isinstance(exc, GAMError) else "")
    if held(now) != c.was:
        return False, None, f"{c.email}'s profile changed since the preview, so nothing changed for them. Preview again.", ""
    done: List[str] = []
    if c.organizations is not None or c.phones is not None:
        res = await conn.update_profile(c.email, c.organizations, c.phones)
        if not res.ok:
            return False, res.kind, res.remediation or "Something went wrong talking to GAM.", res.detail
        done = [LABELS[col].lower() for col in c.after if col != "org_unit"]
    if c.org_unit:
        res = await conn.move_user_to_org_unit(c.email, c.org_unit)
        if not res.ok:
            set_ = f"Their {' and '.join(done)} changed; " if done else ""
            return False, res.kind, f"{set_}the move to {c.org_unit} failed: {res.remediation or 'something went wrong talking to GAM.'}", res.detail
    return True, None, "", ""
