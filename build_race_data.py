#!/usr/bin/env python3
"""
Build the data for the site's tier-race view from the daily raw archive.

    python3 build_race_data.py

Reads  ./archive/YYYY-MM-DD-NAmerica-ceiling.gz   (holds BOTH regions' rows)
       ./archive/YYYY-MM-DD-NAmerica-tierlist.gz  (pooled across regions)
Writes ./docs/archive/race.json

Called at the end of archive_snapshot.py on every run, so the race picks up a
new day as soon as that day is archived. Rebuilt from scratch each time: ~100
small files, well under a second. Zero API calls.

TIERS ARE REBUILT WITH THE SITE'S OWN RULES (build_site_data.py): heroes in
ceiling_rank order, orbit-only ceilings and heroes with no ceiling go to NEW,
the rest split S/A/B/C/D on the same bell. Checked against the published
data.json for 2026-11-06: identical order and tiers in both regions.

MEASURES, per hero per day:
  rank_score  100 for #1 on that day's site list, 0 for last, null if NEW
  points      running total of 43 * (N - rank) / (N - 1), so #1 earns 43 a day
              whatever N is; NEW days earn 0
  winrate     tierlist elite_winrate (pooled NA+EU; NA only before 2026-08-04)
  net_wins    tierlist pool_net_wins (first archived 2026-08-14)

The archive is ONE snapshot per day: the first refresh that day (see
archive_snapshot.py, which skips a region already archived today).
"""

import csv
import glob
import gzip
import json
import os
import sys
from math import erf, sqrt

RAW = "archive"
OUT = os.path.join("docs", "archive", "race.json")
TIERS = ["S", "A", "B", "C", "D"]
_CUTS = [1.2, 0.4, -0.4, -1.2]          # same z cuts as build_site_data.py


def _phi(z):
    return 0.5 * (1 + erf(z / sqrt(2)))


_E = [1.0] + [_phi(z) for z in _CUTS] + [0.0]
_AREAS = [_E[i] - _E[i + 1] for i in range(5)]


def allocate(n):
    raw = [a * n for a in _AREAS]
    base = [int(x) for x in raw]
    for i in sorted(range(5), key=lambda i: -(raw[i] - base[i]))[:n - sum(base)]:
        base[i] += 1
    return base


def _rows(path):
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _rank_key(r):
    v = (r.get("ceiling_rank") or "").strip()
    if v.lstrip("-").isdigit():
        return int(v)
    g = (r.get("global_pos") or "").strip()
    return int(g) if g.isdigit() else 10 ** 9


def _num(v, cast=float):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def build():
    ceil_files = sorted(glob.glob(os.path.join(RAW, "*-NAmerica-ceiling.gz")))
    if not ceil_files:
        raise SystemExit("no archived ceiling files under %s/" % RAW)

    days = []                     # (date, ceiling rows, tierlist rows)
    for f in ceil_files:
        d = os.path.basename(f)[:10]
        tf = f.replace("-ceiling.gz", "-tierlist.gz")
        if not os.path.exists(tf):
            continue
        days.append((d, _rows(f), _rows(tf)))

    heroes = sorted({r["hero"] for _, _, tl in days for r in tl if r.get("elite_winrate")}
                    | {r["hero"] for _, c, _ in days for r in c})
    hix = {h: i for i, h in enumerate(heroes)}

    pooled = {"dates": [], "winrate": [], "net_wins": []}
    regions = {}
    net_wins_order = []           # dates the ceiling was ordered by ranked net wins
    cum = {}

    for d, ceil, tl in days:
        wr = [None] * len(heroes)
        nw = [None] * len(heroes)
        listed = set()
        for r in tl:
            if not r.get("elite_winrate"):
                continue
            listed.add(r["hero"])
            wr[hix[r["hero"]]] = round(float(r["elite_winrate"]), 2)
            nw[hix[r["hero"]]] = _num(r.get("pool_net_wins"), int)
        pooled["dates"].append(d)
        pooled["winrate"].append(wr)
        pooled["net_wins"].append(nw)

        ordered_by_net_wins = False
        for rg in ("NAmerica", "Europe"):
            cr = sorted((r for r in ceil if r.get("region") == rg), key=_rank_key)
            if not cr:
                continue
            ranked = [r for r in cr if r["hero"] in listed
                      and (r.get("match") or "").strip() != "orbit"]
            # which ordering produced ceiling_rank this day: the file says so
            # from 2026-09-03; before that, board order means ranks follow
            # global_pos
            metric = (cr[0].get("ceiling_metric") or "").strip()
            if not metric:
                gp = [_rank_key({"global_pos": r.get("global_pos")}) for r in ranked]
                metric = "board" if gp == sorted(gp) else "net_wins"
            ordered_by_net_wins |= metric == "net_wins"

            n = len(ranked)
            tier = ["-"] * len(heroes)     # "-" = not on the list that day
            rank = [None] * len(heroes)
            i = 0
            for t, k in zip(TIERS, allocate(n)):
                for r in ranked[i:i + k]:
                    j = hix[r["hero"]]
                    tier[j] = t
                    rank[j] = len([x for x in rank if x is not None]) + 1
                i += k
            for h in listed:
                if tier[hix[h]] == "-":
                    tier[hix[h]] = "N"     # NEW: on the tier list, not placed
            score = [None] * len(heroes)
            pts = [None] * len(heroes)
            for j, rk in enumerate(rank):
                key = (rg, j)
                if rk is not None and n > 1:
                    score[j] = round(100.0 * (n - rk) / (n - 1), 1)
                    cum[key] = cum.get(key, 0.0) + 43.0 * (n - rk) / (n - 1)
                if tier[j] != "-":
                    pts[j] = round(cum.get(key, 0.0), 1)
            R = regions.setdefault(rg, {"dates": [], "tier": [], "rank": [],
                                        "score": [], "points": []})
            R["dates"].append(d)
            R["tier"].append("".join(tier))
            R["rank"].append(rank)
            R["score"].append(score)
            R["points"].append(pts)
        if ordered_by_net_wins:
            net_wins_order.append(d)

    out = {
        "heroes": heroes,
        "regions": regions,
        "pooled": pooled,
        "net_wins_ordering": [net_wins_order[0], net_wins_order[-1]] if net_wins_order else None,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, OUT)
    print("  [race] %s: %d days, %d heroes, %.0f KB"
          % (OUT, len(pooled["dates"]), len(heroes), os.path.getsize(OUT) / 1024),
          file=sys.stderr)


if __name__ == "__main__":
    build()
