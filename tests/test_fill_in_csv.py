"""Reports' completeness buckets as spreadsheets to fill in (roadmap 3.3, D3): no title, department, phone or location."""

from __future__ import annotations

import csv
import io
import re

from gamgui.core import reports
from gamgui.core.gam.errors import GAMError, GAMErrorKind
from gamgui.core.gam.models import GAMUser

from .test_users_web import client  # noqa: F401  (the fixture)


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_each_completeness_card_and_only_those_offers_the_sheet(client):  # noqa: F811
    page = client.get("/reports").text
    links = set(re.findall(r'<a href="/reports/fill-in\.csv\?key=(\w+)" download', page))
    shown = {r for r in reports.FILL_IN_REPORTS if f'id="report-{r}"' in page and f"key={r}" in page}
    assert links == shown and links <= set(reports.FILL_IN_REPORTS) and "key=no_2sv" not in page


def test_the_sheet_holds_the_bucket_in_the_columns_bulk_apply_reads(client):  # noqa: F811
    r = client.get("/reports/fill-in.csv", params={"key": "no_title"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="no-title.csv"'
    rows = _rows(r.text)
    assert tuple(rows[0]) == reports.FILL_IN_COLUMNS
    # The same people the card counts and lists, from the same cached directory, by address.
    page = client.get("/reports").text
    for key in reports.FILL_IN_REPORTS:
        card = page.split(f'<div id="report-{key}"', 1)[1].split('<div id="report-', 1)[0]
        listed = re.findall(r"— ([^<\s]+@[^<\s]+)\s*</span>", card)
        sheet = _rows(client.get("/reports/fill-in.csv", params={"key": key}).text)[1:]
        assert len(sheet) == int(re.search(r'text-2xl[^"]*">(\d+)<', card).group(1)) == len(listed), key
        assert [row[0] for row in sheet] == sorted(listed, key=str.lower), key
    assert any(_rows(client.get("/reports/fill-in.csv", params={"key": k}).text)[1:] for k in reports.FILL_IN_REPORTS)


def test_a_value_that_would_run_as_a_formula_is_defused(client, monkeypatch):  # noqa: F811
    st = client.app.state.gamgui

    async def one(force=False, stale_ok=False):
        return [GAMUser(primary_email="eve@example.com", given_name="=HYPERLINK(\"http://x\")", family_name="", department="+1")]

    monkeypatch.setattr(st, "users", one)
    rows = _rows(client.get("/reports/fill-in.csv", params={"key": "no_title"}).text)
    assert rows[1][1].startswith("'=") and rows[1][3] == "'+1"


def test_only_a_completeness_bucket_downloads(client):  # noqa: F811
    for key in ("no_2sv", "admins", "", "../x"):
        r = client.get("/reports/fill-in.csv", params={"key": key})
        assert r.status_code == 400 and "attachment" not in r.headers.get("content-disposition", "")


def test_a_directory_that_couldnt_be_read_is_never_an_empty_sheet(client, monkeypatch):  # noqa: F811
    # Invariant 10: a failed read is said, never a sheet with only its titles, which would read as "everyone has a title".
    st = client.app.state.gamgui

    async def refused(force=False, stale_ok=False):
        raise GAMError(exit_code=1, stderr="ERROR: 403: Not Authorized to access this resource/api", argv=["print", "users"],
                       kind=GAMErrorKind.PERMISSION_DENIED)

    monkeypatch.setattr(st, "users", refused)
    r = client.get("/reports/fill-in.csv", params={"key": "no_title"})
    assert r.status_code == 502 and r.headers["content-type"].startswith("text/plain") and "email" not in r.text
