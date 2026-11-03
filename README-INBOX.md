# Inbox drop: lake queries ran out of memory on DuckDB 2.0.0 (2026-11-03)

    lake.py               MODIFIED - retries out-of-memory with fewer threads; spills to disk
    deadlock_pipeline.py  MODIFIED - pool net wins runs in 15-day slices

Built against main 1b2c326 (lake.py b4c7ec7, deadlock_pipeline.py 059803c).

## What broke

- The 04:14 UTC run (Nov 3) was the first to install duckdb 2.0.0: the lake's
  catalog (manifest v1266) is now written by DuckDB 2.0.0.
- Ring 2 NAmerica ran out of memory (non-fatal, as designed).
- Pool net wins, one query over 90 days, ran out of memory at 13.3 GiB against
  the 7.4 GiB (8 GB) limit. That one is fatal, so the run exited 1.
- No refresh has been committed since 1b2c326 (03:49 UTC Nov 2).

## What changes

1. lake.py
   - An out-of-memory error retries the same query with half the threads
     (16, 8, 4, 2). The lower count stays for the rest of the run. The log
     line is `[lake] Out of Memory Error ... retrying with N threads`.
   - `preserve_insertion_order = false` (no query relies on scan order).
   - `temp_directory` in the run's temp folder, so aggregates can spill.
   - New `before(days)`, the upper bound matching `since(days)`.
2. Pool net wins
   - Six queries of 15 days each (`POOL_WINS_SLICE_DAYS`), summed in Python.
     A row and its re-exported copy share a match, so a start_time, so dedup
     per slice equals dedup over the window. The numbers are unchanged.
   - The pair test is a join against a VALUES list, plus plain
     `account_id IN` / `hero_id IN` lists the scan can apply while reading.

## Settings

`LAKE_MIN_THREADS` (2), `POOL_WINS_SLICE_DAYS` (15). If it still fails:
`LAKE_THREADS: "4"` in daily.yml's env.
