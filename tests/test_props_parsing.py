"""Properties of the code that parses text from outside the app.

Two sources, one rule — whatever arrives, parse it or refuse it, never 500:

* GAM's stdout (``core/gam/parser.py``): JSON object / array / newline-delimited JSON / CSV with a
  ``JSON`` column / plain CSV. Every read view and the Builder's 512 auto-promoted read commands
  (invariant 3) go through ``parse_records``, whose contract is "never raises on shape".
* The bulk-hire CSV an operator uploads (``core/onboarding.py``): each row it returns becomes a real
  account create, group add and password handoff, so a row must carry exactly the columns it was given.
  ``looks_like_email`` is the gate that keeps a comma (GAM splits a <UserList> on it) or a keyword trap
  out of those argv lists (invariant 1 keeps the value one element; this keeps it one *address*).

Six of these found real bugs when they were written (2026-09-30), fixed in the same change: an
oversized CSV cell, a bare CR, deep JSON nesting and U+2028 inside NDJSON broke parse_records; a column
with an empty header filled every missing hire field, notify included; and render expanded a {token}
inside a value.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

from gamgui.core.gam.parser import parse_one, parse_records
from gamgui.core.onboarding import (
    HIRE_COLUMNS,
    WELCOME_VARS,
    looks_like_email,
    parse_hire_csv,
    render,
    split_name,
)

# --- strategies -------------------------------------------------------------------------

# Text that reaches the csv module. Python 3.10 (CI's floor leg) refuses a NUL anywhere with
# csv.Error("line contains NUL"); 3.11+ reads it as data, so only 3.11+ generates it.
_ANY = st.characters(blacklist_categories=("Cs",),
                     blacklist_characters="\x00" if sys.version_info < (3, 11) else "")

# Characters str.isspace() is true for all sit in these categories (\t \n \x1c-\x1f \x85 are Cc).
_NOT_SPACE = st.characters(blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"))
# The three line breaks str.splitlines() honours that json.dumps(ensure_ascii=False) leaves raw.
_RAW_BREAKS = "\x85  "
_JSON_TEXT = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters=_RAW_BREAKS),
                     max_size=12)
# No floats: NaN != NaN would make an honest round trip look lossy.
_JSON_SCALAR = st.none() | st.booleans() | st.integers() | _JSON_TEXT
_JSON_VALUE = st.recursive(_JSON_SCALAR, lambda c: st.lists(c, max_size=3)
                           | st.dictionaries(_JSON_TEXT, c, max_size=3), max_leaves=6)
_RECORD = st.dictionaries(_JSON_TEXT, _JSON_VALUE, max_size=4)

# Column names GAM prints: real ones plus camelCase / dotted identifiers. Never "JSON" (that is the
# formatjson column) and never a bare JSON literal, which no GAM header is.
_HEADER = st.one_of(
    st.sampled_from(["primaryEmail", "id", "name.fullName", "orgUnitPath", "calendarId", "email",
                     "role", "type", "summary", "suspended"]),
    st.from_regex(r"[a-z][A-Za-z0-9]{0,8}(\.[a-z][A-Za-z0-9]{0,8}){0,2}", fullmatch=True),
).filter(lambda h: h not in {"true", "false", "null"})
# Arbitrary text rarely draws the characters CSV quoting is about, so half the cells are made of them.
_CELL = st.text(_ANY, max_size=15) | st.text(st.sampled_from(',"\r\n a1'), max_size=6)

_LOCAL = st.text(st.sampled_from("abcxyz019._+-"), min_size=1, max_size=8)
_LABEL = st.text(st.sampled_from("abcdefxyz09-"), min_size=1, max_size=6)
_EMAIL = st.builds(lambda loc, labels, up: (loc + "@" + ".".join(labels)).upper() if up
                   else loc + "@" + ".".join(labels),
                   _LOCAL, st.lists(_LABEL, min_size=2, max_size=3), st.booleans())


def _csv(rows, lineterminator="\n") -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator=lineterminator).writerows(rows)
    return buf.getvalue()


def _noise() -> st.SearchStrategy[str]:
    """Text that looks enough like GAM output to reach every branch: JSON fragments, CSV punctuation,
    a JSON column header, ragged rows, raw line separators."""
    frag = st.one_of(_CELL, _JSON_VALUE.map(json.dumps),
                     st.sampled_from(["JSON", "primaryEmail,JSON", ",", '"', "{", "[", "}", " "]))
    return st.tuples(st.lists(frag, max_size=8), st.sampled_from(["\n", ",", "", "\r\n"])) \
        .map(lambda t: t[1].join(t[0]))


# --- core/gam/parser.py ------------------------------------------------------------------


# Every CR is followed by LF here: a bare CR in an unquoted field is a known bug, pinned by
# test_parse_records_survives_a_bare_carriage_return below.
@given(st.one_of(st.text(_ANY), _noise()).map(lambda s: s.replace("\r", "\r\n")))
def test_parse_records_never_raises_and_returns_str_keyed_dicts(text):
    # Ragged CSV rows: DictReader files the overflow under a None key, which must be dropped.
    records = parse_records(text)
    assert isinstance(records, list)
    for rec in records:
        assert isinstance(rec, dict)
        assert all(isinstance(k, str) for k in rec)
    assert parse_one(text) == (records[0] if records else {})


@st.composite
def _json_output(draw):
    """(stdout, expected records) for the three JSON shapes GAM prints with formatjson."""
    shape = draw(st.sampled_from(["object", "array", "ndjson"]))
    if shape == "object":
        rec = draw(_RECORD)
        return json.dumps(rec, ensure_ascii=False), [rec]
    if shape == "array":
        items = draw(st.lists(_RECORD | _JSON_SCALAR, max_size=5))   # non-objects are dropped
        indent = draw(st.sampled_from([None, 2]))
        return json.dumps(items, ensure_ascii=False, indent=indent), [i for i in items if isinstance(i, dict)]
    recs = draw(st.lists(_RECORD, max_size=5))
    sep = draw(st.sampled_from(["\n", "\r\n", "\n\n"]))
    return sep.join(json.dumps(r, ensure_ascii=False) for r in recs) + draw(st.sampled_from(["", "\n"])), recs


@given(_json_output())
def test_parse_records_round_trips_json_shapes(case):
    text, expected = case
    assert parse_records(text) == expected
    assert parse_one(text) == (expected[0] if expected else {})


@st.composite
def _plain_csv(draw):
    headers = draw(st.lists(_HEADER, min_size=1, max_size=5, unique=True))
    rows = draw(st.lists(st.fixed_dictionaries({h: _CELL for h in headers}), max_size=5))
    if rows:
        # parse_records strips the whole payload, so trailing whitespace on the very last cell is the
        # one spot it does not keep (a quoted cell ends in '"' and is unaffected).
        rows[-1][headers[-1]] = rows[-1][headers[-1]].rstrip()
    lt = draw(st.sampled_from(["\n", "\r\n"]))
    return _csv([headers] + [[r[h] for h in headers] for r in rows], lt), rows


@given(_plain_csv())
def test_parse_records_round_trips_plain_csv(case):
    text, expected = case
    assert parse_records(text) == expected


@st.composite
def _formatjson_csv(draw):
    """`print ... formatjson`: key columns (primaryEmail, calendarId, ...) then a JSON column."""
    siblings = draw(st.lists(_HEADER, max_size=3, unique=True))
    key = st.sampled_from(siblings) | _JSON_TEXT if siblings else _JSON_TEXT   # collide on purpose
    rec_st = st.dictionaries(key, _JSON_SCALAR, max_size=4)
    rows, expected = [], []
    for _ in range(draw(st.integers(0, 4))):
        sib = draw(st.fixed_dictionaries({h: _CELL for h in siblings}))
        cells = [sib[h] for h in siblings]
        kind = draw(st.sampled_from(["object", "array", "no records", "short"]))
        if kind == "short":            # ragged: the row stops before its JSON cell -> skipped
            rows.append(cells[:draw(st.integers(0, len(cells)))])
            continue
        if kind == "object":
            recs = [draw(rec_st)]
            cell = json.dumps(recs[0], ensure_ascii=False)
        elif kind == "array":          # one row, several records, each gets the row's siblings
            payload = draw(st.lists(rec_st | _JSON_SCALAR, max_size=3))
            recs = [r for r in payload if isinstance(r, dict)]
            cell = json.dumps(payload, ensure_ascii=False)
        else:                          # not JSON, or a JSON scalar -> the row yields nothing
            recs = []
            cell = draw(st.sampled_from(["", "{", "[1,", "n/a"]) | _JSON_SCALAR.map(json.dumps))
        # Overflow cells past the header land under DictReader's None key and are dropped.
        rows.append(cells + [cell] + draw(st.lists(_CELL, max_size=2)))
        # Blank sibling cells are dropped; the JSON record wins every key conflict.
        expected += [{**{k: v for k, v in sib.items() if v != ""}, **r} for r in recs]
    return _csv([siblings + ["JSON"]] + rows), expected


@given(_formatjson_csv())
def test_parse_records_round_trips_formatjson_csv(case):
    text, expected = case
    assert parse_records(text) == expected


@settings(max_examples=5)
@given(st.integers(min_value=131_073, max_value=140_000), st.booleans())
def test_parse_records_survives_an_oversized_csv_cell(size, formatjson):
    big = "x" * size
    if formatjson:
        text = _csv([["primaryEmail", "JSON"], ["a@example.com", json.dumps({"notes": big})]])
    else:
        text = _csv([["primaryEmail", "notes"], ["a@example.com", big]])
    assert parse_records(text)[0]["notes"] == big


@settings(max_examples=10)
@given(st.text(st.sampled_from("abc xyz019.@-"), min_size=1, max_size=10),
       st.text(st.sampled_from("abc xyz019.@-"), max_size=10))
def test_parse_records_survives_a_bare_carriage_return(before, after):
    # A note typed on a classic-Mac line ending, in a column GAM's writer did not quote.
    records = parse_records("primaryEmail,notes\na@example.com," + before + "\r" + after)
    assert isinstance(records, list) and all(isinstance(r, dict) for r in records)


@settings(max_examples=3)
@given(st.integers(min_value=400_000, max_value=450_000))
def test_parse_records_survives_deep_json_nesting(depth):
    # One bracket per line (~87k levels overflow here), so no CSV field nears the csv size limit and
    # only the JSON recursion is under test.
    records = parse_records("[\n" * depth + "]\n" * depth)
    assert isinstance(records, list) and all(isinstance(r, dict) for r in records)


@settings(max_examples=20)
@given(st.lists(_RECORD, min_size=1, max_size=3), st.sampled_from(_RAW_BREAKS), _JSON_TEXT, _JSON_TEXT)
def test_parse_records_ndjson_keeps_raw_line_separators(recs, brk, a, b):
    recs = recs + [{"description": a + brk + b}]
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
    assert parse_records(text) == recs


# --- core/onboarding.py: the bulk-hire CSV ------------------------------------------------

_FLAG_FIELDS = {"create_account", "send_welcome"}
_HEADER_ERRORS = {"The CSV has no header row.",
                  "The CSV needs a 'role' column — that's what picks the template."}
_ROW_ERROR = re.compile(r"Row \d+: ")


@st.composite
def _hire_noise(draw):
    """A hire CSV that is sometimes right: real columns (any case/padding, some missing, extras),
    ragged rows, and cells mixing good and bad addresses, flag words and junk."""
    # Usually carry role plus an address column, so rows get far enough to meet the address gate.
    must = draw(st.sampled_from([[], ["role"], ["role", "email"], ["role", "assignee"], ["role", "email", "assignee"]]))
    more = draw(st.lists(st.sampled_from(HIRE_COLUMNS + ["extra", "Notes"]), unique=True, max_size=8))
    cols = draw(st.permutations(must + [c for c in more if c not in must]))
    cols = [draw(st.sampled_from([c, c.upper(), c.title(), " " + c + " "])) for c in cols]
    cell = st.one_of(_EMAIL, _CELL, st.sampled_from(
        ["", " ", "yes", "No", "Y", "on", "oauthuser", "@example.com", "a,b@example.com", "a@b", "a@@b.c",
         "dup@example.com", "Dup@Example.COM"]))   # the same address twice, in two cases
    rows = draw(st.lists(st.lists(cell, max_size=len(cols) + 2), max_size=8))
    return _csv([cols] + rows)


def _assert_well_formed(rows, errors):
    assert isinstance(rows, list) and isinstance(errors, list)
    seen = set()
    for row in rows:
        assert set(row) == set(HIRE_COLUMNS)
        for k, v in row.items():
            if k in _FLAG_FIELDS:
                assert isinstance(v, bool)
            else:
                assert isinstance(v, str) and v == v.strip()
        assert row["role"]
        assert row["email"] or row["assignee"]
        for addr in (row["email"], row["assignee"]):
            assert addr == "" or looks_like_email(addr)
        if row["email"]:
            assert row["email"].lower() not in seen   # one account per address
            seen.add(row["email"].lower())
    for err in errors:
        assert isinstance(err, str)
        assert err in _HEADER_ERRORS or _ROW_ERROR.match(err)


@given(st.one_of(st.text(), _hire_noise()))
def test_parse_hire_csv_never_raises_and_rows_are_well_formed(text):
    _assert_well_formed(*parse_hire_csv(text))


_TRUTHY = ["1", "true", "yes", "y", "x", "on"]
_FALSY = ["", "no", "0", "false", "n", "off", "nope"]
_STRIPPED = st.text(_ANY, max_size=12).map(str.strip)
_NONBLANK = st.builds(lambda c, s: (c + s).strip(), _NOT_SPACE, st.text(_ANY, max_size=12))


def _flag(value: bool) -> st.SearchStrategy[str]:
    word = st.sampled_from(_TRUTHY if value else _FALSY)
    return st.builds(lambda w, case, pad: pad + case(w) + pad, word,
                     st.sampled_from([str, str.upper, str.title]), st.sampled_from(["", " "]))


@st.composite
def _hire_rows(draw, role=_NONBLANK, name=_STRIPPED, min_emails=0):
    emails = draw(st.lists(_EMAIL, min_size=min_emails, max_size=6, unique_by=str.lower))
    rows = []
    for n, email in enumerate(emails):
        if n >= min_emails and draw(st.booleans()):
            email, assignee = "", email                    # assignee-only row
        else:
            assignee = draw(st.just("") | _EMAIL)          # an assignee may repeat
        rows.append({"role": draw(role), "name": draw(name), "email": email,
                     "manager": draw(_STRIPPED), "assignee": assignee,
                     "create_account": draw(st.booleans()), "first": draw(_STRIPPED),
                     "last": draw(_STRIPPED), "send_welcome": draw(st.booleans()),
                     "notify": draw(_STRIPPED)})
    return rows


@st.composite
def _hire_csv(draw, rows_strategy):
    rows = draw(rows_strategy)
    cols = draw(st.permutations(HIRE_COLUMNS))
    header = [draw(st.sampled_from([c, c.upper(), " " + c.title()])) for c in cols]
    body = [[draw(_flag(r[c])) if c in _FLAG_FIELDS else r[c] for c in cols] for r in rows]
    return _csv([header] + body, draw(st.sampled_from(["\n", "\r\n"]))), rows


@given(_hire_csv(_hire_rows()))
def test_parse_hire_csv_round_trips_valid_rows(case):
    text, expected = case
    assert parse_hire_csv(text) == (expected, [])


_AWKWARD = st.sampled_from([",", '"', "\n", "\r\n", '","', '""', ",\n"])


def _awkward_cell() -> st.SearchStrategy[str]:
    return st.builds(lambda a, sep, b: a + sep + b, _NOT_SPACE, _AWKWARD, _NOT_SPACE)


@given(_hire_csv(_hire_rows(role=_awkward_cell(), name=_awkward_cell())))
def test_parse_hire_csv_keeps_commas_quotes_newlines_inside_a_cell(case):
    text, expected = case
    rows, errors = parse_hire_csv(text)
    assert errors == []
    assert [(r["role"], r["name"]) for r in rows] == [(r["role"], r["name"]) for r in expected]


@settings(max_examples=25)
@given(_hire_rows(), st.lists(st.sampled_from(["name", "manager", "assignee", "first", "last", "notify"]),
                              unique=True, max_size=5),
       st.sampled_from(["", " "]), st.data())
def test_parse_hire_csv_ignores_a_column_with_no_header(rows, present, blank, data):
    # A ' ' header already works (it is never looked up); '' is the one that leaks.
    cols = ["role", "email"] + present
    base = [[r[c] for c in cols] for r in rows]
    stray = [data.draw(_EMAIL) for _ in rows]
    without = parse_hire_csv(_csv([cols] + base))
    with_blank = parse_hire_csv(_csv([cols + [blank]] + [b + [s] for b, s in zip(base, stray, strict=True)]))
    assert with_blank == without


@given(_hire_rows(min_emails=1), st.data())
def test_parse_hire_csv_refuses_a_repeated_address(rows, data):
    # One account per address: a later row repeating an email in any case is refused, naming the row
    # that kept it, and every other row still imports. Cells kept to one line, so body[k] is on line k + 2.
    rows = [{k: v.replace("\n", " ") if isinstance(v, str) else v for k, v in r.items()} for r in rows]
    first = data.draw(st.sampled_from([i for i, r in enumerate(rows) if r["email"]]))
    case = data.draw(st.sampled_from([str.upper, str.lower, str.swapcase, str]))
    dup = {**data.draw(st.sampled_from(rows)), "email": case(rows[first]["email"])}
    at = data.draw(st.integers(first + 1, len(rows)))
    body = rows[:at] + [dup] + rows[at:]
    text = _csv([HIRE_COLUMNS] + [[str(r[c]).lower() if c in _FLAG_FIELDS else r[c] for c in HIRE_COLUMNS]
                                  for r in body])
    assert parse_hire_csv(text) == (rows, ["Row {}: duplicate email {} (first on row {}).".format(
        at + 2, dup["email"], first + 2)])


# --- core/onboarding.py: welcome template, address gate, name split ------------------------


@given(st.text(), st.dictionaries(st.text(max_size=8), st.text()))
def test_render_never_raises_and_leaves_brace_free_text_alone(template, ctx):
    out = render(template, ctx)
    assert isinstance(out, str)
    if "{" not in template:
        assert out == template
    # Only the WELCOME_VARS keys of ctx are ever read.
    assert out == render(template, {k: v for k, v in ctx.items() if k in WELCOME_VARS})


_NO_BRACES = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="{}"), max_size=10)
_UNKNOWN_VAR = st.one_of(st.sampled_from(["first", "last", "title", "ou", "Name", "NAME", " name", ""]),
                         st.from_regex(r"[a-z_][a-z0-9_]{0,8}", fullmatch=True)) \
    .filter(lambda k: k not in WELCOME_VARS)


@given(st.lists(st.one_of(_NO_BRACES.map(lambda s: ("lit", s)),
                          st.sampled_from(WELCOME_VARS).map(lambda k: ("var", k)),
                          _UNKNOWN_VAR.map(lambda k: ("unk", k))), max_size=8),
       st.dictionaries(st.sampled_from(WELCOME_VARS), _NO_BRACES))
def test_render_substitutes_only_welcome_vars(parts, ctx):
    template = "".join(v if kind == "lit" else "{" + v + "}" for kind, v in parts)
    expected = "".join(v if kind == "lit" else ctx.get(v, "") if kind == "var" else "{" + v + "}"
                       for kind, v in parts)
    assert render(template, ctx) == expected


@settings(max_examples=20)
@given(st.sampled_from(WELCOME_VARS[1:]), _NO_BRACES, _NO_BRACES, _EMAIL)
def test_render_inserts_values_verbatim(later, pre, post, other):
    name = pre + "{" + later + "}" + post
    assert render("Welcome {name}", {"name": name, later: other}) == "Welcome " + name


_NEAR_EMAIL = st.one_of(
    st.text(),
    st.text(st.sampled_from("ab.@, \t\n\xa0 \x85"), max_size=14),
    st.builds(lambda e, i, junk: e[:i % (len(e) + 1)] + junk + e[i % (len(e) + 1):],
              _EMAIL, st.integers(0, 40), st.sampled_from([",", "@", " ", "\t", "\n", "\xa0", " ", "."])),
)


@given(_NEAR_EMAIL)
def test_looks_like_email_rejects_commas_whitespace_and_extra_ats(value):
    if not looks_like_email(value):
        return
    s = value.strip()
    assert s.count("@") == 1
    assert "," not in s
    assert not any(c.isspace() for c in s)
    local, domain = s.split("@")
    assert local
    labels = domain.split(".")
    assert len(labels) >= 2 and all(labels)   # a dotted domain, no empty label


_ADDR_CHAR = st.characters(blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"), blacklist_characters="@,")


@given(st.text(_ADDR_CHAR, min_size=1, max_size=8),
       st.lists(st.text(st.characters(blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"),
                                      blacklist_characters="@,."), min_size=1, max_size=6),
                min_size=2, max_size=4),
       st.sampled_from(["", " ", "\t", "\n"]))
def test_looks_like_email_accepts_every_well_formed_address(local, labels, pad):
    # The other direction: the gate is not stricter than it says (non-ASCII, '+', dots in the local part).
    assert looks_like_email(pad + local + "@" + ".".join(labels) + pad)


_WORDS = st.lists(st.text(_NOT_SPACE, min_size=1, max_size=6), max_size=4)
_GAPS = st.sampled_from([" ", "  ", "\t", " \n ", "\xa0", " "])


@given(st.one_of(st.text(max_size=20), st.builds(lambda ws, g: g + g.join(ws) + g, _WORDS, _GAPS)),
       st.one_of(st.just(""), st.just("  "), st.text(max_size=8)),
       st.one_of(st.just(""), st.just("  "), st.text(max_size=8)))
def test_split_name_explicit_names_win(name, first, last):
    f, lst = split_name(name, first, last)
    assert f == f.strip() and lst == lst.strip()
    if first.strip() or last.strip():
        assert (f, lst) == (first.strip(), last.strip())
    else:
        words = name.split()
        assert f == (words[0] if words else "")
        assert lst == " ".join(words[1:])   # runs of any whitespace collapse to one space


_SIG_VARS = ["{name}", "{first}", "{last}", "{email}", "{title}", "{role}", "{phone}", "{department}",
             "{location}", "{ou}"]


@given(st.sampled_from(_SIG_VARS), st.text(max_size=20))
def test_render_signature_inserts_directory_values_verbatim(var, noise):
    # The signature renderer had render()'s shape: a name typed as "{phone}" came out as the phone
    # number. Directory values are data — each is inserted as written, whatever braces it holds.
    from gamgui.core.gam.models import GAMUser
    from gamgui.core.signatures import render_signature
    name = var + noise
    user = GAMUser(primary_email="ada@example.com", given_name=name, family_name="Byte",
                   phone="+1 555 0100", title="Lead", department="Ops", location="HQ")
    assert render_signature("{first}", user) == name
