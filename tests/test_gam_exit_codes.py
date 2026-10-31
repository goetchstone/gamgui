"""GAM exit codes GamGUI branches on are the vendored build's own.

The grammar (GamCommands.txt) proves a command's syntax, not what GAM exits with. Setup's verify once
read `check serviceaccount`'s scope failure as exit 1 because three changes guessed it and the mock
agreed; the build exits SCOPES_NOT_AUTHORIZED_RC (10), and a rejected key OAUTH2SERVICE_JSON_REQUIRED_RC
(16). So the codes come from the build's *_RC table, and the mock must exit with the same constants.

The table is read with the stdlib, not PyInstaller (scripts/gam_rc_table.py): CI's jobs install only the
dev lock, so a PyInstaller import skipped the check everywhere it mattered. The build's bytecode loads
only in its own Python, which can be newer than the suite's, so CI reads the table with that Python and
hands it over as JSON (GAM_RC_TABLE); without one, the suite reads the build itself when it can. Where
GAM isn't vendored the build half skips; a job that vendors it sets EXIT_CODES_REQUIRE_GAM, which turns
that skip, or a build this Python can't read, into a failure (tests/test_workflow_safety.py holds every
such job to it).
"""
from __future__ import annotations

import ast
import functools
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gamgui.core.gam.commands import GAMCommands as C
from gamgui.core.gam.errors import GAMError
from gamgui.core.setup import DWD_SCOPES, OAUTH2SERVICE_JSON_REQUIRED_RC

ROOT = Path(__file__).resolve().parent.parent
GAM_BIN = ROOT / "gamgui" / "resources" / "gam7" / "gam"
MOCK_GAM = ROOT / "tests" / "fixtures" / "mock_gam.sh"
SCRIPT = ROOT / "scripts" / "gam_rc_table.py"
DWD = [scope for scope, _ in DWD_SCOPES]
REQUIRE = "EXIT_CODES_REQUIRE_GAM"
TABLE = "GAM_RC_TABLE"


def _load_reader():
    spec = importlib.util.spec_from_file_location("gam_rc_table", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rct = _load_reader()


def _unavailable(reason: str):
    if os.environ.get(REQUIRE) == "1":
        pytest.fail(f"{REQUIRE} is set: {reason}")
    pytest.skip(reason)


@functools.cache
def _build_reading() -> dict:
    """scripts/gam_rc_table.py's reading of the vendored build, once per session: GAM_RC_TABLE's, or read here
    when this Python can."""
    if not GAM_BIN.exists():
        _unavailable("GAM is not vendored (make gam)")
    if os.environ.get(TABLE):   # read by the build's own Python (CI runs scripts/gam_rc_table.py with it)
        text = Path(os.environ[TABLE]).read_text()
        assert text.strip(), f"{TABLE} is empty: scripts/gam_rc_table.py failed under the build's Python"
        given = json.loads(text)
        assert given["sha256"] == hashlib.sha256(GAM_BIN.read_bytes()).hexdigest(), f"{TABLE} was read from another gam"
        return given
    try:
        return rct.read(GAM_BIN)
    except rct.WrongPython as e:
        _unavailable(f"{e} (or set {TABLE} to its JSON, from scripts/gam_rc_table.py under that Python)")


def build_reading(key: str):
    """One part of the build's reading: "rc" (NAME_RC -> int), "csv", "progress"."""
    reading = _build_reading()
    assert key in reading, f"{TABLE} has no {key}: it came from an older scripts/gam_rc_table.py"
    return reading[key]


@pytest.fixture(scope="module")
def build_rc():
    """The vendored build's NAME_RC -> int table."""
    return build_reading("rc")


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


def test_the_reader_runs_isolated_and_refuses_another_pythons_build():
    # CI runs the script with -I under the build's own Python, where nothing is installed: stdlib only.
    if not GAM_BIN.exists():
        _unavailable("GAM is not vendored (make gam)")

    def run(*args):
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args, str(GAM_BIN)],
                              capture_output=True, text=True, timeout=60)

    version, table = run("--python-version"), run()
    assert version.returncode == 0, version.stderr
    if version.stdout.strip() == f"{sys.version_info[0]}.{sys.version_info[1]}":
        assert table.returncode == 0 and json.loads(table.stdout)["rc"]["UNKNOWN_ERROR_RC"] == 1, table.stderr
    else:   # never a guessed table
        assert (table.returncode, table.stdout) == (1, ""), table.stderr
        assert "run under that Python" in table.stderr
