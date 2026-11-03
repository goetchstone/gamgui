"""The fuzz job's plumbing (fuzz/), proven in every suite run — also on a Mac, where Atheris doesn't
install: every property test is a fuzz target, and each takes raw bytes the way libFuzzer hands them
over (``fuzz_one_input``), holding its property for them."""

from __future__ import annotations

import base64

import pytest

from fuzz.targets import fuzzable, properties, targets

# Bytes of the shapes libFuzzer starts from: empty, all-zero, all-0xff, every byte value, and text.
SEEDS = [b"", b"\x00" * 64, b"\xff" * 64, bytes(range(256)), "Ada Byte, ada@example.com; rm -rf /\n".encode() * 8]

# Inputs the fuzz job failed on: the "Base64:" line of its log, and the property it picked. Each must keep
# passing. The first byte only picked the property (fuzz_props.one_input), and that pick shifts whenever a
# property is added, so the property is named here.
CRASHES = [
    # failure log 2026-11-03: a filler token held the password's characters
    ("test_an_echoed_password_never_reaches_the_operator",
     "XO4AAO3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3tAO3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t7e3t"
     "7e3t7e3t7e3t7e3t7e0A7gEAAAAAAAAAAIMhADoAAAD1/AAA"),
]


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


@pytest.mark.parametrize(("name", "b64"), CRASHES, ids=[name for name, _ in CRASHES])
def test_a_past_fuzz_crash_holds(name, b64):
    target = {f.__name__: f for f in targets()}[name]
    target.hypothesis.fuzz_one_input(base64.b64decode(b64)[1:])
