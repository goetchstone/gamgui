from __future__ import annotations

from gamgui.core.gam.models import GAMGroup, GAMUser, GroupMember


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
