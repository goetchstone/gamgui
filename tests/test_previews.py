"""The shared preview store: what a confirm step may run is what its preview held.

Each flow's route tests prove the end-to-end rule (preview A, edit to B, run: B does not run); these
pin the store itself — single use, a changed form refused, expiry, and a bound per flow (#9).
"""

from __future__ import annotations

from gamgui.web import previews
from gamgui.web.previews import Previews


def test_a_held_value_is_taken_once_with_the_form_it_was_previewed_from():
    store = Previews()
    token = store.hold("flow", ("a", 1), {"run": "this"})
    assert store.take("flow", token, ("a", 1)) == ({"run": "this"}, None)
    value, refusal = store.take("flow", token, ("a", 1))                 # single use
    assert value is None and "expired or was already run" in refusal


def test_a_form_edited_after_the_preview_is_refused_and_spends_the_token():
    store = Previews()
    token = store.hold("flow", ("a",), "A")
    value, refusal = store.take("flow", token, ("b",))
    assert value is None and "The form changed after the preview" in refusal
    assert store.take("flow", token, ("a",))[0] is None                  # preview again for a new one


def test_no_token_an_unknown_one_or_another_flows_is_refused():
    store = Previews()
    token = store.hold("offboard", (), "x")
    for flow, tok in (("offboard", ""), ("offboard", "nope"), ("signatures", token)):
        value, refusal = store.take(flow, tok, ())
        assert value is None and "expired or was already run" in refusal
    assert store.take("offboard", token, ()) == ("x", None)             # the wrong-flow try didn't spend it


def test_an_expired_preview_is_refused(monkeypatch):
    store = Previews()
    token = store.hold("flow", (), "x")
    monkeypatch.setattr(previews, "PREVIEW_TTL", -1)
    assert store.take("flow", token, ())[0] is None


def test_time_asleep_counts_toward_expiry(monkeypatch):
    # time.monotonic() stops while a Mac sleeps, so a preview left open over a closed lid stayed
    # runnable hours later. Expiry reads clock.now(), which counts sleep (CLOCK_MONOTONIC on macOS).
    import time

    from gamgui.core import clock

    assert clock._CLOCK in {getattr(time, "CLOCK_BOOTTIME", None), getattr(time, "CLOCK_MONOTONIC", None)}
    start = clock.now()
    monkeypatch.setattr(clock, "now", lambda: start)
    store = Previews()
    token = store.hold("flow", (), "x")
    monkeypatch.setattr(clock, "now", lambda: start + previews.PREVIEW_TTL + 1)   # the lid was closed
    assert store.take("flow", token, ())[0] is None


def test_the_refusal_says_what_to_click_again():
    store = Previews()
    _, refusal = store.take("flow", "", (), again="click Preview steps again")
    assert refusal.endswith("click Preview steps again.")


def test_held_previews_are_bounded_per_flow():
    store = Previews()
    first = store.hold("a", (), 0)
    for i in range(50):
        store.hold("a", (), i)
        store.hold("b", (), i)
    assert store.count("a") == store.count("b") == previews.PREVIEWS_KEPT
    assert store.take("a", first, ())[0] is None                         # the oldest went first
