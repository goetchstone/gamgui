"""Tripwire: no POST route runs a GAM write on a bare POST.

The guard once lived only in the templates: five routes ran a suspend, an event delete, a
company-wide signature overwrite and a full offboarding for any POST that reached them. This file
enumerates every POST route from the app itself, so a new route fails here until it is classified:
either exempt below, with a reason, or GATED with a plausible form. For each gated route, a POST
without the confirmation must reach the mock `gam` with zero writes; and the confirm step the UI
really renders, posted back as HTMX would post it, must run the write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

import pytest
from fastapi.testclient import TestClient

from gamgui.core.audit import AuditLog
from gamgui.core.calendar_index import CalendarIndex
from gamgui.core.connectors.gam_connector import GAMConnector
from gamgui.core.gam.runner import GAMRunner
from gamgui.core.onboarding import RunbookStore
from gamgui.core.secrets.vault import InMemoryBackend, SecretsVault
from gamgui.core.signatures import SignatureStore
from gamgui.web.server import AppState, create_app

from .helpers import TEST_HOSTS, gam_writes, wait_for_job

FIXTURES = Path(__file__).parent / "fixtures"
WEB_DIR = Path(__file__).parent.parent / "gamgui" / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
DOMAIN = "example.com"

# --- exempt POST routes, by reason -----------------------------------------------------------------
# Previews: they resolve and render the confirm step (a GAM read at most), never write.
PREVIEWS = {
    "/users/bulk/preview", "/users/suspend/preview", "/users/delete/confirm",
    "/calendars/delete/preview", "/calendars/event/preview",
    "/lifecycle/offboard/preview", "/lifecycle/offboard/autoreply", "/signatures/preview",
    "/onboard/preview", "/onboard/bulk/preview", "/builder/preview", "/builder/sequence/preview",
}
# Local state only, no GAM write: the Builder's working sequence, saved templates/roles, the bulk
# onboarding credentials sheet, the setup wizard (Keychain import, printed commands, a read-only
# verify), and the calendar index (a read-only scan into a local SQLite file).
LOCAL_ONLY = {
    "/builder/sequence/add", "/builder/sequence/remove", "/builder/sequence/move", "/builder/sequence/clear",
    "/signatures/templates/save", "/signatures/templates/delete",
    "/onboard/role", "/onboard/role/delete", "/onboard/welcome", "/onboard/bulk/done",
    "/setup/import", "/setup/fresh", "/setup/verify",
    "/calendars/index/rebuild",
}
# Single-target LOW writes: one account/calendar, reversible, and guard.evaluate asks no confirmation
# for them. (/calendars/share to a group also fans out an additive subscribe to its members.)
LOW_WRITES = {
    "/users/signature", "/users/signout", "/users/groups/add", "/users/groups/remove",
    "/users/delegate/add", "/users/delegate/remove", "/users/organization",
    "/users/calendar/add", "/users/calendar/remove", "/users/vacation/set", "/users/vacation/off",
    "/groups/members", "/calendars/share", "/calendars/unshare",
}
EXEMPT = PREVIEWS | LOCAL_ONLY | LOW_WRITES

SEC_CAL = "c_house123@group.calendar.google.com"   # a secondary calendar; owner alice (active) per fixtures
HIRES_CSV = "role,name,email,assignee\nSales,Ada Byte,ada@example.com,it@example.com\n"


def _sales_role(client) -> None:
    client.post("/onboard/role", data={"name": "Sales", "steps": "Set up POS", "org_unit": "/Sales"})


def _ten_deletes(client) -> None:   # destructive + bulk: the typed confirmation
    for _ in range(10):
        client.post("/builder/sequence/add", data={"cid": "build.delete_user", "email": "carol@example.com"})


@dataclass
class Case:
    bare: dict                               # plausible form data (incl. what hx-include adds), no confirmation
    preview: str                             # the route that renders the confirm step
    preview_data: Optional[dict] = None      # its form, when not `bare`
    preview_files: Optional[dict] = None
    typed: dict = field(default_factory=dict)            # what the operator types into the confirm step
    setup: Optional[Callable] = None


GATED = {
    "/users/suspend/apply": Case({"email": "alice@example.com", "suspend": "on"}, "/users/suspend/preview",
                                 {"email": "alice@example.com"}),
    "/users/bulk/apply": Case({"store": "Downtown", "group": "", "emails": "alice@example.com"}, "/users/bulk/preview"),
    "/users/delete/apply": Case({"email": "alice@example.com"}, "/users/delete/confirm",
                                typed={"confirm": "alice@example.com"}),
    "/calendars/event/delete": Case({"cal": "aspen@resource.calendar.google.com", "event_id": "evt-weekly-standup"},
                                    "/calendars/event/preview"),
    "/calendars/delete": Case({"cal": SEC_CAL, "label": "Team Calendar"}, "/calendars/delete/preview",
                              typed={"confirm": "DELETE"}),
    "/lifecycle/offboard/run": Case({"user": "carol@example.com", "manager": "alice@example.com", "subject": "s",
                                     "message": "m", "days": "30", "notify": ""}, "/lifecycle/offboard/preview"),
    "/signatures/apply": Case({"template": "{name}", "scope_type": "company", "scope_value": ""}, "/signatures/preview"),
    "/onboard/run": Case({"role": "Sales", "name": "Ada Byte", "email": "ada@example.com",
                          "assignee": "it@example.com", "create_account": "1"}, "/onboard/preview", setup=_sales_role),
    "/onboard/bulk/run": Case({"csv_text": HIRES_CSV}, "/onboard/bulk/preview", preview_data={},
                              preview_files={"csv_file": ("hires.csv", HIRES_CSV.encode(), "text/csv")},
                              setup=_sales_role),
    "/builder/run": Case({"cid": "build.suspend_user", "email": "alice@example.com"}, "/builder/preview"),
    "/builder/sequence/run": Case({}, "/builder/sequence/preview", typed={"confirm": "confirm"}, setup=_ten_deletes),
}


def _post_routes():
    # The OpenAPI schema is FastAPI's public, flattened view of every route (app.routes nests each
    # included router). A route with include_in_schema=False would hide from it — hence the
    # decorator count in test_every_post_route_is_classified.
    app = create_app(AppState(vault=None, runner=None), allowed_hosts=TEST_HOSTS)
    return sorted(p for p, ops in app.openapi()["paths"].items() if "post" in ops)


POST_ROUTES = _post_routes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAM_MOCK_FIXTURES", str(FIXTURES))
    vault = SecretsVault(InMemoryBackend())
    vault.set_all(DOMAIN, {"client_secrets": "{}", "oauth2": "tok", "oauth2service": '{"client_id": "x"}'})
    runner = GAMRunner(vault=vault, gam_binary=FIXTURES / "mock_gam.sh", base_dir=tmp_path)
    conn = GAMConnector(runner=runner, domain=DOMAIN, audit=AuditLog(tmp_path / "audit.jsonl"))
    state = AppState(vault=vault, runner=runner, audit_domain=DOMAIN, connector=conn, token="t",
                     calendar_index=CalendarIndex(tmp_path / "calendar_index.db"))
    state.runbooks = RunbookStore(tmp_path / "onboarding.json")          # never the real ~/Library files
    state.sig_templates = SignatureStore(tmp_path / "signatures.json")
    with TestClient(create_app(state, allowed_hosts=TEST_HOSTS)) as c:
        c.get("/?token=t")
        yield c


def _finish_jobs(client) -> int:
    """Await every job a route started (its writes run in the background), and count them."""
    jobs = [j for j in client.app.state.gamgui.jobs.values() if getattr(j, "task", None) is not None]
    for job in jobs:
        wait_for_job(client, job)
    return len(jobs)


class _ConfirmStep(HTMLParser):
    """What the element posting to ``route`` sends: its hx-vals and, for a form, its own fields."""

    def __init__(self, route: str) -> None:
        super().__init__(convert_charrefs=True)
        self.route, self.found, self.fields = route, False, {}
        self._in_form, self._textarea = False, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("hx-post") == self.route:
            self.found = True
            self.fields.update(json.loads(a.get("hx-vals") or "{}"))
            self._in_form = tag == "form"
        elif self._in_form and a.get("name"):
            if tag == "input" and a.get("value") is not None:   # a typed field has no value: Case.typed
                self.fields[a["name"]] = a["value"]
            elif tag == "textarea":
                self._textarea = a["name"]
                self.fields[a["name"]] = ""

    def handle_data(self, data):
        if self._textarea:
            self.fields[self._textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self._textarea = None
        elif tag == "form":
            self._in_form = False


def test_every_post_route_is_classified():
    decorators = sum(len(re.findall(r"@\w+\.post\(", p.read_text())) for p in WEB_DIR.rglob("*.py"))
    assert decorators == len(POST_ROUTES), "a POST route is missing from the OpenAPI schema"
    unknown = [p for p in POST_ROUTES if p not in EXEMPT and p not in GATED]
    assert not unknown, f"classify these POST routes (exempt with a reason, or GATED with a form): {unknown}"
    stale = (EXEMPT | set(GATED)) - set(POST_ROUTES)
    assert not stale, f"no longer routes: {sorted(stale)}"
    assert not EXEMPT & set(GATED)


@pytest.mark.parametrize("route", [p for p in POST_ROUTES if p not in EXEMPT])
def test_bare_post_runs_no_write(route, client, gam_calls):
    case = GATED.get(route)
    assert case, f"{route} is unclassified — see test_every_post_route_is_classified"
    if case.setup:
        case.setup(client)
    r = client.post(route, data=case.bare)
    assert r.status_code == 200
    started = _finish_jobs(client)
    assert gam_writes(gam_calls()) == [], f"{route} wrote without confirmation"
    assert started == 0
    assert client.app.state.gamgui.connector.audit.tail() == []


@pytest.mark.parametrize("route", sorted(GATED))
def test_the_rendered_confirm_step_runs_the_write(route, client, gam_calls):
    case = GATED[route]
    if case.setup:
        case.setup(client)
    data = case.bare if case.preview_data is None else case.preview_data
    shown = client.post(case.preview, data=data, files=case.preview_files)
    step = _ConfirmStep(route)
    step.feed(shown.text)
    assert step.found, f"{case.preview} rendered no control posting to {route}"
    r = client.post(route, data={**case.bare, **step.fields, **case.typed})
    assert r.status_code == 200 and "needs confirmation" not in r.text and "Type " not in r.text, r.text[:300]
    _finish_jobs(client)
    # (Not assert_ok_partial: the account-deleted panel is amber on purpose.)
    assert gam_writes(gam_calls()), f"the confirmed POST to {route} ran no write"
    audited = client.app.state.gamgui.connector.audit.tail()
    assert audited and all(e["ok"] for e in audited), audited


def test_every_write_button_disables_itself_while_in_flight():
    # A double-click on Run must not start two offboardings: each control that posts to a write
    # (or starts a job) carries hx-disabled-elt, so htmx disables it until the response lands.
    writes = set(GATED) | LOW_WRITES | {"/calendars/index/rebuild"}
    missing, inheriting = [], []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text()
        for tag in re.finditer(r"<\w+\b[^>]*>", text):
            m = re.search(r'hx-post="([^"]+)"', tag.group(0))
            if m and m.group(1) in writes and "hx-disabled-elt=" not in tag.group(0):
                missing.append(f"{path.name}: {m.group(1)}")
        # hx-disabled-elt is inherited, and "find …" resolves from the element making the request: a
        # form's other htmx control (a Cancel) would find nothing, and htmx throws on the null.
        for form in re.finditer(r'<form\b[^>]*hx-disabled-elt="find [^>]*>(.*?)</form>', text, re.S):
            for tag in re.finditer(r"<\w+\b[^>]*>", form.group(1)):
                if re.search(r"hx-(get|post)=", tag.group(0)) and "hx-disabled-elt=" not in tag.group(0):
                    inheriting.append(f"{path.name}: {tag.group(0)[:60]}")
    assert not missing, missing
    assert not inheriting, inheriting
