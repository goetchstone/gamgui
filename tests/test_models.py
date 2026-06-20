from __future__ import annotations

from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember, Vacation


def test_gam_user_nested_name_and_bool_coercion():
    u = GAMUser.from_json(
        {
            "primaryEmail": "alice@example.com",
            "name": {"givenName": "Alice", "familyName": "Anders"},
            "suspended": "true",  # GAM CSV may stringify booleans
            "orgUnitPath": "/Staff",
        }
    )
    assert u.full_name == "Alice Anders"
    assert u.suspended is True
    assert u.org_unit_path == "/Staff"


def test_gam_user_falls_back_to_email_for_name():
    u = GAMUser.from_json({"primaryEmail": "x@e.com"})
    assert u.full_name == "x@e.com"


def test_gam_group_member_count_parsing():
    g = GAMGroup.from_json({"email": "g@e.com", "directMembersCount": "5"})
    assert g.members_count == 5
    g2 = GAMGroup.from_json({"email": "g@e.com"})
    assert g2.members_count is None


def test_group_member_role_uppercased():
    m = GroupMember.from_json({"email": "a@e.com", "role": "manager"})
    assert m.role == "MANAGER"


def test_gam_user_parses_title_location_and_flags():
    u = GAMUser.from_json(
        {
            "primaryEmail": "a@e.com",
            "organizations": [{"title": "IT Director", "department": "IT", "primary": True}],
            "locations": [{"buildingName": "HQ", "primary": True}],
            "isEnrolledIn2Sv": True,
            "isDelegatedAdmin": True,
            "recoveryEmail": "r@e.com",
        }
    )
    assert u.title == "IT Director"
    assert u.department == "IT"
    assert u.location == "HQ"
    assert u.enrolled_2sv is True
    assert u.is_delegated_admin is True
    assert u.recovery_email == "r@e.com"


def test_vacation_from_show_text():
    text = (
        "User: x@e.com, Vacation:\n  Enabled: True\n  Contacts Only: False\n"
        "  Domain Only: False\n  Subject: OOO\n  Message:\n    Back next week.\n"
    )
    v = Vacation.from_show_text(text)
    assert v.enabled is True
    assert v.subject == "OOO"
    assert "Back next week." in v.message


def test_vacation_disabled_parse():
    v = Vacation.from_show_text("User: x, Vacation:\n  Enabled: False\n  Subject:\n")
    assert v.enabled is False


def test_vacation_message_excludes_gam_init_banner():
    # Regression: GAM's config-init banner can flush AFTER the message block; it must not be
    # absorbed into the auto-reply message.
    text = (
        "User: ashley@e.com, Vacation:\n  Enabled: True\n  Subject: Away\n  Message:\n"
        "    I am out of office.\n"
        "Created: /tmp/gamcfg-abc/gamcache\n"
        "Config File: /tmp/gamcfg-abc/gam.cfg, Initialized\n"
    )
    v = Vacation.from_show_text(text)
    assert v.message == "I am out of office."
    assert "Config File" not in v.message and "gamcache" not in v.message
