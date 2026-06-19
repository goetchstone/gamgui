from __future__ import annotations

import os

from gamgui.core.audit import AuditLog, redact_argv


def test_redact_masks_password_value():
    argv = ["create", "user", "a@e.com", "password", "Sup3rSecret!", "firstname", "A"]
    red = redact_argv(argv)
    assert "Sup3rSecret!" not in red
    assert red[red.index("password") + 1] == "***redacted***"
    # non-sensitive values survive
    assert red[red.index("firstname") + 1] == "A"


def test_redact_masks_signature_value():
    red = redact_argv(["user", "a@e.com", "signature", "secret sig", "html"])
    assert "secret sig" not in red


def test_redact_masks_recovery_fields():
    red = redact_argv(["update", "user", "a@e.com", "recoveryemail", "secret@personal.com", "recoveryphone", "+15551234"])
    assert "secret@personal.com" not in red and "+15551234" not in red
    assert red[red.index("recoveryemail") + 1] == "***redacted***"


def test_record_and_tail(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("set_signature", target="a@e.com", argv=["user", "a@e.com", "signature", "x"], ok=True)
    log.record("suspend", target="b@e.com", ok=True)
    entries = log.tail()
    assert len(entries) == 2
    assert entries[-1]["action"] == "suspend"
    assert entries[0]["argv"][-1] == "***redacted***"  # signature value redacted on the way in


def test_audit_file_permissions_are_600(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record("noop", ok=True)
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600
