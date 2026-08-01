#!/usr/bin/env python3
"""
Team composition by hero TYPE (assassin / brawler / marksman / mystic).

    python3 hero_type_comps.py            # analysis, writes output/type_comps.csv
    python3 hero_type_comps.py --types    # just print the hero->type map, no SQL

Answers: what is the usual split of the four types across a 6-player team, and
do some splits win more?

COST: exactly ONE /v1/sql query (plus one free assets fetch). The 20/hr cap is
the binding SQL limit and a pipeline run uses ~16, so DO NOT run this in the
same hour as the scheduled job. --types costs zero SQL.

WHY UNRANKED, NOT RANKED. Two reasons, both from PROBES.md:
  * Volume. Ranked was ~924 rows in a 3-day window at launch vs ~841k Unranked.
    There are 84 possible type signatures for a 6-player team; ranked cannot
    populate them.
  * average_badge is ZERO on every ranked row since 2026-07-31, so a skill
    filter is impossible there. It still works on Unranked rows, which is the
    only way to restrict this to high-level play.
Composition analysis does not need the ranked ladder cohort, so this is a real
choice rather than a compromise.

READ THE WIN RATES CAREFULLY. A signature's win rate is confounded by which
heroes happen to sit in it: if the strongest heroes in the patch are mystics,
mystic-heavy comps win regardless of whether the SHAPE is good. This measures
association, not composition quality. The `distinct_heroes` column is a rough
guard — a signature carried by few distinct heroes is really a hero effect.
"""

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT_DIR = "output"
TYPES = ["assassin", "brawler", "marksman", "mystic"]

LOOKBACK_DAYS = int(os.environ.get("COMP_LOOKBACK_DAYS") or 14)
BADGE_FLOOR = int(os.environ.get("COMP_BADGE_FLOOR") or 100)   # Unranked badge still works
MATCH_MODE = os.environ.get("COMP_MATCH_MODE") or "Unranked"
GAME_MODE = os.environ.get("COMP_GAME_MODE") or "Normal"
MIN_TEAMS = int(os.environ.get("COMP_MIN_TEAMS") or 50)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-comps/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def hero_types():
    """hero_id -> type, from the assets payload. Free, no rate limit worth noting."""
    heroes = get(BASE + "/v1/assets/heroes")
    if isinstance(heroes, dict):
        heroes = heroes.get("data", heroes.get("heroes", []))
    by_type, names, untyped, disabled = defaultdict(list), {}, [], 0
    for h in heroes:
        if h.get("disabled") or not h.get("player_selectable", True):
            disabled += 1
            continue
        hid = h.get("id")
        if hid is None:
            continue
        names[int(hid)] = h.get("name") or h.get("class_name") or str(hid)
        t = h.get("hero_type")
        if t in TYPES:
            by_type[t].append(int(hid))
        else:
            untyped.append((int(hid), names[int(hid)], t))
    return by_type, names, untyped, disabled


def build_query(by_type):
    """One query: bucket each team by type counts, then aggregate signatures.

    Inner select is per (match_id, team). `won` is a per-player Bool but is
    identical across a team, so any(won) is safe. Teams without exactly 6
    players are dropped — abandons and parse gaps would otherwise create
    signatures that never occurred.
    """
    counts = ",\n        ".join(
        "countIf(hero_id IN (%s)) AS %s" % (",".join(str(i) for i in by_type[t]), t)
        for t in TYPES)
    return """
SELECT
    {cols},
    count()            AS teams,
    round(100 * avg(won), 2) AS winrate,
    sum(won)           AS wins,
    uniqExact(sig)     AS distinct_hero_sets
FROM (
    SELECT
        match_id,
        team,
        {counts},
        any(won)                       AS won,
        arraySort(groupArray(hero_id)) AS sig,
        count()                        AS players
    FROM match_player
    WHERE match_mode = '{mode}'
      AND game_mode = '{gmode}'
      AND start_time >= now() - INTERVAL {lookback} DAY
      AND greatest(average_badge_team0, average_badge_team1) >= {floor}
    GROUP BY match_id, team
)
WHERE players = 6
GROUP BY {cols}
HAVING teams >= {min_teams}
ORDER BY teams DESC
""".format(cols=", ".join(TYPES), counts=counts, mode=MATCH_MODE, gmode=GAME_MODE,
           lookback=LOOKBACK_DAYS, floor=BADGE_FLOOR, min_teams=MIN_TEAMS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", action="store_true",
                    help="print the hero->type map and exit, no SQL")
    args = ap.parse_args()

    by_type, names, untyped, disabled = hero_types()
    total = sum(len(v) for v in by_type.values())
    print("hero types (%d typed, %d untyped, %d disabled/unselectable)"
          % (total, len(untyped), disabled), file=sys.stderr)
    for t in TYPES:
        print("  %-9s %2d  %s" % (t, len(by_type[t]),
                                  ", ".join(sorted(names[i] for i in by_type[t]))),
              file=sys.stderr)
    if untyped:
        print("  UNTYPED (excluded from every count, so their teams will not "
              "sum to 6 and get dropped):", file=sys.stderr)
        for hid, nm, raw in untyped:
            print("    %-14s hero_type=%r" % (nm, raw), file=sys.stderr)

    if args.types:
        return
    if not total:
        raise SystemExit("no typed heroes — hero_type missing from assets")

    q = build_query(by_type)
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)
    print("\n[sql] 1 query, %d char url | %s/%s, last %dd, badge>=%d"
          % (len(url), MATCH_MODE, GAME_MODE, LOOKBACK_DAYS, BADGE_FLOOR),
          file=sys.stderr)
    if len(url) > 9000:
        raise SystemExit("url %d chars, over the ~9000 guard" % len(url))

    rows = get(url)
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    if not rows:
        raise SystemExit("no rows — try lowering COMP_BADGE_FLOOR or COMP_MIN_TEAMS")

    for r in rows:
        r["signature"] = "/".join(str(int(r[t])) for t in TYPES)
        r["teams"] = int(r["teams"])
        r["winrate"] = float(r["winrate"])

    grand = sum(r["teams"] for r in rows)
    print("  -> %d signatures, %d teams total\n" % (len(rows), grand), file=sys.stderr)

    print("  %-14s %8s %7s %8s %s" % ("a/b/mk/my", "teams", "share", "winrate",
                                      "hero sets"), file=sys.stderr)
    for r in rows[:20]:
        print("  %-14s %8d %6.1f%% %7.1f%% %d"
              % (r["signature"], r["teams"], 100.0 * r["teams"] / grand,
                 r["winrate"], int(r["distinct_hero_sets"])), file=sys.stderr)

    # average number of each type per team, weighted by how often comps occur
    print("\n  average per team:", file=sys.stderr)
    for t in TYPES:
        avg = sum(float(r[t]) * r["teams"] for r in rows) / grand
        print("    %-9s %.2f" % (t, avg), file=sys.stderr)

    spread = [r for r in rows if r["teams"] >= max(MIN_TEAMS, grand * 0.005)]
    if len(spread) > 1:
        best = max(spread, key=lambda r: r["winrate"])
        worst = min(spread, key=lambda r: r["winrate"])
        print("\n  best  %s  %.1f%% over %d teams" % (best["signature"],
              best["winrate"], best["teams"]), file=sys.stderr)
        print("  worst %s  %.1f%% over %d teams" % (worst["signature"],
              worst["winrate"], worst["teams"]), file=sys.stderr)
        print("  spread %.1f pts — treat as association, not composition quality;"
              % (best["winrate"] - worst["winrate"]), file=sys.stderr)
        print("  a signature carried by few distinct hero sets is a hero effect.",
              file=sys.stderr)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "type_comps.csv")
    cols = ["signature"] + TYPES + ["teams", "wins", "winrate", "distinct_hero_sets"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("\n  -> %s" % path, file=sys.stderr)


if __name__ == "__main__":
    main()
