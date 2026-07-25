"""Setup / onboarding service.

Turns GAM's terminal-only authorization into a guided flow. Two paths:

* **Import** — read an existing GAM config dir's credential files into the Keychain. Robust and the
  common case for admins who already run GAM.
* **Fresh** — hand the user the exact ``gam create project`` / ``oauth create`` / ``create svcacct``
  commands to run (those open a browser and are interactive), pointed at a managed config dir, then
  import from it.

Both converge on: credentials in the Keychain → the manual Domain-Wide Delegation step → verify with
``gam <admin> check svcacct``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gam.commands import EXPECTED_GAM_VERSION, GAMCommands
from .gam.errors import GAMError
from .gam.runner import GAMRunner
from .secrets.ephemeral import app_runtime_dir
from .secrets.vault import FILENAMES, SecretsVault

_WIPE_CHUNK = 1 << 16

# Depth limit for the descriptor walk up to a root — a directory tree that deep is not a real setup,
# and an unbounded loop on a filesystem that never reports its top would hang the request.
_MAX_TREE_DEPTH = 128

# Directories a chosen import root must never sit above. Spelled as the operator would type them;
# every check resolves them first (``/etc`` is really ``/private/etc`` on macOS).
_SENSITIVE_DIRS = ("/etc", "/var", "/usr", "/System", "/Library", "/dev")

# Where macOS mounts its volumes. Read to enumerate the *other* spelling of a system directory —
# see :func:`_spellings`.
_VOLUMES_DIR = Path("/System/Volumes")


def _fs_id(path) -> Optional[Tuple[int, int]]:
    """``(st_dev, st_ino)`` for *path* — the filesystem's own identity for it — or ``None``.

    ``None`` for anything that can't be stat'ed: missing, unreadable, a broken symlink, a path with
    a NUL in it, or a component that vanished mid-walk. Callers treat ``None`` as "does not match",
    so a permission error or a race can only ever narrow what is allowed, never widen it.
    """
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _within_roots(p: Path, root_ids: set) -> bool:
    """Is the already-resolved *p* one of the allowed roots, or somewhere inside one?

    Compared by filesystem IDENTITY — ``(st_dev, st_ino)`` — not by comparing path strings, because
    on macOS several genuinely different spellings name the very same directory and a string test
    gets all of them wrong:

    * ``resolve()`` does not correct case, so ``/users/x/.gam`` stays lowercase even though home is
      ``/Users/x``. Case-folding the strings instead is not the answer: on a case-SENSITIVE volume
      ``/users`` and ``/Users`` really are two directories, and an attacker who can plant a symlink
      at the folded spelling could turn a "does this volume fold case?" probe into a bypass.
    * ``é`` composed (NFC) and decomposed (NFD) are different byte strings but the same directory —
      an operator whose home folder is named ``josé`` must not be locked out of their own home.
    * ``/Users`` is an APFS *firmlink*, not a symlink, so ``resolve()`` does not collapse
      ``/System/Volumes/Data/Users/x`` to ``/Users/x`` — yet it is the same directory.

    ``stat`` answers all three the same way, and it is the same notion of identity the kernel uses
    when it opens the file. *p* itself may not exist (bounds are checked before existence, so an
    out-of-bounds path gets one uniform answer); its parents then decide.
    """
    if not root_ids:
        return False
    for candidate in (p, *p.parents):
        ident = _fs_id(candidate)
        if ident is not None and ident in root_ids:
            return True
    return False


def _spellings(target) -> List[Path]:
    """Every path this machine can reach *target* by, as far as they can be enumerated: its
    ``realpath``, plus the same path hung under each volume mounted in ``/System/Volumes``.

    macOS mounts the data volume at ``/System/Volumes/Data`` and firmlinks its top-level directories
    into ``/``, so ``/private/etc`` and ``/System/Volumes/Data/private/etc`` are ONE directory with
    two different ancestor chains — and only the second chain shows that ``/System`` and
    ``/System/Volumes`` contain it. That is exactly how ``$GAMCFGDIR=/System`` used to get the real
    ``/etc`` back in reach: ``resolve()`` does not collapse a firmlink, so the long spelling passed
    the bound.

    Candidates whose identity doesn't match *target* are dropped, so an absent or unreadable volume
    contributes nothing. Empty when *target* cannot be stat'ed at all.
    """
    real = Path(os.path.realpath(target))
    ident = _fs_id(real)
    if ident is None:
        return []
    out = [real]
    try:
        volumes = sorted(_VOLUMES_DIR.iterdir())
    except OSError:
        volumes = []                 # no /System/Volumes (not macOS, or unreadable): realpath only
    for volume in volumes:
        candidate = volume.joinpath(*real.parts[1:])
        if _fs_id(candidate) == ident:
            out.append(candidate)
    return out


def _reaches(root_id: Tuple[int, int], target) -> bool:
    """Is *target* at, or below, the directory whose identity is *root_id*?

    Path strings cannot answer this (see :func:`_spellings`), so it is answered by identity, for
    every spelling of *target* the machine can reach: if the root's inode turns up anywhere in the
    ancestor chain of any of them, the root contains the target.
    """
    return any(_within_roots(spelling, {root_id}) for spelling in _spellings(target))


def _root_is_sane(root: Path, home: Optional[Path]) -> bool:
    """Is *root* narrow enough to be an import root? ``$GAMCFGDIR`` is operator-supplied, and a
    handful of plausible-looking values quietly widen the bound to the whole machine. Two rules:

    (a) it has to be an existing DIRECTORY. Without this, ``$GAMCFGDIR=/etc/passwd`` makes that
        FILE's inode an allowed "root" — and a path is in bounds when it *or any parent* matches, so
        the file matched itself and a credential symlink to it imported ``/etc/passwd``'s bytes.
    (b) it must not be an ancestor of the operator's home (home ITSELF is fine — it is already a
        root), nor of ``/etc``, ``/var``, ``/usr``, ``/System``, ``/Library`` or ``/dev``.

    Rule (b) is what stops a root from meaning "the whole disk", and it takes out ``/``,
    ``/private``, ``/etc/..``, ``/Users``, ``/System``, ``/System/Volumes`` and
    ``/System/Volumes/Data`` in one stroke, because each of those sits above home or above the
    system directories (by inode, so the firmlink routes count too). What it deliberately does NOT
    reject is the case this escape hatch exists for: a mounted volume such as ``/Volumes/GAMKEY`` is
    above none of them, so a service-account key on an encrypted stick keeps importing.
    """
    ident = _fs_id(root)
    if ident is None:
        return False                            # missing, unreadable, or a broken link
    try:
        if not stat.S_ISDIR(os.stat(root).st_mode):
            return False                        # (a) a file (or device) is not a root
    except (OSError, ValueError):
        return False
    if home is not None and ident != _fs_id(home) and _reaches(ident, home):
        return False                            # (b) above the operator's own home
    return not any(_reaches(ident, sensitive) for sensitive in _SENSITIVE_DIRS)


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _fd_within_roots(dir_fd: int, root_ids: set) -> bool:
    """Is the directory *dir_fd* refers to one of the allowed roots, or inside one?

    Same question as :func:`_within_roots`, asked in the one way a concurrent attacker cannot
    interfere with: by walking UPWARD from a pinned descriptor. ``openat(fd, "..")`` is resolved by
    the kernel from the inode the descriptor already holds, so there is no window in which a path
    component can be renamed or swapped for a symlink underneath the walk — which is exactly the
    window a name-based walk leaves open. The top of the tree announces itself by ``".."`` being the
    directory itself.
    """
    if not root_ids:
        return False
    cur: Optional[int] = None
    try:
        cur = os.dup(dir_fd)
        for _ in range(_MAX_TREE_DEPTH):
            st = os.fstat(cur)
            if (st.st_dev, st.st_ino) in root_ids:
                return True
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=cur)
            pst = os.fstat(parent)
            if (pst.st_dev, pst.st_ino) == (st.st_dev, st.st_ino):
                _close(parent)
                return False            # ".." of "/" is "/": the top, and no root matched
            _close(cur)
            cur = parent
        return False                    # absurdly deep: fail closed
    except (OSError, ValueError, NotImplementedError):
        return False                    # a race or a permission error can only ever narrow this
    finally:
        if cur is not None:
            _close(cur)


def _pin_bounded_dir(p: Path, root_ids: set) -> Optional[int]:
    """A descriptor for the credentials directory *p*, pinned and proven to be inside the roots.

    This is the one place the bound has to be established, and everything after it is then race-free:
    a descriptor names an inode, so once the directory is pinned, no rename of it or of any component
    above it can change which directory the credential files are read from. The order matters — pin
    first, prove second (:func:`_fd_within_roots`), because proving something about a *name* and then
    opening that name is the TOCTOU this whole path exists to avoid.

    ``None`` when *p* is not a directory, cannot be opened, or is not inside an allowed root. Note
    that a symlink *at* *p* is followed here (``resolve_dir`` has already resolved the operator's
    input): what matters is that whatever it lands on is proven in bounds by descriptor.
    """
    try:
        dir_fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY)
    except (OSError, ValueError):
        return None
    if not _fd_within_roots(dir_fd, root_ids):
        _close(dir_fd)
        return None
    return dir_fd


def _open_in_dir(dir_fd: int, fname: str, flags: int) -> Optional[Tuple[int, Tuple[int, int]]]:
    """``(fd, identity)`` for the regular file *fname* directly inside the pinned *dir_fd*, or ``None``.

    Only one name is resolved — a single component, relative to a directory that is already proven in
    bounds — and ``O_NOFOLLOW`` forbids that component being a symlink. So the descriptor handed back
    cannot refer to anything outside the roots: not through a re-pointed link, and not through a
    renamed parent, because there is no parent lookup left to race.

    A credential file that IS a symlink is therefore not importable at all, even to an in-bounds
    target. The three filenames are GAM's own; no real setup symlinks them, and allowing it would put
    a re-pointable indirection back in the middle of the read.

    HARDLINKS are deliberately out of scope. A hardlink is indistinguishable from the original — it
    *is* the file, with no target to resolve and nothing to compare — and it cannot cross volumes, so
    it can only ever name something already on the operator's own volume. Creating one inside the
    operator's home requires local write access as that user, at which point the attacker can simply
    write the credential file's contents directly and has better options than this.
    """
    try:
        fd = os.open(fname, flags | os.O_NOFOLLOW, dir_fd=dir_fd)
    except (OSError, ValueError, NotImplementedError):
        return None                     # absent, a symlink, unreadable, or vanished mid-import
    try:
        st = os.fstat(fd)
    except OSError:
        _close(fd)
        return None
    if not stat.S_ISREG(st.st_mode):    # a fifo would block, a device is not a credential file
        _close(fd)
        return None
    return fd, (st.st_dev, st.st_ino)


def _read_credential(dir_fd: int, fname: str) -> Optional[Tuple[str, Tuple[int, int]]]:
    """The credential text in *fname*, plus the identity of the inode it actually came from.

    Read from the descriptor that was checked — never re-opened by name — so the bytes that reach the
    Keychain are the bytes of a file proven to be inside an allowed root.

    ``None`` covers everything an import should quietly skip rather than crash on: absent, a symlink,
    not a regular file, unreadable (``chmod 000`` is nothing the operator can fix from the wizard),
    vanished between the listing and the open, or not UTF-8 text.
    """
    opened = _open_in_dir(dir_fd, fname, os.O_RDONLY)
    if opened is None:
        return None
    fd, ident = opened
    try:
        with os.fdopen(fd, "rb") as fh:          # takes ownership of the descriptor
            raw = fh.read()
    except OSError:
        _close(fd)
        return None
    try:
        return raw.decode("utf-8"), ident
    except UnicodeDecodeError:
        return None


def _file_present(dir_fd: int, fname: str) -> bool:
    """Is there a plain regular file called *fname* in the pinned directory? (No symlinks — those are
    not importable, so reporting them as present would promise something the import won't do.)"""
    try:
        st = os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
    except (OSError, ValueError, NotImplementedError):
        return False
    return stat.S_ISREG(st.st_mode)


def _wipe_file(dir_fd: int, fname: str, expect: Tuple[int, int]) -> None:
    """Best-effort overwrite-then-unlink of the plaintext credential file whose inode is *expect*.

    APFS makes true secure-erase unreliable, so the overwrite is defence-in-depth, not a guarantee.
    What this DOES guarantee is that it never destroys a file it has not positively identified — and
    that guarantee is worth more here than on the read side: re-opening by path (``is_file()`` then
    ``open``) meant that swapping the managed staging dir for a symlink to another directory made
    GamGUI zero and unlink files there that it had never imported, while the real credentials survived
    somewhere else. Two things prevent that now. The lookup is a single component inside the pinned,
    in-bounds directory descriptor, and the descriptor is ``fstat``-ed against *expect* — the exact
    inode whose contents reached the Keychain. Anything else is left completely alone.

    ``unlink`` has to name the file (there is no "unlink this descriptor"), so it too goes through
    *dir_fd* and is guarded by an identity re-check immediately before. Losing that last sliver of a
    race can only leave a file undeleted, never delete the wrong one — the content is already gone.
    """
    opened = _open_in_dir(dir_fd, fname, os.O_WRONLY)
    if opened is None:
        return
    fd, ident = opened
    if ident != expect:
        _close(fd)
        return                          # not the file we imported: not ours to destroy
    try:
        size = os.fstat(fd).st_size
        os.lseek(fd, 0, os.SEEK_SET)
        written = 0
        while written < size:
            written += os.write(fd, b"\0" * min(_WIPE_CHUNK, size - written))
        os.fsync(fd)
    except OSError:
        pass                            # the overwrite is best-effort; the unlink still matters
    finally:
        _close(fd)
    try:
        st = os.stat(fname, dir_fd=dir_fd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) == expect:
            os.unlink(fname, dir_fd=dir_fd)
    except (OSError, ValueError, NotImplementedError):
        pass


def _home_root() -> Optional[Path]:
    """The operator's home directory, resolved — or ``None`` when the environment has none.

    ``Path.home()`` raises ``RuntimeError`` when neither ``$HOME`` nor the password database can
    answer (a launchd job with a scrubbed environment). That is not something an operator can fix
    from the wizard, so it must not surface as a traceback: callers read ``None`` as "no home root".
    """
    try:
        return Path.home().expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


ADMIN_CONSOLE_DWD_URL = "https://admin.google.com/ac/owl/domainwidedelegation"
_REQUIRED = ("oauth2service", "oauth2")


@dataclass
class DirInspection:
    path: str
    files: Dict[str, bool]               # credential name -> present?
    label: str = ""

    @property
    def has_required(self) -> bool:
        return all(self.files.get(n) for n in _REQUIRED)

    @property
    def any_present(self) -> bool:
        return any(self.files.values())


@dataclass
class VerifyResult:
    ok: bool
    summary: str
    lines: List[Tuple[str, str]] = field(default_factory=list)   # (label, status)
    raw: str = ""
    auth_url: str = ""   # GAM-provided link to authorize Domain-Wide Delegation, if it failed


class SetupService:
    def __init__(self, vault: SecretsVault, runner: GAMRunner) -> None:
        self.vault = vault
        self.runner = runner

    # --- engine ------------------------------------------------------------------------
    async def engine_version(self) -> str:
        if not self.runner.binary_exists():
            return ""
        try:
            return (await self.runner.version()).splitlines()[0]
        except Exception:
            return ""

    async def engine_version_warning(self) -> str:
        """Fail-soft self-check: a soft warning if the running GAM differs from the tested version.

        Empty when it matches, when GAM isn't vendored, or when the version can't be read — never
        blocks. Catches a swapped binary or a ``GAMGUI_GAM_BINARY`` override silently in use.
        """
        version = await self.engine_version()
        if not version or EXPECTED_GAM_VERSION in version:
            return ""
        return (
            f"GamGUI was built and tested with GAM {EXPECTED_GAM_VERSION}, but you're running "
            f"“{version}”. Most things will work; some commands may behave unexpectedly."
        )

    # --- discovering / inspecting credential directories -------------------------------
    def managed_setup_dir(self) -> Path:
        """A private dir the 'fresh setup' commands write into, which we then import from."""
        d = app_runtime_dir().parent / "setup"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def candidate_dirs(self) -> List[DirInspection]:
        """Likely GAMCFGDIR locations that already hold credentials.

        Only ever offers a folder :meth:`import_dir` would actually accept — the offer is filtered
        through :meth:`resolve_dir`, the same gate the import runs. An "Import" button that the
        import then refuses is a UX bug on its own, and it would also make the list a presence
        oracle for paths outside the bound.
        """
        candidates: List[Tuple[Path, str]] = []
        env = (os.environ.get("GAMCFGDIR") or "").strip()
        if env:
            candidates.append((Path(env), "$GAMCFGDIR"))
        home = _home_root()
        if home is not None:
            candidates.append((home / ".gam", "GAM default (~/.gam)"))
        try:
            candidates.append((self.managed_setup_dir(), "GamGUI setup dir"))
        except (OSError, RuntimeError):
            pass                    # no home / can't create it: simply nothing to offer

        seen: set = set()
        out: List[DirInspection] = []
        for path, label in candidates:
            try:
                resolved = self.resolve_dir(path)
            except ValueError:
                continue            # out of bounds, missing, or not a directory: not offerable
            key = _fs_id(resolved) or str(resolved)
            if key in seen:
                continue
            seen.add(key)
            insp = self.inspect(resolved)
            insp.label = label
            if insp.any_present:
                out.append(insp)
        return out

    def inspect(self, path) -> DirInspection:
        """Which credential files *path* holds — under the same bound as :meth:`import_dir`.

        Unbounded, this reports whether a file exists at an arbitrary path the operator (or anything
        that can reach the route) names, which is a presence oracle for locations the import itself
        refuses to touch. Outside the roots every name simply reads as absent — one uniform answer.
        """
        try:
            p = Path(path).expanduser()
        except (RuntimeError, ValueError):
            return DirInspection(path=str(path), files={name: False for name in FILENAMES})
        dir_fd = _pin_bounded_dir(p, self._allowed_root_ids())
        if dir_fd is None:
            return DirInspection(path=str(p), files={name: False for name in FILENAMES})
        try:
            files = {name: _file_present(dir_fd, fname) for name, fname in FILENAMES.items()}
        finally:
            _close(dir_fd)
        return DirInspection(path=str(p), files=files)

    # --- importing into the vault ------------------------------------------------------
    def allowed_roots(self) -> List[Path]:
        """The directories a chosen credentials folder may live in (the root itself, or below it).

        Picking the folder IS the feature, so an allow-list of exact paths would break it — but an
        unbounded path is more latitude than the feature needs. Two of the three locations
        :meth:`candidate_dirs` offers are always under the home dir (``~/.gam`` and
        :meth:`managed_setup_dir`, under ``~/Library/Application Support``); only ``$GAMCFGDIR`` can
        point elsewhere, and that is exactly the escape hatch for the one legitimate off-home case
        (a domain-impersonation key kept on an encrypted removable volume). So: home plus
        ``$GAMCFGDIR``. Every realistic setup still works; the wizard can no longer be aimed at
        ``/etc``, another user's home, or an arbitrary mount.

        The sanity rules (:func:`_root_is_sane`) apply to the ``$GAMCFGDIR`` root ONLY. A mistyped or
        hostile value is dropped quietly — it simply fails to widen the bound — while home is
        inherently allowed, whatever it happens to be. That asymmetry is deliberate: applying the
        rules to home too meant that a home which resolved to something over-broad (``/`` in a
        launchd/daemon context, where ``$HOME`` is often unset or absurd) emptied the root list and
        refused *every* path, including our own managed staging dir — the wizard could no longer
        import the credentials it had just told the operator to create. A home of ``/`` is a
        misconfiguration that already gives anything running as that user the whole disk; refusing to
        work at all does not fix it. So: never an empty list while a home directory is determinable.
        """
        roots: List[Path] = []
        home = _home_root()
        if home is not None:
            roots.append(home)
        raw = (os.environ.get("GAMCFGDIR") or "").strip()
        if not raw:
            return roots    # an unset/blank $GAMCFGDIR must never become "/" and allow the disk
        try:
            root = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return roots
        if root not in roots and _root_is_sane(root, home):
            roots.append(root)
        return roots

    def _allowed_root_ids(self) -> set:
        """:meth:`allowed_roots` as ``(st_dev, st_ino)`` pairs, for the identity-based bound."""
        ids = set()
        for root in self.allowed_roots():
            ident = _fs_id(root)
            if ident is not None:       # an unreadable root just stops being a root
                ids.add(ident)
        return ids

    def resolve_dir(self, path) -> Path:
        """Normalise a caller-supplied credentials folder, or raise ``ValueError`` for the UI.

        Two things are insisted on. First, the folder has to sit within :meth:`allowed_roots` —
        picking it is the feature, but only within bounds the feature actually needs. Second, the
        thing named has to exist and be a directory: a typo or a file otherwise imports nothing and
        reports it as "no credential files found", which sends the operator looking for the wrong
        problem. Only the fixed ``FILENAMES`` are ever read from what comes back.
        """
        raw = str(path).strip()
        if not raw:
            raise ValueError("Choose the folder that holds your GAM credential files.")
        try:
            p = Path(raw).expanduser().resolve()
        except OSError as exc:
            raise ValueError(f"That folder can't be read: {exc.strerror or raw}") from exc
        except RuntimeError as exc:
            # `~` in the path with no determinable home: operator-facing, not a traceback.
            raise ValueError(f"That folder can't be expanded: {raw}") from exc
        except ValueError as exc:   # e.g. a NUL byte in the path
            raise ValueError(f"That folder name isn't usable: {raw!r}") from exc
        # Order matters: the bounds check runs on the RESOLVED path, because resolving is what
        # collapses `..` and follows symlinks. Checking the typed string instead would wave through
        # `~/../../etc` and a `~/shortcut` symlinked to somewhere off-limits. It also runs before
        # exists()/is_dir(), so an out-of-bounds path gets one uniform answer and can't be used to
        # probe what does or doesn't exist outside the roots.
        if not _within_roots(p, self._allowed_root_ids()):
            # The advice has to be advice that works. Moving the folder under home always works.
            # $GAMCFGDIR is a real root, but only for a GamGUI that can SEE it: the .app has no
            # LSEnvironment in its Info.plist, so an app launched from Finder/Spotlight/Dock does
            # not inherit shell environment variables at all — only a launch from the same shell
            # that exported the variable does. Saying "point $GAMCFGDIR at it and relaunch GamGUI"
            # without that caveat sends the operator in circles.
            raise ValueError(
                f"That folder is outside the places GamGUI can import credentials from: {p} — "
                "move it under your home folder (that always works). Alternatively GamGUI also "
                "accepts $GAMCFGDIR, but only when it is launched from the same terminal session "
                "that exported it — opened from Finder, the app does not see shell variables."
            )
        if not p.exists():
            raise ValueError(f"No such folder: {p}")
        if not p.is_dir():
            raise ValueError(f"That's a file, not a folder — pick the folder it sits in: {p}")
        return p

    def import_dir(self, path, domain: str) -> List[str]:
        """Read whatever credential files exist in ``path`` into the Keychain. Returns imported names.

        Rejects a path that isn't an existing directory (see :meth:`resolve_dir`) instead of
        reporting an empty import.

        Security: once the credentials are in the Keychain they must not also linger as persistent
        plaintext files (a same-UID process could read them without a Keychain prompt). So after a
        successful import from OUR managed staging dir, the plaintext files are wiped — the Keychain
        is the only durable home. A user-chosen dir (e.g. ``~/.gam``, their own GAM install) is
        never touched.

        Security, part two: bounding the *directory* by NAME is not enough — a name-based check
        followed by a name-based open is a TOCTOU, and it is winnable: renaming the folder into a
        symlink to somewhere else, between the check and the read, put out-of-bounds bytes in the
        Keychain. So the directory is pinned to a descriptor and proven in bounds through that
        descriptor (:func:`_pin_bounded_dir`), and every credential file is then opened as a single
        component relative to it, ``O_NOFOLLOW``, and read from that same descriptor
        (:func:`_read_credential`). After the pin there is no name left for an attacker to swap.

        Security, part three: the wipe only ever touches the exact inodes whose contents just reached
        the Keychain — remembered from the read, re-verified on the descriptor, inside the same pinned
        directory. A file that was not imported, or that has been swapped since, is left alone (see
        :func:`_wipe_file`).

        Expected filesystem trouble (a vanished, unreadable or non-text credential file) is skipped
        quietly; the only exception this raises is an operator-facing ``ValueError`` about the folder.
        """
        p = self.resolve_dir(path)
        dir_fd = _pin_bounded_dir(p, self._allowed_root_ids())
        if dir_fd is None:
            # It passed resolve_dir a moment ago, so this is the folder moving, becoming unreadable,
            # or being swapped for something out of bounds mid-request. Nothing to import, and
            # nothing to say beyond what the operator can act on.
            raise ValueError(f"That folder can't be read right now — check it and try again: {p}")
        imported: List[str] = []
        staged: List[Tuple[str, Tuple[int, int]]] = []
        try:
            for name, fname in FILENAMES.items():
                got = _read_credential(dir_fd, fname)
                if got is None:
                    continue    # absent, a symlink, unreadable, or not text
                text, ident = got
                self.vault.set(domain, name, text)
                imported.append(name)
                staged.append((fname, ident))
            if imported and self._is_managed(p, dir_fd=dir_fd):
                for fname, ident in staged:
                    _wipe_file(dir_fd, fname, ident)
        finally:
            _close(dir_fd)
        return imported

    def _is_managed(self, p: Path, dir_fd: Optional[int] = None) -> bool:
        """True only for our own staging dir — never a user's ~/.gam or another chosen path.

        By ``(st_dev, st_ino)``, not by string: ``resolve()`` does not correct case (nor Unicode
        normalization), so a case-variant spelling of our own staging dir used to compare unequal
        and quietly skip the post-import wipe — leaving all three credential files as plaintext on
        disk, readable by any same-UID process without a Keychain prompt. Identity closes that.

        The identity comes from *dir_fd* when the caller has the directory pinned, so that the
        "is this ours to wipe?" decision is made about the very inode being wiped rather than about a
        name that could since have been re-pointed.

        If the staging dir can't even be determined (no home directory, or it can't be created) the
        answer is False: with no dir of ours to compare against, nothing is ours to destroy.
        """
        try:
            managed = _fs_id(self.managed_setup_dir())
        except (OSError, RuntimeError):
            return False
        if managed is None:
            return False
        if dir_fd is not None:
            try:
                st = os.fstat(dir_fd)
            except OSError:
                return False
            return (st.st_dev, st.st_ino) == managed
        return _fs_id(p) == managed

    def is_ready(self, domain: str) -> bool:
        return self.vault.has_credentials(domain)

    # --- Domain-Wide Delegation helper -------------------------------------------------
    def dwd_details(self, domain: str) -> Dict[str, str]:
        """Service-account client ID + the Admin Console link for the manual DWD step."""
        client_id = ""
        raw = self.vault.get(domain, "oauth2service")
        if raw:
            try:
                client_id = str(json.loads(raw).get("client_id", ""))
            except ValueError:
                client_id = ""
        return {"client_id": client_id, "admin_console_url": ADMIN_CONSOLE_DWD_URL}

    # --- fresh-setup guidance ----------------------------------------------------------
    def setup_commands(self, admin: str, cfgdir: Optional[Path] = None) -> Dict[str, object]:
        """The exact commands to run in Terminal for a fresh GAM authorization."""
        cfgdir = Path(cfgdir) if cfgdir else self.managed_setup_dir()
        gam = str(self.runner.gam_binary)
        return {
            "cfgdir": str(cfgdir),
            "env": f'export GAMCFGDIR="{cfgdir}"',
            # Canonical GAM7 order. `create project` takes the admin; `oauth create`
            # (browser sign-in) and `create svcacct` take no positional admin.
            "commands": [
                f'"{gam}" create project {admin}',
                f'"{gam}" oauth create',
                f'"{gam}" create svcacct',
            ],
        }

    # --- verification ------------------------------------------------------------------
    async def verify(self, domain: str, admin: str) -> VerifyResult:
        if not self.is_ready(domain):
            return VerifyResult(ok=False, summary="No credentials imported yet.")
        try:
            out = await self.runner.run_authenticated(domain, GAMCommands.check_svcacct(admin))
        except GAMError as exc:
            return VerifyResult(ok=False, summary=exc.message, raw=exc.stderr)
        lines = _parse_check(out)
        up = out.upper()
        failed = ("FAILED" in up) or ("DISABLED!" in up) or any(s == "FAIL" for _, s in lines)
        ok = bool(lines) and not failed
        return VerifyResult(
            ok=ok,
            summary=(
                "All scopes authorized."
                if ok
                else "Domain-Wide Delegation isn't authorized yet — use the link below, then verify again."
            ),
            lines=lines,
            raw=out,
            auth_url=("" if ok else _extract_auth_url(out)),
        )


_STATUS_RE = re.compile(r"\b(PASS|FAIL)\b")
_AUTH_URL_RE = re.compile(r"https://(?:gam-shortn\.appspot\.com|admin\.google\.com)/\S+")


def _parse_check(stdout: str) -> List[Tuple[str, str]]:
    """Pull (label, PASS/FAIL) pairs from `gam ... check serviceaccount` output, tolerantly.

    Handles both ``Label: PASS`` and GAM's scope-table form ``<scope-url>   FAIL (n/m)``.
    """
    results: List[Tuple[str, str]] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _STATUS_RE.search(line)
        if not m:
            continue
        label = line[: m.start()].strip().rstrip(":").strip()
        if label:
            results.append((label, m.group(1).upper()))
    return results


def _extract_auth_url(stdout: str) -> str:
    """The Admin Console / gam-shortn link GAM prints to authorize Domain-Wide Delegation."""
    m = _AUTH_URL_RE.search(stdout or "")
    return m.group(0).rstrip(".,") if m else ""
