#!/usr/bin/env python3
"""
Does a type ADVANTAGE over the enemy team predict winning?

    python3 type_differential.py

ONE /v1/sql query. The unkeyed cap is 20 req/HOUR and a pipeline run uses ~16,
so do not run this in the same hour as the scheduled job.

WHY THIS RATHER THAN A COMP-VS-COMP MATRIX. 88 signatures give 3,916 unordered
matchup cells across ~39,700 matches — about 10 matches per cell, SE ±15.7
points. Every cell would be noise. Restricting to the top 10 signatures still
only reaches ±3.4 points, which cannot resolve the 1-3 point gradients at
issue. Collapsing to a per-type DIFFERENTIAL gives ~9 buckets instead, roughly
4,400 matches each, SE ±0.75 points — about 20x the resolution, and it tests
the actual hypothesis directly.

It also partly cancels the hero-strength confound that hero_type_comps.py could
not rule out. Both teams draw from the same hero pool, so a matchup with equal
mystic counts should sit at 50% no matter how strong mystics are. Only the
SLOPE carries information about composition.

READ THE SLOPE, NOT THE LEVEL. A monotonic rise with type advantage supports a
compositional effect. A flat line kills it. A non-monotonic jumble means the
buckets are picking up something else.

Unranked, for the same reasons as hero_type_comps.py: ranked is far too thin,
and average_badge is 0 on ranked rows so the badge floor would filter nothing.
"""

import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT_DIR = "output"
TYPES = ["assassin", "brawler", "marksman", "mystic"]

LOOKBACK_DAYS = int(os.environ.get("COMP_LOOKBACK_DAYS") or 14)
BADGE_FLOOR = int(os.environ.get("COMP_BADGE_FLOOR") or 100)
MATCH_MODE = os.environ.get("COMP_MATCH_MODE") or "Unranked"
GAME_MODE = os.environ.get("COMP_GAME_MODE") or "Normal"
MIN_MATCHES = int(os.environ.get("DIFF_MIN_MATCHES") or 100)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-diff/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        raise SystemExit(
            "SQL failed (HTTP %s)\n%s\n\n"
            "If this is a timeout or memory error, lower COMP_LOOKBACK_DAYS "
            "(currently %d) or raise COMP_BADGE_FLOOR (currently %d) to scan "
            "fewer rows. If it is a 429, wait for the hourly window to clear."
            % (e.code, body, LOOKBACK_DAYS, BADGE_FLOOR))


def hero_types():
    heroes = get(BASE + "/v1/assets/heroes")
    if isinstance(heroes, dict):
        heroes = heroes.get("data", heroes.get("heroes", []))
    by_type = defaultdict(list)
    untyped = []
    for h in heroes:
        if h.get("disabled") or not h.get("player_selectable", True):
            continue
        hid = h.get("id")
        if hid is None:
            continue
        t = h.get("hero_type")
        (by_type[t].append(int(hid)) if t in TYPES
         else untyped.append(h.get("name") or hid))
    return by_type, untyped


def build_query(by_type):
    """One pass over match_player, no self-join.

    The first version joined two aggregated subqueries on match_id. That scans
    ~14 days of match_player twice and produces four team-pairings per match
    before the WHERE discards three — expensive enough to fail on a dataset
    this size, which is what happened on 2026-08-02 with plenty of SQL budget
    left.

    Conditional aggregation does it in a single GROUP BY instead: count each
    type separately for Team0 and Team1 within the same pass and subtract.
    Same result, one scan, no join.

    Team0 is the subject and Team1 the enemy, so there is one row per match
    rather than two. The mirror perspective is redundant — the differentials
    are symmetric.

    Matches where either side is not exactly 6 players are dropped; abandons
    and parse gaps would otherwise create differentials that never occurred.
    """
    parts = []
    for t in TYPES:
        ids = ",".join(str(i) for i in by_type[t])
        parts.append(
            "countIf(hero_id IN ({ids}) AND team = 'Team0') - "
            "countIf(hero_id IN ({ids}) AND team = 'Team1') AS d_{t}".format(ids=ids, t=t))
    diffs = ",\n        ".join(parts)
    sel = ", ".join("d_%s" % t for t in TYPES)
    return """
SELECT
    {sel},
    count()                  AS matches,
    sum(won)                 AS wins,
    round(100 * avg(won), 3) AS winrate
FROM (
    SELECT
        match_id,
        {diffs},
        anyIf(won, team = 'Team0')   AS won,
        countIf(team = 'Team0')      AS p0,
        countIf(team = 'Team1')      AS p1
    FROM match_player
    WHERE match_mode = '{mode}' AND game_mode = '{gmode}'
      AND start_time >= now() - INTERVAL {lookback} DAY
      AND greatest(average_badge_team0, average_badge_team1) >= {floor}
    GROUP BY match_id
    HAVING p0 = 6 AND p1 = 6
)
GROUP BY {sel}
ORDER BY matches DESC
""".format(sel=sel, diffs=diffs, mode=MATCH_MODE, gmode=GAME_MODE,
           lookback=LOOKBACK_DAYS, floor=BADGE_FLOOR)


def main():
    by_type, untyped = hero_types()
    if untyped:
        print("untyped heroes excluded: %s\n" % ", ".join(str(u) for u in untyped),
              file=sys.stderr)

    q = build_query(by_type)
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)
    print("[sql] 1 query, %d char url | %s/%s, last %dd, badge>=%d"
          % (len(url), MATCH_MODE, GAME_MODE, LOOKBACK_DAYS, BADGE_FLOOR),
          file=sys.stderr)
    if len(url) > 9000:
        raise SystemExit("url %d chars, over the ~9000 guard" % len(url))

    rows = get(url)
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    if not rows:
        raise SystemExit("no rows — lower COMP_BADGE_FLOOR or widen the lookback")

    for r in rows:
        r["matches"] = int(r["matches"])
        r["wins"] = int(r["wins"])
    total = sum(r["matches"] for r in rows)
    print("  -> %d differential combinations, %d matches\n" % (len(rows), total),
          file=sys.stderr)

    # collapse to one gradient per type
    out = []
    for t in TYPES:
        key = "d_" + t
        agg = defaultdict(lambda: [0, 0])
        for r in rows:
            d = int(r[key])
            agg[d][0] += r["matches"]
            agg[d][1] += r["wins"]
        print("  %s advantage (my count - enemy count)" % t.upper(), file=sys.stderr)
        print("    %5s %9s %9s %9s %7s" % ("diff", "matches", "share", "winrate", "SE"),
              file=sys.stderr)
        pts = []
        for d in sorted(agg):
            m, w = agg[d]
            if m < MIN_MATCHES:
                continue
            wr = 100.0 * w / m
            se = 100 * math.sqrt(0.25 / m)
            pts.append((d, wr, m, se))
            print("    %+5d %9d %8.1f%% %8.2f%% %6.2f"
                  % (d, m, 100.0 * m / total, wr, se), file=sys.stderr)
            out.append({"type": t, "diff": d, "matches": m, "wins": w,
                        "winrate": round(wr, 3), "se": round(se, 3)})

        if len(pts) >= 3:
            # slope per +1 advantage, least squares weighted by matches
            sw = sum(p[2] for p in pts)
            mx = sum(p[0] * p[2] for p in pts) / sw
            my = sum(p[1] * p[2] for p in pts) / sw
            num = sum(p[2] * (p[0] - mx) * (p[1] - my) for p in pts)
            den = sum(p[2] * (p[0] - mx) ** 2 for p in pts)
            slope = num / den if den else 0
            mono = all(pts[i][1] <= pts[i + 1][1] for i in range(len(pts) - 1)) or \
                   all(pts[i][1] >= pts[i + 1][1] for i in range(len(pts) - 1))
            mid = [p for p in pts if p[0] == 0]
            print("    slope %+.2f pts per +1 %s | %s | even matchup %s"
                  % (slope, t,
                     "monotonic" if mono else "NOT monotonic — treat with suspicion",
                     ("%.2f%%" % mid[0][1]) if mid else "n/a"), file=sys.stderr)
        print("", file=sys.stderr)

    print("  Even matchups (diff 0) should sit at ~50%%. If they do not, the",
          file=sys.stderr)
    print("  sample is asymmetric and every slope below is suspect.", file=sys.stderr)
    print("  A flat slope means type counts do not drive outcomes; the earlier",
          file=sys.stderr)
    print("  per-signature win rates were hero strength, not composition.",
          file=sys.stderr)

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "type_differential.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["type", "diff", "matches", "wins",
                                          "winrate", "se"])
        w.writeheader()
        w.writerows(out)
    print("\n  -> %s" % path, file=sys.stderr)


if __name__ == "__main__":
    main()
