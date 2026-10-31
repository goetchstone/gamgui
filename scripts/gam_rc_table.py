#!/usr/bin/env python3
"""Read the vendored gam's exit-code table (its `NAME_RC = <int>` constants) and how it writes CSV, with
the stdlib alone.

    python -I scripts/gam_rc_table.py gamgui/resources/gam7/gam > gam_rc.json
    python -I scripts/gam_rc_table.py --python-version gamgui/resources/gam7/gam    # e.g. 3.15

GAM is a PyInstaller build, and the table lives in its marshalled bytecode, which loads only in the
Python that wrote it. That can be newer than the suite's (7.48.26 is 3.15's, and parts of the dev lock
have no 3.15 wheels yet), so CI sets up the build's own Python beside the suite's and runs this with
it: it imports nothing outside the stdlib, so it needs no install. The JSON names the binary's
SHA-256, and tests/test_gam_exit_codes.py reads it through GAM_RC_TABLE. `--python-version` reads the
build's version from the archive cookie and runs under any Python. On another Python the table read
exits 1: it never falls back to a guess.

The JSON also carries `csv`: rows that need every escape GAM's dialect has, as this Python's csv module
writes them in that dialect — refused (exit 1) unless the build's own setDialect and its
csv_output_no_escape_char default still say that dialect. tests/test_gam_csv_dialect.py reads them back
with the app's parser (failure log 2026-10-30: the parser had no escape character and dropped rows).
And `progress`: every gamlib.glmsgs message that starts "Getting" or "Got", the words core/gam/errors.py
skips as progress; tests/test_gam_progress.py holds it (failure log 2026-10-30, progress read as the error).
"""

from __future__ import annotations

import argparse
import csv
import dis
import hashlib
import importlib.util
import inspect
import io
import json
import marshal
import struct
import sys
import zlib
from pathlib import Path

# How GAM writes every CSV: setDialect with csv_output_no_escape_char off, its default, lines ending in
# "\n" (read at 7.48.22: gam/__init__.py:8830-8853, gamlib/glcfg.py:373). `dialect_in_build` refuses a
# build where this moved.
GAM_DIALECT = {"delimiter": ",", "doublequote": True, "escapechar": "\\", "lineterminator": "\n",
               "quotechar": '"', "quoting": csv.QUOTE_MINIMAL, "skipinitialspace": False, "strict": False}

# A `print … formatjson` table and a plain one, each value needing an escape: a quote inside a JSON string
# (the row the parser dropped), a backslash, a newline, a comma, a quote in a plain cell, non-ASCII.
_RECORDS = [
    {"primaryEmail": "ann@example.com", "name": {"fullName": 'Ann "Q" Lee'}, "orgUnitPath": "/"},
    {"primaryEmail": "bo@example.com", "notes": "C:\\dir\\x", "title": "line1\nline2"},
    {"primaryEmail": "cy@example.com", "name": {"fullName": "Cy, Plain"}, "department": "Zürich \\ R&D"},
]
CSV_SAMPLES = [
    [["primaryEmail", "JSON"]]
    + [[r["primaryEmail"], json.dumps(r, ensure_ascii=False, sort_keys=True)] for r in _RECORDS],
    [["User", "delegateAddress", "note"], ["ann@example.com", "d@example.com", "back\\slash"],
     ["bo@example.com", "e@example.com", 'say "hi", twice\\']],
]

# PyInstaller's CArchive, appended to the executable (PyInstaller/archive/readers.py): a trailing cookie
# (magic, archive length, TOC offset, TOC length, Python version, libpython name), and TOC entries
# (entry length, data offset, data length, uncompressed length, compressed flag, typecode, then the name).
_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE = struct.Struct("!8sIIII64s")
_TOC_ENTRY = struct.Struct("!IIIIBc")


class WrongPython(Exception):
    """The build's bytecode is another Python's, so marshal can't load it here."""


def _cookie(blob: bytes):
    at = blob.rfind(_COOKIE_MAGIC)
    if at < 0:
        raise ValueError("no PyInstaller archive cookie in the vendored gam")
    return at, _COOKIE.unpack_from(blob, at)


def build_python(blob: bytes) -> str:
    """The Python the build was frozen with, as "3.15" (the cookie stores 315)."""
    pyvers = _cookie(blob)[1][4]
    return f"{pyvers // 100}.{pyvers % 100}"


def gam_module_code(blob: bytes):
    """The code object of the `gam` package, from the PYZ inside the executable's CArchive."""
    return module_code(blob, "gam")


def module_code(blob: bytes, name: str):
    """The code object of one module of the build (`gam`, `gamlib.glcfg`), from its PYZ."""
    at, (_, length, toc_offset, toc_length, _, _) = _cookie(blob)
    start = at + _COOKIE.size - length
    pos, end, pyz = start + toc_offset, start + toc_offset + toc_length, None
    while pos < end and pyz is None:
        size, offset, *_, typecode = _TOC_ENTRY.unpack_from(blob, pos)
        pyz = start + offset if typecode == b"z" else None
        pos += size
    # The PYZ (pyimod01_archive.py): magic, the bytecode's Python magic number, TOC offset, then entries
    # of zlib-compressed marshalled code, and the TOC as a marshalled [(name, (typecode, offset, length))].
    if pyz is None or blob[pyz:pyz + 4] != b"PYZ\0":
        raise ValueError("no PYZ archive in the vendored gam's CArchive")
    if blob[pyz + 4:pyz + 8] != importlib.util.MAGIC_NUMBER:   # marshalled code loads only in its own Python
        raise WrongPython(f"the build's bytecode is Python {build_python(blob)}'s; "
                          f"run under that Python, not {sys.version_info[0]}.{sys.version_info[1]}")
    (pyz_toc,) = struct.unpack_from("!i", blob, pyz + 8)
    # The vendored binary is checksum-pinned and runs as the app's own subprocess: trusted, not input.
    toc = dict(marshal.loads(blob[pyz + pyz_toc:]))  # noqa: S302
    if name not in toc:
        raise ValueError(f"no module {name} in the vendored gam's PYZ: read the new tag's source")
    _, offset, size = toc[name]
    return marshal.loads(zlib.decompress(blob[pyz + offset:pyz + offset + size]))  # noqa: S302


def rc_table(code) -> dict[str, int]:
    """NAME_RC -> int for every module-level `NAME_RC = <int>` in the `gam` package."""
    table, prev = {}, None
    for ins in dis.get_instructions(code):
        if ins.opname == "EXTENDED_ARG":   # pairing through it reads its argument, not the constant's
            continue
        if ins.opname == "STORE_NAME" and str(ins.argval).endswith("_RC") and isinstance(getattr(prev, "argval", None), int):
            table[ins.argval] = prev.argval
        prev = ins
    if table.get("UNKNOWN_ERROR_RC") != 1 or len(set(table.values())) <= 10:
        raise ValueError("the build's RC table did not parse")
    return table


def _nested(parent):
    for const in parent.co_consts:
        if inspect.iscode(const):
            yield const
            yield from _nested(const)


def _loads(code) -> list:
    """What a function loads, in order, each as (kind, value): a constant (however this Python's compiler
    stores it), a local, a closure cell, a global or an attribute."""
    out = []
    for ins in dis.get_instructions(code):
        name = ins.opname
        if name in ("LOAD_CONST", "LOAD_SMALL_INT", "LOAD_COMMON_CONSTANT"):
            out.append(("const", ins.argval))
        elif name.startswith("LOAD_FAST"):
            out += [("fast", v) for v in (ins.argval if isinstance(ins.argval, tuple) else (ins.argval,))]
        elif name in ("LOAD_DEREF", "LOAD_GLOBAL", "LOAD_NAME", "LOAD_ATTR"):
            out.append(({"LOAD_DEREF": "deref", "LOAD_ATTR": "attr"}.get(name, "global"), ins.argval))
    return out


def dialect_in_build(blob: bytes, code) -> None:
    """Refuse a build that may not write its CSV as GAM_DIALECT says: setDialect's constants (quotes doubled,
    a backslash escape unless noEscapeChar, then QUOTE_MINIMAL) and csv_output_no_escape_char's default
    (FALSE). A bump that trips this: read the source of the new tag, then update GAM_DIALECT, the parser's
    reader (core/gam/parser.py) and the mock's gam_cell."""
    dialect = [c for c in _nested(code) if c.co_name == "setDialect"]
    # The dictionary it builds, as the values it loads in order: by what each load reads, not how this
    # Python's compiler stores a constant (3.15 loads True, False and None with LOAD_COMMON_CONSTANT).
    expected = [("const", "delimiter"), ("deref", "self"), ("attr", "columnDelimiter"),
                ("const", "doublequote"), ("const", True),
                ("const", "escapechar"), ("fast", "noEscapeChar"), ("const", "\\"), ("const", None),
                ("const", "lineterminator"), ("fast", "lineterminator"),
                ("const", "quotechar"), ("deref", "self"), ("attr", "quoteChar"),
                ("const", "quoting"), ("global", "csv"), ("attr", "QUOTE_MINIMAL"),
                ("const", "skipinitialspace"), ("const", False), ("const", "strict"), ("const", False)]
    if len(dialect) != 1 or _loads(dialect[0])[:len(expected)] != expected:
        raise ValueError("GAM's setDialect changed: read it at the new tag")
    found = [i for i in dis.get_instructions(module_code(blob, "gamlib.glcfg")) if i.opname != "EXTENDED_ARG"]
    after = [(b.opname, b.argval) for a, b in zip(found, found[1:], strict=False)   # each with the next
             if a.opname == "LOAD_NAME" and a.argval == "CSV_OUTPUT_NO_ESCAPE_CHAR"]
    if after != [("LOAD_NAME", "FALSE"), ("LOAD_NAME", "VAR_TYPE")]:   # Defaults = {...: FALSE}, then VAR_INFO
        raise ValueError("GAM's csv_output_no_escape_char default changed: read gamlib/glcfg.py at the new tag")


def progress_messages(blob: bytes) -> dict[str, str]:
    """Every message in the build's gamlib.glmsgs that starts "Getting" or "Got", in any case: the words of
    GAM's progress printers. core/gam/errors.py skips a stderr line that starts with one as progress, so a new
    one (an error worded that way) must be read before a bump is trusted."""
    found, prev = {}, None
    for ins in dis.get_instructions(module_code(blob, "gamlib.glmsgs")):
        if ins.opname == "EXTENDED_ARG":
            continue
        if (ins.opname == "STORE_NAME" and getattr(prev, "opname", "") == "LOAD_CONST" and isinstance(prev.argval, str)
                and prev.argval.lstrip().lower().startswith(("getting", "got"))):
            found[ins.argval] = prev.argval
        prev = ins
    return found


def gam_csv(rows) -> str:
    """Rows as GAM prints them, by this (the build's) Python's csv module."""
    out = io.StringIO()
    csv.writer(out, **GAM_DIALECT).writerows(rows)
    return out.getvalue()


def read(gam: Path) -> dict:
    blob = gam.read_bytes()
    code = gam_module_code(blob)
    dialect_in_build(blob, code)
    return {"sha256": hashlib.sha256(blob).hexdigest(), "python": build_python(blob),
            "rc": rc_table(code), "progress": progress_messages(blob),
            "csv": [{"rows": rows, "text": gam_csv(rows)} for rows in CSV_SAMPLES]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the vendored gam's *_RC table, CSV samples and progress words as JSON.")
    ap.add_argument("gam", type=Path, help="the vendored gam executable")
    ap.add_argument("--python-version", action="store_true",
                    help="print only the build's Python version (e.g. 3.15); runs under any Python")
    args = ap.parse_args(argv)
    try:
        if args.python_version:
            print(build_python(args.gam.read_bytes()))
        else:
            print(json.dumps(read(args.gam), indent=1, sort_keys=True))
    except (OSError, ValueError, WrongPython) as e:
        print(f"gam_rc_table: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
