"""Where a person's account is in use: their Chrome profiles, the phones and tablets syncing it, and the computers signed
in with it. Reads only, and pure: the reads are on ``GAMConnector``. Each answers "where is this account signed in?",
for a person's Security tab and an incident's check. It never shows what a profile holds: Google gives no one a person's
saved passwords. Ported from SwiftGamGui's ``GamEngine/ChromeProfiles.swift`` and ``Compromised.swift``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .gam.errors import GAMError, GAMErrorKind

_NEVER = datetime.min.replace(tzinfo=timezone.utc)

_PLATFORMS = {"MAC": "macOS", "WINDOWS": "Windows", "LINUX": "Linux", "CHROME_OS": "ChromeOS", "ANDROID": "Android", "IOS": "iOS"}
_PHONE_KINDS = {"ANDROID": "Android", "IOS_SYNC": "iPhone or iPad (Google's app)", "GOOGLE_SYNC": "Exchange sync (Google Sync)"}
_COMPUTER_KINDS = {"WINDOWS": "Windows", "MAC_OS": "macOS", "LINUX": "Linux", "CHROME_OS": "ChromeOS"}
_OWNERSHIP = {"COMPANY": "the company's", "BYOD": "personal"}
_NOT_SIGNED_IN = {"BLOCKED", "WIPING", "WIPED", "UNENROLLED"}
_STATES = {"": "", "APPROVED": "", "BLOCKED": "blocked", "PENDING": "waiting for approval", "PENDING_APPROVAL": "waiting for approval",
           "WIPING": "the account being wiped", "WIPED": "the account wiped", "UNENROLLED": "unenrolled",
           "UNPROVISIONED": "not provisioned"}
# A model or maker that says virtual machine, as a remote attacker's often is.
_VIRTUAL_WORDS = ("virtual", "vmware", "virtualbox", "innotek", "parallels", "qemu", "kvm", "xen", "hvm domu",
                  "google compute engine", "amazon ec2", "bochs")

CHROME_SCOPE_NOTE = ("This domain's GAM authorization needs the “Chrome Management API – Profiles” scope (read-only is enough): "
                     "authorize again from Setup's fresh setup with that scope ticked.")
DELEGATION_NOTE = ("Listing computers needs the service account's domain-wide delegation to include "
                   "https://www.googleapis.com/auth/cloud-identity.devices, or its .readonly form if this domain's service "
                   "account was set up read-only for Cloud Identity Devices (Admin console: Security › Access and data control "
                   "› API controls › Domain-wide delegation).")
SCOPE_LIST_NOTE = ("This domain's service-account file carries GAM's own list of APIs, and Cloud Identity Devices isn't on it, "
                   "so GAM never asks Google for the scope: delegating it alone won't help. Set the service account up again "
                   "with Cloud Identity Devices among its APIs (or with no list, so GAM uses its defaults), delegate the scope, "
                   "and import the folder again on Setup.")


def _text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    return "" if value is None else str(value)


def _when(text: str) -> Optional[datetime]:
    """GAM's time with or without fractional seconds; None when it doesn't read as one."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _shown(text: str) -> str:
    """A time as a person reads it, to the minute, in the zone Google gave it."""
    return text[:16].replace("T", " ") if _when(text) else text


@dataclass(frozen=True)
class ChromeProfile:
    """One Chrome profile signed in with the account: where it is, which Chrome, and when it was last used."""

    display_name: str   # the name Chrome shows, as the person set it
    device: str         # the computer's name, or its model where the name isn't reported
    system: str         # "macOS 15.6"
    browser: str        # "141.0.7390.66 (stable)"
    last_used: str
    first_seen: str
    on_managed_device: Optional[bool]    # None when Google doesn't say

    @property
    def where(self) -> str:
        return " on ".join(part for part in (self.display_name or "Chrome profile", self.device) if part)


def _chrome_profile(record: Mapping[str, Any]) -> ChromeProfile:
    info = record.get("deviceInfo")
    info = info if isinstance(info, dict) else {}
    platform = _text(record, "osPlatformType")
    version, channel = _text(record, "browserVersion"), _text(record, "browserChannel")
    affiliation = _text(record, "affiliationState")
    return ChromeProfile(
        display_name=_text(record, "displayName"),
        device=_text(info, "hostname") or _text(info, "machine"),
        system=" ".join(part for part in (_PLATFORMS.get(platform, platform), _text(record, "osVersion")) if part),
        browser=f"{version} ({channel.lower()})" if channel else version,
        last_used=_shown(_text(record, "lastActivityTime")),
        first_seen=_shown(_text(record, "firstEnrollmentTime")),
        on_managed_device=affiliation.startswith("AFFILIATED") if affiliation else None,
    )


def chrome_profiles_from(records: Iterable[Mapping[str, Any]], owner: str) -> List[ChromeProfile]:
    """``owner``'s profiles, the most recently used first; ones with no date last, in GAM's order. One naming someone else
    is left out, as on the other two reads: only a live read can show how exactly Google's filter matches."""
    keyed: List[Tuple[datetime, ChromeProfile]] = [
        (_when(_text(record, "lastActivityTime")) or _NEVER, _chrome_profile(record)) for record in records
        if _text(record, "userEmail").lower() in ("", owner.lower())]
    return [profile for _, profile in sorted(keyed, key=lambda pair: pair[0], reverse=True)]


def chrome_note(error: GAMError) -> str:
    """What to do when GAM's authorization lacks the scope: only for that failure (exit 15, no Chrome Management scope at
    all, or a scope refusal), never for one a new scope can't fix."""
    return CHROME_SCOPE_NOTE if error.exit_code == 15 or error.kind is GAMErrorKind.SCOPE_MISSING else ""


@dataclass(frozen=True)
class MobileDevice:
    """One phone or tablet syncing the account: what it is, how it syncs, and when it first and last did."""

    model: str
    os: str
    kind: str     # how it syncs, in words
    status: str
    first_synced: str
    last_synced: str
    reports_compromise: bool      # it says it's rooted or jailbroken

    @property
    def words(self) -> str:
        """"Pixel 8, Android 15 (Android)"."""
        said = ", ".join(part for part in (self.model or "A device", self.os) if part)
        return f"{said} ({self.kind})" if self.kind else said


def mobile_devices_from(records: Iterable[Mapping[str, Any]], owner: str) -> List[MobileDevice]:
    """The devices that list ``owner`` exactly: the API's ``email:`` search matches a prefix, so carol@example.com.au's
    phone answers carol@example.com."""
    wanted, devices = owner.lower(), []
    for record in records:
        owners = record.get("email")
        if not isinstance(owners, list) or wanted not in {str(o).lower() for o in owners}:
            continue
        kind = _text(record, "type")
        compromise = _text(record, "deviceCompromisedStatus").lower()
        devices.append(MobileDevice(
            model=_text(record, "model"),
            os=_text(record, "os"),
            kind=_PHONE_KINDS.get(kind, kind),
            status=_text(record, "status").lower(),
            first_synced=_shown(_text(record, "firstSync")),
            last_synced=_shown(_text(record, "lastSync")),
            reports_compromise="compromise detected" in compromise and not compromise.startswith("no "),
        ))
    return devices


@dataclass(frozen=True)
class Computer:
    """One computer signed in with the account, which the phones list never shows. An attacker's sign-in can leave one
    here, often a virtual machine."""

    kind: str     # Windows, macOS, Linux, ChromeOS
    model: str
    manufacturer: str
    os: str
    hostname: str
    serial_number: str
    ownership: str         # "the company's", "personal", or "" when Google doesn't say
    state: str             # the person's state on it in words, "" when approved
    is_active: bool        # still signed in: a blocked one stays listed until deleted
    first_signed_in: str
    last_synced: str
    reports_compromise: bool

    @property
    def looks_virtual(self) -> bool:
        said = f"{self.model} {self.manufacturer}".lower()
        return any(word in said for word in _VIRTUAL_WORDS)

    @property
    def words(self) -> str:
        """"Virtual Machine, Windows 10.0.19045 (Windows), named DESKTOP-7Q2"."""
        said = ", ".join(part for part in (self.model or "A computer", self.os) if part)
        return said + (f" ({self.kind})" if self.kind else "") + (f", named {self.hostname}" if self.hostname else "")


def _computer(record: Mapping[str, Any], owner: str) -> Optional[Computer]:
    """The computer in ``record``, when it is one and ``owner`` is exactly among its users: the API's ``email:`` matches a
    prefix, and a phone or tablet is the phones list's."""
    kind = _COMPUTER_KINDS.get(_text(record, "deviceType"), "")
    users = record.get("users")
    mine: Optional[Dict[str, Any]] = None
    for user in users if isinstance(users, list) else []:
        if isinstance(user, dict) and _text(user, "userEmail").lower() == owner.lower():
            mine = user
            break
    if not kind or mine is None:
        return None
    state = _text(mine, "managementState") or _text(record, "managementState")
    return Computer(
        kind=kind,
        model=_text(record, "model"),
        manufacturer=_text(record, "manufacturer"),
        os=_text(record, "osVersion"),
        hostname=_text(record, "hostname"),
        serial_number=_text(record, "serialNumber"),
        ownership=_OWNERSHIP.get(_text(record, "ownerType"), ""),
        state=_STATES.get(state, state.lower()),
        is_active=state not in _NOT_SIGNED_IN,
        # Their own createTime: Google moves firstSyncTime at each registration.
        first_signed_in=_shown(_text(mine, "createTime") or _text(mine, "firstSyncTime")),
        last_synced=_shown(_text(mine, "lastSyncTime") or _text(record, "lastSyncTime")),
        reports_compromise=_text(record, "compromisedState") == "COMPROMISED",
    )


def computers_from(records: Iterable[Mapping[str, Any]], owner: str) -> List[Computer]:
    return [computer for computer in (_computer(record, owner) for record in records) if computer is not None]


def computer_note(error: GAMError) -> str:
    """How to grant the devices scope, by the cause; "" when no scope explains the failure. A dead key's token error is
    exit 12 too, but never GAM's own "API access Denied" of the scope list."""
    if error.exit_code == 12:
        said = error.stderr or ""
        return SCOPE_LIST_NOTE if "API access Denied" in said and "Authentication Token Error" not in said else ""
    return DELEGATION_NOTE if error.exit_code == 73 or error.kind is GAMErrorKind.SCOPE_MISSING else ""
