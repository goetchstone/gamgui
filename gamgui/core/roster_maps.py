"""Saved column maps, by name: a school's SIS exports the same spreadsheet every term, so its map is chosen once (plan
2026-11-11, C5).

A map holds column names and role words only, never a row of the spreadsheet: no student data reaches this file. The file
is the operator's, kept as the offboarding templates are (``offboard_templates``): written atomically ``0600`` in a ``0700``
folder, and one it can't read or that isn't a maps file is moved aside whole (renamed with a timestamp) and read as empty,
never saved over.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

from .paths import app_data_dir
from .roster_mapped import ColumnMap

FILE_VERSION = 1
NAME_CAP = 60
MAPS_CAP = 50


def default_store_path() -> Path:
    return app_data_dir() / "roster-maps.json"


class MapStore:
    def __init__(self, path: Optional[Union[str, Path]] = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self.set_aside: Optional[Path] = None     # where an unreadable file went: said on the screen
        self._maps: Dict[str, ColumnMap] = self._load()

    def _load(self) -> Dict[str, ColumnMap]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError):
            return self._move_aside()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != FILE_VERSION or not isinstance(data.get("maps"), dict):
                raise ValueError("not a maps file")
            maps = data["maps"]
            if len(maps) > MAPS_CAP or not all(isinstance(n, str) and not _name_problem(n) and isinstance(m, dict)
                                               for n, m in maps.items()):
                raise ValueError("not a maps file")
            return {name: ColumnMap.from_json(m) for name, m in maps.items()}
        except (ValueError, TypeError, AttributeError):
            return self._move_aside()

    def _move_aside(self) -> Dict[str, ColumnMap]:
        aside = self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
        try:
            os.replace(self.path, aside)
            self.set_aside = aside
        except OSError:
            pass
        return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        data = {"version": FILE_VERSION, "maps": {name: m.as_json() for name, m in self._maps.items()}}
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".roster-maps-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def names(self) -> List[str]:
        return sorted(self._maps, key=str.lower)

    def get(self, name: str) -> Optional[ColumnMap]:
        return self._maps.get(name.strip())

    def save(self, name: str, cmap: ColumnMap) -> None:
        """ValueError, in words, for a name that can't be kept or a 51st map. Saving under a name already kept (in any
        case) replaces that map."""
        name = name.strip()
        problem = _name_problem(name)
        if problem:
            raise ValueError(problem)
        same = [n for n in self._maps if n.lower() == name.lower()]
        if not same and len(self._maps) >= MAPS_CAP:
            raise ValueError(f"There are {MAPS_CAP} saved maps already. Delete one you no longer use first.")
        for old in same:
            del self._maps[old]
        self._maps[name] = ColumnMap.from_json(cmap.as_json())     # stored as it will read back
        self._save()

    def delete(self, name: str) -> None:
        name = name.strip()
        if name in self._maps:
            del self._maps[name]
            self._save()


def _name_problem(name: str) -> str:
    if not name:
        return "Give the map a name."
    if len(name) > NAME_CAP:
        return f"A map's name is at most {NAME_CAP} characters."
    if name != name.strip() or not name.isprintable():
        return "A map's name can't hold a control or invisible character."
    return ""
