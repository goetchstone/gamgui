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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gam.commands import GAMCommands
from .gam.errors import GAMError
from .gam.runner import GAMRunner
from .secrets.ephemeral import app_runtime_dir
from .secrets.vault import FILENAMES, SecretsVault

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

    # --- discovering / inspecting credential directories -------------------------------
    def managed_setup_dir(self) -> Path:
        """A private dir the 'fresh setup' commands write into, which we then import from."""
        d = app_runtime_dir().parent / "setup"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        return d

    def candidate_dirs(self) -> List[DirInspection]:
        """Likely GAMCFGDIR locations that already hold credentials."""
        seen: set = set()
        out: List[DirInspection] = []
        candidates: List[Tuple[Path, str]] = []

        env = os.environ.get("GAMCFGDIR")
        if env:
            candidates.append((Path(env), "$GAMCFGDIR"))
        candidates.append((Path.home() / ".gam", "GAM default (~/.gam)"))
        candidates.append((self.managed_setup_dir(), "GamGUI setup dir"))

        for path, label in candidates:
            rp = str(path.expanduser().resolve())
            if rp in seen:
                continue
            seen.add(rp)
            insp = self.inspect(path)
            insp.label = label
            if insp.any_present:
                out.append(insp)
        return out

    def inspect(self, path) -> DirInspection:
        p = Path(path).expanduser()
        files = {name: (p / fname).is_file() for name, fname in FILENAMES.items()}
        return DirInspection(path=str(p), files=files)

    # --- importing into the vault ------------------------------------------------------
    def import_dir(self, path, domain: str) -> List[str]:
        """Read whatever credential files exist in ``path`` into the Keychain. Returns imported names."""
        p = Path(path).expanduser()
        imported: List[str] = []
        for name, fname in FILENAMES.items():
            f = p / fname
            if f.is_file():
                self.vault.set(domain, name, f.read_text(encoding="utf-8"))
                imported.append(name)
        return imported

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
            except (json.JSONDecodeError, ValueError):
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
