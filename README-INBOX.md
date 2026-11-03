# Inbox drop: memory limit sized to the runner (2026-11-03, second)

    lake.py   MODIFIED - memory limit 75% of RAM (never under 8GB); thread floor 1

Built against main e5b3be3 (lake-oom-drop applied).

## What broke

- The 05:58 UTC run still ran out of memory in pool net wins (days 45-60).
- Each failure was ONE allocation of 3.8-4.9 GiB, with 4-5 GiB already in
  use. Fewer threads did not shrink it, so it can't fit under 7.4 GiB (8GB).
- DuckDB 2.0.0 needs far more memory than 1.5.x to read some Parquet files:
  duckdb/duckdb#28082, open, reproduced Oct 30, no fix yet. The lake's
  catalog is written by 2.0.0, so the runner has to use 2.0.0.

## What changes

- `LAKE_MEMORY` unset: 75% of the machine's RAM, never below 8GB. On
  ubuntu-latest (16 GB, public repo) that is about 12 GB. The connect log
  line now shows the limit and threads.
- The out-of-memory retry goes down to 1 thread (was 2).
- Setting `LAKE_MEMORY` still overrides it.
