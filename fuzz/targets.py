"""The fuzz targets: every property test in tests/test_props_*.py.

Found, not listed, so a new property is fuzzed the day it is written. A target must be callable as
``test.hypothesis.fuzz_one_input(data)``: a module-level ``@given`` function with no pytest fixtures and
no ``parametrize`` (whose arguments the fuzzer can't supply). ``tests/test_fuzz_targets.py`` fails on one
that isn't, and runs each target on a few fixed inputs, so the plumbing is proven in every suite run —
also where Atheris itself doesn't install.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Callable, List

ROOT = Path(__file__).resolve().parent.parent
PROPERTY_MODULES = sorted(p.stem for p in (ROOT / "tests").glob("test_props_*.py"))

def _marks(func: Callable) -> List[str]:
    return [m.name for m in getattr(func, "pytestmark", [])]


def properties() -> List[Callable]:
    """Every ``@given`` test in the property modules, fuzzable or not (the tripwire checks which)."""
    found = []
    for name in PROPERTY_MODULES:
        module = importlib.import_module(f"tests.{name}")
        found += [f for _, f in inspect.getmembers(module, inspect.isfunction)
                  if f.__module__ == module.__name__ and hasattr(f, "hypothesis")]
    return found


def fuzzable(func: Callable) -> bool:
    """Callable with the fuzzer's bytes alone. An xfail property (a known, reported bug) is left out —
    the fuzzer would only find it again."""
    return not {"parametrize", "usefixtures", "xfail"} & set(_marks(func))


def targets() -> List[Callable]:
    return [f for f in properties() if fuzzable(f)]
