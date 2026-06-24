#!/usr/bin/env python3
"""Generate the vendored command catalog from GamCommands.txt.

Run on each GAM bump (after `scripts/fetch_gam.sh`). Writes a categorized, browse-only catalog to
`gamgui/resources/gam7/command_catalog.json`, stamped with EXPECTED_GAM_VERSION so the contract test
can detect drift. The buildable overlay is defined in Python (`core/catalog/catalog.py`), not here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gamgui.core.catalog.parser import parse_grammar  # noqa: E402
from gamgui.core.gam.commands import EXPECTED_GAM_VERSION  # noqa: E402

REF = ROOT / "gamgui" / "resources" / "gam7" / "GamCommands.txt"
OUT = ROOT / "gamgui" / "resources" / "gam7" / "command_catalog.json"


def main() -> int:
    if not REF.exists():
        print(f"error: {REF} not found — run scripts/fetch_gam.sh first", file=sys.stderr)
        return 1
    commands = parse_grammar(REF.read_text(errors="replace"))
    data = {"version": EXPECTED_GAM_VERSION, "commands": [c.to_json() for c in commands]}
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    cats = sorted({c.category for c in commands})
    print(f"wrote {len(commands)} commands in {len(cats)} categories -> {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
