"""Create, rename and delete an org unit (roadmap 4.2): curated Builder commands, previewed and run through the guard, with the
mock refusing what Google refuses (a missing parent, a unit that exists, a unit that still holds people or units)."""

from __future__ import annotations

import re
from html import unescape

import pytest

from gamgui.core.catalog import load_catalog
from gamgui.core.connectors.base import RiskLevel

from .helpers import gam_writes
from .test_builder import _builder_preview, _builder_run, client  # noqa: F401  (the fixture)


def test_the_three_are_curated_with_their_risk():
    cat = load_catalog()
    risks = {cid: cat.by_id(cid).risk for cid in ("build.create_org_unit", "build.rename_org_unit", "build.delete_org_unit")}
    assert risks == {"build.create_org_unit": RiskLevel.LOW, "build.rename_org_unit": RiskLevel.LOW,
                     "build.delete_org_unit": RiskLevel.DESTRUCTIVE}
    assert all(cat.by_id(cid).buildable and cat.by_id(cid).area == "Org & Domains" for cid in risks)


@pytest.mark.parametrize("form, argv", [
    ({"cid": "build.create_org_unit", "path": "/Sales/East", "description": "East team"},
     ["create", "org", "/Sales/East", "description", "East team"]),
    ({"cid": "build.create_org_unit", "path": "/Sales/East"}, ["create", "org", "/Sales/East"]),
    ({"cid": "build.rename_org_unit", "org_unit": "/Sales/West", "name": "West Coast"},
     ["update", "org", "/Sales/West", "name", "West Coast"]),
])
def test_create_and_rename_run_once_from_their_preview(client, gam_calls, form, argv):  # noqa: F811
    shown, token = _builder_preview(client, **form)
    assert "Will run" in shown and token
    assert "done" in _builder_run(client, token, **form).text
    assert gam_writes(gam_calls()) == [argv]


def test_delete_is_confirmed_like_a_destructive_change(client, gam_calls):  # noqa: F811
    form = {"cid": "build.delete_org_unit", "org_unit": "/Sales/West"}
    shown, token = _builder_preview(client, **form)
    assert "DESTRUCTIVE" in shown and "Confirm &amp; run" in shown
    r = client.post("/builder/run", data={**form, "preview": token})           # the token without the click
    assert "done" not in r.text and gam_writes(gam_calls()) == []
    _, token = _builder_preview(client, **form)
    assert "done" in _builder_run(client, token, **form).text
    assert gam_writes(gam_calls()) == [["delete", "org", "/Sales/West"]]


@pytest.mark.parametrize("form, said", [
    ({"cid": "build.create_org_unit", "path": "/Nope/East"}, "Parent Organizational Unit: /Nope, Does not exist"),
    ({"cid": "build.create_org_unit", "path": "/Sales"}, "Organizational Unit: /Sales, Duplicate"),
    ({"cid": "build.rename_org_unit", "org_unit": "/Nope", "name": "X"}, "Organizational Unit: /Nope, Update Failed"),
    ({"cid": "build.rename_org_unit", "org_unit": "/Staff", "name": "Students"}, "/Students already exists"),
    ({"cid": "build.rename_org_unit", "org_unit": "/Staff", "name": "A/B"}, "Invalid Org Unit Name"),
    ({"cid": "build.delete_org_unit", "org_unit": "/Staff"}, "has users or child organizational units"),
    ({"cid": "build.delete_org_unit", "org_unit": "/Sales"}, "has users or child organizational units"),
    ({"cid": "build.delete_org_unit", "org_unit": "/Nope"}, "Organizational Unit: /Nope, Delete Failed: Does not exist"),
])
def test_what_google_refuses_reaches_the_screen_and_is_not_done(client, form, said):  # noqa: F811
    _, token = _builder_preview(client, **form)
    r = unescape(_builder_run(client, token, **form).text)
    assert said in r and "done" not in r
    rec = client.app.state.gamgui.connector.audit.tail()[-1]
    assert rec["ok"] is False


@pytest.mark.parametrize("form", [{"cid": "build.rename_org_unit", "org_unit": " Root ", "name": "X"},
                                  {"cid": "build.delete_org_unit", "org_unit": "/"},
                                  {"cid": "build.create_org_unit", "path": "root"}])
def test_the_top_level_unit_is_refused_before_gam(client, gam_calls, form):  # noqa: F811
    # GAM reads `root` (and `/`) as the top-level unit: a unit someone means to call "root" would be the whole org's.
    shown, token = _builder_preview(client, **form)
    assert "That is the top-level org unit" in unescape(shown) and not token
    assert gam_writes(gam_calls()) == []


def test_with_no_units_below_the_root_the_writes_refuse_as_the_read_says(client, gam_state):  # noqa: F811
    (gam_state / "no_org_units").write_text("")
    for form, said in (({"cid": "build.create_org_unit", "path": "/Sales/East"}, "Parent Organizational Unit: /Sales"),
                       ({"cid": "build.delete_org_unit", "org_unit": "/Sales/West"}, "Does not exist")):
        _, token = _builder_preview(client, **form)
        assert said in unescape(_builder_run(client, token, **form).text)


def test_a_unit_someone_was_moved_into_is_not_empty(client, gam_state):  # noqa: F811
    r = client.post("/users/ou/preview", data={"email": "alice@example.com", "org_unit": "/Sales/West"})
    token = re.search(r'name="preview" value="([^"]+)"', r.text).group(1)
    moved = client.post("/users/ou/apply", data={"email": "alice@example.com", "org_unit": "/Sales/West", "preview": token,
                                                 "confirmed": "1"})
    assert "Moved alice@example.com" in moved.text
    form = {"cid": "build.delete_org_unit", "org_unit": "/Sales/West"}
    _, token = _builder_preview(client, **form)
    assert "has users or child organizational units" in unescape(_builder_run(client, token, **form).text)


def test_a_value_stays_one_argv_element(client, gam_calls):  # noqa: F811
    form = {"cid": "build.rename_org_unit", "org_unit": "/Sales/West", "name": "West move user alice@example.com"}
    _, token = _builder_preview(client, **form)
    _builder_run(client, token, **form)
    assert gam_writes(gam_calls()) == [["update", "org", "/Sales/West", "name", "West move user alice@example.com"]]
