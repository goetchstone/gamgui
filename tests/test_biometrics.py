from __future__ import annotations

from gamgui.core import biometrics


def test_disabled_via_env_allows_without_prompting(monkeypatch):
    monkeypatch.setenv(biometrics.DISABLE_ENV, "1")
    # With the kill-switch set, the gate must never prompt and must let the user in.
    assert biometrics.require_unlock("test") is True
    assert biometrics.biometrics_available() is False


def test_fails_open_when_frameworks_absent(monkeypatch):
    # Simulate a machine without the pyobjc LocalAuthentication framework (e.g. Linux CI):
    # importing it raises, and the gate must fail OPEN (return True) rather than lock the app.
    monkeypatch.delenv(biometrics.DISABLE_ENV, raising=False)
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("LocalAuthentication", "Foundation"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert biometrics.require_unlock("test") is True
    assert biometrics.biometrics_available() is False
