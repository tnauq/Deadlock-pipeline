#!/usr/bin/env python3
"""
Check whether match_mode / game_mode still behave the way SCHEMA.md documents,
now that a ranked mode update may have shipped.

Known-as-of-last-check (SCHEMA.md quirk #1): match_mode='Ranked' matched ZERO
rows; live rows carried 'Unranked'; the pipeline filters on game_mode='Normal'
and never touches match_mode. If that's changed, this prints the new values
rather than the pipeline silently including/excluding the wrong games.

    python3 diagnose_match_mode.py

No CSVs required. A few small SELECTs, not the full pipeline.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
SQL_PAUSE_S = 32   # /v1/sql allows 2 req/min per IP; this run makes 6 calls,
                   # so budget for ~3 minutes total. The pipeline itself makes
                   # its first SQL call right after this step, so the pause
                   # AFTER the last query here matters too, not just between.
_calls = 0


def sql(query, label=""):
    global _calls
    if _calls:
        print("  ... waiting %ds (rate limit)" % SQL_PAUSE_S, file=sys.stderr)
        time.sleep(SQL_PAUSE_S)
    _calls += 1
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-diag/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        print("  [%s] FAILED (%s): %s" % (label, e.code, body), file=sys.stderr)
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    return rows


def main():
    print("--- distinct match_mode values, last 3 days ---")
    for r in sql("""
        SELECT match_mode, count() AS n
        FROM match_player
        WHERE start_time >= now() - INTERVAL 3 DAY
        GROUP BY match_mode ORDER BY n DESC
    """):
        print(" ", r)

    print("\n--- distinct game_mode values, last 3 days ---")
    for r in sql("""
        SELECT game_mode, count() AS n
        FROM match_player
        WHERE start_time >= now() - INTERVAL 3 DAY
        GROUP BY game_mode ORDER BY n DESC
    """):
        print(" ", r)

    print("\n--- match_mode x game_mode cross-tab, last 3 days ---")
    for r in sql("""
        SELECT match_mode, game_mode, count() AS n
        FROM match_player
        WHERE start_time >= now() - INTERVAL 3 DAY
        GROUP BY match_mode, game_mode ORDER BY n DESC LIMIT 20
    """):
        print(" ", r)

    print("\n--- does the pipeline's current filter still return rows? ---")
    for r in sql("""
        SELECT count() AS n
        FROM match_player
        WHERE match_mode = 'Ranked' AND start_time >= now() - INTERVAL 3 DAY
    """):
        print("  match_mode='Ranked':", r)
    for r in sql("""
        SELECT count() AS n
        FROM match_player
        WHERE game_mode = 'Normal' AND start_time >= now() - INTERVAL 3 DAY
    """):
        print("  game_mode='Normal' (pipeline's current filter):", r)

    print("\n--- is there a badge/rank/mmr-like column that changed? ---")
    for r in sql("SELECT * FROM match_player LIMIT 1"):
        print("  columns:", sorted(r.keys()))

    # This runs right before deadlock_pipeline.py's own first SQL call in the
    # same job. Leave the shared per-IP quota clear for it rather than passing
    # the throttle debt forward.
    print("\n  ... cooldown %ds before handing off to the pipeline" % SQL_PAUSE_S,
          file=sys.stderr)
    time.sleep(SQL_PAUSE_S)


if __name__ == "__main__":
    main()
