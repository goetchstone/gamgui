from __future__ import annotations

import os

from gamgui.core.audit import AuditLog, redact_argv, redact_secrets


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


def test_positional_redaction_is_shifted_by_a_value_that_spells_a_key():
    # Why redaction by value exists: surname "Password" takes the mask meant for the real password.
    argv = ["create", "user", "a@e.com", "lastname", "Password", "password", "S3cret-pw", "changepassword", "on"]
    assert "S3cret-pw" in redact_argv(argv)
    assert "S3cret-pw" not in redact_secrets(redact_argv(argv), ["S3cret-pw"])


def test_redact_secrets_masks_every_occurrence_in_nested_values():
    value = {"error": "Command: gam x password S3cret-pw notifypassword S3cret-pw",
             "argv": ["S3cret-pw", "S3cret-pwX"], "n": 3, "none": None}
    out = redact_secrets(value, ["S3cret-pw", ""])   # an empty secret must not mask everything
    assert "S3cret-pw" not in str(out)
    assert out["argv"] == ["***redacted***", "***redacted***X"] and out["n"] == 3 and out["none"] is None
    assert redact_secrets("unchanged", []) == "unchanged" and redact_secrets(None, ["x"]) is None


def test_record_redacts_secrets_in_every_field(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("create_user", target="S3cret-pw@e.com", argv=["lastname", "Password", "password", "S3cret-pw"],
               ok=False, extra={"error": "... password S3cret-pw ..."}, secrets=["S3cret-pw"])
    assert "S3cret-pw" not in (tmp_path / "audit.jsonl").read_text()


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
