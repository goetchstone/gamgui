# GamGUI at scale

GamGUI is a single-admin app, so "scale" means the size of the domain it manages: thousands to tens of thousands of
people. This page says what the app does at that size, measured, and what a real domain would take, projected.

## How to read the numbers

- **App** columns are measured by `scripts/scale_bench.py`. The real app runs in-process against the strict offline
  mock and a synthetic tenant (`scripts/scale_fixtures.py`: seeded, with suspended accounts, admins, people without 2-Step
  Verification, stale sign-ins, missing titles and phones, aliases, and a few awkward names). The mock's own time is taken
  out, so they show GamGUI's cost alone.
- **Projected** columns are a model: the GAM calls the app makes, counted from the mock's log, multiplied by the latency
  in `LATENCY` (GAM's start-up ~0.35 s, one call ~1 s, ~0.5 s a person inside an `all users` read, ~0.8 s a 500-row
  page). The mock lies (CLAUDE.md). One read-only live timing on a real domain calibrates the model, and this page will
  say when that has happened.
- Not measured offline: Keychain access per call (in-memory here), Google's quotas and back-off, the app window's own
  rendering time.

## What holds at any size

- **One directory read serves every screen.** Users, search, a person's page, Reports and its CSVs all read one cached
  `print users`, five minutes old at most, refreshed behind the screen
  (`tests/test_scale_invariants.py::test_one_directory_read_serves_every_screen`).
- **A table page costs the same at any size.** The Users table pages and filters on the server
  (`test_a_table_page_costs_the_same_at_any_size`).
- **A running job's poll is bounded.** Every bulk run shows a 12-row feed and keeps at most 200 failures, so its 1-second
  poll doesn't grow with the run (invariant 9, `test_a_bulk_run_s_poll_stays_bounded`).

## Measured and projected

<!-- scale:begin -->
Measured 2026-11-28 on an Apple-silicon Mac with `scripts/scale_bench.py --sizes 1000,5000,20000` (base f0a085f plus this change; latency model: start-up 0.35 s, a call 1.0 s, 0.5 s a person in an `all users` read, 0.8 s a 500-row page).

| People | Screen or flow | GAM calls | App time | Response |
|---:|---|---:|---:|---:|
| 1,000 | Users (cold: reads the directory) | 1 | 37 ms | 24.4 KB |
| 1,000 | Users table, page 1 of 50 | 0 | 2 ms | 39.0 KB |
| 1,000 | Users table, last page | 0 | 1 ms | 39.6 KB |
| 1,000 | Users search (one keystroke) | 0 | 1 ms | 3.4 KB |
| 1,000 | A person's page | 0 | 10 ms | 25.7 KB |
| 1,000 | Reports | 0 | 36 ms | 141.1 KB |
| 1,000 | Reports: no title, as CSV | 0 | 2 ms | 16.0 KB |
| 1,000 | Inactive 90 days: preview | 2 | 20 ms | 56.8 KB |
| 1,000 | Suspend 221 inactive: the run | 442 (2.0 a person) | 0.8 s | largest poll 1.1 KB |
| 1,000 | Peak memory | | 108 MB | |
| 5,000 | Users (cold: reads the directory) | 1 | 70 ms | 24.4 KB |
| 5,000 | Users table, page 1 of 50 | 0 | 5 ms | 39.0 KB |
| 5,000 | Users table, last page | 0 | 4 ms | 39.7 KB |
| 5,000 | Users search (one keystroke) | 0 | 3 ms | 13.8 KB |
| 5,000 | A person's page | 0 | 9 ms | 25.7 KB |
| 5,000 | Reports | 0 | 34 ms | 152.2 KB |
| 5,000 | Reports: no title, as CSV | 0 | 4 ms | 85.2 KB |
| 5,000 | Inactive 90 days: preview | 2 | 86 ms | 56.8 KB |
| 5,000 | Suspend 1,160 inactive: the run | 2,320 (2.0 a person) | 3.4 s | largest poll 1.1 KB |
| 5,000 | Peak memory | | 143 MB | |
| 20,000 | Users (cold: reads the directory) | 1 | 223 ms | 24.4 KB |
| 20,000 | Users table, page 1 of 50 | 0 | 16 ms | 39.0 KB |
| 20,000 | Users table, last page | 0 | 15 ms | 39.6 KB |
| 20,000 | Users search (one keystroke) | 0 | 10 ms | 13.8 KB |
| 20,000 | A person's page | 0 | 10 ms | 25.7 KB |
| 20,000 | Reports | 0 | 37 ms | 153.3 KB |
| 20,000 | Reports: no title, as CSV | 0 | 14 ms | 343.2 KB |
| 20,000 | Inactive 90 days: preview | 2 | 241 ms | 56.8 KB |
| 20,000 | Suspend 4,711 inactive: the run | 9,422 (2.0 a person) | 15.6 s | largest poll 1.1 KB |
| 20,000 | Peak memory | | 367 MB | |

| People | Domain-wide read or run | Projected on a real domain |
|---:|---|---:|
| 1,000 | Directory read (cached 5 min) | 2 s |
| 1,000 | Mail review: forwarding, then delegates | 15 min |
| 1,000 | Connected apps (tokens: GAM visits every active person) | 8 min |
| 1,000 | Calendar index | 8 min |
| 1,000 | Signatures, one at a time (backup read + write each) | 31 min |
| 1,000 | Signatures, one GAM call (5.4): backup read | 8 min |
| 1,000 | Suspend everyone inactive (cut-off read + write each) | 6 min |
| 5,000 | Directory read (cached 5 min) | 8 s |
| 5,000 | Mail review: forwarding, then delegates | 77 min |
| 5,000 | Connected apps (tokens: GAM visits every active person) | 38 min |
| 5,000 | Calendar index | 38 min |
| 5,000 | Signatures, one at a time (backup read + write each) | 2.5 h |
| 5,000 | Signatures, one GAM call (5.4): backup read | 38 min |
| 5,000 | Suspend everyone inactive (cut-off read + write each) | 31 min |
| 20,000 | Directory read (cached 5 min) | 32 s |
| 20,000 | Mail review: forwarding, then delegates | 5.1 h |
| 20,000 | Connected apps (tokens: GAM visits every active person) | 2.6 h |
| 20,000 | Calendar index | 2.6 h |
| 20,000 | Signatures, one at a time (backup read + write each) | 10.3 h |
| 20,000 | Signatures, one GAM call (5.4): backup read | 2.6 h |
| 20,000 | Suspend everyone inactive (cut-off read + write each) | 2.1 h |

**Before this change**, on the same machine: Reports was 657 KB at 1,000 people, 3.1 MB at 5,000 and 12.5 MB at 20,000 (every member of every card on the page). The 4,711-account suspend spent about 320 s in the app (each account landing rebuilt the whole 20,000-row cached list). Everything else was as above.
<!-- scale:end -->

## Where the time goes, and what is being done

The single-person screens are flat: a few GAM calls whatever the size. The cost grows in three places:

1. **Domain-wide reads that visit each person in turn inside one GAM process:** the mail review, connected apps, the
   calendar index and Phishing's search. Each grows by about half a second a person, and each has a one-hour limit, after
   which nothing it read is kept. That limit is reached at roughly 5,000–7,000 people. The fix is GAM's own parallel loop
   (`csv … multiprocess`, several workers), with each person's result accounted for separately.
2. **Per-person loops:** a signature run reads and writes each person, and a bulk suspend reads each account before writing
   it (the check that the app never cuts off its own admin). The opt-in signature fast path (roadmap 5.4) already makes
   the write a single GAM call. The read before each suspend can come from the cached directory instead.
3. **App-side costs found by the bench: fixed.** Reports listed every member of every card (12.5 MB at 20,000 people) and now
   lists 50 a card with the rest as a CSV. A bulk run rebuilt the cached directory once an account and now does it once a
   second. A licence card read the whole domain's licences on every click and now reads them once every 5 minutes, while a
   change's preview still reads them fresh. Three picker pages read every group on each load and now share the cached
   list. The directory read had a 2-minute limit, reached at about 75,000 people, and now has 15 minutes.
