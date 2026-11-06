"""The Reports API's activity logs, read into one row shape: when, who, the event, the IP address and the event's parameters.

GAM prints a CSV row per event, newest first: the activity's columns (``id.time``, ``actor.email``, ``ipAddress``) beside the event's
(``name``, ``type``, then each simple parameter under its own name; a nested one, as Gmail's are, flattened under ``parameters.N…``).
Pure. Ported from SwiftGamGui's ``ActivityLogs.swift``.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..gam.parser import field_limit_at_least, parse_records

HOURS = (24, 72, 240, 360, 720, 4320)   # a day, 3, 10, 15 and 30 days, and six months (the Reports API keeps about six; Gmail's 30 days)
DEFAULT_HOURS = 72
CAP = 1000              # each read's bound: the newest this many activities per account
PAGES = 10              # a read at the cap is read on further back, at most this many reads
EXPORT_ROWS = 3_000_000 # a saved read's events, all its logs together: a row of a hostile zip costs about 310 bytes held and a dozen on disk. Past any pull READS_PER_LOG lets one log reach
EARLIEST_YEAR, LATEST_YEAR = 1990, 2200     # a time every screen can say in any zone: year 1 or 9999 can't be moved to one
READS_PER_DAY = 100     # a log read whole: at most this many reads for one day of it, and this many for the log. Past them it stops, oldest
READS_PER_LOG = 200     # first, so what's left unread is the newest, said, with the time it was read through
GMAIL_MAX_HOURS = 720   # the Reports API reads the Gmail log at most this far back (doReport says so)
TABLE_HEADER = ("log", "time", "actor", "event", "ip_address", "flagged", "via", "parameters")

# (GAM's name for the log, the Admin console's, what reading it shows). GAM's REPORT_CHOICE_MAP spells each.
APPLICATIONS: Tuple[Tuple[str, str, str], ...] = (
    ("login", "Sign-ins (user log)", "Sign-ins, failures and challenges, and Google's suspicious-sign-in warnings: the log that matters most."),
    ("token", "OAuth tokens (app access)", "Apps granted or using access to the account, by app and scope."),
    ("saml", "SAML (sign-ins to other apps)", "Sign-ins to other apps through Google (single sign-on): which apps the account reached."),
    ("gmail", "Gmail", "Each message sent, received, read, drafted or deleted, and which it was. Some editions don't keep it."),
    ("accessevaluation", "Access evaluation", "The Admin console's “Access” log: cookie validation and access-token requests."),
    ("chat", "Chat", "Chat messages and spaces."),
    ("useraccounts", "Account changes", "Password, 2-Step Verification and recovery changes."),
    ("takeout", "Takeout", "Exports of the account's data."),
    ("mobile", "Devices", "Phones, tablets and computers registering or syncing: an attacker's computer can join the account as it signs in. It "
                          "records no address, so a pull by address reads its registrations for the accounts asked."),
    ("geminiinworkspaceapps", "Gemini", "Gemini in Gmail, Docs, Drive and the Gemini app: what was asked and done. An attacker can ask it to find "
                                        "invoices or passwords in a mailbox."),
    ("admin", "Admin console changes", "What admins did to these accounts (a password reset, a sign-out: the lock), and what they did as admins."),
    ("contacts", "Contacts", "Contacts printed, exported and changed: a list printed during an attack is who the phish goes to next."),
    ("chrome", "Chrome", "Chrome's warnings and website sign-ins, by the account signed in to Chrome or a Chromebook's own. Only what Chrome reports: "
                         "empty can mean its reporting is off, not that Chrome isn't used."),
    ("drive", "Drive", "Files viewed, downloaded, shared and deleted."),
    ("calendar", "Calendar", "Events made, changed and shared: invitations can carry phishing too."),
    ("groups", "Groups", "Group settings and members: someone added to a group, a group opened to outsiders."),
    ("rules", "Rules", "Data protection and other rules that fired. Some editions don't keep it."),
    ("contextawareaccess", "Context-aware access", "Access that context-aware rules blocked. Some editions don't keep it."),
    ("vault", "Vault", "Vault searches, exports and holds: what matters when an admin's account was taken."),
    ("meet", "Meet", "Meetings hosted and joined, and who took part."),
    ("keep", "Keep", "Notes made, shared and changed."),
    ("tasks", "Tasks", "Tasks made and changed."),
    ("admindataaction", "Admin data actions", "Data actions admins ran, such as exports and deletions."),
    ("accesstransparency", "Access Transparency", "Google staff access to your data, and why. Some editions don't keep it."),
    ("datastudio", "Looker Studio", "Looker Studio reports and data sources made, shared and viewed."),
    ("groupsenterprise", "Groups (Cloud Identity)", "Group membership changes made through the Cloud Identity Groups API."),
    ("profile", "Profiles", "Changes people made to their own profile."),
    ("ldap", "Secure LDAP", "Secure LDAP sign-ins and lookups by the apps that use it."),
    ("directorysync", "Directory sync", "Directory sync runs from another directory."),
    ("gcp", "Google Cloud", "Google Cloud: projects, resources and who changed them."),
    ("cloudsearch", "Cloud Search", "Cloud Search queries and settings."),
    ("classroom", "Classroom", "Classroom activity."),
    ("datamigration", "Data migration", "Data migrated into Workspace."),
    ("meethardware", "Meet hardware", "Meet room hardware."),
    ("assignments", "Assignments", "Assignments (education)."),
    ("graduation", "Graduation", "Graduation (education accounts)."),
)
NAMES: Dict[str, str] = {key: name for key, name, _ in APPLICATIONS}
# What an incident needs first: sign-ins, OAuth, Gmail, Access, Chat and account changes, the other ways in and out an attacker uses, Gemini,
# the admin log, where the lock is, and Contacts, since a contact list printed while the attacker had the account is who the phish goes to next.
INCIDENT_DEFAULT: Tuple[str, ...] = ("login", "token", "saml", "gmail", "accessevaluation", "chat", "useraccounts", "takeout", "mobile",
                                     "geminiinworkspaceapps", "admin", "contacts")

# The activity's own columns, and the counts flattenJSON writes for a list: never a parameter.
_ACTIVITY_COLUMNS = frozenset({
    "name", "id.time", "actor.email", "ipAddress", "id.uniqueQualifier", "id.applicationName", "id.customerId", "actor.profileId",
    "actor.callerType", "actor.key", "parameters", "isAgenticAction", "actor.agentAttributionInfo.agentName",
    "actor.agentAttributionInfo.agentType", "actor.agentAttributionInfo.agentId", "actor.agentAttributionInfo.agentOwner.email",
    "actor.applicationInfo.applicationName", "actor.applicationInfo.oauthClientId", "actor.applicationInfo.impersonation"})
_VALUE_WORDS = ("value", "intValue", "boolValue", "multiValue")
# The events Google raises itself, beside any whose name says suspicious, blocked, deny, disabled, leak or attack.
_FLAGGED_EVENTS = frozenset({"gov_attack_warning", "risky_sensitive_action_blocked", "risky_sensitive_action_allowed", "PASSWORD_REUSE",
                             "PASSWORD_BREACH", "UNSAFE_SITE_VISIT", "MALWARE_TRANSFER"})
_FLAG_WORDS = ("suspicious", "blocked", "deny", "disabled", "leak", "attack")
_EVENT_WORDS = {
    "login_success": "Signed in", "login_failure": "Sign-in failed", "logout": "Signed out", "login_challenge": "Asked to prove it's them",
    "login_verification": "2-Step Verification asked", "suspicious_login": "Google flagged a suspicious sign-in",
    "suspicious_login_less_secure_app": "Google flagged a suspicious sign-in from a less secure app",
    "suspicious_programmatic_login": "Google flagged a suspicious sign-in by a program", "gov_attack_warning": "Google warned of a government-backed attack",
    "account_disabled_hijacked": "Google disabled the account: hijacked", "account_disabled_password_leak": "Google disabled the account: password leaked",
    "account_disabled_spamming": "Google disabled the account: sending spam", "account_disabled_spamming_through_relay": "Google disabled the account: sending spam",
    "account_disabled_generic": "Google disabled the account", "user_signed_out_due_to_suspicious_session_cookie": "Google signed the person out: a suspicious session cookie",
    "blocked_sender": "Blocked a sender in Gmail", "print_contacts": "Printed their contacts", "passkey_enrolled": "Added a passkey", "passkey_removed": "Removed a passkey",
    "2sv_enroll": "Turned on 2-Step Verification", "2sv_disable": "Turned off 2-Step Verification", "password_edit": "Changed their password",
    "recovery_email_edit": "Changed the recovery email", "recovery_phone_edit": "Changed the recovery phone", "recovery_secret_qa_edit": "Changed the recovery question",
    "CHANGE_PASSWORD": "Changed a person's password", "RESET_SIGNIN_COOKIES": "Signed a person out of every session", "REVOKE_3LO_TOKEN": "Revoked an app's access",
    "REVOKE_3LO_DEVICE_TOKENS": "Revoked a device's access", "REVOKE_ASP": "Deleted an app password", "DELETE_2SV_SCRATCH_CODES": "Deleted 2-Step Verification backup codes",
    "SUSPEND_USER": "Suspended a person", "risky_sensitive_action_allowed": "A risky sensitive action was allowed", "risky_sensitive_action_blocked": "A risky sensitive action was blocked",
    "authorize": "Granted an app access", "revoke": "An app's access was revoked", "LOGIN_EVENT": "Signed in to a website in Chrome",
    "PASSWORD_REUSE": "Typed a protected password on another website", "PASSWORD_BREACH": "Used a password found in a data breach", "PASSWORD_CHANGED": "Changed a protected password",
    "UNSAFE_SITE_VISIT": "Chrome warned of an unsafe website", "MALWARE_TRANSFER": "Chrome found malware in a file", "STARTED_USER_TAKEOUT": "Started a Takeout export of the account's data",
    "COMPLETED_USER_TAKEOUT": "A Takeout export finished", "DOWNLOADED_USER_TAKEOUT": "Downloaded a Takeout export", "SCHEDULED_USER_TAKEOUT": "Scheduled repeating Takeout exports",
}
# Google's "Gmail Activity Events": every Gmail event is `delivery`, and its `event_info.mail_event_type` says what happened.
_GMAIL_WORDS = {
    0: "A Gmail delivery step Google doesn't name", 1: "Sent a message", 2: "Received a message", 3: "Marked a message as spam, phishing or not spam",
    4: "Gmail flagged a message as spam after delivery", 5: "A message was quarantined", 6: "A message was released from quarantine",
    7: "Opened a message for the first time", 8: "Marked a message unread", 9: "Replied to a message for the first time", 10: "Forwarded a message for the first time",
    11: "A message was forwarded by the account's forwarding setting", 12: "Moved a message to the Inbox", 13: "Moved a message to Trash", 14: "Took a message out of Trash",
    15: "Clicked a link in a message", 16: "Clicked a link in an attachment's preview", 17: "Downloaded a message's attachments", 18: "Saved a message's attachments to Drive",
    19: "Saved a message's Drive items to their Drive", 20: "A classification label was applied to a message", 21: "A message's classification label was changed",
    22: "A message's classification label was removed", 23: "A classification label was applied to all of a message's attachments",
    24: "The classification label on all of a message's attachments was changed", 25: "The classification label was removed from all of a message's attachments",
    26: "Archived a message", 27: "Permanently deleted a message", 28: "Previewed a message's attachments", 29: "Saved a draft", 30: "A message bounced: it couldn't be delivered",
    31: "Viewed a message", 32: "Downloaded a message", 33: "An app opened a message for them", 34: "A message was rejected or slowed: too much mail arriving at once",
    35: "Started sending a message",
}
_LOGIN_TYPES = {"google_password": "password", "reauth": "signing in again", "saml": "single sign-on (SAML)", "exchange": "Exchange", "unknown": "an unknown way"}
_DEVICE_KINDS = {"WINDOWS": "Windows computer", "MAC": "Mac", "MAC_OS": "Mac", "LINUX": "Linux computer", "CHROME_OS": "Chromebook", "IOS": "iPhone or iPad",
                 "IOS_SYNC": "iPhone or iPad", "ANDROID": "Android device", "GOOGLE_SYNC": "device on Exchange sync"}
_CHROME_REASONS = {"PASSWORD_REUSED_PHISHING_URL": "on a phishing site", "PASSWORD_REUSED_UNAUTHORIZED_SITE": "on a site not on the allowed list",
                   "UNSAFE_SITE_VISIT_SOCIAL_ENGINEERING": "a phishing site", "UNSAFE_SITE_VISIT_MALWARE": "a malware site",
                   "UNSAFE_SITE_VISIT_UNWANTED_SOFTWARE": "an unwanted-software site", "UNSAFE_SITE_VISIT_SSL_ERROR": "a certificate error"}
_MOBILE_HIDDEN = frozenset({"DEVICE_TYPE", "DEVICE_MODEL", "OS_VERSION", "ACCOUNT_STATE", "type", "DEVICE_ID", "RESOURCE_ID", "USER_EMAIL", "SERIAL_NUMBER",
                            "IOS_VENDOR_ID", "LAST_SYNC_AUDIT_DATE"})
_MOBILE_WARNINGS = ("DEVICE_IS_", "DEVICE_HAS_", "DEVICE_OS_IS_", "DEVICE_SECURITY_PATCH_IS_")


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value)


def _sentence(name: str) -> str:
    """`login_failure` or Chrome's `CONTENT_TRANSFER` as "Login failure", "Content transfer"."""
    words = name.replace("_", " ")
    return words[:1].upper() + (words[1:].lower() if words.upper() == words else words[1:])


def _scopes(listed: str) -> str:
    """"mail.google.com, contacts.readonly" for a space-separated list of scope URLs."""
    out = []
    for scope in listed.split():
        for prefix in ("https://www.googleapis.com/auth/", "https://"):
            if scope.startswith(prefix):
                scope = scope[len(prefix):].strip("/")
                break
        out.append(scope)
    return ", ".join(out)


def parse_time(text: str) -> Optional[datetime]:
    """GAM's time, UTC unless its config converts it; None when it doesn't read as one."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not EARLIEST_YEAR <= parsed.year <= LATEST_YEAR:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def report_time(when: datetime) -> str:
    """``when`` as the report's time: UTC, to the second, rounded down."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Columns:
    """What a report's header says of its columns, worked out once for every row under it: which are parameters, in what order, and how each
    nested one is named. One GAM call's CSV gives every row the same columns in the same order."""

    def __init__(self, keys: Sequence[str]) -> None:
        self.keys = tuple(keys)
        self.names = [(i, k[:-5]) for i, k in enumerate(keys) if k.startswith("parameters.") and k.endswith(".name")]
        present = set(keys)

        def padded(key: str) -> str:    # GAM sorts its titles as text (`…parameter.10` before `….2`): the nested ones back in numeric order
            return ".".join(p.rjust(9, "0") if p.isascii() and p.isdigit() and len(p) < 9 else p for p in key.split("."))

        nested = sorted((i for i, k in enumerate(keys) if k.startswith("parameters.")), key=lambda i: padded(keys[i]))
        ordered = [i for i, k in enumerate(keys) if not k.startswith("parameters.")] + nested
        self.said: List[Tuple[int, Optional[str], Tuple[str, ...], bool]] = []   # (place, a simple name, a nested one's prefixes, a list's value)
        for i in ordered:
            key = keys[i]
            if key + ".0" in present or key in _ACTIVITY_COLUMNS:       # a list's count column is never a parameter
                continue
            if not key.startswith("parameters."):
                self.said.append((i, key, (), False))
                continue
            parts = key.split(".")
            at = max((n for n, p in enumerate(parts) if p in _VALUE_WORDS), default=-1)
            if at < 0 or (parts[at] == "multiValue" and at == len(parts) - 1):
                continue
            prefixes = tuple(".".join(parts[:n]) for n in range(2, at + 1))
            if prefixes:
                self.said.append((i, None, prefixes, parts[at] == "multiValue"))

    def parameters(self, record: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
        values = list(record.values())
        names = {prefix: ("" if values[i] is None else str(values[i])) for i, prefix in self.names}
        out: List[Tuple[str, str]] = []
        for i, simple, prefixes, is_list in self.said:
            value = values[i]
            text = "" if value is None else str(value)
            if not text:
                continue
            if simple is not None:
                out.append((simple, text))
                continue
            chain = [names[p] for p in prefixes if p in names and names[p]]
            if not chain:
                continue
            name = ".".join(chain)
            if is_list and out and out[-1][0] == name:
                out[-1] = (name, f"{out[-1][1]} {text}")
            else:
                out.append((name, text))
        return tuple(dict.fromkeys(out))     # an event GAM wrote both simply and nested says each once


@dataclass(frozen=True)
class Row:
    """One event, in the one shape. Whichever read it came from, ``identity`` is the event itself: two reads of one log merge on it."""

    identity: str
    application: str
    time: str
    date: Optional[datetime]
    who: str
    event: str
    ip: str
    parameters: Tuple[Tuple[str, str], ...]
    flagged: bool       # Google itself raised it: a suspicious sign-in, a failed challenge, a denial, a block, a disabled account
    agent: str = ""     # the AI agent that acted, in words
    app: str = ""       # the app that acted for the person: its name, else its OAuth client ID, and whether it impersonated them

    def value(self, name: str) -> str:
        return next((v for n, v in self.parameters if n == name), "")

    @property
    def via(self) -> str:
        """Who acted besides the person, in words: "by the AI agent …", "through Mail Sync Pro"; empty when they acted themselves."""
        return "; ".join(w for w in (f"by {self.agent}" if self.agent else "", f"through {self.app}" if self.app else "") if w)

    @property
    def words(self) -> str:
        """The event in words: "Signed in", "Google flagged a suspicious sign-in", "Granted an app access"."""
        pair = (self.application, self.event)
        if pair == ("saml", "login_success"):
            return "Signed in to an app through Google"
        if pair == ("saml", "login_failure"):
            return "A sign-in to an app through Google failed"
        if pair == ("chat", "message_posted"):
            return "Posted a Chat message"
        if pair == ("geminiinworkspaceapps", "feature_utilization"):
            return "Used Gemini"
        if pair == ("mobile", "DEVICE_REGISTER_UNREGISTER_EVENT"):
            return "Unregistered a device" if self.value("ACCOUNT_STATE") == "UNREGISTERED" else "Registered a device"
        if pair == ("mobile", "DEVICE_SYNC_EVENT"):
            return "A device synced"
        if pair == ("gmail", "delivery"):
            return gmail_words(self.value("event_info.mail_event_type"))
        return _EVENT_WORDS.get(self.event) or _sentence(self.event)

    @property
    def details(self) -> List[str]:
        """What else the event says, in words: how they signed in and the challenge, the app and its access, or its first few parameters."""
        return self._said() + ([self.via] if self.via else [])

    def _said(self) -> List[str]:
        v = self.value
        if self.application == "login":
            said = []
            if v("login_type"):
                said.append(f"by {_LOGIN_TYPES.get(v('login_type'), v('login_type').replace('_', ' '))}")
            if v("login_challenge_method"):
                said.append(f"challenge: {v('login_challenge_method').replace('_', ' ')}")
            if v("login_challenge_status"):
                said.append(v("login_challenge_status").lower())
            if v("login_failure_type"):
                said.append(v("login_failure_type").replace("login_failure_", "").replace("_", " "))
            if _flag(v("is_suspicious")):
                said.append("Google marked it suspicious")
            if self.event == "blocked_sender" and v("affected_email_address"):
                said.append(f"blocked: {v('affected_email_address')}")
            return said
        if self.application == "token":
            said = [v("app_name") or (f"app {v('client_id')}" if v("client_id") else "")]
            if v("scope"):
                said.append(f"access: {_scopes(v('scope'))}")
            return [s for s in said if s]
        if self.application == "chrome":
            said = []
            if v("URL"):
                said.append(v("URL"))
            if v("LOGIN_USER_NAME"):
                said.append(f"as {v('LOGIN_USER_NAME')}")
            device, platform = v("DEVICE_NAME"), v("DEVICE_PLATFORM")
            if device or platform:
                said.append("on " + " ".join(w for w in (device, platform if not device else f"({platform})" if platform else "") if w))
            result, reason = v("EVENT_RESULT"), v("EVENT_REASON")
            if result:
                said.append("warned, and went on anyway" if result == "BYPASSED" else _sentence(result).lower())
            if reason and reason != "EVENT_REASON_UNSPECIFIED":
                said.append(_CHROME_REASONS.get(reason) or _sentence(reason).lower())
            return said
        if self.application == "mobile":
            # "Windows computer (Virtual Machine, Windows 10.0.19045)", its warnings in words, then what else it says, never its identifiers.
            about = [w for w in (v("DEVICE_MODEL"), v("OS_VERSION")) if w]
            kind = v("DEVICE_TYPE")
            device = ", ".join(about) if not kind else _DEVICE_KINDS.get(kind.upper(), kind) + (f" ({', '.join(about)})" if about else "")
            warnings = [_sentence(n[7:]).lower() for n, value in self.parameters if n.startswith(_MOBILE_WARNINGS) and _flag(value)]
            rest = [f"{n}: {value}" for n, value in self.parameters
                    if n not in _MOBILE_HIDDEN and not n.startswith(_MOBILE_WARNINGS) and not n.startswith("resource")][:4]
            return ([device] if device else []) + warnings + rest
        if self.application == "admin":     # whose account it was done to first: the admin who did it is the row's person
            target = v("USER_EMAIL")
            return ([f"to {target}"] if target else []) + [f"{n}: {value}" for n, value in self.parameters if n not in ("type", "USER_EMAIL")][:3]
        return [f"{n}: {value}" for n, value in self.parameters if n not in ("type", "is_suspicious", "event_info.mail_event_type")][:4]


def gmail_words(kind: str) -> str:
    try:
        return _GMAIL_WORDS[int(kind)]
    except (KeyError, ValueError):
        return f"A Gmail event (type {kind})" if kind else "A Gmail event"


def _row(record: Mapping[str, Any], application: str, columns: _Columns) -> Row:
    time = _text(record, "id.time")
    event = _text(record, "name")
    parameters = columns.parameters(record)
    value = dict(reversed(parameters))
    qualifier = _text(record, "id.uniqueQualifier")
    identity = "|".join([qualifier, time, event, *(f"{n}={v}" for n, v in parameters)])
    actor = _text(record, "actor.email")
    if application == "chrome":     # the account signed in to the profile, else the actor, else the device's user
        who = value.get("PROFILE_USER_NAME") or actor or value.get("DEVICE_USER", "")
    elif application == "login":    # Google's own account warnings have no actor: the account they're about is this
        who = actor or value.get("affected_email_address", "")
    else:
        who = actor
    lowered = event.lower()
    flagged = (event in _FLAGGED_EVENTS or any(w in lowered for w in _FLAG_WORDS) or _flag(record.get("is_suspicious"))
               or (event == "login_challenge" and "fail" in _text(record, "login_challenge_status").lower()))
    agent_name, agent_type = _text(record, "actor.agentAttributionInfo.agentName"), _text(record, "actor.agentAttributionInfo.agentType")
    owner = _text(record, "actor.agentAttributionInfo.agentOwner.email")
    agent = ""
    if agent_name or agent_type or _flag(record.get("isAgenticAction")):
        agent = (f"the AI agent {agent_name}" if agent_name else "an AI agent") + (f" ({agent_type.lower().replace('_', ' ')})" if agent_type else "") \
            + (f", made by {owner}" if owner else "")
    app_name, client = _text(record, "actor.applicationInfo.applicationName"), _text(record, "actor.applicationInfo.oauthClientId")
    app = ""
    if (app_name and not (application == "mobile" and app_name == "Device")) or client:     # the Devices log names the device itself "Device"
        app = (app_name if app_name and not (application == "mobile" and app_name == "Device") else f"app {client}") \
            + (" (acting as the person)" if _flag(record.get("actor.applicationInfo.impersonation")) else "")
    return Row(identity, application, time, parse_time(time), who, event, _text(record, "ipAddress"), parameters, flagged, agent, app)


def rows_from(stdout: str, application: str) -> List[Row]:
    """A report's rows, newest first as GAM sorted them, each read by its columns, worked out once while the rows share them."""
    columns: Optional[_Columns] = None
    rows = []
    for record in parse_records(stdout, json_column=False):
        keys = tuple(record)
        if columns is None or columns.keys != keys:
            columns = _Columns(keys)
        rows.append(_row(record, application, columns))
    return rows


_PARAMETER_NAME = re.compile(r"[\w.-]+")


def parameters_from_cell(cell: str) -> Tuple[Tuple[str, str], ...]:
    """The parameters cell back into parameters: `name: value` pieces joined by " | ". A piece that isn't `name: value` is the rest of the value before it, which held " | " (a subject can)."""
    if not cell:
        return ()
    out: List[Tuple[str, List[str]]] = []
    for piece in cell.split(" | "):
        name, colon, value = piece.partition(": ")
        if colon and _PARAMETER_NAME.fullmatch(name):
            out.append((name, [value]))
        elif out:
            out[-1][1].append(piece)
    return tuple((name, " | ".join(pieces)) for name, pieces in out)


def _unsafe(cell: str) -> str:
    """A cell as it was before the export made it safe to open in a spreadsheet: a `'` before a formula's lead. A value that itself began with `'` and one of those reads back without it: the
    export doesn't say which it was."""
    return cell[1:] if len(cell) > 1 and cell[0] == "'" and cell[1] in "=+-@\t\r" else cell


def rows_from_export(text: str, application: str, limit: int = EXPORT_ROWS) -> Optional[List[Row]]:
    """A log's rows read back from this app's export of it, in the file's order, or None when the text isn't one (its header isn't ours, or a row names another log) or holds more than ``limit``.
    The export's own dialect, not GAM's, and a cell as long as GAM's read let it be. Its identity is its content, the export keeping no unique qualifier: one event saved by two exports counts once."""
    reader = csv.reader(io.StringIO(text, newline=""))
    rows: List[Row] = []
    with field_limit_at_least(len(text) + 1):
        try:
            if next(reader) != list(TABLE_HEADER):
                return None
            for raw in reader:
                if not raw:
                    continue
                if len(raw) != len(TABLE_HEADER) or raw[0] != application or len(rows) >= limit:
                    return None
                _, time, who, event, ip, flagged, via, cell = (_unsafe(c) for c in raw)
                agent, app = "", ""
                if via.startswith("by "):
                    agent, _, app = via[3:].partition("; through ")
                elif via.startswith("through "):
                    app = via[8:]
                parameters = parameters_from_cell(cell)
                identity = "|".join([application, time, who, event, ip, *(f"{n}={v}" for n, v in parameters)])
                rows.append(Row(identity, application, time, parse_time(time), who, event, ip, parameters, flagged == "yes", agent, app))
        except (csv.Error, StopIteration):
            return None
    return rows


def newest_first(rows: Iterable[Row]) -> List[Row]:
    """By time, newest first, keeping GAM's order among the same second. One with no time goes last."""
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(rows, key=lambda r: r.date or earliest, reverse=True)


def capped(rows: Iterable[Row], accounts: Sequence[str], cap: int = CAP) -> bool:
    """Some account's events reached the cap, so its older ones weren't read: counted per account (none given: the whole read is one). A row whose address isn't one
    asked (an old name, or none at all) could be any of theirs, so it counts toward each: an extra read, never a missed one."""
    listed = list(rows)
    if not accounts:
        return len(listed) >= cap
    asked = {a.lower() for a in accounts}
    counts: Dict[str, int] = {}
    stray = 0
    for row in listed:
        if row.who.lower() in asked:
            counts[row.who.lower()] = counts.get(row.who.lower(), 0) + 1
        else:
            stray += 1
    return stray >= cap or any(n + stray >= cap for n in counts.values())


def addresses(rows: Iterable[Row]) -> List[str]:
    """The addresses to look for elsewhere: each IP in ``rows``, those on a flagged event first, then by how often."""
    seen: Dict[str, List[Any]] = {}
    for i, row in enumerate(rows):
        if row.ip:
            entry = seen.setdefault(row.ip, [0, False, i])
            entry[0] += 1
            entry[1] = entry[1] or row.flagged
    return sorted(seen, key=lambda ip: (not seen[ip][1], -seen[ip][0], seen[ip][2]))


def table(rows: Iterable[Row]) -> List[List[str]]:
    """The rows as a table in the one shape, header first, for a CSV the caller makes safe to open in a spreadsheet."""
    return [list(TABLE_HEADER)] + [[r.application, r.time, r.who, r.event, r.ip, "yes" if r.flagged else "", r.via,
                                    " | ".join(f"{n}: {v}" for n, v in r.parameters)] for r in rows]


_ACCOUNT_SPLIT = re.compile(r"[,\s]+")


def accounts_typed(typed: str) -> List[str]:
    """The accounts as typed, split on commas, spaces and new lines, each once (case aside), as GAM reads a list."""
    seen = set()
    out = []
    for word in _ACCOUNT_SPLIT.split(typed or ""):
        if word and word.lower() not in seen:
            seen.add(word.lower())
            out.append(word)
    return out


def window_words(hours: int) -> str:
    """"12 hours", or "3 days" from two days up."""
    return f"{hours} hours" if hours < 48 else f"{hours // 24} days"
