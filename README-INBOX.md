# Inbox drop: keep the lake's old files out of the main queries (2026-11-03, fourth)

    lake.py               MODIFIED - each query starts at full threads again
    deadlock_pipeline.py  MODIFIED - items split recent/older; lake check only for recent games

Built against main b817d73 (lake-reopen-drop applied).

## What happened at 10:59 UTC

- Pool net wins: all six slices at 16 threads, 2.5 minutes in total. The
  pre-query reopen fired three times.
- "Which sampled games the lake has": 1,054 s and four OOM retries, then passed.
- Items: OOM at 2 threads and then at 1 thread after an hour, at "54.7 GiB used" of
  11.9. Exit 1, about 2h10m into the run.
- Both slow queries name games by match_id, and the list starts at 23M. ~5%
  of builds are a board player's newest game on the hero from months ago, and
  every such game makes DuckDB 2.0.0 read an old lake file. Those reads need
  single blocks of 4-10 GiB (duckdb/duckdb#28082).

## What changes

1. **Lake check:** only builds played in the last `LAKE_CHECK_DAYS` (2) are
   checked, in a query bounded on start_time. Older games are on the lake by
   definition (it trails by 10-70 min). Builds with no play time are still
   checked.
2. **Items:**
   - Builds played in the last `ITEMS_RECENT_DAYS` (90): one query bounded on
     start_time, so old files are never opened. Fatal on failure, as before.
   - Older builds: one query per lake partition (match_id / 1,000,000), at
     `ITEMS_OLD_THREADS` (2), within `ITEMS_OLD_BUDGET_S` (1500 s). A partition
     that fails, or isn't reached in the budget, leaves its builds without items,
     like an unparsed match.
   - Log: `[items] N of M player-matches found ... (R recent, O older; S older skipped)`.
3. **lake.py:** each query starts at 16 threads. A lower count forced by
   an OOM applies only to that query.
