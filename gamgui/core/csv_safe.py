"""Neutralize spreadsheet formula injection in a CSV cell.

A value from Google (a display name, a subject) that starts with ``=``, ``+``, ``-`` or ``@`` would execute as a formula when the exported CSV is opened in Excel or Sheets; a leading ``'`` makes
the cell inert text. Used by every CSV this app writes.
"""

from __future__ import annotations

from typing import Any

_CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in _CSV_FORMULA_LEADS else s
