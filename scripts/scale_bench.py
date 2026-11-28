"""What GamGUI does at 1,000 / 5,000 / 20,000 people, offline: the app against the strict mock and a synthetic tenant.

    .venv/bin/python scripts/scale_bench.py                       # sizes 1000,5000,20000
    .venv/bin/python scripts/scale_bench.py --sizes 2000 --write-docs

Each size runs in its own child process (a clean peak RSS) with scripts/preview_mock.py's app: an in-memory vault, the
strict mock (tests/fixtures/mock_gam.sh) over scripts/scale_fixtures.py's N people, a TestClient. Per screen and flow it
records the GAM calls (the mock's argv log), the app's own time, the response's bytes, and for a run the largest poll.

**Two kinds of number, never mixed.** "App" is measured here: time and memory in GamGUI itself, with the mock answering
in milliseconds. "Projected" is what a real domain would take, from the calls counted and the latency model in LATENCY
(GAM's start-up measured ~0.23–0.34 s per spawn; an authenticated call ~0.5–1.5 s; an `all users` read visits each
person in turn). The mock lies (CLAUDE.md): projections are a model, to be calibrated by one read-only live timing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Seconds. GAM's own start-up per process; one authenticated call to one person; each person an `all users` or
# `file -` read visits in turn inside one process; each 500-row page of a directory read.
LATENCY = {"startup": 0.35, "call": 1.0, "per_person": 0.5, "page": 0.8, "page_rows": 500}
DOCS = ROOT / "docs" / "scale.md"


def read_argv_log(path: Path) -> list:
    """The mock's argv log: NUL-separated, each call its argument count then its arguments."""
    if not path.exists():
        return []
    parts, calls, i = path.read_bytes().split(b"\0"), [], 0
    while i < len(parts) and parts[i]:
        n = int(parts[i])
        calls.append([p.decode() for p in parts[i + 1:i + 1 + n]])
        i += 1 + n
    return calls


def projected(argv: list, n: int, active: int, stdin_rows: int = 0) -> float:
    """One call's modelled wall time on a real domain of ``n`` people (``active`` not suspended)."""
    L = LATENCY
    if argv[:2] == ["print", "users"]:
        return L["startup"] + math.ceil(n / L["page_rows"]) * L["page"]
    if argv[:2] == ["all", "users"]:
        return L["startup"] + active * L["per_person"]
    if argv[:2] in (["file", "-"], ["csv", "-"]):
        return L["startup"] + stdin_rows * L["per_person"]
    return L["call"]


def human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


# --- the child: one size -----------------------------------------------------------------------------------------------

def child(n: int, workdir: Path) -> dict:
    import scale_fixtures
    from fastapi.testclient import TestClient

    fixtures = scale_fixtures.generate(n, workdir / "fixtures")
    state_dir = workdir / "state"
    state_dir.mkdir()
    argv_log = workdir / "argv.log"
    os.environ["HOME"] = str(state_dir)
    for var in ("GAMCFGDIR", "XDG_DATA_HOME", "GAMGUI_GAM_BINARY", "GAM_MOCK_STATE"):
        os.environ.pop(var, None)
    os.environ["GAM_MOCK_FIXTURES"] = str(fixtures)
    os.environ["GAM_MOCK_ARGV_LOG"] = str(argv_log)
    mock_state = workdir / "gam_state"
    mock_state.mkdir()
    os.environ["GAM_MOCK_STATE"] = str(mock_state)

    import preview_mock
    app = preview_mock.build_app(state_dir)
    base = f"http://127.0.0.1:{preview_mock.PORT}"
    rows: list = []

    with TestClient(app, base_url=base, headers={"Origin": base}) as c:
        c.get(f"/?token={preview_mock.TOKEN}")
        st = app.state.gamgui
        # GAM's (here the mock's) own time, so "app" is only GamGUI's: the mock writes each row with a shell fork or two.
        in_gam = [0.0]
        real_exec = st.runner._exec

        async def timed_exec(*args, **kw):
            t0 = time.perf_counter()
            try:
                return await real_exec(*args, **kw)
            finally:
                in_gam[0] += time.perf_counter() - t0
        st.runner._exec = timed_exec
        # The mock writes ~9 ms a row (a shell fork or two each), so its 20,000-row directory read outlasts the runner's
        # 2-minute default that real GAM (~30 s for 20,000) never meets. Here only: the app's own timeouts are what they are.
        st.runner.timeout = 3600

        def step(name: str, method: str, path: str, **kw) -> object:
            before, gam0 = len(read_argv_log(argv_log)), in_gam[0]
            t = time.perf_counter()
            r = c.request(method, path, **kw)
            ms = (time.perf_counter() - t - (in_gam[0] - gam0)) * 1000
            calls = read_argv_log(argv_log)[before:]
            rows.append({"screen": name, "status": r.status_code, "calls": len(calls), "app_ms": round(ms, 1),
                         "kb": round(len(r.content) / 1024, 1), "argv": [" ".join(a[:3]) for a in calls]})
            return r

        step("Users (cold: reads the directory)", "GET", "/users")
        step("Users table, page 1 of 50", "GET", "/users/table?page=1&size=50")
        step("Users table, last page", "GET", "/users/table?page=99999&size=50")
        step("Users search (one keystroke)", "GET", "/users/table?q=user01")
        step("A person's page", "GET", "/users/detail?email=user00042%40example.com")
        step("Reports", "GET", "/reports")
        step("Reports: no title, as CSV", "GET", "/reports/fill-in.csv?key=no_title")

        # Inactive → suspend: preview, then the run, every poll measured.
        r = step("Inactive 90 days: preview", "POST", "/reports/inactive/preview", data={"days": "90"})
        token = (re.search(r'"preview":\s*"([^"]+)"', r.text) or re.search(r'name="preview" value="([^"]+)"', r.text))
        suspend = {"targets": 0}
        if token:
            target_n = len(st.previews.peek("suspend_inactive", token.group(1)) or [])
            before, gam0 = len(read_argv_log(argv_log)), in_gam[0]
            t = time.perf_counter()
            r = c.post("/reports/inactive/apply", data={"preview": token.group(1), "days": "90", "confirmed": "1", "confirm": "confirm",
                                                         "confirm_count": str(target_n)})
            job_id = re.search(r"status\?job=([\w-]+)", r.text)
            if not job_id:
                suspend = {"targets": target_n, "refused": re.sub(r"<[^>]+>|\s+", " ", r.text).strip()[:300]}
            polls, max_poll = 0, 0
            while job_id:
                p = c.get(f"/reports/inactive/status?job={job_id.group(1)}")
                polls += 1
                max_poll = max(max_poll, len(p.content))
                job = st.jobs.get(job_id.group(1))
                if job is None or job.finished:
                    break
                time.sleep(0.2)
            calls = read_argv_log(argv_log)[before:]
            if job_id:
                suspend = {"targets": target_n, "calls": len(calls), "calls_per_target": round(len(calls) / max(target_n, 1), 2),
                       "app_s": round(time.perf_counter() - t - (in_gam[0] - gam0), 1), "polls": polls, "max_poll_kb": round(max_poll / 1024, 1)}

    active = sum(1 for line in (fixtures / "print_users.json").read_text().splitlines() if '"suspended": false' in line)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    return {"n": n, "active": active, "rows": rows, "suspend": suspend, "peak_rss_mb": round(peak_mb)}


# --- domain-wide reads: projected from the calls each one makes ---------------------------------------------------------

def domain_wide(n: int, active: int) -> list:
    """The reads and runs whose cost grows with the domain, each from the argv it sends (the same builders the app uses)."""
    from gamgui.core.gam.commands import GAMCommands as C

    one = lambda argv, rows=0: projected(argv, n, active, rows)   # noqa: E731
    return [
        ("Directory read (cached 5 min)", one(C.print_users())),
        ("Mail review: forwarding, then delegates", one(C.all_users_print_forward()) + one(C.all_users_print_delegates())),
        ("Connected apps (tokens: GAM visits every active person)", LATENCY["startup"] + active * LATENCY["per_person"]),
        ("Calendar index", one(C.print_all_calendars())),
        ("Signatures, one at a time (backup read + write each)", active * 2 * LATENCY["call"]),
        ("Signatures, one GAM call (5.4): backup read", one(C.print_signatures_from_stdin(), active)),
        ("Suspend everyone inactive (cut-off read + write each)", round(active * 0.2) * 2 * LATENCY["call"]),
    ]


def table(results: list) -> str:
    out = ["| People | Screen or flow | GAM calls | App time | Response |", "|---:|---|---:|---:|---:|"]
    for r in results:
        for row in r["rows"]:
            out.append(f"| {r['n']:,} | {row['screen']} | {row['calls']} | {row['app_ms']:.0f} ms | {row['kb']:,.1f} KB |")
        s = r["suspend"]
        if s.get("refused"):
            out.append(f"| {r['n']:,} | Suspend {s['targets']:,} inactive: refused | | | {s['refused'][:120]} |")
        elif s.get("targets"):
            out.append(f"| {r['n']:,} | Suspend {s['targets']:,} inactive: the run | {s['calls']:,} ({s['calls_per_target']} a person) "
                       f"| {s['app_s']} s | largest poll {s['max_poll_kb']} KB |")
        out.append(f"| {r['n']:,} | Peak memory | | {r['peak_rss_mb']} MB | |")
    out += ["", "| People | Domain-wide read or run | Projected on a real domain |", "|---:|---|---:|"]
    for r in results:
        for name, seconds in domain_wide(r["n"], r["active"]):
            out.append(f"| {r['n']:,} | {name} | {human(seconds)} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,5000,20000")
    ap.add_argument("--child", type=int)
    ap.add_argument("--dir")
    ap.add_argument("--write-docs", action="store_true")
    a = ap.parse_args()
    if a.child:
        print("RESULT " + json.dumps(child(a.child, Path(a.dir))), flush=True)
        return
    results = []
    for n in (int(s) for s in a.sizes.split(",")):
        with tempfile.TemporaryDirectory(prefix=f"gamgui-scale-{n}-") as tmp:
            t = time.perf_counter()
            out = subprocess.run([sys.executable, __file__, "--child", str(n), "--dir", tmp], capture_output=True, text=True)
            line = next((ln for ln in out.stdout.splitlines() if ln.startswith("RESULT ")), None)
            if line is None:
                sys.exit(f"size {n} failed:\n{out.stderr[-3000:]}")
            results.append(json.loads(line[7:]))
            print(f"{n:,} people: {time.perf_counter() - t:.0f} s", file=sys.stderr)
    text = table(results)
    print(text)
    if a.write_docs:
        head = (f"<!-- scale:begin -->\nMeasured {time.strftime('%Y-%m-%d')} with `scripts/scale_bench.py` "
                f"(sizes {a.sizes}; latency model {LATENCY}).\n\n")
        doc = DOCS.read_text()
        doc = re.sub(r"<!-- scale:begin -->.*<!-- scale:end -->", lambda _: head + text + "\n<!-- scale:end -->", doc, flags=re.S)
        DOCS.write_text(doc)


if __name__ == "__main__":
    main()
