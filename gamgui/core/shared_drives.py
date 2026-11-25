"""Shared drives (roadmap 4.7): a drive's members, and one change to them at a time, previewed.

Pure: what a members read says and what a change would do. The connector reads (``GAMConnector.shared_drives``,
``drive_members``) and writes (``change_drive_member``); ``web/routes/drives.py`` previews a change, holds it under a
single-use token and runs it from its confirm step, reading the members again first (``moved``).

A drive's members are users and groups (Google's sharing guide, the Membership row: "Applies to `user` or `group` only").
Drive's role names (developers.google.com/workspace/drive/api/guides/ref-roles) and the words the Drive screen shows for
them: organizer "Manager", fileOrganizer "Content manager", writer "Contributor", commenter "Commenter", reader "Viewer".

A drive is never left without a manager here: removing or demoting its last one is refused, since a drive with none can
only be looked after from the Admin console. A read that failed or came back without the drive is unknown, never "no
members" (invariant 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .gam.commands import DRIVE_MEMBER_TYPES, DRIVE_ROLES, GAMCommands, is_drive_item_id, is_one_address
from .phishing import Incomplete, is_internal

# Drive's role -> GAM's word for it (DRIVEFILE_ACL_ROLES_MAP), and the label the Drive screen uses.
GAM_WORD = {"organizer": "organizer", "fileOrganizer": "fileorganizer", "writer": "writer", "commenter": "commenter",
            "reader": "reader"}
LABELS = {"organizer": "Manager", "fileOrganizer": "Content manager", "writer": "Contributor", "commenter": "Commenter",
          "reader": "Viewer"}
ROLE_CHOICES = tuple((word, LABELS[drive]) for drive, word in GAM_WORD.items())   # (GAM word, label), most access first

ADD, ROLE, REMOVE = "add", "role", "remove"
KIND_LABELS = {"user": "Person", "group": "Group"}


class Unread(Incomplete):
    """A members read that didn't answer: unknown, never "no members"."""


def label(role: str) -> str:
    """A role as the Drive screen names it; a role this app doesn't offer, as Google gave it."""
    return LABELS.get(role, role or "unknown role")


@dataclass(frozen=True)
class SharedDrive:
    id: str
    name: str

    @classmethod
    def from_record(cls, r: Dict[str, Any]) -> Optional["SharedDrive"]:
        drive_id = str(r.get("id") or "").strip()
        return cls(drive_id, str(r.get("name") or "").strip()) if is_drive_item_id(drive_id) else None


@dataclass(frozen=True)
class Member:
    id: str          # the permission's ID: what a change names
    kind: str        # user, group (a drive's members are only these), or what Google said
    role: str        # Drive's name for it: organizer, fileOrganizer, writer, commenter, reader
    email: str
    name: str = ""
    deleted: bool = False   # the account behind it is gone (Drive keeps the permission)

    @classmethod
    def from_permission(cls, p: Dict[str, Any]) -> Optional["Member"]:
        pid = str(p.get("id") or "").strip()
        if not is_drive_item_id(pid):
            return None
        return cls(pid, str(p.get("type") or ""), str(p.get("role") or ""), str(p.get("emailAddress") or "").strip(),
                   str(p.get("displayName") or "").strip(), p.get("deleted") is True)

    @property
    def who(self) -> str:
        return self.email or self.name or f"permission {self.id}"

    @property
    def manages(self) -> bool:
        """A manager who counts: an organizer whose account is still there."""
        return self.role == "organizer" and not self.deleted


def members_from(records: Sequence[Dict[str, Any]], drive_id: str) -> List[Member]:
    """The members of ``drive_id`` from ``print drivefileacls shareddriveid <id> formatjson``: exactly one row, for that drive,
    holding its permissions (a list, empty for none). No row is GAM passing the drive over without a word (a token Google
    refused), and a permission without an ID couldn't be named by a change: either way the read is unknown (``Unread``)."""
    rows = [r for r in records if str(r.get("id") or "") == drive_id]
    if len(rows) != 1 or len(records) != 1:
        raise Unread("GAM answered without the drive's members, so who has access can't be told.")
    permissions = rows[0].get("permissions")
    if not isinstance(permissions, list):
        raise Unread("GAM's answer left the drive's members out, so who has access can't be told.")
    members = [Member.from_permission(p) if isinstance(p, dict) else None for p in permissions]
    if any(m is None for m in members):
        raise Unread("GAM listed a member of the drive without its permission ID, so it couldn't be named.")
    order = {r: i for i, r in enumerate(GAM_WORD)}
    return sorted((m for m in members if m is not None), key=lambda m: (order.get(m.role, 9), m.who.lower()))


@dataclass(frozen=True)
class Change:
    """One change to one drive's members, as its preview showed it: the confirm step runs exactly this."""

    drive_id: str
    drive_name: str
    action: str          # ADD, ROLE, REMOVE
    who: str             # the member's address (or name), as shown
    role: str = ""       # the role it gets, as GAM's word (ADD, ROLE)
    kind: str = ""       # ADD: user or group
    member_id: str = ""  # ROLE, REMOVE: the permission's ID
    was: str = ""        # ROLE, REMOVE: the member's role as read (Drive's name)

    @property
    def argv(self) -> List[str]:
        if self.action == ADD:
            return GAMCommands.add_drive_member(self.drive_id, self.kind, self.who, self.role)
        if self.action == ROLE:
            return GAMCommands.set_drive_member_role(self.drive_id, self.member_id, self.role)
        if self.action == REMOVE:
            return GAMCommands.remove_drive_member(self.drive_id, self.member_id)
        raise ValueError(f"unknown change {self.action!r}")

    @property
    def new_label(self) -> str:
        return LABELS[_drive_role(self.role)] if self.role else ""

    @property
    def words(self) -> str:
        """What the change does, in a sentence's middle: the confirm step's question and the result's message."""
        drive = self.drive_name or self.drive_id
        if self.action == ADD:
            return f"add {self.who} to {drive} as {self.new_label}"
        if self.action == ROLE:
            return f"change {self.who} on {drive} from {label(self.was)} to {self.new_label}"
        return f"remove {self.who} ({label(self.was)}) from {drive}"

    @property
    def question(self) -> str:
        return self.words[:1].upper() + self.words[1:] + "?"

    @property
    def done(self) -> str:
        """The change once made, for the panel's message."""
        drive = self.drive_name or self.drive_id
        if self.action == ADD:
            return f"Added {self.who} to {drive} as {self.new_label}."
        if self.action == ROLE:
            return f"{self.who} is now {self.new_label} on {drive}."
        return f"Removed {self.who} from {drive}."


def _drive_role(word: str) -> str:
    return next(drive for drive, w in GAM_WORD.items() if w == word)


def _last_manager(member: Member, members: Sequence[Member]) -> bool:
    return member.manages and sum(1 for m in members if m.manages) == 1


LAST_MANAGER = ("{who} is {drive}'s only manager. A shared drive with none can only be looked after from the Admin console, "
                "so add another manager first.")


def plan(members: Sequence[Member], drive: SharedDrive, action: str, *, email: str = "", kind: str = "", role: str = "",
         member_id: str = "") -> Tuple[Optional[Change], str]:
    """``(change, "")`` for a change the members just read allow, else ``(None, why not)`` in words."""
    name = drive.name or drive.id
    if action == ADD:
        email = email.strip()
        if not is_one_address(email):
            return None, "Enter one full email address: a bare name or a list isn't one member."
        if kind not in DRIVE_MEMBER_TYPES:
            return None, "Pick whether it's a person or a group."
        if role not in DRIVE_ROLES:
            return None, "Pick a role."
        there = next((m for m in members if m.email.lower() == email.lower()), None)
        if there is not None:
            return None, f"{there.who} is already on {name} as {label(there.role)}: change their role instead."
        return Change(drive.id, drive.name, ADD, email, role=role, kind=kind), ""
    member = next((m for m in members if m.id == member_id), None)
    if member is None:
        return None, f"That member isn't on {name} any more. The list below is as it is now."
    if action == ROLE:
        if role not in DRIVE_ROLES:
            return None, "Pick a role."
        if _drive_role(role) == member.role:
            return None, f"{member.who} is already {label(member.role)} on {name}."
        if _last_manager(member, members):
            return None, LAST_MANAGER.format(who=member.who, drive=name)
        return Change(drive.id, drive.name, ROLE, member.who, role=role, member_id=member.id, was=member.role), ""
    if action == REMOVE:
        if _last_manager(member, members):
            return None, LAST_MANAGER.format(who=member.who, drive=name)
        return Change(drive.id, drive.name, REMOVE, member.who, member_id=member.id, was=member.role), ""
    return None, "Pick what to change."


def moved(change: Change, members: Sequence[Member]) -> str:
    """Why ``change`` no longer fits the members read again at the click, or "" when it does: a preview is only run against
    the drive it was made from."""
    name = change.drive_name or change.drive_id
    if change.action == ADD:
        there = next((m for m in members if m.email.lower() == change.who.lower()), None)
        return f"{change.who} was added to {name} since the preview." if there else ""
    member = next((m for m in members if m.id == change.member_id), None)
    if member is None:
        return f"{change.who} isn't on {name} any more."
    if member.role != change.was:
        return f"{change.who}'s role on {name} changed since the preview, to {label(member.role)}."
    if _last_manager(member, members):   # still a manager (role as read), and now the only one: a demotion or removal
        return LAST_MANAGER.format(who=change.who, drive=name)
    return ""


# --- the orphan check (roadmap 4.7): drives a leaver is the only active manager of ------------------------------------------
@dataclass(frozen=True)
class AtRisk:
    drive: SharedDrive
    why: str   # who else manages it, and why they don't count, in words


@dataclass(frozen=True)
class Alone:
    """What the orphan check found: the drives ``email`` manages with no other active manager, and the drives whose members
    weren't read (GAM passes over a drive Google refuses without a word), for which it can't be told."""

    at_risk: Tuple[AtRisk, ...]
    unread: Tuple[SharedDrive, ...]


def managed_alone(email: str, drives: Sequence[SharedDrive], acl_rows: Sequence[Dict[str, Any]],
                  users: Sequence[Any]) -> Alone:
    """The drives ``email`` manages where no one else does who can: from every drive's permissions (``print shareddriveacls
    oneitemperrow shownopermissionsdrives true formatjson``, one row a permission) and the directory, suspended accounts
    included. Another manager counts when it is a group (its members can't be told from here, as the Shared drive
    permissions audit run reads it) or an account the directory lists and hasn't suspended; one whose account is gone, isn't
    in the directory or is suspended doesn't. A drive the list holds but the permissions read left out is unread: unknown,
    never "not theirs"."""
    me = email.strip().lower()
    status = {str(u.primary_email).strip().lower(): u for u in users}
    by_drive: Dict[str, List[Dict[str, Any]]] = {}
    for r in acl_rows:
        perms = by_drive.setdefault(str(r.get("id") or ""), [])
        if isinstance(r.get("permission"), dict):
            perms.append(r["permission"])
    at_risk = []
    for drive in drives:
        organizers = [p for p in by_drive.get(drive.id, []) if p.get("role") == "organizer" and p.get("deleted") is not True]
        if not any(str(p.get("emailAddress") or "").strip().lower() == me for p in organizers):
            continue
        whys, counted = [], False
        for p in organizers:
            address = str(p.get("emailAddress") or "").strip().lower()
            if address == me:
                continue
            holder = status.get(address)
            if p.get("type") == "group" or (holder is not None and not getattr(holder, "suspended", False)):
                counted = True
                break
            whys.append(f"{address or 'an account'}: {'suspended' if holder is not None else 'not in the directory'}")
        if not counted:
            at_risk.append(AtRisk(drive, "; ".join(whys) or "no other manager"))
    unread = tuple(d for d in drives if d.id not in by_drive)
    return Alone(tuple(sorted(at_risk, key=lambda a: a.drive.name.lower())), unread)


# --- a folder of mixed ownership into a shared drive (roadmap 4.8) ----------------------------------------------------------
FOLDER = "application/vnd.google-apps.folder"
NAMES_SHOWN = 5   # the files owned outside the domain a preview names; the rest are counted


@dataclass(frozen=True)
class FolderCount:
    """What a folder holds, by who owns each file: what its move takes into the drive, what Google may refuse to move (a file
    someone else in the domain owns), and what must be copied (a file owned outside the domain, which Google never moves into
    a shared drive). Folders are made again in the drive, so they are counted apart."""

    mine: int
    domain: int
    outside: int
    folders: int
    outside_names: Tuple[str, ...]

    @property
    def files(self) -> int:
        return self.mine + self.domain + self.outside


def count_folder(records: Sequence[Dict[str, Any]], email: str, domains: Sequence[str], folder_id: str) -> FolderCount:
    """Count ``print filelist select id:<folder> showparent … excludetrashed``, read as the folder's owner. The folder's own row
    must be there: without it GAM didn't read the folder (it may not exist, or the person can't see it), never "empty". A
    file without an owner can't be placed (one in a shared drive has none), so the count is Incomplete rather than a guess,
    and so is a row that isn't an item (invariant 10)."""
    me = email.strip().lower()
    mine = domain = outside = folders = 0
    names: List[str] = []
    if not any(r.get("id") == folder_id for r in records):
        raise Incomplete(f"GAM didn't list folder {folder_id} itself: it may not exist, or {email} can't see it.")
    for r in records:
        if not r.get("id") or not r.get("mimeType"):
            raise Incomplete("GAM listed something in the folder without its ID or kind, so what would move can't be told.")
        if r["id"] == folder_id and r["mimeType"] == FOLDER:
            continue   # the folder itself (showparent); a file ID names one file, which is counted
        if r["mimeType"] == FOLDER:
            folders += 1
            continue
        owners = r.get("owners")
        owner = str(owners[0].get("emailAddress") or "").strip().lower() \
            if isinstance(owners, list) and owners and isinstance(owners[0], dict) else ""
        if not owner:
            raise Incomplete(f"GAM listed {r.get('name') or r['id']} without its owner (a file already in a shared drive has "
                             "none), so what would move can't be told.")
        if owner == me:
            mine += 1
        elif is_internal(owner, domains) or any(owner.endswith("." + d.lower()) for d in domains):   # or a subdomain
            domain += 1
        else:
            outside += 1
            if len(names) < NAMES_SHOWN:
                names.append(str(r.get("name") or r["id"]))
    return FolderCount(mine, domain, outside, folders, tuple(names))


def folder_move_of(argv: Sequence[str]) -> Optional[Tuple[str, str, str]]:
    """(person, folder ID, drive ID) of a move or copy ``GAMCommands`` built for roadmap 4.8, else None: what the Builder's
    preview counts and checks. Read off the argv the preview holds, so the count is of what will run."""
    if (len(argv) >= 7 and argv[0] == "user" and argv[2] in ("move", "copy") and argv[3] == "drivefile"
            and argv[4].startswith("id:") and argv[5] == "teamdriveparentid"):
        return argv[1], argv[4][3:], argv[6]
    return None


def manages_directly(email: str, members: Sequence[Member]) -> bool:
    """Whether ``email`` is a Manager of the drive in their own name: GAM's verifyorganizer asks Drive for the person's own
    permission, so a Manager only through a group is refused."""
    me = email.strip().lower()
    return any(m.kind == "user" and m.email.strip().lower() == me and m.manages for m in members)
