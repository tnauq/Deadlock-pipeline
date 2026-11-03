# Inbox drop: give memory back between lake queries (2026-11-03, third)

    lake.py   MODIFIED - reopens DuckDB on the same catalog to free memory; limit 80% of RAM

Built against main 9734a98 (lake-memory-drop applied).

## What happened at 10:01 UTC

- Pool net wins completed for the first time on DuckDB 2.0.0, at 1 thread.
  Pool stats were logged.
- The next query ("which of 1760 sampled games the lake has") failed at
  1 thread. 9.6 of 11.1 GiB was already in use and it needed one 5.0 GiB block.
  A 1-thread scan doesn't use 9.6 GiB, so DuckDB was holding memory from
  earlier queries.

## What changes

- `reopen()`: closes the DuckDB instance and attaches the SAME downloaded
  catalog file, so the run still reads one snapshot. It frees everything and
  takes under a second.
- Before each query: if DuckDB holds more than `LAKE_REOPEN_ABOVE_MB` (1024)
  it is reopened first. Log line: `[lake] reopened DuckDB (N GiB held ...)`.
- On out-of-memory: retry on a fresh instance at the same threads, then halve
  the threads (fresh instance each time) down to 1.
- Memory limit 80% of RAM (DuckDB's own default), never below 8GB: about
  12.8 GB on the runner.
