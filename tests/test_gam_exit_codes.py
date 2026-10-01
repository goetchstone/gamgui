"""GAM exit codes GamGUI branches on are the vendored build's own.

The grammar (GamCommands.txt) proves a command's syntax, not what GAM exits with. Setup's verify once
read `check serviceaccount`'s scope failure as exit 1 because three changes guessed it and the mock
agreed; the build exits SCOPES_NOT_AUTHORIZED_RC (10), and a rejected key OAUTH2SERVICE_JSON_REQUIRED_RC
(16). So the codes come from the build's *_RC table, and the mock must exit with the same constants.
"""
from __future__ import annotations

import ast
import dis
import os
import subprocess
from pathlib import Path

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.errors import GAMError
from gamgui.core.setup import DWD_SCOPES, OAUTH2SERVICE_JSON_REQUIRED_RC

ROOT = Path(__file__).resolve().parent.parent
GAM_BIN = ROOT / "gamgui" / "resources" / "gam7" / "gam"
MOCK_GAM = ROOT / "tests" / "fixtures" / "mock_gam.sh"
DWD = [scope for scope, _ in DWD_SCOPES]


@pytest.fixture(scope="module")
def build_rc():
    """The vendored build's NAME_RC -> int table, read from the `gam` module in its PyInstaller archive."""
    if not GAM_BIN.exists():
        pytest.skip("GAM is not vendored (make gam)")
    readers = pytest.importorskip("PyInstaller.archive.readers")
    pkg = readers.CArchiveReader(str(GAM_BIN))
    code = pkg.open_embedded_archive(next(n for n in pkg.toc if n.endswith(".pyz"))).extract("gam")
    if isinstance(code, tuple):
        code = code[-1]
    table, prev = {}, None
    for ins in dis.get_instructions(code):
        if ins.opname == "EXTENDED_ARG":   # pairing through it reads its argument, not the constant's
            continue
        if ins.opname == "STORE_NAME" and str(ins.argval).endswith("_RC") and isinstance(getattr(prev, "argval", None), int):
            table[ins.argval] = prev.argval
        prev = ins
    assert table.get("UNKNOWN_ERROR_RC") == 1 and len(set(table.values())) > 10, "the build's RC table did not parse"
    return table


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
