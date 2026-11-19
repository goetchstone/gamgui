"""Reports' named reports (roadmap 3.1): domain-wide reads the Builder already runs, named, each with the access it needs."""

from __future__ import annotations

import json
import re

import pytest

from gamgui.core import reports
from gamgui.core.catalog.catalog import load_catalog
from gamgui.core.connectors.base import RiskLevel

from .helpers import OAUTH2_FAKE
from .test_users_web import client  # noqa: F401  (the fixture)

DOMAIN = "example.com"


@pytest.mark.parametrize("report", reports.NAMED_REPORTS, ids=lambda r: r.key)
def test_every_named_report_is_one_read(report):
    # Named by syntax, not by `raw.N` (which moves with every GAM bump): a bump that renames one, or makes it match two
    # commands, fails here rather than opening the wrong read.
    cmd = reports.named_report_command(load_catalog(), report)
    assert cmd is not None, f"{report.syntax} is no longer exactly one runnable catalog command"
    assert cmd.buildable and cmd.risk is RiskLevel.READ_ONLY


def test_the_keys_are_unique_and_each_names_a_token():
    keys = [r.key for r in reports.NAMED_REPORTS]
    assert len(keys) == len(set(keys)) == 9
    assert {r.token for r in reports.NAMED_REPORTS} == {reports.ADMIN_TOKEN, reports.SERVICE_ACCOUNT}


@pytest.mark.parametrize("granted, held", [
    (["https://www.googleapis.com/auth/apps.licensing"], True),
    (["https://www.googleapis.com/auth/apps.licensing.readonly"], True),
    (["https://www.googleapis.com/auth/admin.directory.user"], False),
    (None, None),            # a token whose scopes can't be read is unknown, never "held"
])
def test_the_admin_token_s_scope_is_held_or_not_or_unknown(granted, held):
    licenses = next(r for r in reports.NAMED_REPORTS if r.key == "licenses")
    assert reports.admin_scope_held(licenses, granted) is held


def test_a_service_account_read_is_never_claimed_held():
    alerts = next(r for r in reports.NAMED_REPORTS if r.key == "alerts")
    assert reports.admin_scope_held(alerts, [alerts.scope]) is None


def test_the_reports_page_names_each_and_opens_it_in_the_builder(client):  # noqa: F811
    page = client.get("/reports").text
    cat = load_catalog()
    for report in reports.NAMED_REPORTS:
        cid = reports.named_report_command(cat, report).id
        assert f'href="/builder?cid={cid}"' in page and report.title.replace("'", "&#39;") in page
        assert report.scope in page


def test_the_page_says_when_the_admin_token_lacks_a_scope(client):  # noqa: F811
    vault = client.app.state.gamgui.vault
    token = json.loads(OAUTH2_FAKE)
    token["scopes"] = ["https://www.googleapis.com/auth/admin.directory.user"]
    vault.set(DOMAIN, "oauth2", json.dumps(token))
    row = re.search(r'<tr id="named-licenses".*?</tr>', client.get("/reports").text, re.S).group(0)
    assert "The admin token doesn" in row and "apps.licensing" in row


def test_a_token_without_recorded_scopes_is_said_unknown(client):  # noqa: F811
    row = re.search(r'<tr id="named-licenses".*?</tr>', client.get("/reports").text, re.S).group(0)
    assert "couldn" in row and "has it" not in row


def test_the_builder_opens_a_command_named_in_its_address(client):  # noqa: F811
    cid = reports.named_report_command(load_catalog(), reports.NAMED_REPORTS[0]).id
    assert f'hx-get="/builder/command/{cid}"' in client.get("/builder", params={"cid": cid}).text
    assert 'hx-get="/builder/command/' not in client.get("/builder", params={"cid": "raw.0"}).text
    assert 'hx-get="/builder/command/' not in client.get("/builder").text
