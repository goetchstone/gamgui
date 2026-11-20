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


_WRAPPED = """# Users
gam print widgets [todrive <ToDriveAttribute>*]
        [fields <WidgetFieldNameList>]
\t[formatjson]
gam info widget <WidgetItem>

<WidgetFieldName> ::=
        name|
        size
gam show widgets
        update [now]
gam <UserTypeEntity>
        print gadgets
"""


def test_an_entrys_continuation_lines_join_its_syntax():
    # GAM wraps a long entry onto indented lines (691 of 1052 entries in 7.52.03). The parser once
    # kept the first line only, so the Builder showed a cut-off syntax.
    cmds = parse_grammar(_WRAPPED)
    assert [c.raw_syntax for c in cmds] == [
        "gam print widgets [todrive <ToDriveAttribute>*] [fields <WidgetFieldNameList>] [formatjson]",
        "gam info widget <WidgetItem>",             # the blank line ends it: the definition below
        "gam show widgets update [now]",            # is not part of the entry before or after it
        "gam <UserTypeEntity> print gadgets",
    ]
    assert [c.id for c in cmds] == ["raw.1", "raw.4", "raw.9", "raw.11"]   # ids stay first-line


def test_a_continuation_line_never_classifies_an_entry():
    # Verb and risk come from the first line, as they did before the join: a write keyword wrapped
    # under a read verb (`version`'s `update`/`install`) never makes the line look like a write, and
    # a read verb wrapped under a verbless first line never makes an uncertain line confidently read.
    show, gadgets = parse_grammar(_WRAPPED)[2:]
    assert show.verb == "show" and show.risk == RiskLevel.READ_ONLY and not show.uncertain
    assert gadgets.uncertain and gadgets.risk == RiskLevel.LOW


def test_load_catalog_has_buildable_and_browse():
    cat = load_catalog()
    assert cat.version  # stamped from the committed JSON
    assert len(cat.commands) > 900
    assert len(cat.buildable()) >= 10
    assert cat.by_id("build.set_signature") is None   # signatures are set where the old one is kept (roadmap 4.1)
    assert cat.by_id("build.suspend_user").risk == RiskLevel.DESTRUCTIVE
    assert cat.by_id("build.delete_user") is None   # deleted only from Users (roadmap 2.9)


def test_runnable_gam_version_can_never_self_update():
    """GAM 7.52.00 added `gam version [... (update [quiet])]`, which downloads the latest release
    and installs it over the running binary: an unpinned, unattested GAM (invariant 7). `version`
    is a read verb, so the line is auto-promoted; it stays safe only because the read builder
    drops optional groups. Hold it to exactly `gam version`, whatever a slot is given. GAM 7.52.02
    added `install <Path>` and `update path <Path>` on continuation lines, which the parser joins
    into the entry's syntax: the runnable form below carries them and must still never emit them."""
    cat = load_catalog()
    lines = [c for c in cat.commands if c.verb == "version"]
    runnable = [c for c in lines if c.buildable]
    assert runnable                         # else this guards nothing; revisit if it goes browse-only
    assert any("update" in c.raw_syntax and "install" in c.raw_syntax for c in runnable)
    for c in runnable:
        assert not c.slots
        assert c.build({}) == ["version"]
        assert c.build({"a0": "update", "a1": "install"}) == ["version"]


@_needs_grammar
def test_gam_version_still_offers_the_self_update_the_catalog_must_never_run():
    # The whole `gam version` entry, first line and continuation lines: it still writes a GAM binary
    # (`update`, `install <Path>`), so test_runnable_gam_version_can_never_self_update guards something.
    lines = _GRAMMAR.read_text(errors="replace").splitlines()
    start = next(i for i, line in enumerate(lines) if line.split()[:2] == ["gam", "version"])
    entry = [lines[start]]
    for line in lines[start + 1:]:
        if not line[:1].isspace() or not line.strip():
            break
        entry.append(line)
    words = set(" ".join(entry).replace("(", " ").replace(")", " ").replace("|", " ").replace("[", " ")
                .replace("]", " ").split())
    assert {"update", "install"} <= words


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


def test_transfer_offers_combined_service_list():
    cat = load_catalog()
    xfer = cat.by_id("build.transfer_data")
    assert xfer is not None and xfer.buildable
    service = next(s for s in xfer.slots if s.key == "service")
    # "drive,calendar" lets the Builder transfer both at once (one transfer avoids the 409).
    assert service.choices == ["drive", "calendar", "drive,calendar"]
    # The combined choice rides as ONE argv element — never split into two tokens.
    assert xfer.build({"old_owner": "a@e.com", "service": "drive,calendar", "new_owner": "b@e.com"}) == [
        "create", "datatransfer", "a@e.com", "drive,calendar", "b@e.com", "all",
    ]
    # Drive names its privacy level (every file moves, as offboarding's transfer does); calendar alone
    # takes none — GAM refuses one there.
    assert xfer.build({"old_owner": "a@e.com", "service": "calendar", "new_owner": "b@e.com"})[-1] == "b@e.com"


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
