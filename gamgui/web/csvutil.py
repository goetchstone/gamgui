"""Shared CSV-export helper: neutralize spreadsheet formula injection in cell values. Used by every route that serves a CSV download. See ``core/csv_safe.py``."""

from __future__ import annotations

from ..core.csv_safe import csv_safe

__all__ = ["csv_safe"]
