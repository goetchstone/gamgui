#!/usr/bin/env python3
"""Read the vendored gam's exit-code table (its `NAME_RC = <int>` constants) with the stdlib alone.

    python -I scripts/gam_rc_table.py gamgui/resources/gam7/gam > gam_rc.json
    python -I scripts/gam_rc_table.py --python-version gamgui/resources/gam7/gam    # e.g. 3.15

GAM is a PyInstaller build, and the table lives in its marshalled bytecode, which loads only in the
Python that wrote it. That can be newer than the suite's (7.48.26 is 3.15's, and parts of the dev lock
have no 3.15 wheels yet), so CI sets up the build's own Python beside the suite's and runs this with
it: it imports nothing outside the stdlib, so it needs no install. The JSON names the binary's
SHA-256, and tests/test_gam_exit_codes.py reads it through GAM_RC_TABLE. `--python-version` reads the
build's version from the archive cookie and runs under any Python. On another Python the table read
exits 1: it never falls back to a guess.
"""

from __future__ import annotations

import argparse
import dis
import hashlib
import importlib.util
import json
import marshal
import struct
import sys
import zlib
from pathlib import Path

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
    _, offset, size = dict(marshal.loads(blob[pyz + pyz_toc:]))["gam"]  # noqa: S302
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


def read(gam: Path) -> dict:
    blob = gam.read_bytes()
    return {"sha256": hashlib.sha256(blob).hexdigest(), "python": build_python(blob),
            "rc": rc_table(gam_module_code(blob))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print the vendored gam's *_RC table as JSON.")
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
