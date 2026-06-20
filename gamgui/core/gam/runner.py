"""The only place that spawns the ``gam`` binary.

Everything else goes through :class:`GAMRunner`, which handles binary location, the ephemeral
``GAMCFGDIR`` materialization, environment, timeouts, and error mapping. Output parsing lives in
``parser.py``; callers receive raw stdout and parse it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from ..secrets.ephemeral import EphemeralConfig
from ..secrets.vault import SecretsVault
from .errors import GAMError, GAMErrorKind

# Env var that overrides binary discovery (used by tests with a mock gam, and power users).
GAM_BINARY_ENV = "GAMGUI_GAM_BINARY"

DEFAULT_TIMEOUT = 120.0


def strip_cfgdir_noise(stdout: str, cfgdir: Path) -> str:
    """Drop GAM's config-init banner from stdout.

    Because we hand GAM a brand-new ``GAMCFGDIR`` per call, it prints lines like
    ``Created: <dir>/gamcache`` and ``Config File: <dir>/gam.cfg, Initialized`` on stdout every time.
    Those reference our ephemeral dir and never appear in real data, so any line mentioning the dir
    is safe to remove — otherwise they leak into text parsers (e.g. the vacation message).
    """
    needle = str(cfgdir)
    if not needle or needle not in stdout:
        return stdout
    return "\n".join(line for line in stdout.splitlines() if needle not in line)


@dataclass
class RunResult:
    stdout: str
    stderr: str
    returncode: int


def locate_gam_binary() -> Path:
    """Resolve the bundled ``gam`` executable.

    Order: explicit env override → PyInstaller bundle (``sys._MEIPASS``) → repo source tree.
    """
    override = os.environ.get(GAM_BINARY_ENV)
    if override:
        return Path(override)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # frozen .app
        return Path(meipass) / "resources" / "gam7" / "gam"

    # Source tree: gamgui/core/gam/runner.py -> gamgui/resources/gam7/gam
    return Path(__file__).resolve().parents[2] / "resources" / "gam7" / "gam"


class GAMRunner:
    def __init__(
        self,
        vault: SecretsVault,
        gam_binary: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.vault = vault
        self.gam_binary = Path(gam_binary) if gam_binary else locate_gam_binary()
        self.base_dir = base_dir
        self.timeout = timeout
        # Serializes mutating calls so two writes can't race the same ephemeral GAMCFGDIR.
        self._write_lock = asyncio.Lock()

    def binary_exists(self) -> bool:
        return self.gam_binary.exists()

    def _require_binary(self) -> None:
        if not self.binary_exists():
            raise RuntimeError(
                f"GAM binary not found at {self.gam_binary}. Run scripts/fetch_gam.sh to vendor it."
            )

    def _build_env(self, cfgdir: Path) -> dict:
        env = os.environ.copy()
        env["GAMCFGDIR"] = str(cfgdir)
        # Keep GAM quiet/non-interactive where possible.
        env.setdefault("GAM_NO_UPDATE_CHECK", "1")
        return env

    async def _exec(self, argv: Sequence[str], cfgdir: Path, timeout: float) -> RunResult:
        self._require_binary()
        proc = await asyncio.create_subprocess_exec(
            str(self.gam_binary),
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_env(cfgdir),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise GAMError(GAMErrorKind.TIMEOUT, exit_code=None, stderr="command timed out", argv=list(argv))
        return RunResult(
            stdout=(out or b"").decode("utf-8", "replace"),
            stderr=(err or b"").decode("utf-8", "replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
        )

    async def run_authenticated(
        self,
        domain: str,
        argv: Sequence[str],
        timeout: Optional[float] = None,
        serialize: bool = False,
    ) -> str:
        """Run a gam command as ``domain`` (credentials materialized from the vault).

        Returns stdout on success; raises :class:`GAMError` on failure.
        Set ``serialize=True`` for mutating commands.
        """
        argv = list(argv)
        timeout = timeout or self.timeout

        async def _do() -> str:
            with EphemeralConfig(self.vault, domain, base_dir=self.base_dir) as cfgdir:
                res = await self._exec(argv, cfgdir, timeout)
            if res.returncode != 0:
                raise GAMError.from_run(res.returncode, res.stderr, argv)
            return strip_cfgdir_noise(res.stdout, cfgdir)

        if serialize:
            async with self._write_lock:
                return await _do()
        return await _do()

    async def run_in_cfgdir(
        self,
        cfgdir: Path,
        argv: Sequence[str],
        timeout: Optional[float] = None,
    ) -> RunResult:
        """Run a gam command against an explicit, persistent ``GAMCFGDIR``.

        Used by the setup wizard, where credentials don't exist in the vault yet and the files GAM
        creates must persist long enough to be harvested. Returns the raw :class:`RunResult` (the
        wizard inspects exit code + output itself).
        """
        return await self._exec(list(argv), Path(cfgdir), timeout or self.timeout)

    async def version(self) -> str:
        """Return GAM's reported version (no credentials needed)."""
        from ..secrets.ephemeral import app_runtime_dir
        from .commands import GAMCommands

        cfgdir = self.base_dir or app_runtime_dir()
        res = await self._exec(GAMCommands.version(), Path(cfgdir), self.timeout)
        return res.stdout.strip()
