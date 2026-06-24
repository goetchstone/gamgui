"""The GAM command catalog: a categorized, browsable model of GAM's commands.

`parser.py` shallow-parses the vendored `GamCommands.txt` grammar into browse-only `CatalogCommand`s
(category from the `# ` section headers, risk inferred from the verb). `catalog.py` adds the small
*curated* overlay — commands with typed slots + a `build` callable that produces an injection-safe
argv via `GAMCommands` — which are the only ones the Builder can actually run.
"""

from .catalog import load_catalog  # noqa: F401
from .models import Catalog, CatalogCommand, CommandSlot, SlotKind  # noqa: F401
