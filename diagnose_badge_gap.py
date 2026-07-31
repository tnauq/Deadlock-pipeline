#!/usr/bin/env python3
"""
mmr and ceiling.csv's badge_level are both dead under MATCH_MODE=Ranked — every
row identical (-6.0 / empty), not just thin. -6.0 is exactly what the mmr
formula returns when team_badge resolves to 0, which happens if `team` no
longer contains 'Team0'/'Team1' for ranked rows and the if() falls through.

    python3 diagnose_badge_gap.py

No CSVs required. Throttled for the 2/min SQL limit.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
SQL_PAUSE_S = 32
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


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-diag/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    print("--- distinct 'team' values on Ranked rows, last 3 days ---")
    for r in sql("""
        SELECT team, count() AS n
        FROM match_player
        WHERE match_mode = 'Ranked' AND start_time >= now() - INTERVAL 3 DAY
        GROUP BY team ORDER BY n DESC
    """, "team values"):
        print(" ", r)

    print("\n--- are average_badge_team0/1 actually populated on Ranked rows? ---")
    for r in sql("""
        SELECT
            count() AS n,
            countIf(average_badge_team0 IS NOT NULL) AS t0_set,
            countIf(average_badge_team1 IS NOT NULL) AS t1_set,
            countIf(average_badge_team0 IS NOT NULL AND average_badge_team0 > 0) AS t0_nonzero,
            countIf(average_badge_team1 IS NOT NULL AND average_badge_team1 > 0) AS t1_nonzero
        FROM match_player
        WHERE match_mode = 'Ranked' AND start_time >= now() - INTERVAL 3 DAY
    """, "badge population"):
        print(" ", r)

    print("\n--- 5 raw Ranked rows: team + both badge columns ---")
    for r in sql("""
        SELECT team, average_badge_team0, average_badge_team1, account_id, hero_id
        FROM match_player
        WHERE match_mode = 'Ranked' AND start_time >= now() - INTERVAL 3 DAY
        LIMIT 5
    """, "raw sample"):
        print(" ", r)

    print("\n--- does the if(team='Team0', ...) branch actually match anything? ---")
    for r in sql("""
        SELECT
            countIf(team = 'Team0') AS literal_team0,
            countIf(team = 'Team1') AS literal_team1,
            count() AS total
        FROM match_player
        WHERE match_mode = 'Ranked' AND start_time >= now() - INTERVAL 3 DAY
    """, "literal match"):
        print(" ", r)

    print("\n--- raw leaderboard entry: is badge_level actually null now? ---")
    data = fetch(BASE + "/v1/leaderboard/NAmerica")
    entries = data.get("entries") if isinstance(data, dict) else data
    for e in entries[:3]:
        print(" ", json.dumps(e)[:300])

    # This runs right before deadlock_pipeline.py's own first SQL call in the
    # same job. Leave the shared per-IP quota clear for it rather than passing
    # the throttle debt forward — missing this caused a 429 on the very next
    # step (2026-07-31).
    print("\n  ... cooldown %ds before handing off to the pipeline" % SQL_PAUSE_S,
          file=sys.stderr)
    time.sleep(SQL_PAUSE_S)


if __name__ == "__main__":
    main()
