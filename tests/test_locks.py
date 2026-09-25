"""The hash-locked dependencies: requirements/*.in -> `make lock` -> requirements/*.txt, which CI,
`make setup` and scripts/build_app.sh install with --require-hashes. pyproject.toml keeps the
flexible ranges, which only `make setup-latest` installs."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO = Path(__file__).resolve().parents[1]
REQS = REPO / "requirements"
LOCKED_INSTALL = "python -m pip install --require-hashes --only-binary :all: -r requirements/dev.txt"
LOCKS = ["app", "dev", "pip"]
# pip's --build-constraint (pip 25.3) hash-checks the setuptools that builds proxy-tools, pywebview's
# source-only dependency; Python 3.10-3.12 bundle an older pip, which has no such option.
PIP_BOOTSTRAP = '-m pip install -q --require-hashes -r requirements/pip.txt'


def _key(req: str) -> "tuple[str, str, str]":
    r = Requirement(req)
    return canonicalize_name(r.name), str(r.specifier), str(r.marker or "")


def _inputs(name: str) -> "set[tuple[str, str, str]]":
    lines = (REQS / f"{name}.in").read_text().splitlines()
    return {_key(line) for line in (raw.split("#", 1)[0].strip() for raw in lines) if line}


def _pins(name: str) -> "dict[str, set[str]]":
    pins: "dict[str, set[str]]" = {}
    for m in re.finditer(r"^([A-Za-z0-9._-]+)==([^\s;]+)", (REQS / f"{name}.txt").read_text(), re.M):
        pins.setdefault(canonicalize_name(m[1]), set()).add(m[2])
    return pins


def test_lock_inputs_list_what_pyproject_declares():
    # Dependabot's uv ecosystem bumps a .in and its .txt; pyproject's copy of an exact pin can lag.
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    project, backend = pyproject["project"], pyproject["build-system"]["requires"]
    runtime, extras = project["dependencies"], project["optional-dependencies"]
    app = {k for k in _inputs("app") if k[0] != "pyinstaller"}
    assert app == {_key(r) for r in runtime + extras["desktop"]}
    # The build backend too: `make setup` installs the project with --no-build-isolation, so the
    # backend comes from the lock instead of an unhashed download into an isolated build env; and
    # what hatchling asks for to build an editable (its get_requires_for_build_editable).
    editable = ["editables~=0.3"]
    assert _inputs("dev") == {_key(r) for r in runtime + extras["dev"] + backend + editable}


def test_pip_lock_holds_only_a_pip_with_build_constraint():
    assert _inputs("pip") == {_key("pip>=25.3")}
    assert set(_pins("pip")) == {"pip"}


def test_dev_and_app_locks_agree_on_every_shared_pin():
    # `make setup` installs both in one pip call; a package pinned differently in each is a conflict.
    dev, app = _pins("dev"), _pins("app")
    assert {d: (dev[d], app[d]) for d in dev.keys() & app.keys() if dev[d] != app[d]} == {}


def test_make_lock_regenerates_every_lock():
    loop = re.search(r"^lock:.*?for f in ([^;]+);", (REPO / "Makefile").read_text(), re.M | re.S)
    assert loop and sorted(loop[1].split()) == sorted(p.stem for p in REQS.glob("*.in")) == LOCKS


@pytest.mark.parametrize("name", LOCKS)
def test_every_input_is_locked_at_a_version_it_allows(name):
    # An .in edited without `make lock` would install something other than what it now says.
    pins = _pins(name)
    for dist, spec, _ in _inputs(name):
        assert dist in pins, f"{dist} is in requirements/{name}.in but not locked; run make lock"
        allowed = Requirement(f"{dist}{spec}").specifier
        assert any(allowed.contains(v, prereleases=True) for v in pins[dist]), (dist, spec, pins[dist])


@pytest.mark.parametrize("name", LOCKS)
def test_locks_are_universal_and_every_pin_carries_a_hash(name):
    text = (REQS / f"{name}.txt").read_text()
    # One file for the whole CI matrix: resolved for every OS from pyproject's Python floor up. The
    # uv ecosystem of Dependabot re-runs exactly this header, so it is what keeps its PRs universal.
    floor = re.search(r'requires-python = ">=([\d.]+)"', (REPO / "pyproject.toml").read_text())[1]
    assert (
        "#    uv pip compile --universal --generate-hashes --python-version "
        f"{floor} --output-file=requirements/{name}.txt requirements/{name}.in\n"
    ) in text
    entries = re.split(r"\n(?=[^\s#])", text.split("\n", 2)[2])
    assert len(entries) >= (1 if name == "pip" else 10)
    for entry in entries:
        assert re.match(r"[A-Za-z0-9._-]+==", entry), entry
        assert "--hash=sha256:" in entry, entry


def test_ci_installs_only_the_hash_locked_dev_deps():
    installs = [
        line.strip()
        for wf in (REPO / ".github" / "workflows").glob("*.yml")
        for line in wf.read_text().splitlines()
        if "pip install" in line
    ]
    assert installs
    assert set(installs) == {LOCKED_INSTALL}, installs


def test_build_app_installs_only_the_app_lock_in_a_venv_of_its_own():
    build = (REPO / "scripts" / "build_app.sh").read_text().replace("\\\n", " ")
    installs = [line for line in build.splitlines() if "pip install" in line]
    assert len(installs) == 2, installs
    # First the locked pip, so --build-constraint works whatever pip the build Python bundles.
    assert installs[0].split() == ['"$BUILD_VENV/bin/python"', *PIP_BOOTSTRAP.split()], installs[0]
    words = installs[1].split()
    assert '"$BUILD_VENV/bin/python"' in words[0]
    for flag in ("--require-hashes", "--build-constraint requirements/app.txt", "-r requirements/app.txt"):
        assert flag in installs[1], installs[1]
    assert '"$BUILD_VENV/bin/python" -m PyInstaller' in build


def _recipe(target: str) -> "list[str]":
    make = (REPO / "Makefile").read_text()
    body = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", make, re.M)
    assert body, target
    return [line.strip() for line in body[1].replace("\\\n", " ").splitlines()]


def test_make_setup_installs_only_the_hash_locked_deps():
    # README's start path (`make setup && make gam && make run`) runs the credential-handling app
    # from this venv, so it holds what CI tested: every file hash-checked, nothing from the ranges.
    installs = [line for line in _recipe("setup") if "pip install" in line]
    assert installs[0] == f"$(PY) {PIP_BOOTSTRAP}", installs
    assert installs[1].startswith("$(PY) -m pip install"), installs
    for flag in (
        "--require-hashes",
        "--build-constraint requirements/app.txt",
        "-r requirements/dev.txt",
        "-r requirements/app.txt",
    ):
        assert flag in installs[1], installs[1]
    # The project itself (the `gamgui` script, `import gamgui` from scripts/): no dependencies, and
    # built by the backend the dev lock holds rather than one fetched into an isolated env.
    assert installs[2:] == ["$(PY) -m pip install -q --no-deps --no-build-isolation -e ."], installs


def test_only_make_setup_latest_installs_the_flexible_ranges():
    make = (REPO / "Makefile").read_text()
    assert make.count('pip install -e ".[dev,desktop]"') == 1
    assert 'pip install -e ".[dev,desktop]"' in " ".join(_recipe("setup-latest"))


def test_dependabot_regenerates_the_locks_with_uv():
    # pip's ecosystem would re-run pip-compile, which knows no --universal: a lock for one Python.
    config = (REPO / ".github" / "dependabot.yml").read_text()
    ecosystems = re.findall(r'package-ecosystem: "([^"]+)"', config)
    assert "uv" in ecosystems and "pip" not in ecosystems, ecosystems
