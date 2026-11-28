"""A synthetic N-person tenant for the strict mock (scripts/scale_bench.py): offline, deterministic, never a real domain.

    .venv/bin/python scripts/scale_fixtures.py 5000 /tmp/scale-5000      # writes a fixtures dir for GAM_MOCK_FIXTURES

It copies tests/fixtures/ whole (the mock reads ~40 files and fails closed on a missing one) and rewrites the directory,
print_users.json, to N people. alice, bob and carol stay rows 1–3 so every per-person fixture still resolves; the rest are
userNNNNN@example.com, drawn from a seeded random so a run's bytes repeat. The mix is set in MIX and printed into
docs/scale.md: suspended, admins, no 2SV, last sign-in, the gaps Reports counts, aliases, and a small share of awkward
values (an NFD accent, a comma or quote in a title), because escaping at scale is where the mock has lied before.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

MIX = {
    "suspended": 0.08, "admin": 0.01, "no_2sv": 0.35,
    "login_recent": 0.70, "login_old": 0.20, "login_never": 0.05,    # the rest: between 30 and 90 days ago
    "no_title": 0.30, "no_phone": 0.40, "no_location": 0.60, "no_recovery": 0.25, "alias": 0.10,
    "nfd_name": 0.02, "odd_title": 0.01,
}
ORG_UNITS = ["/Staff", "/Sales", "/Sales/East", "/Sales/West", "/Engineering", "/Engineering/Platform", "/Support",
             "/Finance", "/HR", "/Legal", "/Marketing", "/Operations", "/IT", "/Contractors", "/Leavers", "/Interns",
             "/Exec", "/Research", "/Design", "/Facilities"]
FIRST = ["Ana", "Ben", "Chloé", "Dev", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun", "Kai", "Lea", "Mo", "Noor", "Otto", "Pia"]
LAST = ["Abara", "Brandt", "Costa", "Diaz", "Eriksen", "Fofana", "García", "Huang", "Ito", "Jensen", "Kowalski", "Li"]


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def person(i: int, rng: random.Random, now: datetime) -> dict:
    email = f"user{i:05d}@example.com"
    given, family = rng.choice(FIRST), rng.choice(LAST)
    if rng.random() < MIX["nfd_name"]:
        given = unicodedata.normalize("NFD", "Zoë")
    r = rng.random()
    if r < MIX["login_recent"]:
        login = _stamp(now - timedelta(days=rng.randint(0, 29)))
    elif r < MIX["login_recent"] + MIX["login_old"]:
        login = _stamp(now - timedelta(days=rng.randint(91, 900)))
    elif r < MIX["login_recent"] + MIX["login_old"] + MIX["login_never"]:
        login = "1970-01-01T00:00:00.000Z"   # Google's "never signed in"
    else:
        login = _stamp(now - timedelta(days=rng.randint(30, 90)))
    org = {"department": rng.choice(["Sales", "Engineering", "Support", "Finance", "Ops"]), "primary": True}
    if rng.random() >= MIX["no_title"]:
        org["title"] = 'Lead, "Platform" \\ Ops' if rng.random() < MIX["odd_title"] else rng.choice(["Engineer", "Manager", "Analyst", "Associate"])
    record = {
        "id": f"1000000000000{i:08d}", "primaryEmail": email,
        "name": {"givenName": given, "familyName": family},
        "suspended": rng.random() < MIX["suspended"], "orgUnitPath": rng.choice(ORG_UNITS),
        "isAdmin": rng.random() < MIX["admin"], "isEnrolledIn2Sv": rng.random() >= MIX["no_2sv"],
        "lastLoginTime": login, "organizations": [org],
        "creationTime": _stamp(now - timedelta(days=rng.randint(1, 2000))),
    }
    if rng.random() >= MIX["no_recovery"]:
        record["recoveryEmail"] = f"{given.lower()}.{i}@personal.example.org"
    if rng.random() >= MIX["no_phone"]:
        record["phones"] = [{"value": f"+1 555 {i % 10000:04d}", "type": "work"}]
    if rng.random() >= MIX["no_location"]:
        record["locations"] = [{"buildingName": rng.choice(["HQ", "East", "West"]), "type": "desk", "primary": True}]
    if rng.random() < MIX["alias"]:
        record["aliases"] = [f"u{i}@example.com"]
    return record


def generate(n: int, out: Path, seed: int = 7) -> Path:
    """A fixtures dir under ``out`` holding an n-person directory; returns it."""
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(FIXTURES, out)
    rng = random.Random(seed)  # noqa: S311 — seeded test data that must repeat, not a secret
    now = datetime.now(timezone.utc)
    keep = (FIXTURES / "print_users.json").read_text().splitlines()[:3]   # alice, bob, carol
    rows = keep + [json.dumps(person(i, rng, now), ensure_ascii=False) for i in range(4, n + 1)]
    (out / "print_users.json").write_text("\n".join(rows) + "\n")
    return out


if __name__ == "__main__":
    print(generate(int(sys.argv[1]), Path(sys.argv[2])))
