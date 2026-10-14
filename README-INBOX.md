# Inbox drop: SQL moves to deadlock-api's data lake (2026-10-14)

    lake.py               NEW      - DuckDB on deadlock-api's public data lake; installs duckdb on first use
    deadlock_pipeline.py  MODIFIED - every query ported to DuckDB on the lake; writes pool_players.csv
    ceiling_rank.py       MODIFIED - orbit query ported the same way; ceiling.csv gains `winrate`

Before editing, I checked both scripts against live main (cc33b4d): their blob
hashes matched the versions these edits started from.

## What broke

- **deadlock-api moved `/v1/sql` off ClickHouse onto DuckDB over its public
  parquet lake** on 2026-10-14 (their commit 9773572, 11:03 UTC).
  - Queries now time out after 45 s with a 504.
  - Results are capped at 100,000 rows.
  - Queries must be DuckDB SQL.
  - The endpoint is marked for removal.
- **The 21:08 UTC run died on its first query, orbit 1.** The outer half had no
  `start_time` bound. On the lake only `match_id` and `start_time` filters skip
  files, so it read all of `match_player`.
- **The rest would have failed too.** The item query uses ClickHouse's
  `ARRAY JOIN` and reads `items.net_worth_at_buy`, a MATERIALIZED column the lake
  doesn't publish. Ring 2 uses `uniqExact`. Both fail on DuckDB 1.5.6, the
  version the API runs.
- The last good refresh was at 09:19 UTC. Every run since has failed at its first
  SQL call, and the site still shows the 09:19 data.

## What changes

1. **SQL runs on the lake straight from the runner** (`lake.py`): one DuckDB
   connection, attached read-only to the catalog the lake's manifest names.
   - It is the same data `/v1/sql` now reads, in the same hourly snapshots.
   - There is no rate limit, no 40 s pause between queries, no URL chunking and
     no 45 s timeout.
   - `pip` installs `duckdb` on first use, at the version the catalog was written
     with, so the workflow needs no change.
2. **Queries ported:**
   - **Orbit 1** (both scripts): the outer scan is bounded on `start_time` too.
     The rows are the same.
   - **Ring 2:** `count(DISTINCT …)` replaces `uniqExact`, and every scan is
     bounded on `start_time`.
   - **Pool net wins:** one query instead of 1–3. It keeps one row per
     player-match, because the lake re-exports some rows.
   - **Items:** one query instead of ~5. It returns one row per player-match
     with the purchase arrays, and Python expands them.
     - It keeps the newest copy of a duplicated row; a duplicate would double
       that build's purchases and ability upgrades.
     - Net worth at buy is rebuilt from the stats timeline with deadlock-api's
       own expression (`item_flow_stats.rs`).
3. **New step: games the lake doesn't have yet.** The lake trails the live data
   by 10–70 minutes, so a player's newest game can be missing. That build steps
   back to the player's previous game on the hero, within 7 days. The log line
   is `[lake] N of M sampled games are on the lake; K stepped back …`.
4. **For analysis:**
   - **`pool_players.csv`** has one row per pool player:
     - the name: the board name, or for orbit players the Steam persona from the
       lake (`name_from` says which);
     - how they got into the pool, and their standing;
     - their record on the hero, all-time and over the last 90 days;
     - whether their build made it into the item counts.

     It carries no account ids and no match ids.
   - **`ceiling.csv`** gains `winrate` (`ranked_wins / ranked_games`, 0–1),
     next to `shrunk_winrate`.

## After uploading

1. **Add one line to `.github/workflows/daily.yml` by hand.** The inbox can't
   write workflow files without a PAT. In the "Upload aggregate CSVs" step, add
   this to the `path:` list, indented like its neighbours:

       output/pool_players.csv

2. Run "Daily refresh" by hand, or wait for the next scheduled run.
3. In the log, check:
   - the first `[lake]` lines: the manifest version and the column types;
   - rows and seconds per query;
   - `[items] N of M player-matches found`;
   - the step-back line.

## Verified

- **Every ported query matched a Python reference of the original ClickHouse
  semantics** on a synthetic lake: 19,074 rows, 6% of them duplicated with a
  newer `created_at`, both game modes and both match modes.
  - **Two type layouts:** timestamps with time zone and enum names, and naive
    timestamps and enum codes.
  - **Covered:** orbit 1 (both scripts, both match-mode settings), ring 2, pool
    net wins (decay off and on, any mode and Ranked), items, the step-back,
    Steam names and `pool_players.csv`.
- **Net worth at buy** matched an independent formulation on every purchase,
  plus boundary times.
- **Query plans on parquet:** every `match_player` scan gets a constant
  `start_time` filter (orbit, ring 2, pool wins, step-back) or the `match_id`
  list (items). No cast lands on the column, so the lake can skip files.
- **The lake's connect path,** with the manifest and catalog downloads faked:
  URLs, catalog attach, choice of DuckDB version and the pip fallbacks. A network
  error retries on a fresh catalog; a query error raises at once.
- `pyflakes` reports nothing, and the drop was unpacked through the inbox
  against a fresh clone of main.

## Not verified

- **Nothing touched the real lake:** the sandbox can't reach
  data.deadlock-api.com. The first run is the test. The open questions:
  - the lake's real column types, which the first `[lake]` line prints (both
    likely layouts are handled);
  - how long the 90-day pool-wins scan and the item query take from a GitHub
    runner.
- **`main()` was not run end to end,** because there is no mock of the REST
  endpoints here. The changed functions were tested directly.
- **Still on `/v1/sql`:** `comps.yml` and `top-heroes.yaml` run
  `hero_type_comps.py`, `type_*.py` and `top_heroes.py`, which still call it.
  Both are manual workflows; those scripts need the same port before they run
  again.
