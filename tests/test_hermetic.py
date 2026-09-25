"""The suite never reaches the operator's real app data (review R7).

A store built without a path falls back to app_data_dir() — on the operator's Mac their real
~/Library/Application Support/GamGUI (roles, signature templates, the audit log). conftest points HOME
at each test's tmp_path and installs an audit hook that refuses, and fails the test for, any access under
the real folder or ~/.gam. These pin both halves: the redirect, and that the hook really refuses.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from gamgui.core.audit import AuditLog
from gamgui.core.onboarding import RunbookStore
from gamgui.core.paths import app_data_dir
from gamgui.core.signatures import SignatureStore

from .conftest import REAL_DATA_ROOTS, REAL_DATA_VIOLATIONS, RealAppDataAccess, _guard_variants


def test_every_default_store_lands_in_this_tests_tmp_path(tmp_path):
    assert Path.home() == tmp_path
    assert app_data_dir().is_relative_to(tmp_path)
    for store in (AuditLog(), RunbookStore(), SignatureStore()):
        assert Path(store.path).is_relative_to(tmp_path), store.path


def test_the_real_app_data_and_gam_dirs_are_guarded():
    # The roots were taken at import, from the real home — not from this test's HOME.
    here = _guard_variants(app_data_dir()) | _guard_variants(Path.home() / ".gam")
    assert REAL_DATA_ROOTS and not here & REAL_DATA_ROOTS
    if sys.platform == "darwin":
        assert any(r.endswith("/library/application support/gamgui") for r in REAL_DATA_ROOTS)
    assert any(r.endswith(os.sep + ".gam") for r in REAL_DATA_ROOTS)


def test_the_hook_refuses_before_the_access_happens(tmp_path):
    # A stand-in for the real folder, so this proof never goes near the operator's.
    fake = tmp_path / "Real GamGUI"
    fake.mkdir()
    roots = _guard_variants(fake)
    added = roots - REAL_DATA_ROOTS
    start = len(REAL_DATA_VIOLATIONS)
    REAL_DATA_ROOTS.update(added)
    try:
        with pytest.raises(RealAppDataAccess):
            (fake / "audit.jsonl").write_text("x")                  # open
        with pytest.raises(RealAppDataAccess):
            (fake / "run").mkdir()                                  # os.mkdir
        with pytest.raises(RealAppDataAccess):
            sqlite3.connect(str(fake / "calendar_index.db"))        # sqlite3.connect
        with pytest.raises(RealAppDataAccess):
            os.listdir(fake)                                        # os.listdir
        if sys.platform == "darwin":                                # APFS is case-insensitive
            with pytest.raises(RealAppDataAccess):
                open(str(fake / "roles.json").upper(), "w")
        hits = REAL_DATA_VIOLATIONS[start:]
        assert [e for e, _ in hits][:4] == ["open", "os.mkdir", "sqlite3.connect", "os.listdir"]
    finally:
        REAL_DATA_ROOTS.difference_update(added)
        del REAL_DATA_VIOLATIONS[start:]        # these were the point, not a leak: don't fail teardown
    assert sorted(p.name for p in fake.iterdir()) == []            # nothing was created inside
