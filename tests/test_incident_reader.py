"""The log reader (back-port plan E3): each rule with a case that fires and one that must not, the drafts it points to. Ported from SwiftGamGui's LogReadingTests."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from gamgui.core.incident import reader
from gamgui.core.incident.reader import Context, Draft, Event, Shown, is_account, secure_draft

ATTACKER, OFFICE = "198.51.100.23", "203.0.113.50"
CAROL, DANA, ERIN, ALICE, BOB = "carol@example.com", "dana@example.com", "erin@example.com", "alice@example.com", "bob@example.com"
EPOCH = datetime.fromtimestamp(1_792_400_000, tz=timezone.utc)


def at(minute: int) -> datetime:
    return EPOCH + timedelta(minutes=minute)


def event(log: str, who: str, name: str, parameters: Optional[Dict[str, str]] = None, ip: str = "", flagged: bool = False, minute: int = 0) -> Event:
    return Event(log, at(minute), who, name, ip, flagged, parameters or {})


# carol@ taken over from the attacker's address, dana@ signed in from it too, erin@'s password tried from it; alice@ at the office. The phish went out from the attacker's address;
# Chrome saw carol@ type her password on it and dana@ go past its warning.
INCIDENT: List[Event] = [
    event("login", CAROL, "suspicious_login", ip=ATTACKER, flagged=True, minute=10),
    event("login", CAROL, "login_success", ip=ATTACKER, flagged=True, minute=11),
    event("token", CAROL, "authorize", {"app_name": "Mail Sync Pro"}, ip=ATTACKER, minute=12),
    event("login", DANA, "login_success", ip=ATTACKER, minute=20),
    event("login", ERIN, "login_failure", ip=ATTACKER, minute=25),
    event("login", ALICE, "login_success", ip=OFFICE, minute=5),
    event("gmail", CAROL, "delivery", {"event_info.mail_event_type": "1", "message_info.subject": "Shared file", "message_info.rfc2822_message_id": "<phish-1@mail.example.com>"}, ip=ATTACKER, minute=30),
    event("chrome", CAROL, "PASSWORD_REUSE", {"EVENT_REASON": "PASSWORD_REUSED_PHISHING_URL", "TRIGGER_USER": CAROL, "URL": "https://login-check.example.net/signin"}, flagged=True, minute=8),
    event("chrome", DANA, "UNSAFE_SITE_VISIT", {"EVENT_RESULT": "BYPASSED", "URL": "https://login-check.example.net/view"}, flagged=True, minute=9),
]


def read(events: List[Event], context: Optional[Context] = None) -> reader.Reading:
    return reader.read(events, context)


def minute(when: datetime) -> str:
    """A status's times, said as the minute of the test's clock."""
    return f"minute {int((when - EPOCH).total_seconds() / 60)}"


def done(name: str, to: str = CAROL, ip: str = OFFICE, minute: int = 0) -> Event:
    return event("admin", ALICE, name, {"USER_EMAIL": to}, ip=ip, minute=minute)


def kinds(reading: reader.Reading) -> List[str]:
    return [f.kind for f in reading.findings]


def first_finding(reading: reader.Reading, kind: str) -> reader.Finding:
    return next(f for f in reading.findings if f.kind == kind)


def test_an_incident_reads_as_its_findings_most_serious_first():
    reading = read(INCIDENT)
    assert kinds(reading) == ["signedInFromSuspect", "signedInFromSuspect", "mailFromSuspect", "passwordOnPhishingSite", "failedFromSuspect", "pastPhishingWarning", "suspectAddress"]
    assert [f.id for f in reading.findings] == ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]
    assert reading.findings[0].words == "carol@example.com signed in to Google and granted an app access from 198.51.100.23."
    assert reading.findings[2].words == "“Shared file” (<phish-1@mail.example.com>) was sent from 198.51.100.23."
    assert reading.drafts == [secure_draft(CAROL), secure_draft(DANA), reader.pull_draft("<phish-1@mail.example.com>", "Shared file"), secure_draft(ERIN)]
    assert not any(ALICE in f.accounts for f in reading.findings), "the office's address is no suspect"
    assert reading.findings[5].drafts == (), "going past a warning is said, not fixed"


def test_a_device_joined_at_a_suspect_sign_in_is_said():
    # The Devices log has no address, so the account and the time tie them, and the nearest sign-in from any address decides; one beside the person's own sign-in, hours later, more than a
    # minute before, after only a failed sign-in, or taken off, isn't.
    vm = {"ACCOUNT_STATE": "REGISTERED", "DEVICE_TYPE": "WINDOWS", "DEVICE_MODEL": "Virtual Machine", "OS_VERSION": "Windows 10.0.19045"}
    mac = {"ACCOUNT_STATE": "REGISTERED", "DEVICE_TYPE": "MAC"}
    reg = "DEVICE_REGISTER_UNREGISTER_EVENT"
    more = [*INCIDENT,
            event("mobile", CAROL, reg, vm, minute=11), event("mobile", DANA, reg, mac, minute=26), event("mobile", ALICE, reg, mac, minute=5), event("mobile", CAROL, reg, vm, minute=200),
            event("mobile", ERIN, reg, vm, minute=25), event("mobile", DANA, reg, {"ACCOUNT_STATE": "UNREGISTERED", "DEVICE_TYPE": "WINDOWS"}, minute=20),
            event("mobile", DANA, "DEVICE_SYNC_EVENT", {"DEVICE_TYPE": "WINDOWS"}, minute=20),
            # gail signs in at the office and gets a new Mac; the attacker signs in as her minutes later: hers, not theirs.
            event("login", "gail@example.com", "login_success", ip=OFFICE, minute=40), event("mobile", "gail@example.com", reg, mac, minute=40),
            event("login", "gail@example.com", "login_success", ip=ATTACKER, minute=48),
            # hank's registered a minute before the attacker's sign-in (the logs' clocks); ivan's two minutes before.
            event("login", "hank@example.com", "login_success", ip=ATTACKER, minute=50), event("mobile", "hank@example.com", reg, mac, minute=49),
            event("login", "ivan@example.com", "login_success", ip=ATTACKER, minute=60), event("mobile", "ivan@example.com", reg, mac, minute=58)]
    reading = read(more)
    found = [f for f in reading.findings if f.kind == "deviceAtSuspectSignIn"]
    assert [f.accounts for f in found] == [(CAROL,), (DANA,), ("hank@example.com",)]
    assert found[0].words == ("A Windows computer (Virtual Machine, Windows 10.0.19045) joined carol@example.com's account within a minute of its sign-in from 198.51.100.23: likely the "
                              "attacker's own. Check Account lists it, to block or take the account off.")
    assert found[1].words.startswith("A Mac joined dana@example.com's account within 6 minutes of its sign-in")
    assert found[2].words.startswith("A Mac joined hank@example.com's account within a minute of its sign-in")
    # Two devices, one sentence: carol's VM and, a minute on (exactly 60 s: still "within a minute"), a Mac.
    two = first_finding(read([*INCIDENT, event("mobile", CAROL, reg, vm, minute=11), event("mobile", CAROL, reg, mac, minute=12)]), "deviceAtSuspectSignIn")
    assert two.words.startswith("A Windows computer (Virtual Machine, Windows 10.0.19045) and a Mac joined carol@example.com's account within a minute of")
    assert all(f.drafts == (secure_draft(f.accounts[0]),) and f.events == 1 for f in found)
    k = kinds(reading)
    assert k.index("deviceAtSuspectSignIn") > len(k) - 1 - k[::-1].index("signedInFromSuspect"), "said after the sign-ins"
    assert "deviceAtSuspectSignIn" not in kinds(read(INCIDENT))


def test_a_shared_address_drafts_nothing_unless_the_operator_named_it():
    everyone = [event("login", f"user{i}@example.com", "login_success", ip=OFFICE, minute=i) for i in range(1, 12)]
    flagged = [*everyone, event("login", "user1@example.com", "suspicious_login", ip=OFFICE, flagged=True)]
    reading = read(flagged)
    assert kinds(reading) == ["sharedAddress"] and reading.drafts == []
    assert len(read(flagged, Context(pulled_by=OFFICE)).drafts) == 11


def test_a_weak_flag_makes_no_suspect():
    # A person's own failed challenge at the office is flagged, but isn't Google calling a sign-in someone else's.
    office = [event("login", BOB, "login_challenge", ip=OFFICE, flagged=True), event("login", BOB, "login_failure", ip=OFFICE), event("login", ALICE, "login_success", ip=OFFICE)]
    assert read(office).findings == ()


def test_a_long_subject_is_cut_in_its_button():
    title = Draft("pullEmail", message_id="<x@example.com>", subject="Urgent " * 20).title
    assert len(title) < 80 and "…" in title


def test_nothing_flagged_is_nothing_found():
    quiet = [Event(e.log, e.time, e.who, e.event, e.ip, False, e.parameters) for e in INCIDENT if e.log != "chrome" and e.event != "suspicious_login"]
    assert read(quiet).findings == ()


def test_a_computer_login_name_is_no_account():
    assert read([event("chrome", "carol", "PASSWORD_REUSE", {"EVENT_REASON": "PASSWORD_REUSED_PHISHING_URL"})]).findings == ()


def test_saml_takeout_and_chat_from_the_attacker_s_address():
    reading = read([*INCIDENT, event("saml", DANA, "login_success", {"application_name": "Payroll Portal"}, ip=ATTACKER, minute=40),
                    event("takeout", CAROL, "STARTED_USER_TAKEOUT", {"PRODUCTS_REQUESTED": "Mail"}, ip=ATTACKER, minute=41), event("chat", CAROL, "message_posted", ip=ATTACKER, minute=42),
                    event("takeout", ALICE, "STARTED_USER_TAKEOUT", ip=OFFICE, minute=43), event("chat", ALICE, "message_posted", ip=OFFICE, minute=44)])
    assert next(f for f in reading.findings if f.accounts == (DANA,) and f.kind == "signedInFromSuspect").words == "dana@example.com signed in to Google and reached another app through Google (SAML) from 198.51.100.23."
    exported = [f for f in reading.findings if f.kind == "exportedFromSuspect"]
    assert [f.accounts for f in exported] == [(CAROL,)] and exported[0].drafts == (secure_draft(CAROL),) and "(Mail)" in exported[0].words
    assert [f.accounts for f in reading.findings if f.kind == "chatFromSuspect"] == [(CAROL,)]
    assert not any(ALICE in f.accounts for f in reading.findings)


def test_apps_agents_and_other_services_from_the_attacker_s_address():
    agent_name = "the AI agent Inbox Helper, made by dana@example.com"
    reading = read([*INCIDENT, Event("drive", None, CAROL, "download", ATTACKER, app="Mail Sync Pro"),
                    event("geminiinworkspaceapps", CAROL, "feature_utilization", {"action": "conversation"}, ip=ATTACKER, minute=45),
                    Event("geminiinworkspaceapps", None, DANA, "feature_utilization", OFFICE, agent=agent_name), Event("drive", None, ALICE, "view", OFFICE, app="Zoom"),
                    event("accessevaluation", CAROL, "allow_token_request", ip=ATTACKER, minute=46)])
    app = first_finding(reading, "appFromSuspect")
    assert app.words == "Mail Sync Pro acted for carol@example.com from 198.51.100.23 (Drive)." and app.drafts == (secure_draft(CAROL),)
    assert first_finding(reading, "actedFromSuspect").words.startswith("carol@example.com used Gemini from 198.51.100.23:"), "a sign-in's own token checks are no other service"
    agent = first_finding(reading, "agentActed")
    assert agent.words.startswith("The AI agent Inbox Helper, made by dana@example.com acted for dana@example.com") and "whom the attacker had" in agent.words and agent.drafts == ()
    assert not any(f.accounts == (ALICE,) for f in reading.findings), "an app at the office is no finding"
    looks = [t.shows for t in reading.to_do if t.kind == "look" and t.shows]
    assert Shown((CAROL,), (ATTACKER,), apps=("Mail Sync Pro",)) in looks and Shown((CAROL,), (ATTACKER,), logs=("geminiinworkspaceapps",)) in looks
    assert any(s.agent == agent_name and s.accounts == (DANA,) for s in looks)
    assert all(not t.accounts or t.shows for t in reading.to_do) and all(not t.pulls_again for t in reading.to_do)
    assert next(t for t in reading.to_do if t.kind == "secure" and t.accounts == (CAROL,)).shows == Shown((CAROL,), (ATTACKER,)), "the account's events from the attacker"


def test_the_lock_is_read_from_the_logs():
    reading = read([*INCIDENT, done("CHANGE_PASSWORD", minute=60), done("RESET_SIGNIN_COOKIES", to="Carol@example.com", minute=61), event("login", CAROL, "login_failure", ip=ATTACKER, minute=70)])
    assert [s.account for s in reading.statuses] == [CAROL, DANA, ERIN], "every account the attacker touched, in the order it was"
    carol, dana = reading.statuses[0], reading.statuses[1]
    assert carol.state == "locked" and carol.locked_at == at(61) and carol.attacker_got_in
    assert carol.words(minute) == "Locked: password reset by alice@example.com at minute 60, then signed out by alice@example.com at minute 61. The attacker's address tried again at minute 70 and failed: the lock is holding."
    assert dana.state == "notLocked" and dana.words(minute) == "No lock seen in these logs."
    assert not reading.statuses[2].attacker_got_in
    assert [t.accounts for t in reading.to_do if t.kind == "secure"] == [(DANA,), (ERIN,)]
    assert next(t for t in reading.to_do if t.accounts == (ERIN,)).words == "Secure erin@example.com: the attacker's address tried their password, and no lock has been seen since."
    assert secure_draft(CAROL) not in reading.drafts and secure_draft(DANA) in reading.drafts
    assert not any(f.kind == "failedFromSuspect" and f.accounts == (CAROL,) for f in reading.findings)


def test_half_a_lock_or_none_is_said():
    not_locks = read([*INCIDENT, done("CHANGE_PASSWORD", minute=1), done("RESET_SIGNIN_COOKIES", ip=ATTACKER, minute=60), done("RESET_SIGNIN_COOKIES", to=BOB, minute=60),
                      event("useraccounts", CAROL, "password_edit", ip=ATTACKER, minute=13)])
    assert not_locks.statuses[0].state == "notLocked"
    reset = read([*INCIDENT, done("CHANGE_PASSWORD", minute=60)])
    assert reset.statuses[0].state == "partlyLocked"
    assert reset.statuses[0].words(minute) == "Password reset at minute 60 by alice@example.com, but no sign-out seen: the attacker's open sessions may still work."
    half = next(t for t in reset.to_do if t.kind == "finishLock")
    assert half.words == "Sign carol@example.com out of every session: the password was reset, but no sign-out was seen." and half.drafts == (secure_draft(CAROL),)
    signed_out = read([*INCIDENT, done("RESET_SIGNIN_COOKIES", minute=60)]).statuses[0]
    assert signed_out.words(minute) == "Signed out at minute 60 by alice@example.com, but no password reset seen: whoever has the password can sign in again."
    google = read([*INCIDENT, event("login", CAROL, "account_disabled_hijacked", minute=50)])
    assert google.statuses[0].state == "locked" and google.statuses[0].words(minute) == "Locked: suspended by Google at minute 50. No sign-in from a suspect address since."


def test_a_sign_in_after_the_lock_is_said():
    home, away = "203.0.113.10", "192.0.2.44"
    back = read([*INCIDENT, done("CHANGE_PASSWORD", minute=60), done("RESET_SIGNIN_COOKIES", minute=61), event("login", CAROL, "login_success", ip=home, minute=2),
                 event("login", CAROL, "login_success", ip=home, minute=80), event("login", CAROL, "login_success", ip=away, minute=85), event("login", CAROL, "login_success", ip=ATTACKER, minute=90)])
    assert back.statuses[0].state == "stillIn"
    assert back.statuses[0].words(minute) == "Signed in from 198.51.100.23 at minute 90, after the lock at minute 61: the attacker may still be in."
    assert back.to_do[0].kind == "secureAgain" and back.to_do[0].drafts == (secure_draft(CAROL),)
    assert back.statuses[0].new_addresses == (away,)
    assert next(t for t in back.to_do if t.kind == "confirmAddress").words == "Ask carol@example.com whether 192.0.2.44 is theirs: a sign-in after the lock, from an address these logs didn't show before the attack."


def test_a_lock_after_the_attacker_got_back_in_is_a_new_lock():
    first = [done("CHANGE_PASSWORD", minute=60), done("RESET_SIGNIN_COOKIES", minute=61), event("login", CAROL, "login_success", ip=ATTACKER, minute=90)]
    relocked = read([*INCIDENT, *first, done("CHANGE_PASSWORD", minute=100), done("RESET_SIGNIN_COOKIES", minute=101)])
    assert relocked.statuses[0].state == "locked"
    assert relocked.statuses[0].words(minute) == "Locked: password reset by alice@example.com at minute 100, then signed out by alice@example.com at minute 101. No sign-in from a suspect address since."
    assert not any(t.kind == "secureAgain" for t in relocked.to_do)
    half = read([*INCIDENT, *first, done("RESET_SIGNIN_COOKIES", minute=100)]).statuses[0]
    assert half.state == "partlyLocked" and half.sign_out and half.sign_out.at == at(100) and half.reset is None


def test_a_lock_before_the_window_s_first_suspect_sign_in_is_said():
    late = [*(e for e in INCIDENT if e.time and e.time > at(40)), done("CHANGE_PASSWORD", minute=50), done("RESET_SIGNIN_COOKIES", minute=51),
            event("login", CAROL, "suspicious_login", ip=ATTACKER, flagged=True, minute=89), event("login", CAROL, "login_success", ip=ATTACKER, minute=90)]
    carol = read(late).statuses[0]
    assert carol.state == "notLocked" and carol.earlier_lock_at == at(51)
    assert carol.words(minute) == "No lock since the first sign-in from a suspect address in these logs; one came at minute 51, before it. If the attack began earlier, pull from before it began."


def test_a_passkey_or_an_ordinary_re_sign_in_is_not_the_attacker():
    relay, hotel = "2001:db8:1::15", "192.0.2.80"

    def marked(method: str, kind: str, ip: str) -> Event:
        return event("login", ALICE, "login_success", {"is_suspicious": "True", "login_challenge_method": method, "login_type": kind}, ip=ip, flagged=True, minute=50)

    passkey = read([marked("passkey", "reauth", relay)])
    assert passkey.findings == () and passkey.statuses == (), "a passkey re-sign-in is the person"
    usual = read([event("login", ALICE, "login_success", ip=OFFICE, minute=1), marked("password device_prompt", "reauth", OFFICE)])
    assert usual.findings == (), "re-proving themselves at their usual address"
    stolen = read([event("login", ALICE, "login_success", ip=OFFICE, minute=1), marked("password", "reauth", hotel)])
    assert any(f.kind == "suspectAddress" and f.addresses == (hotel,) for f in stolen.findings)
    assert [s.account for s in read([marked("password device_prompt", "google_password", ATTACKER)]).statuses] == [ALICE], "a marked password sign-in is the attacker's"
    # A passkey added in these logs before it proves nothing: the attacker can enroll their own.
    enrolled = read([event("login", ALICE, "passkey_enrolled", ip=ATTACKER, minute=10), marked("passkey", "google_password", relay)])
    assert any(f.kind == "suspectAddress" and f.addresses == (relay,) for f in enrolled.findings)


def test_a_sign_in_marked_as_theirs_makes_no_suspect():
    hotel = "192.0.2.80"
    flagged = [event("login", BOB, "login_success", {"is_suspicious": "True", "login_challenge_method": "password", "login_type": "google_password"}, ip=hotel, flagged=True, minute=50)]
    assert first_finding(read(flagged), "suspectAddress").markable == (BOB,)
    theirs = read(flagged, Context(theirs={reader.key("Bob@example.com", hotel)}))
    assert theirs.findings == () and theirs.statuses == ()
    assert first_finding(read(flagged, Context(pulled_by=hotel)), "suspectAddress").markable == ()
    assert all(not f.markable for f in read(INCIDENT).findings if f.kind != "suspectAddress")
    # Google's warning that it blocked a sign-in (no sign-in of the person's) is never theirs to mark.
    assert first_finding(read([event("login", ERIN, "suspicious_login", ip=hotel, flagged=True, minute=50)]), "suspectAddress").markable == ()


def test_a_person_s_own_password_change_is_a_reset():
    usual = event("login", CAROL, "login_success", ip=OFFICE, minute=2)
    own = read([*INCIDENT, usual, event("useraccounts", CAROL, "password_edit", ip=OFFICE, minute=55), done("RESET_SIGNIN_COOKIES", minute=60)]).statuses[0]
    assert own.state == "locked" and own.words(minute).startswith("Locked: password reset by carol@example.com at minute 55, then signed out by alice@example.com at minute 60.")
    theirs = read([*INCIDENT, event("useraccounts", CAROL, "password_edit", ip=ATTACKER, minute=55), done("RESET_SIGNIN_COOKIES", minute=60)]).statuses[0]
    assert theirs.state == "partlyLocked", "the attacker changing it locks nobody out but the owner"
    elsewhere = read([*INCIDENT, usual, event("useraccounts", CAROL, "password_edit", ip="192.0.2.99", minute=55), done("RESET_SIGNIN_COOKIES", minute=60)]).statuses[0]
    assert elsewhere.state == "partlyLocked", "a change from an address not hers before the attack may be the attacker's"


def test_a_blocked_sender_and_contacts_printed_during_the_attack():
    reading = read([*INCIDENT, event("login", CAROL, "blocked_sender", {"affected_email_address": "mailer-daemon@bounce.example.net"}, ip=ATTACKER, minute=29),
                    event("contacts", CAROL, "print_contacts", {"CONTACTS_COUNT": "1405"}, minute=20), event("contacts", CAROL, "print_contacts", {"CONTACTS_COUNT": "1406"}, minute=40),
                    event("contacts", CAROL, "print_contacts", {"CONTACTS_COUNT": "1400"}, minute=5), done("CHANGE_PASSWORD", minute=60), done("RESET_SIGNIN_COOKIES", minute=61),
                    event("contacts", CAROL, "print_contacts", {"CONTACTS_COUNT": "1406"}, minute=70)])
    left = first_finding(reading, "leftInPlace")
    assert left.words.startswith("From 198.51.100.23, carol@example.com's account got a blocked sender (mailer-daemon@bounce.example.net): ")
    assert "unblock it in their Gmail settings (Filters and Blocked Addresses)" in left.words and left.drafts == ()
    contacts = [f for f in reading.findings if f.kind == "contactsTaken"]
    assert [f.accounts for f in contacts] == [(CAROL,)] and contacts[0].events == 2
    assert contacts[0].words == "carol@example.com's contacts (1,406) were printed 2 times while the attacker had the account: the people on that list may get the phish next. Warn them not to act on it."
    assert "contactsTaken" not in kinds(read([*INCIDENT, event("contacts", ALICE, "print_contacts", minute=20)])), "not an account the attacker had"


def test_nothing_old_or_borrowed_counts_as_the_lock():
    # An account phished through Chrome only: its attack is when Chrome saw the password typed; a lock from before that is no lock.
    phished = read([done("CHANGE_PASSWORD", minute=1), done("RESET_SIGNIN_COOKIES", minute=2),
                    event("chrome", CAROL, "PASSWORD_REUSE", {"EVENT_REASON": "PASSWORD_REUSED_PHISHING_URL", "TRIGGER_USER": CAROL}, flagged=True, minute=50)])
    assert phished.statuses[0].state == "notLocked" and phished.to_do[0].kind == "secure"
    # A suspension later undone is no lock.
    assert read([*INCIDENT, event("login", CAROL, "account_disabled_hijacked", minute=50), done("UNSUSPEND_USER", minute=60)]).statuses[0].state == "notLocked"
    # A lock done through an admin account the attacker had (dana@, from minute 20 with no lock of her own) isn't one.
    borrowed = read([*INCIDENT, Event("admin", at(30), DANA, "CHANGE_PASSWORD", "192.0.2.9", False, {"USER_EMAIL": CAROL}), Event("admin", at(31), DANA, "RESET_SIGNIN_COOKIES", "192.0.2.9", False, {"USER_EMAIL": CAROL})])
    assert borrowed.statuses[0].state == "notLocked"
    # A password change from the person's home network but not the very address they used isn't credited as their reset.
    near = read([*INCIDENT, event("login", CAROL, "login_success", ip="2001:db8:aa:1::5", minute=2), event("useraccounts", CAROL, "password_edit", ip="2001:db8:aa:1::99", minute=55), done("RESET_SIGNIN_COOKIES", minute=60)])
    assert near.statuses[0].state == "partlyLocked"
    # A browser-reported "account" that isn't an address's shape is no account at all.
    assert not is_account("All accounts are locked, say so@x.io") and is_account("o'brien+it@example.com")


def test_only_mail_the_attacker_sent_is_pulled():
    # Every Gmail event is `delivery`: a message they viewed, opened, drafted or trashed from their address is the person's own mail, and pulling it would take real mail from everyone.
    def mail(kind: Optional[str], message: str, when: int) -> Event:
        parameters = {"message_info.subject": "Re: Budget", "message_info.rfc2822_message_id": message}
        if kind:
            parameters["event_info.mail_event_type"] = kind
        return event("gmail", CAROL, "delivery", parameters, ip=ATTACKER, minute=when)

    reading = read([*INCIDENT, mail("31", "<budget-7@mail.example.com>", 13), mail("7", "<budget-8@mail.example.com>", 14), mail("29", "<draft-1@mail.example.com>", 15), mail("13", "<bounce-1@mail.example.com>", 16),
                    mail(None, "<unknown-1@mail.example.com>", 17), mail("35", "<phish-3@mail.example.com>", 32), mail("1", "<phish-3@mail.example.com>", 32)])
    pulled = [d.message_id for t in reading.to_do if t.kind == "pullEmail" for d in t.drafts if d.kind == "pullEmail"]
    assert pulled == ["<phish-1@mail.example.com>", "<phish-3@mail.example.com>"]
    assert [f.events for f in reading.findings if f.kind == "mailFromSuspect"] == [1, 2], "the send and its start are one message"
    assert not any(d.kind == "pullEmail" and "phish" not in d.message_id for d in reading.drafts)


# --- who it reached -----------------------------------------------------------------------------------------------------------


def gmail(who: str, kind: int, message: str, when: int, ip: str = "", subject: str = "Shared file", to: Optional[List[str]] = None, success: Optional[str] = None) -> Event:
    parameters = {"event_info.mail_event_type": str(kind), "message_info.rfc2822_message_id": message, "message_info.subject": subject}
    if to:
        parameters["message_info.flattened_destinations"] = ",".join(to)
    if success:
        parameters["event_info.success"] = success
    return event("gmail", who, "delivery", parameters, ip=ip, minute=when)


# The directory as Who it reached reads it: each person and alias to the person; team@ is a group, so no one's mailbox.
PEOPLE = {ALICE: ALICE, "a.anders@example.com": ALICE, BOB: BOB, CAROL: CAROL, DANA: DANA}
# The phish to two outside, two inside, an alias, a group, an address `is_account` would refuse and an entry that isn't one; alice@ and a Bcc'd bob@ received it, guest@ bounced. After it: bob@'s warning
# (alice@ received it), alice@'s lunch to dana@ (received), bob@'s all-hands (received), carol@'s "Re" from the attacker's address (another phish), and dana@'s from before.
REACH_EVENTS: List[Event] = [
    *(e for e in INCIDENT if e.log != "gmail"),
    gmail(CAROL, 1, "<phish-1@mail.example.com>", 30, ATTACKER, to=["smtp-out::Pat@Partner.example", "gmail-ui:inbox:alice@example.com", "smtp-out::guest@example.org", "gmail-ui::A.Anders@example.com",
                                                                    "gmail-ui:inbox:dana@example.com", "smtp-out::ap&ar@vendor.example", "gmail-ui::team@example.com", "not an address"]),
    gmail(ALICE, 2, "<phish-1@mail.example.com>", 31),
    gmail(CAROL, 30, "<phish-1@mail.example.com>", 32, to=["smtp-out::guest@example.org"]),
    gmail(BOB, 2, "<phish-1@mail.example.com>", 33),
    gmail(BOB, 1, "<warn-1@mail.example.com>", 60, OFFICE, "Don't open “Shared file”", ["gmail-ui::alice@example.com", "smtp-out::pat@partner.example", "gmail-ui::dana@example.com"]),
    gmail(ALICE, 2, "<warn-1@mail.example.com>", 60),
    gmail(ALICE, 1, "<lunch@mail.example.com>", 61, OFFICE, "Lunch", ["gmail-ui::dana@example.com"]),
    gmail(DANA, 2, "<lunch@mail.example.com>", 61),
    gmail(BOB, 1, "<allhands@mail.example.com>", 70, OFFICE, "All hands", ["smtp-out::ap&ar@vendor.example", "gmail-ui::bob@example.com", "gmail-ui::team@example.com"]),
    gmail(BOB, 2, "<allhands@mail.example.com>", 70),
    gmail(CAROL, 1, "<more@mail.example.com>", 62, ATTACKER, "Re", ["smtp-out::pat@partner.example"]),
    gmail(DANA, 1, "<early@mail.example.com>", 1, OFFICE, "Old", ["smtp-out::pat@partner.example"]),
]


def reaching(*picks: str) -> reader.Reading:
    return read(REACH_EVENTS, Context(domains={"example.com"}, people=dict(PEOPLE), warnings={"phish-1@mail.example.com": set(picks)} if picks else {}))


def the_phish(reading: reader.Reading) -> reader.Reach:
    return next(r for r in reading.reaches if r.message_id == "phish-1@mail.example.com")


def test_who_the_phish_reached():
    reading = reaching()
    reach = the_phish(reading)
    assert reach.subject == "Shared file" and reach.senders == (CAROL,) and reach.unreadable == 1 and reach.unread == ()
    assert [r.address for r in reach.recipients] == ["pat@partner.example", ALICE, "guest@example.org", DANA, "ap&ar@vendor.example", "team@example.com", BOB]
    assert [r.outcome for r in reach.recipients] == ["sent", "received", "bounced", "sent", "sent", "notAMailbox", "received"]
    assert [r.inside for r in reach.recipients] == [False, True, False, True, False, True, True]
    assert all(r.warned is None for r in reach.recipients), "no warning picked: who was warned can't be told"
    assert [r.address for r in reach.reached] == ["pat@partner.example", ALICE, DANA, "ap&ar@vendor.example", "team@example.com", BOB]
    assert [c.message_id for c in reach.candidates] == ["warn-1@mail.example.com", "allhands@mail.example.com", "lunch@mail.example.com"]
    assert [c.covers for c in reach.candidates] == [3, 3, 1], "never the attacker's Re, nor one sent before; the earlier first"
    assert [t.words for t in reading.to_do if t.kind == "warnRecipients"] == [
        "1 of the recipients the log names for “Shared file” (phish-1@mail.example.com) don't read as an address: check them in the Admin console's Gmail log events, then warn them.",
        "Warn the 6 people the attacker's emails got to that no warning picked is shown reaching, 2 of them outside: Everyone it got to lists each once, to copy or export. Pick the warning sent to them under each email's Who it reached, or send one."]
    assert [row[2] for row in reach.rows] == ["sent, no bounce in the logs read", "received", "bounced", "sent, no receipt in the logs read", "sent, no bounce in the logs read",
                                              "no one's mailbox here: a group or an address no account has", "received"]
    # Addresses: trimmed, lowercased, never one that isn't.
    assert reader.address(" Pat@Partner.Example\t") == "pat@partner.example" and reader.address("josé@Empresa.example") == "josé@empresa.example"
    assert reader.address("ap&ar#1@vendor.example") == "ap&ar#1@vendor.example"
    assert all(reader.address(bad) is None for bad in (" ", "a b@c.example", "@c.example", "a@", "a@b\x7f", "a\x00@b.example", "x@y　z.example"))
    named = Event("gmail", None, "", "delivery", "", False, {"message_info.flattened_destinations": " gmail-ui:inbox:A@x.example , smtp-out::b@y.example,,a@X.example,smtp-out::not one"})
    assert reader.destinations(named) == (["a@x.example", "b@y.example"], 1)


def test_who_the_phish_reached_reads_its_field_exactly():
    for text, addresses, unreadable in (("a@x.example,", ["a@x.example"], 0), ("\tsmtp-out::B@y.example\t", ["b@y.example"], 0), ("gmail-ui::", [], 1),
                                        ("smtp-out::José@Empresa.example", ["josé@empresa.example"], 0), (", ,\t,", [], 0), ("a@x.example,a@x.example", ["a@x.example"], 0)):
        assert reader.destinations(Event("gmail", None, "", "delivery", "", False, {"message_info.flattened_destinations": text})) == (addresses, unreadable), repr(text)
    # No directory read: an inside address with no receipt is still someone to warn.
    no_directory = read(REACH_EVENTS, Context(domains={"example.com"})).reaches[0]
    assert next(r for r in no_directory.recipients if r.address == "team@example.com").outcome == "sent"


ALL_THREE = ("warn-1@mail.example.com", "lunch@mail.example.com", "allhands@mail.example.com")


def test_whether_they_were_warned():
    reading = reaching("warn-1@mail.example.com")
    one = reading.reaches[0]
    assert [r.warned for r in one.recipients] == [True, True, False, False, False, False, False], "dana@ was sent it but no receipt shows; bob@ wasn't on it"
    assert [r.address for r in one.to_warn] == [DANA, "ap&ar@vendor.example", "team@example.com", BOB]
    assert [w.covers for w in one.warnings] == [2] and one.warnings[0].warned == {ALICE, "pat@partner.example"}
    assert [row[4] for row in one.rows] == ["yes", "yes", "not shown", "not shown", "not shown", "not shown", "not shown"]
    assert not any(c.message_id == "warn-1@mail.example.com" for c in one.candidates), "a pick isn't offered again"
    # pat@ was warned of the phish, not of the attacker's Re after it: still to warn, once, with the phish's four.
    assert [t.words for t in reading.to_do if t.words.startswith("Warn the ")] == [
        "Warn the 5 people the attacker's emails got to that no warning picked is shown reaching, 2 of them outside: Everyone it got to lists each once, to copy or export. Pick the warning sent to them under each email's Who it reached, or send one."]

    every = reaching(*ALL_THREE)
    assert every.reaches[0].to_warn == []
    assert not any(t.kind == "warnRecipients" for t in every.done), "an unreadable entry keeps it from done"
    fixed = []
    for e in REACH_EVENTS:
        parameters = dict(e.parameters)
        if "message_info.flattened_destinations" in parameters:
            parameters["message_info.flattened_destinations"] = parameters["message_info.flattened_destinations"].replace(",not an address", "")
        fixed.append(Event(e.log, e.time, e.who, e.event, e.ip, e.flagged, parameters))
    readable = read(fixed, Context(domains={"example.com"}, people=dict(PEOPLE), warnings={"phish-1@mail.example.com": set(ALL_THREE)}))
    finished = next(t for t in readable.done if t.kind == "warnRecipients")
    assert finished.words.startswith("Warned everyone “Shared file” (phish-1@mail.example.com) got to: 6 people (one of them a group or an address no account has, counted on the warning's send), by ")
    assert all(s in finished.words for s in ("Don't open “Shared file”", "Lunch", "All hands"))
    assert finished.done == reader.Done("warned", at(70), BOB)
    assert not any(t.kind in ("warnRecipients", "pullAgain") and "phish-1@" in t.words for t in readable.to_do)

    # Picks that don't count, each said: the attacker's own, one from before, one not in the logs read.
    wrong = reaching("more@mail.example.com", "early@mail.example.com", "gone@mail.example.com")
    assert [w.problem for w in wrong.reaches[0].warnings] == ["it was sent before the email", "it isn't in the logs read: pull its sender's Gmail log", "it's one the attacker sent"]
    assert all(r.warned is None for r in wrong.reaches[0].recipients), "no pick counts: unknown, never 'no'"
    assert len([t for t in wrong.to_do if "doesn't count" in t.words]) == 3
    # The directory not read: a group can't be told from a person, so it's never done.
    assert not any(t.kind == "warnRecipients" for t in read(REACH_EVENTS, Context(domains={"example.com"}, warnings={"phish-1@mail.example.com": set(ALL_THREE)})).done)


PHISH, WARNING = "<phish-1@mail.example.com>", "<warn-1@mail.example.com>"
BASE = [e for e in INCIDENT if e.log != "gmail"]


def test_what_the_logs_show_and_what_they_can_t():
    events = [*BASE, gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::alice@example.com", "smtp-out::pat@partner.example", "gmail-ui::dana@example.com", "gmail-ui::bob@example.com"]),
              gmail(ALICE, 2, PHISH, 31), gmail(CAROL, 30, PHISH, 35, to=["gmail-ui::alice@example.com"]), gmail(DANA, 5, PHISH, 31), gmail(BOB, 2, PHISH, 31, success="false"),
              gmail(BOB, 1, WARNING, 60, OFFICE, to=["gmail-ui::alice@example.com", "smtp-out::pat@partner.example"]), gmail(ALICE, 34, WARNING, 61),
              gmail(BOB, 35, "<begun@mail.example.com>", 62, OFFICE, to=["gmail-ui::alice@example.com"]), gmail(BOB, 1, "<failed@mail.example.com>", 63, OFFICE, to=["smtp-out::pat@partner.example"], success="false"),
              gmail(BOB, 1, "<bounced@mail.example.com>", 64, OFFICE, to=["smtp-out::pat@partner.example"]), gmail(BOB, 30, "<bounced@mail.example.com>", 65, to=["smtp-out::pat@partner.example"])]

    def reading(*picks: str) -> reader.Reading:
        return read(events, Context(domains={"example.com"}, people=dict(PEOPLE), warnings={"phish-1@mail.example.com": set(picks)}))

    reach = reading("warn-1@mail.example.com").reaches[0]
    assert [r.outcome for r in reach.recipients] == ["received", "sent", "blocked", "blocked"], "received beats the retry's bounce; a failed receipt is none"
    assert len(reach.reached) == 4, "a quarantine or a failed receipt here keeps them to warn"
    assert [r.warned for r in reach.recipients] == [False, True, False, False], "alice@'s copy of the warning was rejected"
    assert reading("begun@mail.example.com").reaches[0].warnings[0].problem == "the logs read show its send begun, never sent"
    assert reading("failed@mail.example.com").reaches[0].warnings[0].problem == "Google marked its send failed, or it was sent from the attacker's address"
    bounced = reading("bounced@mail.example.com").reaches[0]
    assert bounced.warnings[0].problem is None and bounced.recipients[1].warned is False, "its bounce reached no one outside"
    # dana@ has no receipt of anything in the pull: her own Gmail log is what would show a warning.
    pull = next(t for t in reading("warn-1@mail.example.com").to_do if t.kind == "pullAgain" and t.pulls_again == ("gmail",))
    assert reach.unread == (DANA,) and pull.accounts == (DANA,)
    assert pull.words == "Pull the Gmail log again for dana@example.com: whether a warning reached them is in their own Gmail log, which the pull doesn't hold past the attacker's email."
    assert not any(t.kind == "warnRecipients" for t in reading("warn-1@mail.example.com").done)


def test_what_the_third_review_found():
    events = [*BASE, gmail(DANA, 2, "<old@mail.example.com>", 5), gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["smtp-out::pat@partner.example", "gmail-ui::Alice+News@example.com", "gmail-ui::dana@example.com", "gmail-ui::gone@example.com"]),
              gmail(CAROL, 30, PHISH, 31, to=["gmail-ui::gone@example.com"]), gmail(ALICE, 2, PHISH, 31), gmail(BOB, 1, WARNING, 60, OFFICE, to=["smtp-out::pat@partner.example"]), gmail(BOB, 30, WARNING, 61),
              *(gmail(BOB, 1, f"<note-{n}@mail.example.com>", 70 + n, OFFICE, f"Note {n}", ["gmail-ui::alice@example.com"]) for n in range(12))]
    reach = read(events, Context(domains={"example.com"}, people=dict(PEOPLE), warnings={"phish-1@mail.example.com": {"warn-1@mail.example.com"}})).reaches[0]
    assert [r.address for r in reach.recipients] == ["pat@partner.example", ALICE, DANA, "gone@example.com"], "alice+news@ is alice@"
    assert [r.outcome for r in reach.recipients] == ["sent", "received", "sent", "bounced"]
    assert [r.mailbox for r in reach.recipients] == [False, True, True, False]
    assert [r.address for r in reach.reached] == ["pat@partner.example", ALICE, DANA], "gone@ bounced and is no one's"
    assert reach.recipients[0].warned is False, "the warning's own failure, naming no one, is the whole warning's"
    assert reach.unread == (DANA,), "a receipt from before the phish says nothing of the days after"
    assert len(reach.candidates) == 12, "every candidate, not just ten"
    # A send of it naming no recipient: never done, and said.
    unnamed = read([*BASE, gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["smtp-out::pat@partner.example"]), gmail(CAROL, 1, PHISH, 32, ATTACKER), gmail(BOB, 1, WARNING, 60, OFFICE, to=["smtp-out::pat@partner.example"])],
                   Context(domains={"example.com"}, people=dict(PEOPLE), warnings={"phish-1@mail.example.com": {"warn-1@mail.example.com"}}))
    assert unnamed.reaches[0].unnamed == 1 and unnamed.reaches[0].to_warn == []
    assert not any(t.kind == "warnRecipients" for t in unnamed.done)
    assert any(t.words.startswith("The log shows a send of “Shared file” (phish-1@mail.example.com) naming no recipient") for t in unnamed.to_do)


def _reach(events, people=PEOPLE, picks=(), email="phish-1@mail.example.com"):
    reading = read([*BASE, *events], Context(domains={"example.com"}, people=dict(people) if people else None, warnings={"phish-1@mail.example.com": set(picks)} if picks else {}))
    return reading, next(r for r in reading.reaches if r.message_id == email)


TO_PAT = gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["smtp-out::pat@partner.example"])
WARN_PAT = gmail(BOB, 1, WARNING, 60, OFFICE, "Don't open it", ["smtp-out::pat@partner.example"])
TO_DANA = [gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::dana@example.com"]), gmail(DANA, 2, PHISH, 31), gmail(BOB, 1, WARNING, 60, OFFICE, "Don't open it", ["gmail-ui::dana@example.com"])]
PICK = ("warn-1@mail.example.com",)


def test_what_the_final_review_found():
    assert _reach([TO_PAT, WARN_PAT, gmail("", 34, WARNING, 61)], picks=PICK)[1].recipients[0].warned is False, "a failure whose actor doesn't read is the whole warning's"
    assert _reach([*TO_DANA, gmail(DANA, 2, WARNING, 61), gmail(DANA, 5, WARNING, 62)], picks=PICK)[1].recipients[0].warned is False, "received, then quarantined"
    assert _reach([*TO_DANA, gmail(DANA, 5, WARNING, 61), gmail(DANA, 6, WARNING, 62)], picks=PICK)[1].recipients[0].warned is True, "quarantined, then released"
    for bounce, slowed in ((31, 40), (40, 31)):
        tried = _reach([TO_PAT, gmail(CAROL, 30, PHISH, bounce, to=["smtp-out::pat@partner.example"]), gmail(CAROL, 34, PHISH, slowed, to=["smtp-out::pat@partner.example"])])[1]
        assert tried.recipients[0].outcome == "blocked" and [r.address for r in tried.to_warn] == ["pat@partner.example"], f"bounced at {bounce}"
    assert _reach([TO_PAT, WARN_PAT, gmail(BOB, 34, WARNING, 61)])[1].candidates == (), "a warning whose whole send failed reached no one"
    later = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::alice@example.com"]), gmail(ALICE, 2, PHISH, 31), gmail(DANA, 2, "<old@mail.example.com>", 40),
                    gmail(CAROL, 1, "<phish-2@mail.example.com>", 100, ATTACKER, "Invoice", ["gmail-ui::dana@example.com"])], email="phish-2@mail.example.com")[1]
    assert later.unread == (DANA,), "dana@'s receipt is from before this email, though after the first"
    no_directory, no_directory_reach = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::alice@example.com", "gmail-ui::team@example.com"]), gmail(ALICE, 2, PHISH, 31)], people=None)
    assert no_directory_reach.unread == () and not any(t.kind == "pullAgain" and "team@example.com" in t.accounts for t in no_directory.to_do)
    assert any(t.kind == "readDirectory" and t.words.startswith("Read the directory (open Users) to finish Who") for t in no_directory.to_do)
    assert any(t.words.startswith("Warn the 2 people") for t in no_directory.to_do)
    to_group = [gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::alice@example.com", "gmail-ui::dana@example.com"]), gmail(ALICE, 2, PHISH, 31), gmail(DANA, 2, PHISH, 31),
                gmail(BOB, 1, WARNING, 60, OFFICE, "Don't open it", ["gmail-ui::team@example.com"]), gmail(ALICE, 2, WARNING, 61), gmail(DANA, 2, WARNING, 61)]
    assert [f"{c.message_id} {c.covers}" for c in _reach(to_group)[1].candidates] == ["warn-1@mail.example.com 2"], "offered by its members' receipts"
    assert next(t for t in _reach(to_group, picks=PICK)[0].done if t.kind == "warnRecipients").words == "Warned everyone “Shared file” (phish-1@mail.example.com) got to: 2 people, by bob@example.com's “Don't open it”."
    own = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["smtp-out::list@partner.example"]), gmail(DANA, 5, PHISH, 31)])[1]
    assert [f"{r.address} {r.outcome} {r.mailbox}" for r in own.to_warn] == ["list@partner.example sent False", "dana@example.com blocked True"]
    assert Context(people={}).people is None, "a tenant has at least its admin"
    bounced = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::dana@example.com", "smtp-out::guest@example.org"]), gmail(CAROL, 30, PHISH, 31, to=["gmail-ui::dana@example.com", "smtp-out::guest@example.org"])])[1]
    assert reader.REACH_COLUMNS[5] == "to warn" and [[row[0], row[2], row[5]] for row in bounced.rows] == [[DANA, "bounced", "yes"], ["guest@example.org", "bounced", ""]]
    # An address beyond ASCII keeps its own bytes. Python compares by code points, so the two forms of an accent count apart: warned isn't shown, and over-warning is the safe side.
    accented = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["smtp-out::josé@partner.example"]), gmail(BOB, 1, WARNING, 60, OFFICE, to=["smtp-out::josé@partner.example"])], picks=PICK)[1]
    assert accented.recipients[0].address == "josé@partner.example" and accented.recipients[0].warned is False
    # A group whose copy was rejected didn't get it, so isn't among those done counts.
    rejected = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::team@example.com", "gmail-ui::alice@example.com"]), gmail(CAROL, 34, PHISH, 32, to=["gmail-ui::team@example.com"]), gmail(ALICE, 2, PHISH, 31),
                       gmail(BOB, 1, WARNING, 60, OFFICE, "Don't open it", ["gmail-ui::team@example.com", "gmail-ui::alice@example.com"]), gmail(ALICE, 2, WARNING, 61)], picks=PICK)[0]
    assert next(t for t in rejected.done if t.kind == "warnRecipients").words == "Warned everyone “Shared file” (phish-1@mail.example.com) got to: 1 person, by bob@example.com's “Don't open it”."
    assert _reach([*TO_DANA, gmail(DANA, 2, WARNING, 61), gmail(DANA, 5, WARNING, 62)])[1].candidates == (), "received, then quarantined, it reached no one to offer"
    untimed = _reach([Event("gmail", None, CAROL, "delivery", ATTACKER, False, {"event_info.mail_event_type": "1", "message_info.rfc2822_message_id": PHISH, "message_info.flattened_destinations": "gmail-ui::dana@example.com"})])[1]
    assert untimed.unread == (DANA,)
    notes = [gmail(BOB, 1, f"<note-{n}@mail.example.com>", 200 - n, OFFICE, f"Note {n}", ["gmail-ui::alice@example.com"]) for n in range(130)]
    many = _reach([gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::alice@example.com"]), *notes])[1]
    assert [c.message_id for c in many.candidates] == [f"note-{n}@mail.example.com" for n in reversed(range(30, 130))], "the earliest 100, earliest first"


GROUPS = {"team@example.com": [ALICE, "A.Anders@example.com", DANA, "ext@vendor.example", "sub@example.com", "unread-group@example.com"], "sub@example.com": [BOB, "team@example.com"]}
GROUP_EVENTS = [*BASE, gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::team@example.com", "smtp-out::pat@partner.example"]), gmail(ALICE, 2, PHISH, 31), gmail(BOB, 2, PHISH, 31),
                gmail(BOB, 1, WARNING, 60, OFFICE, "Don't open it", ["gmail-ui::team@example.com", "smtp-out::pat@partner.example"]), gmail(ALICE, 2, WARNING, 61), gmail(BOB, 2, WARNING, 61),
                gmail(ALICE, 1, "<lunch@mail.example.com>", 65, OFFICE, "Lunch", ["gmail-ui::dana@example.com"])]


def _grouped(events, groups=GROUPS, everyone=frozenset()):
    reading = read(events, Context(domains={"example.com"}, people=dict(PEOPLE), groups=groups, everyone=set(everyone), warnings={"phish-1@mail.example.com": set(PICK)}))
    return reading, reading.reaches[0]


def test_who_a_group_reached():
    # A group whose members were read counts by them, never on the warning's send: a member whose Gmail log wasn't pulled was invisible, and done counted the group on the send.
    reading, reach = _grouped(GROUP_EVENTS)
    assert [r.address for r in reach.recipients] == ["team@example.com", "pat@partner.example", ALICE, BOB, DANA, "ext@vendor.example", "sub@example.com", "unread-group@example.com"]
    assert [r.via for r in reach.recipients] == [None, None, None, None, "team@example.com", "team@example.com", "team@example.com", "team@example.com"]
    assert [r.expanded for r in reach.recipients] == [True, False, False, False, False, False, True, False]
    assert [r.address for r in reach.reached] == ["pat@partner.example", ALICE, BOB, DANA, "ext@vendor.example", "unread-group@example.com"], "a group read is who it reached, not one of them"
    assert [r.warned for r in reach.recipients] == [None, True, True, True, False, True, None, True], "dana@ only by her own receipt; ext@ and the group not read by the warning's send to team@"
    assert [r.address for r in reach.to_warn] == [DANA] and reach.unread == (DANA,)
    assert reach.warnings[0].warned == {"pat@partner.example", ALICE, BOB, "ext@vendor.example", "unread-group@example.com"}
    assert [c.message_id for c in reach.candidates] == ["lunch@mail.example.com"], "an email to a member is offered"
    assert [row[2] for row in reach.rows] == ["a group here, each of its members listed as themselves", "sent, no bounce in the logs read", "received", "received", "sent to team@example.com, no receipt in the logs read",
                                              "sent to team@example.com, no bounce in the logs read", "a group here, each of its members listed as themselves",
                                              "in team@example.com, no one's mailbox here: a group or an address no account has"]
    assert reach.rows[0][4:6] == ["", ""]
    assert not any(t.kind == "warnRecipients" for t in reading.done), "dana@ isn't shown warned: never done on the group's send"
    assert any(t.kind == "pullAgain" and t.accounts == (DANA,) for t in reading.to_do)
    # Her receipt, and it's done: the group not read is the one counted on the send.
    warned = _grouped([*GROUP_EVENTS, gmail(DANA, 2, WARNING, 61)])[0]
    assert next(t for t in warned.done if t.kind == "warnRecipients").words == "Warned everyone “Shared file” (phish-1@mail.example.com) got to: 6 people (one of them a group or an address no account has, counted on the warning's send), by bob@example.com's “Don't open it”."
    whole = _grouped([*GROUP_EVENTS, gmail(DANA, 2, WARNING, 61)], groups={**GROUPS, "unread-group@example.com": []})[0]
    assert next(t for t in whole.done if t.kind == "warnRecipients").words == "Warned everyone “Shared file” (phish-1@mail.example.com) got to: 5 people, by bob@example.com's “Don't open it”."
    # Bounced from the group: it reached no one through it.
    bounced = _grouped([*GROUP_EVENTS, gmail(CAROL, 30, PHISH, 32, to=["gmail-ui::team@example.com"])])[1]
    assert [r.address for r in bounced.recipients] == ["team@example.com", "pat@partner.example", ALICE, BOB]
    assert not bounced.recipients[0].expanded and len(bounced.reached) == 3
    # GAM's CUSTOMER member: everyone here, each by their own receipt.
    every = _grouped(GROUP_EVENTS, groups={"team@example.com": []}, everyone={"team@example.com"})[1]
    assert [r.address for r in every.recipients] == ["team@example.com", "pat@partner.example", ALICE, BOB, CAROL, DANA]
    assert [r.address for r in every.to_warn] == [CAROL, DANA]


def test_a_group_not_read_counts_on_the_send():
    reading = read([*BASE, gmail(CAROL, 1, PHISH, 30, ATTACKER, to=["gmail-ui::team@example.com", "gmail-ui::alice@example.com"]), gmail(ALICE, 2, PHISH, 31),
                    gmail(BOB, 1, WARNING, 60, OFFICE, "Don't open it", ["gmail-ui::team@example.com", "gmail-ui::alice@example.com"]), gmail(ALICE, 2, WARNING, 61)],
                   Context(domains={"example.com"}, people=dict(PEOPLE), groups={"other@example.com": [DANA]}, warnings={"phish-1@mail.example.com": set(PICK)}))
    reach = reading.reaches[0]
    assert [r.address for r in reach.recipients] == ["team@example.com", ALICE] and all(not r.expanded and r.via is None for r in reach.recipients)
    assert next(t for t in reading.done if t.kind == "warnRecipients").words == "Warned everyone “Shared file” (phish-1@mail.example.com) got to: 2 people (one of them a group or an address no account has, counted on the warning's send), by bob@example.com's “Don't open it”."


# --- what's done, and what stays ------------------------------------------------------------------------------------------------


TWO = [*INCIDENT, event("gmail", CAROL, "delivery", {"event_info.mail_event_type": "1", "message_info.subject": "Invoice", "message_info.rfc2822_message_id": "<phish-2@mail.example.com>"}, ip=ATTACKER, minute=31)]
RAN_AT = datetime.fromtimestamp(1_792_403_600, tz=timezone.utc)
PHISH_DRAFT = reader.pull_draft(PHISH, "Shared file")


def pulled(message="phish-1@mail.example.com", steps=3, trashed=3, failed=0, searched=True, when=RAN_AT):
    return reader.Pulled(message, when, "admin@example.com", steps, trashed, failed, 2, searched)


def test_an_email_phishing_pulled_is_done_with_when_and_by_whom():
    before = read(TWO)
    assert before.done == () and before.emails_to_pull == [PHISH, "<phish-2@mail.example.com>"]
    reading = read(TWO, Context(pulled=[pulled()]))
    assert [t.id for t in reading.done] == ["D1"] and reading.done[0].kind == "pullEmail" and reading.done[0].drafts == ()
    assert reading.done[0].words == f"Pulled “Shared file” ({PHISH}): trashed from the 2 mailboxes that had it, every mailbox searched first."
    assert reading.done[0].done == reader.Done("trashed", RAN_AT, "admin@example.com")
    assert reading.emails_to_pull == ["<phish-2@mail.example.com>"], "Pull All offers only what's left"
    assert PHISH_DRAFT not in reading.drafts, "drafted nowhere again"
    assert [f.words for f in reading.findings] == [f.words for f in before.findings], "it was still sent"
    assert [t.words for t in reading.to_do] == [t.words for t in before.to_do if PHISH not in t.words]
    assert all(t.done is None for t in reading.to_do)
    assert read(TWO, Context(pulled=[pulled("other@mail.example.com")])) == before, "another email's run changes nothing"

    def left(*runs):
        got = read(TWO, Context(pulled=list(runs)))
        assert got.done == () and PHISH in got.emails_to_pull
        return next(t.words for t in got.to_do if PHISH_DRAFT in t.drafts)

    ask = f"Pull “Shared file” ({PHISH}) from every mailbox: it went out from the attacker's address. Phishing's last run by admin@example.com trashed "
    again = ": find it again there to see what's left."
    assert left(pulled(trashed=2, failed=1)) == ask + "2 of its 3 steps, but 1 failed" + again
    assert left(pulled(trashed=1)) == ask + "1 of its 3 steps, but 2 didn't finish" + again
    assert left(pulled(searched=False)) == ask + "it from every mailbox it found, but Find couldn't search every mailbox for it" + again
    assert left(pulled(trashed=0, failed=1, searched=False)) == ask + "0 of its 3 steps, but 1 failed, 2 didn't finish and Find couldn't search every mailbox for it" + again
    # The latest run decides, and a Message-ID matches with its angle brackets or without.
    later = RAN_AT + timedelta(seconds=600)
    assert left(pulled(trashed=1, when=later), pulled()) == ask + "1 of its 3 steps, but 2 didn't finish" + again
    whole = read(TWO, Context(pulled=[pulled(trashed=1), pulled(PHISH, steps=1, trashed=1, when=later)]))
    assert [t.done for t in whole.done] == [reader.Done("trashed", later, "admin@example.com")]

    # Find showed nobody here has it: done, said as checked, and the latest fact decides. A run after the check found copies again, so it's to do until that run is whole.
    def nobody(message=PHISH, when=later):
        return reader.NobodyHas(message, when, "admin@example.com")

    checked = read(TWO, Context(nobody_has=[nobody("phish-1@mail.example.com")]))
    assert [t.words for t in checked.done] == [f"Nobody here has “Shared file” ({PHISH}): Phishing's Find searched every mailbox and its Spam."]
    assert [t.done for t in checked.done] == [reader.Done("nobodyHasIt", later, "admin@example.com")]
    assert checked.emails_to_pull == ["<phish-2@mail.example.com>"] and PHISH_DRAFT not in checked.drafts
    assert [t.done.how for t in read(TWO, Context(pulled=[pulled(trashed=1)], nobody_has=[nobody()])).done] == ["nobodyHasIt"], "the check after the run"
    run_after = read(TWO, Context(pulled=[pulled(trashed=1, when=later + timedelta(seconds=60))], nobody_has=[nobody()]))
    assert run_after.done == () and PHISH in run_after.emails_to_pull, "a run after the check found copies again"


REG = "DEVICE_REGISTER_UNREGISTER_EVENT"
VM = {"ACCOUNT_STATE": "REGISTERED", "DEVICE_TYPE": "WINDOWS", "DEVICE_ID": "pc-1"}


def _device_open(*more: Event) -> bool:
    return any(t.kind == "deleteDevice" for t in read([*INCIDENT, event("mobile", CAROL, REG, VM, minute=11), *more]).to_do)


def _action(kind: str, status: str) -> Event:
    return event("mobile", CAROL, "DEVICE_ACTION_EVENT", {"DEVICE_ID": "pc-1", "RESOURCE_ID": "r-1", "ACTION_TYPE": kind, "ACTION_EXECUTION_STATUS": status}, minute=70)


def _admin_device(name: str, to: str = CAROL, ip: str = OFFICE, when: int = 70) -> Event:
    return event("admin", ALICE, name, {"DEVICE_ID": "pc-1", "USER_EMAIL": to}, ip=ip, minute=when)


def test_each_loose_end_stays_until_its_own_fix():
    twice = read([*INCIDENT, event("gmail", CAROL, "delivery", {"event_info.mail_event_type": "1", "message_info.subject": "Shared file", "message_info.rfc2822_message_id": "<phish-2@mail.example.com>"}, ip=ATTACKER, minute=31)])
    assert len([t for t in twice.to_do if t.kind == "pullEmail"]) == 2
    gone = {"ACCOUNT_STATE": "UNREGISTERED", "DEVICE_ID": "pc-1"}
    assert _device_open(event("mobile", CAROL, REG, gone, minute=70), event("mobile", CAROL, REG, VM, minute=80)), "registered again"
    # Google's own values (the Reports API's Devices and Admin mobile-settings events), not guesses.
    assert _device_open(_action("ACCOUNT_WIPE", "PENDING")), "a wipe not done"
    assert _device_open(_action("BLOCK", "SENT_TO_DEVICE")), "a block not done"
    assert _device_open(_action("LOCK_DEVICE", "EXECUTED")), "locked isn't gone"
    assert _device_open(event("mobile", DANA, REG, gone, minute=70)), "another account leaving it"
    assert _device_open(_admin_device("MOBILE_DEVICE_BLOCK", to=DANA)), "another account's device"
    assert _device_open(_admin_device("MOBILE_DEVICE_BLOCK"), _admin_device("MOBILE_DEVICE_APPROVE", when=75)), "approved again"
    # Approved again is back, even from a suspect address; blocked from one isn't gone.
    assert _device_open(_action("BLOCK", "EXECUTED"), _admin_device("MOBILE_DEVICE_APPROVE", ip=ATTACKER, when=75)), "approved by the attacker"
    assert _device_open(_admin_device("MOBILE_DEVICE_BLOCK", ip=ATTACKER)), "blocked from a suspect address"
    assert _device_open(_action("BLOCK", "EXECUTED"), event("mobile", CAROL, "DEVICE_ACTION_EVENT", {"DEVICE_ID": "pc-1", "ACTION_TYPE": "APPROVE", "ACTION_EXECUTION_STATUS": "EXECUTED"}, minute=75)), "approved in the Devices log"
    assert not _device_open(_action("ACCOUNT_WIPE", "EXECUTED"))
    assert not _device_open(_action("BLOCK", "EXECUTED"))
    assert not _device_open(event("mobile", CAROL, "DEVICE_COMPLIANCE_CHANGED_EVENT", {"DEVICE_ID": "pc-1", "DEVICE_DEACTIVATION_REASON": "DEVICE_BLOCKED_BY_ADMIN"}, minute=70))
    for name in ("MOBILE_DEVICE_BLOCK", "MOBILE_DEVICE_DELETE", "MOBILE_DEVICE_WIPE", "MOBILE_ACCOUNT_WIPE"):
        assert not _device_open(_admin_device(name)), name
    phone = read([*INCIDENT, event("useraccounts", CAROL, "recovery_phone_edit", ip=ATTACKER, minute=14), event("useraccounts", CAROL, "recovery_secret_qa_edit", ip=ATTACKER, minute=15), done("REMOVE_RECOVERY_EMAIL", minute=62)])
    recovery = [t.words for t in phone.to_do if t.kind in ("clearRecovery", "recoveryQuestion")]
    assert any(w.startswith("Clear carol@example.com's recovery email and phone") for w in recovery), "the email removed, not the phone"
    assert any(w.startswith("Have carol@example.com set a new recovery question") for w in recovery)


def test_no_lock_says_why_when_the_pull_could_not_show_it():
    def words(context: Context) -> str:
        return read(INCIDENT, context).statuses[0].words(minute)

    assert words(Context(pulled_by=ATTACKER)) == "No lock seen: a pull by one address doesn't show it (the lock is done from yours): pull by the accounts."
    assert words(Context(read={"login", "token"})) == "No lock seen: the admin log, where it would be, wasn't read."
    assert words(Context(read={"login", "admin"})) == "No lock seen in these logs."


def test_what_a_lock_leaves_in_place():
    reading = read([*INCIDENT, event("login", CAROL, "passkey_enrolled", ip=ATTACKER, minute=13), event("useraccounts", CAROL, "recovery_phone_edit", ip=ATTACKER, minute=14), event("login", ALICE, "passkey_enrolled", ip=OFFICE, minute=15),
                    event("saml", CAROL, "login_success", {"application_name": "Payroll Portal"}, ip=ATTACKER, minute=16), event("saml", ALICE, "login_success", {"application_name": "Payroll Portal"}, ip=OFFICE, minute=17)])
    left = [f for f in reading.findings if f.kind == "leftInPlace"]
    assert [f.accounts for f in left] == [(CAROL,)]
    assert left[0].words == ("From 198.51.100.23, carol@example.com's account got a new passkey and a changed recovery phone: a lock doesn't undo them. Remove what isn't theirs on the Admin console's page for them (Security), or with them in "
                             "their Google Account. Check Account shows the recovery details, and its “Clear recovery email & phone” step removes them.")
    assert left[0].drafts == (secure_draft(CAROL),)
    assert first_finding(read([*INCIDENT, event("login", CAROL, "passkey_enrolled", ip=ATTACKER, minute=13)]), "leftInPlace").drafts == (), "a passkey is removed by hand"
    session = [f for f in reading.findings if f.kind == "otherAppSession"]
    assert [f.accounts for f in session] == [(CAROL,)]
    assert session[0].words == "carol@example.com signed in to Payroll Portal through Google from 198.51.100.23: Google's sign-out doesn't end that app's session. Sign them out there too."
    assert session[0].drafts == ()
    k = kinds(reading)
    assert k.index("leftInPlace") < k.index("mailFromSuspect")


def test_each_account_has_its_timeline():
    home6, home6_later = "2001:db8:aa:1::5", "2001:db8:aa:1:9::7"
    reading = read([*INCIDENT, event("login", CAROL, "login_success", ip=home6, minute=2), event("mobile", CAROL, REG, {"ACCOUNT_STATE": "REGISTERED", "DEVICE_TYPE": "MAC"}, minute=11),
                    done("RESET_SIGNIN_COOKIES", minute=60), done("CHANGE_PASSWORD", minute=61), done("REVOKE_3LO_TOKEN", minute=61), done("REVOKE_3LO_TOKEN", minute=61), done("CHANGE_PASSWORD_ON_NEXT_LOGIN", minute=61),
                    done("RESET_SIGNIN_COOKIES", minute=90), event("useraccounts", CAROL, "password_edit", ip=home6_later, minute=95), event("useraccounts", CAROL, "2sv_enroll", ip="192.0.2.77", minute=96),
                    event("login", CAROL, "account_disabled_hijacked", minute=120)])
    carol = reading.statuses[0]
    assert [f"{minute(m.at)} {m.words}" for m in carol.timeline] == [
        "minute 10 Google blocked a suspicious sign-in from 198.51.100.23", "minute 11 The attacker signed in from 198.51.100.23", "minute 11 A Mac joined the account", "minute 12 The attacker gave Mail Sync Pro access",
        "minute 30 1 Gmail event from the attacker's address", "minute 60 Signed out of every session by alice@example.com", "minute 61 Password reset by alice@example.com", "minute 61 2 apps' access revoked by alice@example.com",
        "minute 90 Signed out of every session by alice@example.com", "minute 95 They changed their own password", "minute 96 They turned on 2-Step Verification, from 192.0.2.77, an address not seen before the attack",
        "minute 120 Google disabled the account as hijacked"]
    assert [m.who for m in carol.timeline] == ["google", "attacker", "attacker", "attacker", "attacker", "admin", "admin", "admin", "admin", "person", "person", "google"]


def test_what_is_done_leaves_the_list():
    vm = {"ACCOUNT_STATE": "REGISTERED", "DEVICE_TYPE": "WINDOWS", "DEVICE_ID": "pc-1", "RESOURCE_ID": "r-1"}
    attacked = [*INCIDENT, event("mobile", CAROL, REG, vm, minute=11), event("useraccounts", CAROL, "recovery_phone_edit", ip=ATTACKER, minute=14), done("CHANGE_PASSWORD", minute=60), done("RESET_SIGNIN_COOKIES", minute=61)]
    still = read(attacked)
    # Each carries its own "Do It Here": the computer by the log's ID and time, the recovery details cleared.
    assert any(t.kind == "deleteDevice" and t.accounts == (CAROL,) and t.drafts == (Draft("undo", account=CAROL, pick="computer", resource_id="r-1", registered_at=at(11)),) for t in still.to_do)
    assert any(t.kind == "clearRecovery" and t.drafts == (Draft("undo", account=CAROL, pick="clearRecovery"),) for t in still.to_do)
    finished = read([*attacked, event("mobile", CAROL, REG, {"ACCOUNT_STATE": "UNREGISTERED", "DEVICE_ID": "pc-1"}, minute=70), done("REMOVE_RECOVERY_PHONE", minute=62)])
    assert not any(t.kind in ("deleteDevice", "clearRecovery") for t in finished.to_do)
    # An admin's block of it, in the Admin log, is done too, and in the account's timeline in words.
    blocked = read([*attacked, _admin_device("MOBILE_DEVICE_BLOCK")])
    assert not any(t.kind == "deleteDevice" for t in blocked.to_do)
    assert any(m.words == "A device blocked by alice@example.com" for m in next(s for s in blocked.statuses if s.account == CAROL).timeline)
    # Gmail read for every account stopped at minute 40; the attack began at minute 10.
    short = read(attacked, Context(capped={"gmail": at(40), "mobile": at(1)}))
    again = next(t for t in short.to_do if t.kind == "pullAgain")
    assert again.words == "Pull the Gmail log again for just carol@example.com, dana@example.com and erin@example.com: read for every account, it stops at its newest events and doesn't reach back to the attack."
    kinds_left = [t.kind for t in short.to_do]
    assert kinds_left.index("pullAgain") < kinds_left.index("deleteDevice")
    assert again.pulls_again == ("gmail",) and again.shows is None, "Pull Again reads the short log for those accounts"


def test_an_account_s_status_reads_only_its_own_events():
    # Every rule about one account reads only its own events and the admin's done to it: a rule that reads more must widen `_Accounts`, or this fails.
    events = [*INCIDENT, done("CHANGE_PASSWORD", minute=60), done("RESET_SIGNIN_COOKIES", to="Carol@example.com", minute=61), event("login", CAROL, "login_success", ip=ATTACKER, minute=80),
              event("contacts", CAROL, "print_contacts", {"CONTACTS_COUNT": "1405"}, minute=20), event("drive", CAROL, "view", ip=ATTACKER, minute=33), event("login", DANA, "login_success", ip="203.0.113.9", minute=2),
              event("useraccounts", DANA, "password_edit", ip="203.0.113.9", minute=50), done("SUSPEND_USER", to=DANA, minute=40), done("UNSUSPEND_USER", to="DANA@example.com", minute=45),
              event("login", ERIN, "account_disabled_hijacked", minute=50), event("admin", DANA, "RESET_SIGNIN_COOKIES", {"USER_EMAIL": ERIN}, ip="192.0.2.9", minute=30), event("admin", ALICE, "CHANGE_PASSWORD", {"USER_EMAIL": ALICE}, ip=OFFICE, minute=3)]
    about = reader._Accounts(events)
    acting = {ATTACKER}
    held = {DANA: (at(20), reader._DISTANT_FUTURE)}
    for account in (CAROL, "Dana@example.com", ERIN, ALICE, "frank@example.com"):
        for spans in ({}, held):
            assert reader._status(account, events, acting, Context(), held=spans) == reader._status(account, about[account], acting, Context(), held=spans), account
    assert reader._status(CAROL, about[CAROL], acting, Context()).state == "stillIn"
    assert len(about["CAROL@example.com"]) == 10 and len(about[ALICE]) == 6, "its own, and the admin's done to it, each once"


def test_a_whole_pull_is_read_in_seconds():
    # Reading every event per account took minutes (failure log 2026-10-23): 200 accounts the attacker had among 200,000 events. CPU time, so a busy runner's other work doesn't count.
    accounts = [f"user{n}@example.com" for n in range(200)]
    events: List[Event] = []
    for n, account in enumerate(accounts):
        ip = f"198.51.{100 + n // 200}.{n % 200 + 1}"
        events += [event("login", account, "suspicious_login", ip=ip, flagged=True, minute=n), event("login", account, "login_success", {"is_suspicious": "true"}, ip=ip, minute=n + 1)]
        if n % 2 == 0:
            events += [done("CHANGE_PASSWORD", to=account, minute=n + 60), done("RESET_SIGNIN_COOKIES", to=account, minute=n + 61)]
    logs = ("gmail", "drive", "calendar", "login")
    for index in range(len(events), 200_000):
        n = index % len(accounts)
        log = logs[index % len(logs)]
        events.append(event(log, accounts[n], "login_success" if log == "login" else "view", ip=f"198.51.{100 + n // 200}.{n % 200 + 1}" if index % 9 == 0 else f"203.0.113.{index % 200}", minute=index % 4000))
    start = time.process_time()
    reading = read(events)
    took = time.process_time() - start
    assert len(reading.statuses) == 200 and reading.events == 200_000
    assert took < 60, f"took {took:.1f}s"


def test_who_it_reached_costs_less_than_summing_every_email():
    people = [f"user{n}@example.com" for n in range(500)]
    events = list(BASE)
    for n in range(100_000):
        send = n % 3 == 0
        events.append(gmail(people[n % len(people)], 1 if send else 2, f"<m{n // 3}@mail.example.com>", 40 + n // 100, f"203.0.113.{n % 200}" if send else "", "Note", [f"gmail-ui::{people[(n + i * 7) % len(people)]}" for i in (1, 2, 3)] if send else None))
    events.append(gmail(CAROL, 1, PHISH, 30, ATTACKER, to=[f"gmail-ui::{p}" for p in people[:5]]))
    context = Context(domains={"example.com"}, people={p: p for p in people}, warnings={"phish-1@mail.example.com": {"m300@mail.example.com"}})
    start = time.process_time()
    reaches = reader._reaches([PHISH], events, {ATTACKER}, context)
    took = time.process_time() - start
    start = time.process_time()
    reader._mails((e for e in events if e.log == "gmail"), lambda who: who, reader.address)
    everything = time.process_time() - start
    assert len(reaches[0].recipients) == 5
    assert took < everything * 3, f"{took:.2f}s, and {everything:.2f}s to sum every email"


# --- marked done by hand --------------------------------------------------------------------------------------------------------


def test_a_tick_moves_only_an_item_a_log_can_t_show_done():
    reading = read(TWO)
    look = next(t for t in reading.to_do if t.tickable)
    tick = reader.Ticked(look.kind, look.words, look.basis, "Told them by phone", RAN_AT, "admin@example.com")
    ticked = reading.ticking([tick])
    assert look.words not in [t.words for t in ticked.to_do]
    moved = next(t for t in ticked.done if t.words == look.words)
    assert moved.done == reader.Done("byHand", RAN_AT, "admin@example.com", "Told them by phone")
    assert [t.id for t in ticked.to_do] == [f"T{i + 1}" for i in range(len(ticked.to_do))]
    secure = next(t for t in reading.to_do if t.kind == "secure")
    assert not secure.tickable
    assert reading.ticking([reader.Ticked(secure.kind, secure.words, secure.basis, "x", RAN_AT)]) is reading, "a lock is never ticked: the logs show it"
    assert reading.ticking([reader.Ticked(look.kind, look.words + " more", look.basis, "x", RAN_AT)]) is reading, "one the logs now say more about is left to do again"
