"""Ephemeral GAMCFGDIR materialization.

GAM needs its credential files present on disk. We don't keep them on disk — they live in the
Keychain. So for the duration of each authenticated ``gam`` call we:

1. create a private temp dir (``chmod 700``),
2. write the credentials from the vault into it (each file ``chmod 600``),
3. hand the dir path to the caller to use as ``GAMCFGDIR``,
4. on exit — even on error — write any refreshed ``oauth2.txt`` back to the vault, then wipe the dir.

The real protection is the short lifetime + restrictive perms + private location, not cryptographic
shredding (APFS/SSD make true secure-erase unreliable; we best-effort overwrite anyway).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from .vault import FILENAMES, SecretsVault

_REQUIRED = ("oauth2service", "oauth2")


def app_runtime_dir() -> Path:
    """Private base directory for transient runtime files (created ``0700``)."""
    base = Path.home() / "Library" / "Application Support" / "GamGUI" / "run"
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    return base


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class EphemeralConfig:
    """Context manager yielding a ``GAMCFGDIR`` path populated from the vault.

    Parameters
    ----------
    vault: the secret store to read credentials from / write refreshed tokens back to.
    domain: the Workspace domain whose credentials to materialize.
    require: if True (default), raise if the credentials needed to act as the domain are missing.
    base_dir: parent dir for the temp dir (tests pass a tmp path); defaults to the app runtime dir.
    """

    def __init__(
        self,
        vault: SecretsVault,
        domain: str,
        require: bool = True,
        base_dir: Optional[Path] = None,
    ) -> None:
        self.vault = vault
        self.domain = domain
        self.require = require
        self.base_dir = Path(base_dir) if base_dir else app_runtime_dir()
        self.path: Optional[Path] = None
        self._oauth2_hash: Optional[str] = None

    def __enter__(self) -> Path:
        creds = self.vault.get_all(self.domain)
        if self.require:
            missing = [n for n in _REQUIRED if not creds.get(n)]
            if missing:
                raise PermissionError(
                    f"missing credentials for {self.domain}: {missing}. Complete setup first."
                )

        self.path = Path(tempfile.mkdtemp(prefix="gamcfg-", dir=str(self.base_dir)))
        os.chmod(self.path, 0o700)

        for name, value in creds.items():
            if value is None:
                continue
            self._write_secret(self.path / FILENAMES[name], value)
            if name == "oauth2":
                self._oauth2_hash = _sha(value)

        return self.path

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        try:
            self._write_back_refreshed_token()
        finally:
            self._wipe()

    # --- internals ---------------------------------------------------------------------
    @staticmethod
    def _write_secret(target: Path, value: str) -> None:
        # Open with 0600 from the start to avoid any world-readable window.
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, value.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(target, 0o600)

    def _write_back_refreshed_token(self) -> None:
        """GAM rewrites oauth2.txt when it refreshes the access token; persist the change."""
        if not self.path:
            return
        token_file = self.path / FILENAMES["oauth2"]
        if not token_file.exists():
            return
        try:
            new_value = token_file.read_text(encoding="utf-8")
        except OSError:
            return
        if new_value and _sha(new_value) != self._oauth2_hash:
            self.vault.set(self.domain, "oauth2", new_value)
            self._oauth2_hash = _sha(new_value)

    def _wipe(self) -> None:
        if not self.path or not self.path.exists():
            return
        for child in self.path.iterdir():
            try:
                if child.is_file():
                    size = child.stat().st_size
                    with open(child, "r+b") as fh:
                        fh.write(b"\x00" * size)
                        fh.flush()
                        os.fsync(fh.fileno())
            except OSError:
                pass  # best-effort overwrite; removal below is what matters
        shutil.rmtree(self.path, ignore_errors=True)
        self.path = None
