"""The fuzz job's plumbing (fuzz/), proven in every suite run — also on a Mac, where Atheris doesn't
install: every property test is a fuzz target, and each takes raw bytes the way libFuzzer hands them
over (``fuzz_one_input``), holding its property for them."""

from __future__ import annotations

import pytest

from fuzz.targets import fuzzable, properties, targets

# Bytes of the shapes libFuzzer starts from: empty, all-zero, all-0xff, every byte value, and text.
SEEDS = [b"", b"\x00" * 64, b"\xff" * 64, bytes(range(256)), "Ada Byte, ada@example.com; rm -rf /\n".encode() * 8]


def test_every_property_is_a_fuzz_target():
    props = properties()
    assert len(props) >= 10, "the property modules hold fewer tests than expected — did discovery break?"
    stuck = [f"{f.__module__}.{f.__name__}" for f in props
             if not fuzzable(f) and "xfail" not in [m.name for m in getattr(f, "pytestmark", [])]]
    assert not stuck, f"not callable by the fuzzer (parametrize/fixtures): {stuck}"


@pytest.mark.parametrize("target", targets(), ids=lambda f: f"{f.__module__.rsplit('.', 1)[-1]}.{f.__name__}")
def test_each_target_takes_raw_bytes(target):
    for data in SEEDS:
        target.hypothesis.fuzz_one_input(data)
