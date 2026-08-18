# `history.db` — all-time session history

A read-only SQLite database of **every session ever opened** against every
registered provider on Base mainnet, built by
`research/foundation/history_build.py`.

It is additive. It does not replace or modify `census.json`, which stays exactly
as it was: `census.json` is the rolling recent-window view, `history.db` is the
all-time archive behind it.

> **Note for whoever owns `index.html`:** this document is the whole contract.
> Nothing here is wired into the dashboard — that is your call to make. The
> `meta` table is deliberately explicit about coverage so the page can state
> what it actually has rather than implying completeness.

---

## Why a database and not a JSON aggregate

Written out verbosely the raw sessions are ~124 MB, but that is an encoding
artefact rather than information: the whole dataset is a few dozen providers, a
few hundred models and a few dozen buyers, repeated hundreds of thousands of
times. Interning those strings to integers collapses it by roughly 8x.

The reason it ships as SQLite rather than a columnar JSON blob is that GitHub
Pages honours HTTP range requests. [`sql.js-httpvfs`](https://github.com/phiresky/sql.js-httpvfs)
uses that to run real SQL against the file **without downloading it** — a typical
indexed query pulls tens of KB of pages, not the whole database. That keeps
arbitrary drill-down (by provider, model, buyer, date range) available to the
browser instead of freezing one set of pre-baked rollups at build time.

The file is built with `page_size = 1024` so range requests fetch at fine
granularity, and `VACUUM`ed at the end so pages are laid out contiguously.

---

## Schema

```sql
-- dimension tables: every repeated string interned exactly once
CREATE TABLE provider (id INTEGER PRIMARY KEY, addr TEXT);
CREATE TABLE model    (id INTEGER PRIMARY KEY, mid  TEXT, name TEXT);
CREATE TABLE buyer    (id INTEGER PRIMARY KEY, addr TEXT);

-- one row per *finished* session
CREATE TABLE session (
  p    INTEGER,   -- provider.id
  m    INTEGER,   -- model.id
  u    INTEGER,   -- buyer.id
  t    INTEGER,   -- openedAt, unix seconds (UTC)
  dur  INTEGER,   -- session duration in seconds
  mor  INTEGER    -- MOR earned, as MOR * 1e9
);
CREATE INDEX i_t   ON session(t);
CREATE INDEX i_mt  ON session(m, t);
CREATE INDEX i_pmt ON session(p, m, t);

CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
```

### `mor` is an integer, not a float

Earnings are stored as **MOR × 1e9**. Divide by `1e9` for display. Storing MOR
as a float would drift over a sum of hundreds of thousands of rows; storing it
as wei would overflow SQLite's signed 64-bit integers on large sums. `1e9` sits
comfortably between the two and keeps every total exact.

```sql
SELECT SUM(mor)/1e9 FROM session;   -- total MOR earned, all time
```

### What a row means

Per-session MOR is `pricePerSecond × dur`, where `pricePerSecond` comes from the
session's bid (`getBid`).

**Sessions still running are not in the table at all.** They have not earned
anything yet, and including them as zero-value rows would drag every average
down. `meta.sessionsOpenSkipped` counts them.

### `dur` is capped at the session term — this matters a lot

```
dur = min(closedAt, endsAt) - openedAt
```

The obvious definition, `closedAt - openedAt`, is wrong, and not by a little.
`closedAt` records when somebody got round to calling close on chain, which is
not when the session stopped being paid for. **53% of all sessions are closed
after `endsAt`** — usually by about 23 seconds, which is ordinary settlement
lag, but with a long tail. The worst example found runs 8.8 days of wall clock
against a 599-second term.

Because MOR is `price × duration`, those few stale sessions dominate any sum.
Measured across the sessions where the provider's own withdrawal is recorded on
chain:

| duration definition | Σ MOR | vs Σ withdrawn | median ratio | p99 |
|---|---|---|---|---|
| `closedAt - openedAt` | 74,316 | **4.799x** | 1.0050 | 2.39 |
| `min(closedAt, endsAt) - openedAt` | 15,511 | **1.002x** | 1.0000 | 1.00 |
| `endsAt - openedAt` | 16,019 | 1.035x | 1.0000 | 88.5 |

`providerWithdrawnAmount` is an independent quantity — it comes off the session
record, not off the bid — so agreeing with it to within 0.2% in aggregate is a
real check on the formula rather than a restatement of it. The uncapped version
would have overstated all-time market earnings by nearly five times.

`meta.wallClockOverstatement` records the ratio for the built dataset, so the
discrepancy stays visible instead of being quietly corrected away.

Two consequences worth knowing:

* `dur` is **billable** duration, not wall clock. For 99% of sessions the two
  differ by seconds. Where they differ a lot, the session was abandoned rather
  than served, and `dur` is the number that pairs correctly with `mor`.
* `mor` is **gross earned**, not received. Re-reading `providerWithdrawnAmount`
  for a random sample spanning the whole history: where a withdrawal exists, the
  computed figure matches the chain to **0.02%**, median ratio exactly 1.00000.
  But **~44% of sessions were never withdrawn from at all**, accounting for
  roughly a third of all MOR earned. So `mor` is what the price and the clock
  entitled the provider to — not a claimable balance, and not what landed in
  their wallet. A dashboard that labels it "earned" is right; one that labels it
  "received" is not.

`research/foundation/history_validate.py` re-runs that check against live chain
state and exits non-zero if it regresses.

---

## `meta` keys

| key | meaning |
|---|---|
| `asOf` | UTC timestamp of the build |
| `firstDay`, `lastDay` | date range actually present, `YYYY-MM-DD` |
| `sessionsTotal` | lifetime sessions on chain, summed from `getProviderSessions` array lengths — **the checksum target** |
| `sessionsDetailed` | how many of those were successfully read |
| `coverage` | `sessionsDetailed / sessionsTotal`, as a decimal string |
| `sessionsPriced` | rows actually in `session` (detailed, finished, and priceable) |
| `sessionsOpenSkipped` | finished-session filter: still running, so excluded |
| `sessionsUnresolvedBid` | sessions dropped because their bid could not be read |
| `unresolvedBids` | how many distinct bids that was |
| `incompleteProviders` | JSON array of `{addr, chain, detailed}` for any provider whose detail count does not match its on-chain array length |
| `providers`, `models`, `buyers` | dimension cardinalities. Note `providers` counts providers **with at least one session** (29); the `provider` table itself holds all 36 registered ones, so a join never dangles |
| `durationBasis`, `morBasis` | how `dur` and `mor` are defined, so the definition travels with the data |
| `wallClockOverstatement` | what billing raw `closedAt - openedAt` would have inflated MOR by (3.56x) |
| `morUnit` | `MOR * 1e9`, so the unit travels with the data |
| `schema` | one-line description of the `session` table |

**Read `coverage` and show it.** If it is not `1.000000`, the dashboard should
say so rather than presenting the totals as complete. `incompleteProviders`
names exactly which providers are short and by how much.

---

## `history-daily.json`

A tiny companion file for first paint, so the page can draw something before the
database loads:

```jsonc
{
  "days": [["2026-08-17", 941, 187.4, 13], ...],  // day, sessions, MOR, active providers
  "asOf": "2026-08-18T...Z",
  "coverage": 0.9998,
  "sessionsTotal": 318870,
  "sessionsDetailed": 318812
}
```

`MOR` here is a plain float — it is a display-only rollup, not the source of
truth. Anything that needs exact numbers should query the database.

---

## What is actually in this build

```
coverage           1.000000     318,870 of 318,870 sessions on chain
date range         2025-12-17 .. 2026-08-18   (245 days)
priced rows        318,866      (4 sessions still running, excluded)
dimensions         29 providers with sessions, 220 models, 65 buyers
total MOR earned   199,886.4
file size          23.2 MB, page_size 1024, VACUUMed
```

Every provider's detail count matches its `getProviderSessions` array length
exactly, so `incompleteProviders` is empty and `coverage` is a real 1.0 rather
than an assumed one.

---

## Querying it from the browser

```js
import { createDbWorker } from "sql.js-httpvfs";

const worker = await createDbWorker(
  [{
    from: "inline",
    config: {
      serverMode: "full",          // one file, plain range requests
      url: "/morcompute-watchdog/history.db",
      requestChunkSize: 1024,      // must match the db's page_size
    },
  }],
  workerUrl.toString(), wasmUrl.toString(),
);

// MOR per day for one model
const rows = await worker.db.query(`
  SELECT date(s.t,'unixepoch') AS day, COUNT(*) AS n, SUM(s.mor)/1e9 AS mor
  FROM session s JOIN model m ON m.id = s.m
  WHERE m.name = ? AND s.t >= ?
  GROUP BY day ORDER BY day
`, ["deepseek-v4-flash", cutoffUnix]);
```

`requestChunkSize` **must** equal the database's `page_size` (1024). Getting it
wrong does not error — it just quietly fetches far more data than it needs.

### Queries the indexes are built for

```sql
-- market-wide daily volume                                   (uses i_t)
SELECT date(t,'unixepoch') d, COUNT(*), SUM(mor)/1e9
FROM session GROUP BY d ORDER BY d;

-- one model's history across every provider                  (uses i_mt)
SELECT date(t,'unixepoch') d, COUNT(*), SUM(mor)/1e9
FROM session WHERE m = ? GROUP BY d ORDER BY d;

-- one provider on one model over time — the handover view    (uses i_pmt)
SELECT date(t,'unixepoch') d, COUNT(*), SUM(mor)/1e9
FROM session WHERE p = ? AND m = ? GROUP BY d ORDER BY d;

-- lifetime leaderboard
SELECT pr.addr, COUNT(*) n, SUM(s.mor)/1e9 mor
FROM session s JOIN provider pr ON pr.id = s.p
GROUP BY pr.addr ORDER BY mor DESC;
```

### Two query shapes that do *not* use an index

Verified with `EXPLAIN QUERY PLAN` against the built file:

| query | plan |
|---|---|
| one model over time | `SEARCH … USING INDEX i_mt` |
| provider on one model | `SEARCH … USING COVERING INDEX i_pmt` |
| date-range slice | `SEARCH … USING INDEX i_t` |
| provider lifetime | `SEARCH … USING INDEX i_pmt (p=?)` |
| **market-wide daily totals** | `SCAN session` |
| **buyer drill-down** (`WHERE u = ?`) | `SCAN session` |

The market-wide scan does not matter — that is precisely the view
`history-daily.json` exists to serve, in 7 KB, with no database fetch at all.

**Buyer drill-down does matter,** and it is the one gap in this build. The
schema shipped here is the one that was specified, with exactly three indexes,
so `WHERE u = ?` walks the whole `session` table — 8.3 MB of pages over HTTP,
on every such query. That is a deliberate omission left visible rather than
silently patched, because whoever owns the dashboard should decide it.

If you want it, it is a one-line change and a rebuild — the `db` stage is
offline and takes a few seconds:

```sql
CREATE INDEX i_ut ON session(u, t);
```

Cost, measured from `dbstat` on the existing indexes: about **4–5 MB**, taking
the file from 23.2 MB to roughly 28 MB. Still far under GitHub's 50 MB warning.

```
session   8068 pages  8.26 MB      i_mt   4907 pages  5.02 MB
i_pmt     5531 pages  5.66 MB      i_t    4080 pages  4.18 MB
```
