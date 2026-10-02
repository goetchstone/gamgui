"""The credential files GAM writes must never be committable from a checkout.

`.gitignore` once listed the service-account key as `*.<name>`, a glob that needs a character before
the dot, so GAM's own bare file name was not ignored: a config folder copied into the repo would have
offered the one credential that can impersonate anyone to `git add`. Asked of git itself, by name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gamgui.core.secrets.vault import FILENAMES

ROOT = Path(__file__).resolve().parent.parent


def _ignored(path: str) -> bool:
    try:
        r = subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT, capture_output=True)
    except OSError:
        pytest.skip("git is not available")
    if r.returncode == 128:
        pytest.skip("not a git checkout")
    return r.returncode == 0


@pytest.mark.parametrize("name", sorted(FILENAMES.values()))
@pytest.mark.parametrize("where", ["", "some/config/dir/"])
def test_every_credential_file_name_is_ignored_wherever_it_sits(name, where):
    assert _ignored(where + name), f"{where + name} is not ignored by .gitignore"


def test_gams_license_is_committed_with_the_catalog_derived_from_its_grammar():
    assert not _ignored("gamgui/resources/gam7/LICENSE")
    assert not _ignored("gamgui/resources/gam7/command_catalog.json")
