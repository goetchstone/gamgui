"""GAM7 command builders.

Every ``gam`` invocation in the app is constructed here and nowhere else. Two reasons:

1. **Safety** — builders return an ``argv`` *list* (e.g. ``["print", "users", ...]``), which is
   passed straight to ``exec`` with no shell. User-supplied values (emails, signatures) are never
   interpolated into a shell string, so there is no shell-injection surface.
2. **Maintainability** — GAM occasionally tweaks sub-command syntax between releases. Keeping it in
   one file with arg-shape tests means a version bump is a single-file change.

NOTE: the exact sub-syntax of a few mutating commands (group membership, signature flags) should be
re-verified against the pinned GAM version during the real-tenant acceptance pass. The arg-shape
unit tests pin today's intended form so drift is caught early.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

# The GAM7 version GamGUI is pinned to and tested against — the SINGLE SOURCE OF TRUTH.
# `scripts/fetch_gam.sh` (TAG), the mock, and the version tests must all match this; that's enforced by
# tests/test_command_contract.py so they can't drift. Bump deliberately via the "Updating GAM" runbook.
# Compared (as a substring) against the running `gam version` for the fail-soft runtime self-check.
EXPECTED_GAM_VERSION = "7.50.00"

# Roles accepted by Google Directory for group membership.
GROUP_ROLES = ("member", "manager", "owner")
# `<CalendarACLRole>` in the vendored grammar, verbatim (tests/test_command_contract.py pins it).
CALENDAR_ACL_ROLES = ("editor", "freebusy", "freebusyreader", "owner", "reader", "writer",
                      "writerwithoutprivateaccess", "none")

# `gam print users` returns ONLY primaryEmail unless fields are requested — these populate the list.
# `organizations` carries the job title (the practical "role" for automations).
USER_LIST_FIELDS = ("primaryEmail", "name", "suspended", "orgUnitPath", "organizations")
# Fields for the detail view: identity + role/automation signals + security flags.
USER_DETAIL_FIELDS = (
    "primaryEmail", "name", "suspended", "orgUnitPath", "isAdmin", "isDelegatedAdmin",
    "isEnrolledIn2Sv", "lastLoginTime", "aliases", "organizations", "locations", "phones", "recoveryEmail",
)
# `gam print groups` likewise returns only email unless fields are requested.
GROUP_LIST_FIELDS = ("email", "name", "description", "directMembersCount")
# Tidy column sets for the Builder "Find Chromebooks" / "Find Drive files" searches.
CROS_LIST_FIELDS = ("deviceId", "serialNumber", "status", "orgUnitPath", "annotatedAssetId",
                    "annotatedUser", "lastSync", "model")
FILE_LIST_FIELDS = ("id", "name", "mimeType", "owners", "modifiedTime", "webViewLink")
# Superset fetched once and cached to serve the users list (needs title), reports, AND the detail
# view (so opening a user is instant + uses the reliable JSON path, not the `info user` text format).
CACHE_FIELDS = (
    "primaryEmail", "name", "suspended", "orgUnitPath", "organizations",
    "isAdmin", "isDelegatedAdmin", "isEnrolledIn2Sv", "lastLoginTime", "recoveryEmail",
    "aliases", "locations", "phones",
)


def cut_off_account(argv: Sequence[str]) -> Optional[str]:
    """The account ``argv`` cuts off from Google, as sent, or None for any other command: deleted,
    suspended, its OAuth tokens deleted (``deprovision``, which took GamGUI's own token, failure log
    2026-09-24), its password changed or its sessions ended. Whether GAM's token outlives the last two
    isn't proven, so they count. By shape rather than by builder, and in any case, as GAM reads its words, so
    a variant fails closed; ``all users`` comes back as that, which no account reads as. Run on the admin
    GamGUI signs in as, any of them signs the app out: ``GAMConnector.cut_off_refusal`` refuses it."""
    original = list(argv)
    words = [w.lower().replace("_", "") for w in original]
    entity = ("user", "users")
    if len(words) >= 3 and words[0] == "delete" and words[1] in entity:
        return original[2]
    if len(words) >= 3 and words[0] in entity and words[2] in ("signout", "deprovision", "deprov"):
        return original[1]
    if len(words) >= 3 and words[:2] == ["all", "users"] and words[2] in ("signout", "deprovision", "deprov"):
        return "all users"
    if len(words) >= 4 and words[0] == "update" and words[1] in entity:
        attributes = words[3:]
        for i, word in enumerate(attributes):
            if word == "password":
                return original[2]
            if word in ("suspended", "suspend") and attributes[i + 1:i + 2] not in (["off"], ["false"]):
                return original[2]
    return None


# What a person's Chrome profile, phone and computer say. SwiftGamGui's chromeProfileFields, mobileFields, deviceFields.
CHROME_PROFILE_FIELDS = ("displayname,useremail,lastactivitytime,firstenrollmenttime,osplatformtype,osversion,"
                         "browserversion,browserchannel,deviceinfo,affiliationstate")
MOBILE_FIELDS = "resourceid,email,model,os,type,status,firstsync,lastsync,devicecompromisedstatus"
DEVICE_FIELDS = ("devicetype,model,manufacturer,osversion,hostname,serialnumber,ownertype,managementstate,"
                 "compromisedstate,createtime,lastsynctime")
SCRIPT_FIELDS = "id,name,createdtime,modifiedtime,trashed,lastmodifyinguser"
DEVICE_USER_FIELDS = "name,useremail,createtime,firstsynctime,lastsynctime,managementstate"

# GAM's phone types (`phone [type assistant|…|work_pager|(custom <String>)]`). One outside them GAM keeps as a custom type
# of that name, so a phone goes back only with one of these, `custom` with its name, or no type.
PHONE_TYPES = frozenset({"assistant", "callback", "car", "company_main", "grand_central", "home", "home_fax", "isdn", "main",
                         "mobile", "other", "other_fax", "pager", "radio", "telex", "tty_tdd", "work", "work_fax",
                         "work_mobile", "work_pager"})


@dataclass(frozen=True)
class Phone:
    """One of a person's phones as Google keeps it: GAM's word for its type (or `custom` with ``custom_type``)."""

    type: str = ""
    custom_type: str = ""
    value: str = ""
    primary: bool = False


# RFC 5322's atext, less Gmail's { and }, and *.
_MESSAGE_ID_ATEXT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'+-/=?^_`|~")


def is_report_time(when: str) -> bool:
    """A time GAM reads as one, in the one form the app writes: `yyyy-mm-ddThh:mm:ssZ`, UTC to the second."""
    try:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", when)) and bool(datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return False


def is_device_user_name(name: str) -> bool:
    """A device user's name as Google gives it, held to Google's ID characters: never `query:`, `id`, a list, the API's `-` wildcard (`devices/-/deviceUsers/-` is every one) or anything GAM reads as more
    than the one name."""
    parts = (name or "").split("/")
    return (len(parts) == 4 and parts[0] == "devices" and parts[2] == "deviceUsers"
            and all(1 <= len(part) <= 256 and part != "-" and all(c.isascii() and (c.isalnum() or c in "-_") for c in part) for part in (parts[1], parts[3])))


def is_one_address(address: str) -> bool:
    """One plain address, as GAM will act on it as given: GAM reads `Name <a@b>` as `a@b` and `id:`/`uid:` as an ID, and splits a list on
    commas and whitespace. A removal names what the operator was shown, or nothing."""
    local, at, host = (address or "").partition("@")
    return bool(at and local and host) and not any(c.isspace() or c in "<>:,@" for c in local + host)


@dataclass(frozen=True)
class MessageId:
    """An email's Message-ID, without its angle brackets: Gmail's `rfc822msgid:` takes it bare. RFC 5322 `id-left@id-right`,
    each side `dot-atom-text`, so it holds no space, quote, parenthesis, colon, comma or backslash, and can't end the Gmail term
    or start another operator. Checked however it's made, so a builder never sends what ``parse`` would refuse. SwiftGamGui's
    MessageID."""

    id: str

    def __post_init__(self) -> None:
        value = self.id
        if not value:
            raise ValueError("Paste the email's Message-ID, from its headers (in Gmail, More › Show original).")
        if len(value) > 998:
            raise ValueError("That's longer than any Message-ID can be (998 characters).")
        if any(c.isspace() for c in value):
            raise ValueError("A Message-ID has no spaces: paste only its value, like <…@mail.example.com>.")
        if any(c in value for c in "{}|"):
            raise ValueError("This Message-ID has {, } or |, which Gmail's search may read as its own syntax, so it can't be searched for.")
        for c in value:
            if c not in "@." and c not in _MESSAGE_ID_ATEXT:
                shown = f"“{c}”" if 0x21 <= ord(c) <= 0x7E else f"U+{ord(c):04X}"
                raise ValueError(f"A Message-ID can't contain {shown}: paste it exactly as the header has it.")
        sides = value.split("@")
        if len(sides) != 2:
            raise ValueError("A Message-ID has exactly one @, like <…@mail.example.com>.")
        if not all(word for side in sides for word in side.split(".")):
            raise ValueError("Each side of a Message-ID's @ is words joined by single dots, with none at either end.")

    @classmethod
    def parse(cls, text: str) -> "MessageId":
        """What an operator copies: the bare ID, `<id>`, the whole header line, or `rfc822msgid:id`. Anything else is refused
        with what's wrong, never repaired."""
        value = (text or "").strip()
        for label in ("message-id:", "rfc822msgid:"):
            if value[:len(label)].lower() == label:
                value = value[len(label):].strip()
        if len(value) > 1 and value[0] == "<" and value[-1] == ">":
            value = value[1:-1]
        return cls(value)

    @property
    def query(self) -> str:
        """Every copy but the sender's own: `-in:sent` keeps the Sent copy in the account that sent it, as evidence."""
        return f"rfc822msgid:{self.id} -in:sent"

    @property
    def any_copy_query(self) -> str:
        """Every copy in one mailbox, Sent included: the sender's own is the only one that keeps the Bcc header."""
        return f"rfc822msgid:{self.id}"

    @property
    def spam_query(self) -> str:
        """The copies Gmail filed to Spam, which `query` can't reach through `trash messages`: it lists without Spam."""
        return f"rfc822msgid:{self.id} in:spam"


_CALENDAR_ID = re.compile(r"[A-Za-z0-9._+\-]+@[A-Za-z0-9._+\-]*\.calendar\.google\.com", re.I)
_FILTER_ID = re.compile(r"[A-Za-z0-9_\-]+")
_LIST_SELECTORS = frozenset({"csvkmd", "csvsubkey", "csv", "csvfile", "file", "csvdata"})   # GAM's ENTITY_LIST_SELECTORS


def id_list(ids: Sequence[str]) -> Optional[str]:
    """``ids`` as one entity-list element GAM splits on commas, or None: each ID ASCII letters, digits, `-` and `_`, and the
    element never a list selector GAM reads a file from instead."""
    joined = ",".join(ids)
    if not ids or not all(_FILTER_ID.fullmatch(i) for i in ids) or joined.lower().replace("_", "").replace("-", "") in _LIST_SELECTORS:
        return None
    return joined


_FILTER_LOCAL = re.compile(r"[a-z0-9._'-]+")
_FILTER_LABEL = re.compile(r"[a-z0-9-]+")


def filter_address(address: str) -> str:
    """``address`` lowercased, for a Google search or filter value, or ValueError for one outside Google's address
    characters: it goes inside a query, so it must stay one ``field:value``. SwiftGamGui's filterAddress."""
    lowered = address.lower()
    local, at, domain = lowered.partition("@")
    labels = domain.split(".")
    if not at or not _FILTER_LOCAL.fullmatch(local) or len(labels) < 2 or not all(_FILTER_LABEL.fullmatch(p) for p in labels):
        raise ValueError(f"“{address}” isn't one email address")
    return lowered


class GAMCommands:
    # GAM's REPORT_CHOICE_MAP keys, as `report` takes them. Not `gplus` or `jamboard`, whose services have ended.
    REPORT_APPLICATIONS = ("login", "token", "saml", "gmail", "accessevaluation", "chat", "useraccounts", "takeout", "mobile", "chrome", "drive", "calendar",
                           "groups", "admin", "rules", "contextawareaccess", "vault", "geminiinworkspaceapps", "meet", "contacts", "keep", "tasks",
                           "admindataaction", "accesstransparency", "datastudio", "groupsenterprise", "profile", "ldap", "directorysync", "gcp",
                           "cloudsearch", "classroom", "datamigration", "meethardware", "assignments", "graduation")

    # --- diagnostics / setup ----------------------------------------------------------
    @staticmethod
    def version() -> List[str]:
        return ["version"]

    @staticmethod
    def check_svcacct(admin: str, scopes: Sequence[str]) -> List[str]:
        # Verifies domain-wide delegation for exactly `scopes` — grammar:
        # `gam <UserTypeEntity> check serviceaccount (scope|scopes <APIScopeURLList>)*`, the list one
        # comma-joined element. Without it GAM checks its own, larger default scope set and refuses an
        # operator who authorized only the scopes the app asked for. Each must be a service-account
        # scope (GAM exits "Invalid choice" otherwise). NOTE: the noun is `serviceaccount` here (GAM
        # uses `create svcacct` but `check serviceaccount` — not symmetric).
        if not scopes:
            raise ValueError("check_svcacct needs the scopes to check")
        return ["user", admin, "check", "serviceaccount", "scopes", ",".join(scopes)]

    # --- users (read) -----------------------------------------------------------------
    @staticmethod
    def print_users(query: Optional[str] = None, fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["print", "users"]
        if query:
            argv += ["query", query]
        argv += ["fields", ",".join(fields or USER_LIST_FIELDS)]
        argv.append("formatjson")
        return argv

    @staticmethod
    def print_cros(query: str = "", fields: Optional[Sequence[str]] = None) -> List[str]:
        """Search the ChromeOS device fleet by a CrOS query (read-only). `query` rides as one argv
        element. Emits CSV via formatjson for the result table."""
        argv = ["print", "cros"]
        if query:
            argv += ["query", query]
        argv += ["fields", ",".join(fields or CROS_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def print_filelist(email: str, query: str = "", fields: Optional[Sequence[str]] = None) -> List[str]:
        """Search a user's Drive files by a Drive v3 query (read-only). `query` rides as one argv
        element (never shell-spliced)."""
        argv = ["user", email, "print", "filelist"]
        if query:
            argv += ["query", query]
        argv += ["fields", ",".join(fields or FILE_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def report_users(date: str, params: Sequence[str]) -> List[str]:
        # Admin SDK usage report (storage, mail, drive). Data lags ~2-3 days.
        return ["report", "users", "date", date, "parameters", ",".join(params)]

    @staticmethod
    def clear_recovery(email: str) -> List[str]:
        # Both recovery fields emptied, so "Forgot password?" can't reach the leaver's own phone or address. GAM sends an
        # empty value as a cleared field. Shown and audited masked. SwiftGamGui's clearRecovery.
        return ["update", "user", email, "recoveryemail", "", "recoveryphone", ""]

    @staticmethod
    def hide_from_directory(email: str) -> List[str]:
        # Out of the Global Address List and autocomplete. Always the explicit `off`: GAM reads a bare `gal` as on.
        return ["update", "user", email, "gal", "off"]

    @staticmethod
    def imap_off(email: str) -> List[str]:
        # GAM sends its defaults with it (auto-expunge, archive, no folder limit), which matter only while IMAP is on.
        return ["user", email, "imap", "off"]

    @staticmethod
    def pop_off(email: str) -> List[str]:
        # GAM sends `leaveInInbox` with it, so no mail is moved or deleted.
        return ["user", email, "pop", "off"]

    @staticmethod
    def print_owned_calendars(email: str) -> List[str]:
        # The secondary calendars `email` owns, hidden ones too: GAM filters the person's own list, which leaves hidden
        # entries out. The grammar has `ownedsecondary` and `showhidden` as alternatives, but GAM's source reads both.
        # CSV `primaryEmail,calendarId,JSON`. SwiftGamGui's printOwnedSecondaryCalendars.
        return ["user", email, "print", "calendars", "ownedsecondary", "showhidden", "fields", "id,summary", "formatjson"]

    @staticmethod
    def transfer_calendar_ownership(calendar_ids: Sequence[str], new_owner: str) -> List[str]:
        # Each calendar's owner made `new_owner`, in one command: GAM goes through the list and a failure leaves the rest
        # done. `transfer ownership`: the grammar's bare `transfer` lacks a word GAM's source requires. The list is one
        # element GAM splits on commas and whitespace, so each ID is checked. Any other ID GAM takes as a user's calendar.
        if not calendar_ids or not all(_CALENDAR_ID.fullmatch(c) for c in calendar_ids):
            raise ValueError("each calendar to hand over is one calendar ID")
        return ["calendars", ",".join(calendar_ids), "transfer", "ownership", new_owner]

    @staticmethod
    def print_filters(email: str) -> List[str]:
        # The person's Gmail filters, each one's JSON with its `action.forward`. `labelidsonly` skips a label-name lookup.
        return ["user", email, "print", "filters", "labelidsonly", "formatjson"]

    @staticmethod
    def delete_filters(email: str, filter_ids: Sequence[str]) -> List[str]:
        # The filters deleted in one command: GAM goes through the list, a failure leaving the rest done. The list is one
        # element split on commas, and must not be a list selector GAM reads a file from instead.
        joined = id_list(filter_ids)
        if joined is None:
            raise ValueError("each filter to delete is one filter ID")
        return ["user", email, "delete", "filters", joined]

    @staticmethod
    def find_message_copies(message: MessageId) -> List[str]:
        # Who has a copy of one email: GAM's own dry run of the trash below, over every active user. Without `doit` it lists
        # each user's matches and prints a count per user on stderr, returning before any trash call. The exit code is the
        # last user's, so never 0 across a domain: read its lines. `notquick`, so a count is every match. The one reviewed read
        # with a destructive verb (tests/test_phishing.py): only the absent `doit` makes it a read. SwiftGamGui's findMessageCopies.
        return ["all", "users", "trash", "messages", "query", message.query, "notquick", "max_to_trash", "1"]

    @staticmethod
    def trash_message(email: str, message: MessageId, copies: int) -> List[str]:
        # One mailbox's copies to its Trash (Gmail empties it after 30 days). At most `copies`, the count the dry run showed, so
        # a run never trashes more than its preview did. GAM reads `max_to_trash 0` as no limit, so below 1 is refused.
        if copies < 1:
            raise ValueError("a mailbox to trash from holds at least one copy")
        return ["user", email, "trash", "messages", "query", message.query, "notquick", "max_to_trash", str(copies), "doit"]

    @staticmethod
    def find_spam_copies(message: MessageId) -> List[str]:
        # Who has a copy in Spam, by Gmail's ID, which the dry run never lists. Only the Message-ID header (GAM's default set has
        # To, hundreds of addresses a mailbox for a mass phish) and the labels. SwiftGamGui's findSpamCopies.
        return ["all", "users", "print", "messages", "query", message.spam_query, "includespamtrash", "headers", "message-id", "showlabels"]

    @staticmethod
    def trash_messages(email: str, ids: Sequence[str]) -> List[str]:
        # One mailbox's copies in Spam to its Trash, by the IDs the read found: nothing is listed, so Spam is no bar, and no `doit`
        # is needed. Never more than the preview showed; one gone since is GAM's "Does not exist", the rest trashed.
        joined = id_list(ids)
        if joined is None:
            raise ValueError("each message to trash is one message ID")
        return ["user", email, "trash", "messages", "ids", joined]

    @staticmethod
    def print_vault_holds(email: str) -> List[str]:
        # The open Vault matters' holds that cover `email`, directly or through an org unit above them. CSV only:
        # `User,matterId,matterName,holdId,name,orgUnitId,orgUnitPath`.
        return ["user", email, "print", "vaultholds"]

    @staticmethod
    def update_phones(email: str, phones: Sequence[Phone]) -> List[str]:
        # The person's phones, every one: GAM sends them as the list Google keeps in place of the old one, so a phone left
        # out is deleted, and none left is `phone clear`. A phone GAM wouldn't send back as it is raises ValueError: two
        # primaries, a type it would keep as custom, a custom type it would trim, or a number over lines. SwiftGamGui's
        # updatePhones.
        if sum(p.primary for p in phones) > 1:
            raise ValueError("only one phone can be the primary")
        argv = ["update", "user", email]
        for phone in phones:
            if not phone.value or any(unicodedata.category(c) == "Cc" for c in phone.value):
                raise ValueError("each phone is a number on one line")
            kind = phone.type.lower()
            if kind != "custom" and phone.custom_type:
                raise ValueError(f"a phone of type “{phone.type}” with its own name would lose the name")
            typed: List[str] = []
            if kind == "custom":
                if not phone.custom_type.strip():
                    raise ValueError("a custom phone type has a name")
                if phone.custom_type.strip() != phone.custom_type:
                    raise ValueError(f"the custom phone type “{phone.custom_type}” would come back without its spaces")
                typed = ["type", "custom", phone.custom_type]
            elif kind:
                if kind not in PHONE_TYPES:
                    raise ValueError(f"“{phone.type}” isn't a phone type GAM keeps as it is")
                typed = ["type", kind]
            argv += ["phone", *typed, "value", phone.value, "primary" if phone.primary else "notprimary"]
        return argv if phones else [*argv, "phone", "clear"]

    @staticmethod
    def set_password(email: str, password: str) -> List[str]:
        # A password the operator hands over, which Google makes the person change at their next sign-in. reset_password is
        # `random` with `changepassword off`, which nobody sees. `password` comes from onboarding.generate_temp_password,
        # so it's never one of GAM's words there (random, uniquerandom, blocklogin, prompt). SwiftGamGui's setPassword.
        return ["update", "user", email, "password", password, "changepassword", "on"]

    @staticmethod
    def print_chrome_profiles(user: str) -> List[str]:
        # The Chrome profiles signed in with `user`'s account, by the Chrome Management API's own field name, the address
        # quoted and lowercased as Google keeps it. CSV `name,profileId,JSON`. Exit 15 with no Chrome Management scope, 50
        # with the API's refusal. SwiftGamGui's printChromeProfiles.
        return ["print", "chromeprofiles", "filter", f'user_email="{filter_address(user)}"', "fields", CHROME_PROFILE_FIELDS,
                "formatjson"]

    @staticmethod
    def print_mobile_devices(user: str) -> List[str]:
        # The phones and tablets syncing `user`'s account. `email:` matches a prefix, so the reader keeps exact owners.
        # `listlimit 0`: GAM cuts every list in the JSON to one by default, so a shared device listed only its first owner,
        # and the reader then said the second had no phone. CSV `resourceId,JSON`. A refusal is exit 12.
        return ["print", "mobile", "query", f"email:{filter_address(user)}", "fields", MOBILE_FIELDS, "listlimit", "0",
                "formatjson"]

    @staticmethod
    def print_devices(user: str) -> List[str]:
        # Every device signed in with `user`'s account that Google's endpoint management knows, computers among them, each
        # with its users. Reads as the admin through the service account, so delegation needs the devices scope: without
        # it exit 73, and a scope list without the API exit 12. CSV `name,JSON`. SwiftGamGui's printDevices.
        return ["print", "devices", "query", f"email:{filter_address(user)}", "fields", DEVICE_FIELDS,
                "userfields", DEVICE_USER_FIELDS, "formatjson"]

    @staticmethod
    def print_script_projects(user: str) -> List[str]:
        # The person's own standalone Apps Script projects: Drive files of that type, the bin's included. A script someone authorized keeps acting for
        # them on its triggers. GAM has no Apps Script commands, so nothing lists a project's triggers. printFileList exits 60 whenever it printed no
        # row, overwriting a failure's own code, so "none" is read only on GAM's word (person_reads.script_projects_from). SwiftGamGui's printScriptProjects.
        if not is_one_address(user):
            raise ValueError(f"“{user}” isn't one email address")
        return ["user", user, "print", "filelist", "showmimetype", "gscript", "fields", SCRIPT_FIELDS, "formatjson"]

    @staticmethod
    def print_user_tokens(user: str) -> List[str]:
        # One account's OAuth grants. CSV `user,clientId,displayText,anonymous,nativeApp,userKey,scopes`; not a user is exit 56.
        if not is_one_address(user):
            raise ValueError(f"“{user}” isn't one email address")
        return ["user", user, "print", "tokens"]

    @staticmethod
    def block_device_user(name: str) -> List[str]:
        # One account's sign-in on a computer, blocked in Google's endpoint management: it can't reach the account's data, and stays listed as blocked
        # (approving it undoes it). GAM acts on a name without `doit`, which is for a query. Needs the devices scope itself: `.readonly` reads but can't block.
        if not is_device_user_name(name):
            raise ValueError("a computer's sign-in is devices/<id>/deviceUsers/<id>")
        return ["block", "deviceuser", name]

    @staticmethod
    def delete_device_user(name: str) -> List[str]:
        # One account's sign-in on a computer, deleted: the account is taken off it and its access to the account's data revoked. It shows again only if someone signs in on it again.
        if not is_device_user_name(name):
            raise ValueError("a computer's sign-in is devices/<id>/deviceUsers/<id>")
        return ["delete", "deviceuser", name]

    @staticmethod
    def print_tokens() -> List[str]:
        # Every account's OAuth grants, suspended accounts aside (GAM's default entity): one Directory API call
        # per account. CSV `user,clientId,displayText,anonymous,nativeApp,userKey,scopes`. SwiftGamGui's printTokens.
        return ["print", "tokens"]

    @staticmethod
    def info_user(email: str, fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["info", "user", email, "fields", ",".join(fields or USER_DETAIL_FIELDS)]
        argv.append("formatjson")
        return argv

    # --- users (mutating) -------------------------------------------------------------
    @staticmethod
    def create_user(
        email: str,
        first_name: str,
        last_name: str,
        password: str,
        change_password: bool = True,
        org_unit: Optional[str] = None,
        notify: Optional[str] = None,
    ) -> List[str]:
        argv = [
            "create", "user", email,
            "firstname", first_name,
            "lastname", last_name,
            "password", password,
            "changepassword", "on" if change_password else "off",
        ]
        if org_unit:
            argv += ["org", org_unit]
        # GAM/Google emails the sign-in info (incl. the one-time password) straight to `notify`, so the
        # operator never handles it. The notify clause comes after the user attributes (see the grammar's
        # `create|add user` rule). notifypassword carries the same temp password; it's redacted before audit.
        if notify:
            argv += ["notify", notify, "notifypassword", password]
        return argv

    @staticmethod
    def update_organization(email: str, title: str = "", department: str = "") -> List[str]:
        """Set the primary organization's title + department.

        GAM's ``organization`` replaces the primary org, so we always pass both fields together (the
        editor pre-fills the current values) to avoid clearing one while changing the other.
        """
        return ["update", "user", email, "organization", "title", title, "department", department, "primary"]

    @staticmethod
    def set_suspended(email: str, suspended: bool) -> List[str]:
        # `update user ... suspended on/off` is the canonical, version-stable form.
        return ["update", "user", email, "suspended", "on" if suspended else "off"]

    # --- calendar access ---------------------------------------------------------------
    @staticmethod
    def print_calendar_acls(email: str, calendar: str = "primary") -> List[str]:
        return ["user", email, "print", "calendaracls", calendar, "formatjson"]

    @staticmethod
    def add_calendar_acl(email: str, target: str, role: str = "reader", calendar: str = "primary") -> List[str]:
        # `target` is a scope: a bare email = a user; pass "group:<email>"/"domain"/"default" as-is.
        return ["user", email, "add", "calendaracls", calendar, _validate_calendar_role(role), target]

    @staticmethod
    def delete_calendar_acl(email: str, scope: str, calendar: str = "primary") -> List[str]:
        return ["user", email, "delete", "calendaracls", calendar, scope]

    # --- calendars / resources / events ------------------------------------------------
    @staticmethod
    def print_resources(query: str = "") -> List[str]:
        argv = ["print", "resources", "fields", "id,name,email,resourcetype,buildingid"]
        if query:
            argv += ["query", query]
        argv.append("formatjson")
        return argv

    @staticmethod
    def print_user_calendars(email: str) -> List[str]:
        return ["user", email, "print", "calendars", "fields", "id,summary,accessrole,primary", "formatjson"]

    @staticmethod
    def print_all_calendars() -> List[str]:
        # Every user's calendar list (incl. secondary calendars) — filtered by name in Python.
        return ["all", "users", "print", "calendars", "fields", "id,summary,accessrole,primary", "formatjson"]

    @staticmethod
    def print_calendar_acls_cal(calendar_id: str) -> List[str]:
        # Standalone form: ACLs for ANY calendar id (room/secondary) via admin access.
        return ["calendars", calendar_id, "print", "calendaracls", "formatjson"]

    @staticmethod
    def add_calendar_acl_cal(
        calendar_id: str, scope: str, role: str = "reader", send_notifications: bool = False
    ) -> List[str]:
        # Standalone admin form (same auth path as `calendars <id> print calendaracls`) — no owner
        # impersonation, no formatjson. `scope`: bare email = user; pass "group:<email>"/"domain"/
        # "default" through unchanged. Notifications default OFF: the subscribe makes it visibly
        # appear (the whole point is people miss the sharing email).
        argv = ["calendars", calendar_id, "add", "calendaracls", _validate_calendar_role(role), scope]
        if send_notifications:
            argv += ["sendnotifications", "true"]
        return argv

    @staticmethod
    def delete_calendar_acl_cal(calendar_id: str, scope: str) -> List[str]:
        return ["calendars", calendar_id, "delete", "calendaracls", scope]

    @staticmethod
    def subscribe_calendar(email: str, calendar_id: str, selected: bool = True) -> List[str]:
        # Makes the calendar appear in the recipient's sidebar; runs as the recipient.
        argv = ["user", email, "add", "calendars", calendar_id]
        if selected:
            argv += ["selected", "true"]
        return argv

    @staticmethod
    def remove_calendar(owner: str, calendar_id: str) -> List[str]:
        # PERMANENTLY delete a secondary calendar, acting as an owner (Calendars.delete).
        # GAM footgun: `remove calendars` deletes the calendar for everyone; `delete calendars`
        # would only unsubscribe this user (CalendarList.delete). Must impersonate an owner.
        return ["user", owner, "remove", "calendars", calendar_id]

    _EVENT_FIELDS = "id,summary,start,end,recurrence,recurringeventid,organizer,creator,status"

    @staticmethod
    def print_events(calendar_id: str, query: str = "", after: str = "", before: str = "") -> List[str]:
        argv = ["calendars", calendar_id, "print", "events"]
        if query:
            argv += ["query", query]
        if after:
            argv += ["after", after]
        if before:
            argv += ["before", before]
        argv += ["fields", GAMCommands._EVENT_FIELDS, "formatjson"]
        return argv

    @staticmethod
    def get_event(calendar_id: str, event_id: str) -> List[str]:
        # Re-read one event by id for the delete preview.
        return ["calendars", calendar_id, "print", "events", "eventid", event_id,
                "fields", GAMCommands._EVENT_FIELDS, "formatjson"]

    @staticmethod
    def delete_event(calendar_id: str, event_id: str, doit: bool = True) -> List[str]:
        # GAM dry-runs `delete events` without `doit`. Deleting a recurring master id drops the series.
        argv = ["calendars", calendar_id, "delete", "events", "eventid", event_id]
        if doit:
            argv.append("doit")
        argv += ["sendupdates", "none"]
        return argv

    # --- lifecycle (offboarding) -------------------------------------------------------
    @staticmethod
    def reset_password(email: str) -> List[str]:
        # Random password + no change-prompt: the old password stops working; the mailbox stays live.
        return ["update", "user", email, "password", "random", "changepassword", "off"]

    @staticmethod
    def signout_user(email: str) -> List[str]:
        return ["user", email, "signout"]

    @staticmethod
    def deprovision_user(email: str) -> List[str]:
        # `deprovision|deprov [popimap] [signout] [turnoff2sv]` (GamCommands.txt 7899): deletes the
        # user's app passwords, invalidates their backup codes and deletes their OAuth tokens; with
        # `signout`, then ends every web and device session. Not `turnoff2sv` (it would weaken a
        # locked account and nobody needs to sign in as the leaver — the manager gets delegation);
        # not `popimap` (POP/IMAP need a password, app password or token, all revoked here).
        return ["user", email, "deprovision", "signout"]

    # `[private|shared|all]` (GamCommands.txt 3599): which of the old owner's Drive files move — `all`
    # is both. GAM sends PRIVACY_LEVEL only when one is named, and only to a listed app that takes it
    # (Drive; with no such app it is a usage error), so without one the Data Transfer API's own
    # default decides whether the files the old owner had shared move.
    TRANSFER_PRIVACY = ("private", "shared", "all")

    @staticmethod
    def create_datatransfer(old_owner: str, service: str, new_owner: str, privacy: str = "") -> List[str]:
        # `service` is a <DataTransferServiceList>: one service ("drive" | "calendar") OR a
        # comma-joined list ("drive,calendar") that rides as ONE argv element. Passing both in a
        # single transfer avoids Google's 409 "transfer already in progress" when two separate
        # transfers for the same user overlap.
        argv = ["create", "datatransfer", old_owner, service, new_owner]
        if privacy:
            if privacy not in GAMCommands.TRANSFER_PRIVACY:
                raise ValueError(f"invalid transfer privacy level {privacy!r}; expected one of "
                                 f"{GAMCommands.TRANSFER_PRIVACY}")
            argv.append(privacy)
        return argv

    @staticmethod
    def print_datatransfers(old_owner: str = "") -> List[str]:
        # Transfers are async; the CSV carries `overallTransferStatusCode` (completed/inProgress/...).
        argv = ["print", "datatransfers"]
        if old_owner:
            argv += ["olduser", old_owner]
        return argv

    @staticmethod
    def remove_all_calendar_acls(email: str) -> List[str]:
        # Remove the departing user from EVERY other user's primary calendar (GAM loops all users).
        return ["all", "users", "delete", "calendaracls", "primary", email]

    @staticmethod
    def add_calendar_event(
        calendar: str, summary: str, start: str, end: str, description: str = "", attendee: str = ""
    ) -> List[str]:
        argv = ["user", calendar, "add", "event", "primary",
                "summary", summary, "start", "allday", start, "end", "allday", end]
        if description:
            argv += ["description", description]
        if attendee:
            # GAM's `add event` defaults sendUpdates to none (read from the vendored build), so an
            # attendee would get no invitation email. `[<EventNotificationAttribute>]` comes after the
            # attributes (GamCommands.txt 6469; `sendupdates all|…|none`, 6460).
            argv += ["attendee", attendee, "sendupdates", "all"]
        return argv

    @staticmethod
    def delete_user(email: str) -> List[str]:
        return ["delete", "user", email]

    @staticmethod
    def undelete_user(email: str) -> List[str]:
        return ["undelete", "user", email]

    # --- onboarding runbook: a Google Tasks checklist on the assignee + a welcome email ---
    @staticmethod
    def create_tasklist(assignee: str, title: str) -> List[str]:
        # `returnidonly` so we get just the new tasklist id back to attach tasks to.
        return ["user", assignee, "create", "tasklist", "title", title, "returnidonly"]

    @staticmethod
    def create_task(assignee: str, tasklist_id: str, title: str, notes: str = "") -> List[str]:
        argv = ["user", assignee, "create", "task", tasklist_id, "title", title]
        if notes:
            argv += ["notes", notes]
        return argv

    @staticmethod
    def send_email(to: str, subject: str, body: str, html: bool = True) -> List[str]:
        argv = ["sendemail", "to", to, "subject", subject, "message", body]
        if html:
            argv += ["html"]
        return argv

    # --- gmail: signature / delegate / forwarding / vacation --------------------------
    @staticmethod
    def set_signature(email: str, signature: str, html: bool = True) -> List[str]:
        argv = ["user", email, "signature", signature]
        if html:
            argv.append("html")
        return argv

    @staticmethod
    def show_signature(email: str) -> List[str]:
        # `show signature` returns text (no formatjson).
        return ["user", email, "show", "signature"]

    @staticmethod
    def add_delegate(email: str, delegate: str) -> List[str]:
        return ["user", email, "add", "delegate", delegate]

    @staticmethod
    def remove_delegate(email: str, delegate: str) -> List[str]:
        return ["user", email, "delete", "delegate", delegate]

    @staticmethod
    def print_delegates(email: str) -> List[str]:
        # NOTE: `print delegates` does NOT support `formatjson` (it errors "Invalid argument").
        # Output is plain CSV with a `delegateAddress` column.
        return ["user", email, "print", "delegates"]

    @staticmethod
    def set_vacation(
        email: str,
        subject: str,
        message: str,
        html: bool = True,
        start: Optional[str] = None,
        end: Optional[str] = None,
        contacts_only: bool = False,
        domain_only: bool = False,
    ) -> List[str]:
        # GAM's `vacation` does not replace the settings: it reads them, overwrites only the fields the
        # command names, and writes the result back (setVacation, read from the vendored build). So every
        # setting is named — each flag as an explicit Boolean, a missing date as `Started`/`NotSpecified`
        # (no date; GamCommands.txt 8286-8288) — or a leftover "domain only" or past end date survives.
        argv = ["user", email, "vacation", "on", "subject", subject, "message", message]
        if html:
            argv.append("html")
        argv += ["contactsonly", "true" if contacts_only else "false",
                 "domainonly", "true" if domain_only else "false",
                 "start", start or "Started", "end", end or "NotSpecified"]
        return argv

    @staticmethod
    def vacation_off(email: str) -> List[str]:
        return ["user", email, "vacation", "off"]

    # --- gmail: forwarding ------------------------------------------------------------
    FORWARD_ACTIONS = ("keep", "archive", "markread", "trash", "delete")

    @staticmethod
    def add_forwarding_address(email: str, address: str) -> List[str]:
        return ["user", email, "add", "forwardingaddress", address]

    @staticmethod
    def print_forwarding_addresses(email: str) -> List[str]:
        return ["user", email, "print", "forwardingaddresses"]

    @staticmethod
    def report_activity(application: str, users: Sequence[str] = (), hours: int = 72, start: str = "", end: str = "", ip: str = "",
                        events: Sequence[str] = (), max_activities: int = 1000, chrome: str = "profile", admin: str = "actor") -> List[str]:
        # Activity events, newest first (GamCommands.txt `gam report <ActivityApplicationName>`): `user` for one account, `select users` for
        # several, `user all` for none. `start -<hours>h end now`, or both given as times, which GAM passes on as they are: a window fixed once, read
        # further back page by page. `ip` is server-side. `maxactivities` bounds each read. The admin log reads the accounts by `admin`: with
        # `userisactor` what they did as admins, without it what admins did to them, which doReport maps to the filter `USER_EMAIL==<account>`. Chrome's
        # is read one account at a time, by `chrome`: the profile's account (`user all filter PROFILE_USER_NAME==<account>`, the filter passed as given,
        # so lowercased as Google keeps it), or GAM's own `user <account>`, which it turns into `DEVICE_USER==<account>`.
        if application not in GAMCommands.REPORT_APPLICATIONS:
            raise ValueError(f"“{application}” isn't an activity log")
        if not 1 <= hours <= 4320 or max_activities < 1:
            raise ValueError("a report reads 1 hour to 180 days")
        if bool(start) != bool(end) or (start and not (is_report_time(start) and is_report_time(end) and start < end)):
            raise ValueError("a report's window is a start and an end, each yyyy-mm-ddThh:mm:ssZ, the start first")
        if application == "gmail" and hours > 720:
            raise ValueError("the Gmail log reads at most 30 days")
        if len(users) > 20 or not all(is_one_address(u) for u in users):
            raise ValueError("each account is one email address, and at most 20 at once")
        if application == "chrome" and len(users) > 1:
            raise ValueError("the Chrome log reads one account at a time")
        if chrome not in ("profile", "device") or admin not in ("actor", "affected"):
            raise ValueError("a log is read by one of its two matches")
        if application == "admin" and admin == "affected" and not all(filter_address(u) for u in users):
            raise ValueError("an account the admin log reads what was done to is one plain address")
        account = ""
        if application == "chrome" and users:
            account = filter_address(users[0]) or ""
            if not account:
                raise ValueError(f"“{users[0]}” isn't an address the Chrome log can match")
        if ip:
            try:
                ipaddress.ip_address(ip)
            except ValueError as exc:
                raise ValueError(f"“{ip}” isn't one IP address") from exc
        if not all(re.fullmatch(r"[A-Za-z_]+", e) for e in events):
            raise ValueError("an event name is letters and “_”")
        if not users:
            who = ["user", "all"]
        elif account:
            who = ["user", "all", "filter", f"PROFILE_USER_NAME=={account}"] if chrome == "profile" else ["user", account]
        else:
            who = ["user", users[0]] if len(users) == 1 else ["select", "users", ",".join(users)]
        argv = ["report", application, *who]
        if application == "admin" and users and admin == "actor":
            argv.append("userisactor")
        argv += ["start", start or f"-{hours}h", "end", end or "now"]
        if ip:
            argv += ["ip", ip]
        if events:
            argv += ["events", ",".join(events)]
        return [*argv, "maxactivities", str(max_activities)]

    @staticmethod
    def print_forward(email: str) -> List[str]:
        # `User,forwardEnabled,forwardTo,disposition` (printShowForward), the address and disposition only while on.
        return ["user", email, "print", "forward"]

    # --- gmail: what an attacker leaves behind ------------------------------------------
    @staticmethod
    def print_send_as(email: str) -> List[str]:
        # The person's send-as addresses, the primary among them. `verifyonly` makes each signature a yes or no, not its HTML.
        return ["user", email, "print", "sendas", "verifyonly"]

    @staticmethod
    def delete_send_as(email: str, address: str) -> List[str]:
        if not is_one_address(address):
            raise ValueError("each send-as address to remove is one email address")
        return ["user", email, "delete", "sendas", address]

    @staticmethod
    def print_admins(email: str) -> List[str]:
        # The roles assigned to the person directly. Not one held through a group: that takes `recursive`.
        return ["print", "admins", "user", email]

    @staticmethod
    def list_drafts(email: str, most: int) -> List[str]:
        # A draft is a message with the DRAFT label: GAM has no drafts command. Only the subject, since a phishing draft's Bcc runs
        # to hundreds of addresses. Without includespamtrash, so one already in Trash isn't listed.
        return ["user", email, "print", "messages", "query", "in:drafts", "max_to_print", str(most), "headers", "subject", "showlabels", "showdate"]

    @staticmethod
    def search_scheduled(email: str, most: int) -> List[str]:
        # The emails waiting in Gmail's Scheduled, each with its recipients. Gmail sends one at its time with nobody signed in, and
        # the drafts' listing never shows it. The mailbox search's own shape (`search_messages`), to more than its cap of 50.
        return ["user", email, "print", "messages", "query", "in:scheduled", "includespamtrash", "max_to_print", str(most), "headers", "all",
                "showlabels", "showdate"]

    @staticmethod
    def delete_messages(email: str, ids: Sequence[str]) -> List[str]:
        # For good, as Gmail's Discard deletes a draft: a draft in Trash may still be a draft, and sendable. With `ids` GAM needs no
        # `doit` and checks no count. Batches of 50: one holding an ID Gmail refuses fails whole, the other batches done.
        joined = id_list(ids)
        if joined is None:
            raise ValueError("each message to delete is one message ID")
        return ["user", email, "delete", "messages", "ids", joined]

    @staticmethod
    def set_forward(email: str, address: str, action: str = "keep") -> List[str]:
        # Forward to an already-added/verified forwarding address; `action` is what to do with the
        # original copy (keep | archive | markread | trash | delete).
        if action not in GAMCommands.FORWARD_ACTIONS:
            raise ValueError(f"invalid forward action: {action!r}")
        return ["user", email, "forward", "on", action, address]

    @staticmethod
    def forward_off(email: str) -> List[str]:
        return ["user", email, "forward", "off"]

    # --- message search (read-only) ---------------------------------------------------
    MESSAGE_DETAIL = ("Headers", "Headers + body", "Summary")
    MESSAGE_CAP = 50   # bound an empty/broad query so it can't dump a whole mailbox into the table

    @staticmethod
    def search_messages(email: str, query: str = "", detail: str = "Headers") -> List[str]:
        """Find messages in one mailbox by Gmail search and show their headers (read-only).

        ``query`` is a Gmail search string (e.g. ``rfc822msgid:<id>``, ``from:… after:2026/06/23
        before:2026/06/24``) and is passed as a *single* argv element — never shell-spliced. ``headers
        all`` surfaces ``Return-Path``/``Received`` so an envelope/bounce address (e.g. an Amazon SES
        sender) is visible. Spam/Trash are included (bounces often land there) and results are capped.
        """
        # NB: `print messages` has NO `formatjson` mode (unlike `print users` et al.) — it emits CSV.
        # Appending formatjson makes GAM reject the command ("format json is invalid"). We let it
        # return CSV and parse that (parse_records handles CSV).
        argv = ["user", email, "print", "messages"]
        if query:
            argv += ["query", query]
        argv += ["includespamtrash", "max_to_print", str(GAMCommands.MESSAGE_CAP)]
        if detail == "Summary":
            argv += ["showlabels", "showdate", "showsize", "showsnippet"]
        elif detail == "Headers + body":
            argv += ["headers", "all", "showbody", "showlabels", "showdate"]
        else:  # "Headers" (default)
            argv += ["headers", "all", "showlabels", "showdate"]
        return argv

    # --- aliases ----------------------------------------------------------------------
    @staticmethod
    def create_user_alias(alias: str, email: str) -> List[str]:
        return ["create", "alias", alias, "user", email]

    @staticmethod
    def delete_alias(alias: str) -> List[str]:
        return ["delete", "alias", alias]

    @staticmethod
    def todrive_args(user: str = "", title: str = "") -> List[str]:
        # Append to a `print …` command to export its CSV to a Google Sheet. `user` = whose Drive
        # owns the sheet (blank = the admin/oauth account's Drive); `title` names it.
        argv = ["todrive"]
        if user:
            argv += ["tduser", user]
        if title:
            argv += ["tdtitle", title]
        return argv

    @staticmethod
    def show_vacation(email: str) -> List[str]:
        # `show vacation` does NOT support formatjson — returns parseable text.
        return ["user", email, "show", "vacation"]

    # --- groups -----------------------------------------------------------------------
    @staticmethod
    def print_groups(fields: Optional[Sequence[str]] = None) -> List[str]:
        argv = ["print", "groups", "fields", ",".join(fields or GROUP_LIST_FIELDS), "formatjson"]
        return argv

    @staticmethod
    def create_group(email: str, name: str = "", description: str = "") -> List[str]:
        argv = ["create", "group", email]
        if name:
            argv += ["name", name]
        if description:
            argv += ["description", description]
        return argv

    @staticmethod
    def print_group_members(group: str) -> List[str]:
        return ["print", "group-members", "group", group, "formatjson"]

    @staticmethod
    def print_domains() -> List[str]:
        # The tenant's primary and secondary domains, each with its domain aliases in the JSON record
        # (GamCommands.txt `gam print domains ... [formatjson]`) — what the Builder's External-only lens
        # counts as internal.
        return ["print", "domains", "formatjson"]

    @staticmethod
    def print_groups_member(email: str) -> List[str]:
        # Groups that <email> belongs to. Returns CSV with an `email` column.
        return ["print", "groups", "member", email]

    @staticmethod
    def add_group_member(group: str, member: str, role: str = "member") -> List[str]:
        role = _validate_role(role)
        return ["update", "group", group, "add", role, member]

    @staticmethod
    def remove_group_member(group: str, member: str) -> List[str]:
        return ["update", "group", group, "remove", member]


def _validate_role(role: str) -> str:
    role = (role or "member").strip().lower()
    if role not in GROUP_ROLES:
        raise ValueError(f"invalid group role {role!r}; expected one of {GROUP_ROLES}")
    return role


def _validate_calendar_role(role: str) -> str:
    role = (role or "").strip().lower()
    if role not in CALENDAR_ACL_ROLES:
        raise ValueError(f"invalid calendar role {role!r}; expected one of: {', '.join(CALENDAR_ACL_ROLES)}")
    return role


def build_user_query(search: str = "", include_suspended: bool = True) -> Optional[str]:
    """Translate a free-text search box into a Directory API query string.

    Empty search returns ``None`` (list everyone). A bare token matches email/name prefixes.
    """
    clauses: List[str] = []
    search = (search or "").strip()
    if search:
        # Directory API supports prefix matching with '*'. Match common fields.
        token = search.replace("'", "")
        clauses.append(f"email:{token}* givenName:{token}* familyName:{token}*")
    if not include_suspended:
        clauses.append("isSuspended=false")
    return " ".join(clauses) if clauses else None
