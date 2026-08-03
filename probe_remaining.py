#!/usr/bin/env python3
"""
Close out every remaining open item in PROBES.md, in one run.

    python3 probe_remaining.py
    SKIP_SQL=1 python3 probe_remaining.py    # free half only, safe any time

OPEN ITEMS COVERED
  1. banned_hero_ids            - does ranked have a ban/draft phase in the data?
  2. item_cohort_stats_*        - contents and grain of the two unused tables
  3. upgrades.* vs items.*      - does upgrades add anything?
  4. average_badge recovery     - has the 2026-07-31 zeroing resolved?

COST DESIGN. Schema discovery (/v1/sql/tables/{t}/schema) is 10 req/min and does
NOT consume the 20/hr SQL budget, so columns and types are FREE - item 3 is
answerable with no query at all. Only grain and population need real SQL:
4 queries total. A pipeline run uses ~16 of 20/hr, so DO NOT run this in the
same hour as the scheduled job.

WHY BANS MATTER. Ranked has a pregame lobby: the enemy team is visible and one
hero swap is allowed. That makes counterpicking mechanically possible, which is
the proposed explanation for NA and EU ceiling orderings being uncorrelated
(Spearman 0.017 across two run pairs). If banned_hero_ids is populated we can
test whether ban pressure differs by region; if it is empty, that mechanism is
not visible in this data and the hypothesis needs another route.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
SKIP_SQL = (os.environ.get("SKIP_SQL") or "0") == "1"
SQL_PAUSE = float(os.environ.get("SQL_PAUSE_S") or 35)
LOOKBACK = int(os.environ.get("PROBE_LOOKBACK_DAYS") or 3)
TABLES = ["item_cohort_stats_net_worth_agg", "item_cohort_stats_time_agg"]
_sql_calls = 0


def get(url, label):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:400])
    except Exception as e:
        return None, str(e)


def sql(query, label):
    global _sql_calls
    if _sql_calls:
        print("    ... waiting %.0fs (SQL 2/min)" % SQL_PAUSE, file=sys.stderr)
        time.sleep(SQL_PAUSE)
    _sql_calls += 1
    rows, err = get(BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query), label)
    if err:
        print("    [%s] %s" % (label, err), file=sys.stderr)
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    return rows


def schema(table):
    data, err = get(BASE + "/v1/sql/tables/%s/schema" % table, table)
    if err:
        print("  %s -> %s" % (table, err), file=sys.stderr)
        return None
    cols = data if isinstance(data, list) else data.get("columns", [])
    return [((c.get("name"), c.get("type") or "") if isinstance(c, dict) else (c, ""))
            for c in cols]


def head(t):
    print("\n" + "=" * 66, file=sys.stderr)
    print(t, file=sys.stderr)
    print("=" * 66, file=sys.stderr)


def main():
    head("FREE: schemas (10 req/min, no SQL budget)")
    mp = schema("match_player") or []
    items = sorted(n for n, _ in mp if n.startswith("items."))
    upg = sorted(n for n, _ in mp if n.startswith("upgrades."))
    print("\n-- upgrades.* vs items.* (schema alone) --", file=sys.stderr)
    print("  items.*    (%d): %s" % (len(items), [n.split('.', 1)[1] for n in items]),
          file=sys.stderr)
    print("  upgrades.* (%d): %s" % (len(upg), [n.split('.', 1)[1] for n in upg]),
          file=sys.stderr)
    if not upg:
        print("  >>> no upgrades.* columns exist - item CLOSED", file=sys.stderr)
    else:
        extra = {n.split('.', 1)[1] for n in upg} - {n.split('.', 1)[1] for n in items}
        print("  >>> in upgrades.* but NOT items.*: %s"
              % (sorted(extra) or "none - looks redundant"), file=sys.stderr)

    bans = [(n, t) for n, t in mp if "ban" in n.lower()]
    print("\n-- ban-related columns on match_player --", file=sys.stderr)
    print("  %s" % (bans or "none"), file=sys.stderr)

    for t in TABLES:
        print("\n-- %s --" % t, file=sys.stderr)
        cols = schema(t)
        if cols:
            print("  %d columns:" % len(cols), file=sys.stderr)
            for n, ty in cols:
                print("     %-34s %s" % (n, ty[:70]), file=sys.stderr)

    if SKIP_SQL:
        print("\nSKIP_SQL=1 - stopping before any budgeted query", file=sys.stderr)
        return

    head("BUDGETED: 4 SQL queries")

    print("\n-- 1. banned_hero_ids: is there a draft phase in the data? --", file=sys.stderr)
    q1 = ("SELECT count() AS ranked_rows, "
          "countIf(length(banned_hero_ids) > 0) AS rows_with_bans, "
          "round(avg(length(banned_hero_ids)), 2) AS avg_bans, "
          "max(length(banned_hero_ids)) AS max_bans "
          "FROM match_player WHERE match_mode = 'Ranked' "
          "AND start_time >= now() - INTERVAL %d DAY" % LOOKBACK)
    for r in sql(q1, "bans"):
        print("  ", r, file=sys.stderr)
        if int(r.get("rows_with_bans") or 0) == 0:
            print("  >>> EMPTY - no ban data; the pregame swap is not visible here,",
                  file=sys.stderr)
            print("      so counterpick pressure needs another route.", file=sys.stderr)
        else:
            print("  >>> POPULATED - regional ban comparison is viable.", file=sys.stderr)

    print("\n-- 2. has average_badge recovered for ranked? --", file=sys.stderr)
    q2 = ("SELECT count() AS n, countIf(average_badge_team0 > 0) AS t0_nonzero, "
          "countIf(average_badge_team1 > 0) AS t1_nonzero, "
          "max(greatest(average_badge_team0, average_badge_team1)) AS top_badge "
          "FROM match_player WHERE match_mode = 'Ranked' "
          "AND start_time >= now() - INTERVAL %d DAY" % LOOKBACK)
    for r in sql(q2, "badge"):
        print("  ", r, file=sys.stderr)
        if int(r.get("t0_nonzero") or 0) == 0:
            print("  >>> STILL ZERO - the 2026-07-31 collapse persists.", file=sys.stderr)
        else:
            print("  >>> RECOVERED - a badge floor is viable again.", file=sys.stderr)

    # These are AggregatingMergeTree tables. SELECT * returns HTTP 400 because
    # players_state is AggregateFunction(uniq, UInt32) and an unmerged aggregate
    # state cannot be serialised to JSON. Name the columns instead, and merge the
    # one aggregate explicitly. (Discovered 2026-08-03.)
    BUCKET = {"item_cohort_stats_net_worth_agg": "bucket_net_worth",
              "item_cohort_stats_time_agg": "bucket_minute"}
    for t in TABLES:
        print("\n-- %s: grain --" % t, file=sys.stderr)
        rows = sql("SELECT match_mode, game_mode, day, cohort_item_id, item_id, "
                   "%s AS bucket, n_matches, n_wins, n_sold, "
                   "uniqMerge(players_state) AS players "
                   "FROM %s GROUP BY match_mode, game_mode, day, cohort_item_id, "
                   "item_id, bucket, n_matches, n_wins, n_sold "
                   "ORDER BY n_matches DESC LIMIT 3" % (BUCKET[t], t), t)
        if not rows:
            print("    (no rows - table may be empty)", file=sys.stderr)
            continue
        for r in rows[:2]:
            print("     %s" % json.dumps(r)[:420], file=sys.stderr)
        keys = sorted(rows[0].keys())
        print("     keys: %s" % keys, file=sys.stderr)
        print("     >>> grain: one row per (match_mode, game_mode, day, "
              "cohort_item_id, item_id, bucket)", file=sys.stderr)
        print("     >>> cohort_item_id + item_id = item CO-OCCURRENCE: how often "
              "item_id is held", file=sys.stderr)
        print("         in builds that also hold cohort_item_id, with n_wins. There "
              "is NO hero_id,", file=sys.stderr)
        print("         so this is item-pair data pooled across heroes - it does not "
              "replace the", file=sys.stderr)
        print("         pipeline's per-hero item aggregation.", file=sys.stderr)

    print("\n%d SQL calls used. Pipeline needs ~16 of 20/hr - do not run both in the "
          "same hour." % _sql_calls, file=sys.stderr)


if __name__ == "__main__":
    main()
