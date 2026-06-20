"""Optional Touch ID unlock gate (macOS).

This is a *convenience* gate layered on top of the real security boundary (the Keychain + the
ephemeral ``chmod 600`` credential files). Because it is not the boundary, it is designed to **fail
open**: if biometrics is unavailable, the frameworks aren't present (non-mac / headless CI), an error
occurs, or it times out, the app still launches. Only an *explicit* failed/cancelled biometric
prompt blocks entry. Disable it entirely with ``GAMGUI_NO_BIOMETRICS=1``.

Kept dependency-light: the macOS frameworks are imported lazily inside the functions so this module
imports cleanly on any platform and in tests.
"""

from __future__ import annotations

import os
import time

DISABLE_ENV = "GAMGUI_NO_BIOMETRICS"
_TIMEOUT_S = 30.0


def _disabled() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


def biometrics_available() -> bool:
    """True only if Touch ID can actually be evaluated on this machine (and not disabled)."""
    if _disabled():
        return False
    try:
        import LocalAuthentication  # type: ignore

        ctx = LocalAuthentication.LAContext.alloc().init()
        policy = LocalAuthentication.LAPolicyDeviceOwnerAuthenticationWithBiometrics
        ok, _err = ctx.canEvaluatePolicy_error_(policy, None)
        return bool(ok)
    except Exception:
        return False


def require_unlock(reason: str = "unlock GamGUI") -> bool:
    """Prompt for Touch ID and return whether to allow entry.

    Returns True (allow) when biometrics is disabled, unavailable, errors, or times out — so the app
    is never bricked. Returns the real result only when a biometric prompt was actually shown:
    True on success, False on an explicit failure/cancel.
    """
    if _disabled():
        return True
    try:
        import Foundation  # type: ignore
        import LocalAuthentication  # type: ignore
    except Exception:
        return True  # frameworks absent (non-mac / headless) -> don't block

    try:
        ctx = LocalAuthentication.LAContext.alloc().init()
        policy = LocalAuthentication.LAPolicyDeviceOwnerAuthenticationWithBiometrics
        can, _err = ctx.canEvaluatePolicy_error_(policy, None)
        if not can:
            return True  # no Touch ID enrolled on this Mac -> skip the gate, don't lock out

        # The biometric sheet needs an NSApplication context to present; pywebview only creates one
        # later, so ensure it exists now (idempotent — pywebview reuses the same shared instance).
        try:
            import AppKit  # type: ignore

            AppKit.NSApplication.sharedApplication()
        except Exception:
            pass

        state = {"done": False, "ok": False}

        def _reply(success, error) -> None:
            state["ok"] = bool(success)
            state["done"] = True

        ctx.evaluatePolicy_localizedReason_reply_(policy, reason, _reply)

        # The biometric UI needs the main run loop to be pumping to appear and to deliver the reply.
        # We're on the main thread before pywebview starts its own loop, so pump it here, bounded.
        run_loop = Foundation.NSRunLoop.currentRunLoop()
        deadline = time.monotonic() + _TIMEOUT_S
        while not state["done"] and time.monotonic() < deadline:
            run_loop.runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))

        if not state["done"]:
            return True  # timed out (e.g. flaky sensor) -> fail open
        return state["ok"]
    except Exception:
        return True


if __name__ == "__main__":  # pragma: no cover - manual hardware self-test
    # Run:  python -m gamgui.core.biometrics
    print("biometrics_available:", biometrics_available())
    print("Prompting for Touch ID now (Esc/cancel to decline)…")
    print("result (True = allowed in):", require_unlock("test GamGUI Touch ID"))
