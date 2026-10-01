"""GAM exit codes GamGUI branches on are the vendored build's own.

The grammar (GamCommands.txt) proves a command's syntax, not what GAM exits with. Setup's verify once
read `check serviceaccount`'s scope failure as exit 1 because three changes guessed it and the mock
agreed; the build exits SCOPES_NOT_AUTHORIZED_RC (10), and a rejected key OAUTH2SERVICE_JSON_REQUIRED_RC
(16). So the codes come from the build's *_RC table, and the mock must exit with the same constants.

The table is read with the stdlib, not PyInstaller: CI's jobs install only the dev lock, so a PyInstaller
import skipped the check everywhere it mattered. Where GAM isn't vendored the build half skips; a job
that vendors it sets EXIT_CODES_REQUIRE_GAM, which turns that skip, or a build this Python can't read,
into a failure (tests/test_workflow_safety.py holds every such job to it).
"""
from __future__ import annotations

import ast
import dis
import importlib.util
import marshal
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.errors import GAMError
from gamgui.core.setup import DWD_SCOPES, OAUTH2SERVICE_JSON_REQUIRED_RC

ROOT = Path(__file__).resolve().parent.parent
GAM_BIN = ROOT / "gamgui" / "resources" / "gam7" / "gam"
MOCK_GAM = ROOT / "tests" / "fixtures" / "mock_gam.sh"
DWD = [scope for scope, _ in DWD_SCOPES]
REQUIRE = "EXIT_CODES_REQUIRE_GAM"

# PyInstaller's CArchive, appended to the executable (PyInstaller/archive/readers.py): a trailing cookie
# (magic, archive length, TOC offset, TOC length, Python version, libpython name), and TOC entries
# (entry length, data offset, data length, uncompressed length, compressed flag, typecode, then the name).
_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE = struct.Struct("!8sIIII64s")
_TOC_ENTRY = struct.Struct("!IIIIBc")


def _unavailable(reason: str):
    if os.environ.get(REQUIRE) == "1":
        pytest.fail(f"{REQUIRE} is set: {reason}")
    pytest.skip(reason)


def _gam_module_code(blob: bytes):
    """The code object of the `gam` package, from the PYZ inside the executable's CArchive."""
    at = blob.rfind(_COOKIE_MAGIC)
    assert at >= 0, "no PyInstaller archive cookie in the vendored gam"
    _, length, toc_offset, toc_length, pyvers, _ = _COOKIE.unpack_from(blob, at)
    start = at + _COOKIE.size - length
    pos, end, pyz = start + toc_offset, start + toc_offset + toc_length, None
    while pos < end and pyz is None:
        size, offset, *_, typecode = _TOC_ENTRY.unpack_from(blob, pos)
        pyz = start + offset if typecode == b"z" else None
        pos += size
    # The PYZ (pyimod01_archive.py): magic, the bytecode's Python magic number, TOC offset, then entries
    # of zlib-compressed marshalled code, and the TOC as a marshalled [(name, (typecode, offset, length))].
    assert pyz is not None and blob[pyz:pyz + 4] == b"PYZ\0", "no PYZ archive in the vendored gam's CArchive"
    if blob[pyz + 4:pyz + 8] != importlib.util.MAGIC_NUMBER:   # marshalled code loads only in its own Python
        _unavailable(f"the build's bytecode is Python {pyvers // 100}.{pyvers % 100}'s; "
                     f"run under that Python, not {sys.version_info[0]}.{sys.version_info[1]}")
    (pyz_toc,) = struct.unpack_from("!i", blob, pyz + 8)
    # The vendored binary is checksum-pinned and runs as the app's own subprocess: trusted, not input.
    _, offset, size = dict(marshal.loads(blob[pyz + pyz_toc:]))["gam"]  # noqa: S302
    return marshal.loads(zlib.decompress(blob[pyz + offset:pyz + offset + size]))  # noqa: S302


@pytest.fixture(scope="module")
def build_rc():
    """The vendored build's NAME_RC -> int table, read from the `gam` module in its PyInstaller archive."""
    if not GAM_BIN.exists():
        _unavailable("GAM is not vendored (make gam)")
    code = _gam_module_code(GAM_BIN.read_bytes())
    table, prev = {}, None
    for ins in dis.get_instructions(code):
        if ins.opname == "EXTENDED_ARG":   # pairing through it reads its argument, not the constant's
            continue
        if ins.opname == "STORE_NAME" and str(ins.argval).endswith("_RC") and isinstance(getattr(prev, "argval", None), int):
            table[ins.argval] = prev.argval
        prev = ins
    assert table.get("UNKNOWN_ERROR_RC") == 1 and len(set(table.values())) > 10, "the build's RC table did not parse"
    return table


def test_a_job_that_requires_the_build_fails_where_others_skip(monkeypatch):
    monkeypatch.delenv(REQUIRE, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _unavailable("not vendored")
    monkeypatch.setenv(REQUIRE, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRE):
        _unavailable("not vendored")


def _named_exit_codes():
    """Every module-level `NAME_RC = <int>` in the app (the vendored GAM excluded)."""
    for py in (ROOT / "gamgui").rglob("*.py"):
        if "resources" in py.parts:
            continue
        for node in ast.parse(py.read_text()).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.endswith("_RC") and isinstance(node.value, ast.Constant)):
                yield str(py.relative_to(ROOT)), node.targets[0].id, node.value.value


def test_every_gam_exit_code_the_app_names_is_the_builds(build_rc):
    found = list(_named_exit_codes())
    assert {name for _, name, _ in found} >= {"SCOPES_NOT_AUTHORIZED_RC", "OAUTH2SERVICE_JSON_REQUIRED_RC"}
    wrong = [(path, name, ours, build_rc.get(name)) for path, name, ours in found if build_rc.get(name) != ours]
    assert not wrong, f"(file, name, ours, the build's): {wrong}"


async def test_the_mock_fails_a_rejected_key_with_the_builds_code(runner, domain):
    with pytest.raises(GAMError) as ei:
        await runner.run_authenticated(domain, C.check_svcacct("badkey-admin@example.com", DWD))
    assert ei.value.exit_code == OAUTH2SERVICE_JSON_REQUIRED_RC


@pytest.mark.parametrize("present,rc", [
    ([], "OAUTH2SERVICE_JSON_REQUIRED_RC"),               # no key file: GAM stops before anything else
    (["oauth2service.json"], "OAUTH2_TXT_REQUIRED_RC"),   # a key but no admin token
])
def test_the_mock_fails_a_missing_credentials_file_with_the_builds_code(build_rc, tmp_path, present, rc):
    for name in present:
        (tmp_path / name).write_text("{}")
    env = {**os.environ, "GAMCFGDIR": str(tmp_path), "GAM_MOCK_FIXTURES": str(MOCK_GAM.parent)}
    done = subprocess.run(["bash", str(MOCK_GAM), "info", "domain"], env=env, capture_output=True, text=True,
                          timeout=15)
    assert done.returncode == build_rc[rc], (rc, done.returncode, done.stderr)
