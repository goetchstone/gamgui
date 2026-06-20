#!/usr/bin/env python3
"""Read-only live acceptance pass — run after a GAM version bump.

Exercises the REAL ``gam`` against your configured tenant with read-only commands and confirms each
parser still yields sane typed objects. This is the only true output-shape check (CI can't do it
without a tenant). It performs NO mutations, so it's safe to run against production.

Prereqs: the vendored binary + credentials already in the Keychain (i.e. setup is done).

    .venv/bin/python scripts/acceptance.py
"""

from __future__ import annotations

import asyncio
import sys

from gamgui.core.reports import USAGE_PARAMS
from gamgui.web.server import AppState


async def main() -> int:
    st = AppState.create()
    conn = st.connector
    if conn is None:
        print("No domain configured — run setup first.")
        return 2

    print(f"Tenant : {conn.domain}")
    print(f"GAM    : {(await st.runner.version()).splitlines()[0]}\n")

    ok = True

    async def check(name, coro, describe) -> None:
        nonlocal ok
        try:
            result = await coro
            print(f"  PASS  {name:16} {describe(result)}")
        except Exception as exc:  # noqa: BLE001 - we want every check to report, not abort
            ok = False
            print(f"  FAIL  {name:16} {type(exc).__name__}: {exc}")

    users = await st.users()
    sample = users[0].primary_email if users else None
    print(f"Directory: {len(users)} users cached; sample = {sample}\n")

    await check("list_users", st.users(), lambda u: f"{len(u)} users, first={u[0].primary_email if u else '-'}")
    await check("list_groups", conn.list_groups(), lambda g: f"{len(g)} groups")
    if sample:
        await check("get_user", conn.get_user(sample), lambda u: f"{u.full_name} <{u.primary_email}> admin={u.is_admin}")
        await check("get_signature", conn.get_signature(sample), lambda s: f"{len(s)} chars")
        await check("list_delegates", conn.list_delegates(sample), lambda d: f"{len(d)} delegates")
        await check("get_vacation", conn.get_vacation(sample), lambda v: f"enabled={v.enabled}")
        await check("list_user_groups", conn.list_user_groups(sample), lambda g: f"{len(g)} memberships")
    await check("usage_report", conn.usage_report(USAGE_PARAMS), lambda r: f"{len(r['rows'])} rows ({r['date']})")

    print("\n" + ("All read-only acceptance checks passed." if ok else "Some checks FAILED — investigate before shipping."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
