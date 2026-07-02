"""The GAM command catalog: shallow grammar parse (categories + risk inference) + the buildable overlay."""

from __future__ import annotations

import pytest

from gamgui.core.catalog import load_catalog
from gamgui.core.catalog.catalog import _resource
from gamgui.core.catalog.parser import parse_grammar
from gamgui.core.connectors.base import RiskLevel

# The raw grammar (GamCommands.txt) ships with the GAM binary and is NOT committed — only the
# generated command_catalog.json is. Tests that parse the raw grammar skip in clean-room/CI.
_GRAMMAR = _resource("GamCommands.txt")
_needs_grammar = pytest.mark.skipif(not _GRAMMAR.exists(), reason="GamCommands.txt not vendored")


def _risk(line: str) -> RiskLevel:
    return parse_grammar(line)[0].risk


@_needs_grammar
def test_parse_grammar_categorizes_and_counts():
    cmds = parse_grammar(_resource("GamCommands.txt").read_text(errors="replace"))
    assert len(cmds) > 900                                  # ~1,040 command lines
    assert len({c.category for c in cmds}) >= 18            # many API areas
    assert any("Gmail" in c.subcategory for c in cmds)     # "Users - Gmail - …" subcategories
    assert all(c.raw_syntax.startswith("gam ") for c in cmds)  # header/continuation lines excluded


def test_risk_inference_handles_entity_prefix():
    assert _risk("gam print users") == RiskLevel.READ_ONLY
    assert _risk("gam info user <x>") == RiskLevel.READ_ONLY
    # verb is NOT token 2 — it follows the entity prefix:
    assert _risk("gam <UserTypeEntity> delete delegate <x>") == RiskLevel.DESTRUCTIVE
    assert _risk("gam all users delete calendaracls primary <x>") == RiskLevel.DESTRUCTIVE
    # `update` comes first, so a group member removal is LOW (not destructive):
    assert _risk("gam update group <g> remove member <x>") == RiskLevel.LOW
    unknown = parse_grammar("gam frobnicate widgets")[0]
    assert unknown.risk == RiskLevel.LOW and unknown.uncertain


def test_load_catalog_has_buildable_and_browse():
    cat = load_catalog()
    assert cat.version  # stamped from the committed JSON
    assert len(cat.commands) > 900
    assert len(cat.buildable()) >= 10
    assert cat.by_id("build.set_signature") is not None
    assert cat.by_id("build.delete_user").risk == RiskLevel.DESTRUCTIVE


def test_new_curated_commands_build_correct_argv():
    cat = load_catalog()

    signout = cat.by_id("build.signout_user")
    assert signout is not None and signout.buildable and signout.risk == RiskLevel.LOW
    assert signout.build({"email": "a@e.com"}) == ["user", "a@e.com", "signout"]

    undelete = cat.by_id("build.undelete_user")
    assert undelete is not None and undelete.buildable and undelete.risk == RiskLevel.LOW
    assert undelete.build({"email": "a@e.com"}) == ["undelete", "user", "a@e.com"]

    create = cat.by_id("build.create_group")
    assert create is not None and create.buildable and create.risk == RiskLevel.LOW
    assert create.build({"email": "g@e.com"}) == ["create", "group", "g@e.com"]
    assert create.build({"email": "g@e.com", "name": "Sales", "description": "d"}) == [
        "create", "group", "g@e.com", "name", "Sales", "description", "d",
    ]


def test_areas_group_the_categories():
    cat = load_catalog()
    counts = cat.area_counts()
    assert len(counts) <= 14                     # the ~53 grammar categories collapse to a short set
    assert counts.get("Users & Identity", 0) > 100   # Users + aliases + schemas + …
    assert "Calendars" in counts and "Devices" in counts
    # every command lands in some area (no unmapped strays beyond the explicit "Other")
    assert all(c.area for c in cat.commands)
    items = cat.in_area("Users & Identity")
    assert items and any(c.buildable for c in items)   # the area includes the curated buildables
    # grouped by category for section headers (Administrators sorts before Users)
    assert [c.category for c in items] == sorted(c.category for c in items)
