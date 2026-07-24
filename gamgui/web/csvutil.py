"""Shared CSV-export helper: neutralize spreadsheet formula injection in cell values.

A GAM-controlled value (a display name, a group description) that starts with ``=``, ``+``, ``-``
or ``@`` would execute as a formula when the exported CSV is opened in Excel/Sheets; prefixing a
``'`` makes the cell inert text. Used by every route that serves a CSV download.
"""

from __future__ import annotations

from typing import Any

_CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in _CSV_FORMULA_LEADS else s
