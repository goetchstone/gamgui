"""The GAM command catalog: shallow grammar parse (categories + risk inference) + the buildable overlay."""

from __future__ import annotations

from gamgui.core.catalog import load_catalog
from gamgui.core.catalog.catalog import _resource
from gamgui.core.catalog.parser import parse_grammar
from gamgui.core.connectors.base import RiskLevel


def _risk(line: str) -> RiskLevel:
    return parse_grammar(line)[0].risk


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
    users = cat.in_category("Users")
    assert users and users[0].buildable          # buildable commands sort first within a category
