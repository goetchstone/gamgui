"""Tolerant parsing of GAM output into ``list[dict]``.

GAM's machine-readable output varies by command and version. With ``formatjson`` you may get:

* a single JSON object (e.g. ``gam info user X formatjson``),
* a JSON array,
* newline-delimited JSON (one object per line),
* a CSV whose single ``JSON`` column holds a JSON object per row.

Without ``formatjson`` you get ordinary CSV. This module normalizes all of those shapes to a
list of dicts so the rest of the app never has to care which one GAM produced.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List


def parse_records(stdout: str) -> List[Dict[str, Any]]:
    """Parse GAM stdout into a list of record dicts. Never raises on shape; returns ``[]`` if empty."""
    text = (stdout or "").strip()
    if not text:
        return []

    # 1) A single JSON value (object or array).
    obj = _try_json(text)
    if obj is not None:
        return _coerce_to_records(obj)

    # 2) Newline-delimited JSON (each line a JSON object). Require the first non-empty
    #    line to parse as JSON before committing to this interpretation.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and _try_json(lines[0]) is not None:
        records: List[Dict[str, Any]] = []
        ndjson_ok = True
        for ln in lines:
            val = _try_json(ln)
            if val is None:
                ndjson_ok = False
                break
            records.extend(_coerce_to_records(val))
        if ndjson_ok:
            return records

    # 3) CSV (possibly with an embedded JSON column).
    return _parse_csv(text)


def parse_one(stdout: str) -> Dict[str, Any]:
    """Parse output expected to describe a single record; returns ``{}`` if none."""
    records = parse_records(stdout)
    return records[0] if records else {}


# --- internals -------------------------------------------------------------------------


def _try_json(s: str) -> Any:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _coerce_to_records(val: Any) -> List[Dict[str, Any]]:
    if isinstance(val, dict):
        return [val]
    if isinstance(val, list):
        return [v for v in val if isinstance(v, dict)]
    return []


def _parse_csv(text: str) -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    # GAM's `formatjson` output is a CSV that carries a "JSON" column holding the full record
    # (often alongside a plain key column, e.g. `primaryEmail,JSON`). When present, that column
    # is the source of truth — parse it.
    fieldnames = [f for f in (reader.fieldnames or []) if f is not None]
    if "JSON" in fieldnames:
        out: List[Dict[str, Any]] = []
        for row in rows:
            parsed = _try_json(row.get("JSON") or "")
            out.extend(_coerce_to_records(parsed) if parsed is not None else [])
        return out
    # Plain CSV: rows are already dicts; drop the None key DictReader may add for ragged rows.
    return [{k: v for k, v in row.items() if k is not None} for row in rows]
