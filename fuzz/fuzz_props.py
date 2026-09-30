"""Coverage-guided fuzzing of GamGUI's property tests with Atheris (CI's ``fuzz`` job).

Each input's first byte picks a property (``fuzz/targets.py``: every test in tests/test_props_*.py);
Hypothesis turns the rest into that property's arguments. So the fuzzer steers, by coverage of
``gamgui``, the same invariants the suite checks with fixed examples — argv-only (invariant 1), secret
redaction, the parsers of GAM's output and the hire CSV, the error classifier. A property that fails,
or any uncaught exception, is a crash: libFuzzer writes the input to ``<artifact_prefix>crash-<sha1>``
and exits non-zero.

Atheris ships wheels only for Linux x86_64 CPython 3.12+ (requirements/dev.in's marker), so this runs
in CI, not on a Mac. Reproduce a crash there with the same command plus the crash file as the argument:

    PYTHONPATH=. PYTHONHASHSEED=0 python fuzz/fuzz_props.py -seed=1 -runs=300000 -max_len=4096
"""

from __future__ import annotations

import sys

import atheris
import pytest  # noqa: F401 — the property modules import it; imported first, it stays uninstrumented
from hypothesis import settings

# The fuzzer supplies the inputs: no example database written into the tree, and no deadline — an
# instrumented run is slower than the suite's, and a slow input is not a failure.
settings.register_profile("fuzz", deadline=None, database=None)
settings.load_profile("fuzz")

with atheris.instrument_imports(include=["gamgui"]):
    from fuzz.targets import targets

    TARGETS = targets()


def one_input(data: bytes) -> None:
    if len(data) < 2:
        return
    TARGETS[data[0] % len(TARGETS)].hypothesis.fuzz_one_input(data[1:])


if __name__ == "__main__":
    if not TARGETS:
        sys.exit("no fuzz targets: tests/test_props_*.py holds no fuzzable property")
    print(f"fuzzing {len(TARGETS)} properties", flush=True)
    atheris.Setup(sys.argv, one_input)
    atheris.Fuzz()
