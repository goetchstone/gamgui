"""GAM's CSV reads back whole: the parser and the mock are held to how the vendored build writes CSV.

GAM writes every CSV in its setDialect: quotes doubled AND a backslash escape character. The parser read
it without the escape, so a `print … formatjson` row whose JSON held a quote was dropped without a word and
every backslash came back doubled (failure log 2026-10-30). Nothing failed, because the mock printed
NDJSON and the property tests' writer had no escape either: every CSV in the suite was written by the
same blind spot. Two holds here:

* The build's own. scripts/gam_rc_table.py, run with the build's Python (CI's real-reference job hands its
  JSON over as GAM_RC_TABLE), refuses a build whose setDialect or csv_output_no_escape_char default moved,
  and writes sample rows in that dialect. The parser must read them back as they were. Where GAM isn't
  vendored, or this Python can't read the build, this skips, as tests/test_gam_exit_codes.py does (and
  fails instead under EXIT_CODES_REQUIRE_GAM).
* The mock's. Its `print users` must be the bytes Python's csv writer prints in GAM's dialect over the
  fixture records, carol's escaped title and department included.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.parser import parse_records

from .test_gam_exit_codes import GAM_BIN, TABLE, _unavailable, rct


@pytest.fixture(scope="module")
def build_csv():
    """[{"rows", "text"}]: sample rows and the text the build's own Python writes for them."""
    if not GAM_BIN.exists():
        _unavailable("GAM is not vendored (make gam)")
    blob = GAM_BIN.read_bytes()
    if os.environ.get(TABLE):   # written by the build's own Python (CI runs scripts/gam_rc_table.py with it)
        given = json.loads(Path(os.environ[TABLE]).read_text())
        assert given["sha256"] == hashlib.sha256(blob).hexdigest(), f"{TABLE} was read from another gam"
        assert "csv" in given, f"{TABLE} has no csv: it came from an older scripts/gam_rc_table.py"
        return given["csv"]
    try:
        return rct.read(GAM_BIN)["csv"]
    except rct.WrongPython as e:
        _unavailable(f"{e} (or set {TABLE} to its JSON, from scripts/gam_rc_table.py under that Python)")


def _records(rows: list) -> list:
    """What the parser should make of rows GAM printed: a formatjson table's JSON record over its
    non-empty key columns, or a plain table's row as a dict."""
    header, *body = rows
    if header[-1] != "JSON":
        return [dict(zip(header, row, strict=True)) for row in body]
    return [{**{k: v for k, v in zip(header[:-1], row[:-1], strict=True) if v != ""}, **json.loads(row[-1])}
            for row in body]


def test_the_parser_reads_back_what_the_build_writes(build_csv):
    formatjson, plain = build_csv
    assert '\\\\""' in formatjson["text"], "the sample no longer holds a quote inside its JSON, the dropped row"
    assert "\\\\" in plain["text"], "the plain sample no longer holds a backslash"
    for sample in build_csv:
        assert parse_records(sample["text"]) == _records(sample["rows"])


def _writer_escapes_its_escape_char() -> bool:
    out = io.StringIO()
    csv.writer(out, **rct.GAM_DIALECT).writerow(["a\\b"])
    return out.getvalue() == "a\\\\b\n"


async def test_the_mock_prints_users_as_gam_writes_them(runner, domain, fixtures_dir):
    if not _writer_escapes_its_escape_char():
        pytest.skip("this Python's csv writer doesn't escape its escape character; GAM's own (3.15) does")
    lines = [ln for ln in (fixtures_dir / "print_users.json").read_text().split("\n") if ln]
    assert any('\\"' in ln for ln in lines) and any("\\\\" in ln for ln in lines), \
        "no fixture user needs the escapes any more (carol's title and department did)"
    expected = rct.gam_csv([["primaryEmail", "JSON"]] + [[json.loads(ln)["primaryEmail"], ln] for ln in lines])
    assert await runner.run_authenticated(domain, C.print_users()) == expected
