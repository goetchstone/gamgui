"""Ephemeral GAMCFGDIR materialization.

GAM needs its credential files present on disk. We don't keep them on disk — they live in the
Keychain. So for the duration of each authenticated ``gam`` call we:

1. create a private temp dir (``chmod 700``),
2. write the credentials from the vault into it (each file ``chmod 600``),
3. hand the dir path to the caller to use as ``GAMCFGDIR``,
4. on exit — even on error — write any refreshed ``oauth2.txt`` back to the vault, then wipe the dir.

The real protection is the short lifetime + restrictive perms + private location, not cryptographic
shredding (APFS/SSD make true secure-erase unreliable; we best-effort overwrite anyway).

``__exit__`` is the primary wipe, but it can be skipped entirely — the server runs in a daemon
thread that the interpreter kills mid-call at shutdown. Two backstops cover that: an ``atexit``
handler that wipes everything still registered in :data:`_LIVE`, and a sweep that removes dirs whose
owning process (recorded in a ``.pid`` file) is gone — or that have outlived any plausible call.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from ..paths import app_data_dir
from .vault import FILENAMES, SecretsVault

_REQUIRED = ("oauth2service", "oauth2")

# Name of the owner-PID marker written into every ephemeral dir. Dotted so it can't collide with a
# GAM config filename; GAM only reads the files it knows about, so the extra entry is inert.
_PID_FILENAME = ".gamgui.pid"

# How long a live-looking owner PID may protect a dir from the sweep. PIDs are recycled — and
# restart from low numbers after a reboot — so an orphan's number is eventually reused by an
# unrelated process (often one we can't signal, which `_pid_alive` has to read as alive). Without a
# ceiling such a dir would keep its plaintext credentials forever. No real in-flight gam call lasts
# a day, so past this point "the owner is alive" stops being a credible claim.
_LIVE_PID_TRUST_SECONDS = 24 * 60 * 60

# Realpaths of the dirs materialized by this process that haven't been wiped yet.
_LIVE: set[str] = set()


def _key(path: Path) -> str:
    return os.path.realpath(path)


def app_runtime_dir() -> Path:
    """Private base directory for transient runtime files (created ``0700``)."""
    base = app_data_dir() / "run"
    base.mkdir(parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    return base


def _shred_dir(path: Path) -> None:
    """Best-effort zero every file under *path*, then remove the tree. Never raises."""
    try:
        for child in path.iterdir():
            try:
                if child.is_file():
                    size = child.stat().st_size
                    with open(child, "r+b") as fh:
                        fh.write(b"\x00" * size)
                        fh.flush()
                        os.fsync(fh.fileno())
            except OSError:
                pass  # best-effort overwrite; removal below is what matters
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def wipe_live_configs() -> None:
    """Wipe every dir this process materialized but never wiped.

    Registered with :mod:`atexit`, which runs on the main thread once ``main()`` returns — exactly
    the path that leaks today, since a ``gam`` call still in flight dies with the daemon server
    thread and its ``__exit__`` never runs. Also called explicitly at shutdown so the credentials
    are gone before the interpreter finishes tearing down.
    """
    for key in list(_LIVE):
        try:
            _shred_dir(Path(key))
        except Exception:
            pass  # a failed cleanup must never mask the real exit
    _LIVE.clear()


atexit.register(wipe_live_configs)


def _owner_pid(child: Path) -> Optional[int]:
    """PID recorded in *child*'s marker file, or None if absent/unreadable/nonsensical."""
    try:
        pid = int((child / _PID_FILENAME).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None  # 0/negative mean "process group" to os.kill — never pass those


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. EPERM: it exists, it just isn't ours
    return True


def sweep_stale_configs(base_dir: Optional[Path] = None, max_age_seconds: float = 600) -> int:
    """Remove orphaned ``gamcfg-*`` dirs left by a crash/force-kill before the wipe could run.

    Normal exits wipe their own dir; this is the safety net for SIGKILL. A dir whose recorded owner
    PID is dead is orphaned by definition and goes immediately, whatever its age — age alone is a
    bad proxy, because an orphaned ``gam`` child keeps touching the dir and pushes eligibility out.
    A live-looking owner buys the dir a reprieve, but only up to :data:`_LIVE_PID_TRUST_SECONDS`;
    that ceiling is deliberately independent of *max_age_seconds* so the shutdown sweep
    (``max_age_seconds=0``) still can't shred a second instance's dir mid-``gam``-call. Dirs with no
    usable marker fall back to the age rule. Dirs this process is still using are never touched.
    Returns the number of stale directories removed.
    """
    base = Path(base_dir) if base_dir else app_runtime_dir()
    removed = 0
    now = time.time()
    try:
        for child in base.glob("gamcfg-*"):
            try:
                if not child.is_dir() or _key(child) in _LIVE:
                    continue
                pid = _owner_pid(child)
                age = now - child.stat().st_mtime
                if pid is None:
                    stale = age > max_age_seconds
                elif not _pid_alive(pid):
                    stale = True
                else:
                    stale = age > _LIVE_PID_TRUST_SECONDS
                if stale:
                    _shred_dir(child)
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


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
        # All-or-nothing from here: if setup dies partway, the caller never gets the path and so can
        # never run __exit__, which would strand a half-populated dir on disk (and in _LIVE) for the
        # rest of the process lifetime.
        try:
            os.chmod(self.path, 0o700)
            # Register before writing anything, so the atexit backstop can't miss a populated dir.
            _LIVE.add(_key(self.path))
            self._write_secret(self.path / _PID_FILENAME, str(os.getpid()))

            for name, value in creds.items():
                if value is None:
                    continue
                self._write_secret(self.path / FILENAMES[name], value)
                if name == "oauth2":
                    self._oauth2_hash = _sha(value)
        except BaseException:
            self._wipe()
            raise

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
        if not self.path:
            return
        _LIVE.discard(_key(self.path))
        if self.path.exists():
            _shred_dir(self.path)
        self.path = None
