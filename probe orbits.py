#!/usr/bin/env python3
"""
probe_orbits.py — co-play graph expansion from the ceiling players.

Idea: treat the ranked population as a graph. Orbit 0 is the ceiling players.
Orbit 1 is everyone who shared a ranked match with them. Orbit 2 is everyone
who shared a match with orbit 1. Candidates sourced this way carry a real
`account_id` straight from match_player — no name resolution, no
possible_account_ids, so none of the 110-of-371 identity problem.

WHAT THIS IS ACTUALLY TESTING. Orbit 1 was already measured at 1,373 distinct
players from 12 seeds over 3 days. Expanding from 1,373 could plausibly reach
most of ranked NA, and if orbit 2 is the whole population then it is not a
cohort and cannot fix thinness in any meaningful way — it would just be
"everyone", which the pipeline could get more cheaply.

So the questions are size first, quality second:

  Q1  How many distinct accounts are in each orbit, and what is the growth
      factor? Orbit 2 approaching the ranked population means the radius is
      already too wide at 2.
  Q2  Does standing DECAY with orbit distance? Win rate and games are pulled
      for a sample of each orbit. If orbit 2 looks like orbit 1, distance
      carries no information. If it degrades sharply, orbit 1 is the useful
      radius and orbit 2 is dilution.
  Q3  Coverage of the thin hero-regions: how many orbit members have ranked
      games on the heroes that currently have too few candidates? That is the
      only reason to want this, so it is measured directly.

Win rate is used rather than net wins deliberately. Net wins conflates skill
with volume, and ranked placement SEEDS from prior standing — a player with 30
games was placed at a rank, not climbing to one — so a low game count says
nothing about how good they are.

Cost: 1 SQL call for orbit 1, plus ~2-4 chunked calls for orbit 2 depending on
how wide orbit 1 turns out to be. Budget against the 20/HOUR cap: this is a
daily-cadence tool, not a 4-hourly one. Set ORBITS=1 to stop after orbit 1.

    python3 probe_orbits.py

Writes probe_out/orbits.json. Stdlib only.
"""

import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"
REGION = os.environ.get("PROBE_REGION") or "NAmerica"
SEEDS = int(os.environ.get("ORBIT_SEEDS") or 12)
ORBITS = int(os.environ.get("ORBITS") or 2)
LOOKBACK_DAYS = int(os.environ.get("PROBE_LOOKBACK_DAYS") or 3)
MATCH_MODE = os.environ.get("MATCH_MODE") or "Ranked"
MAX_URL = int(os.environ.get("MAX_URL") or 9000)
# Sampled per orbit for the quality read — hero-stats is free but the response
# grows with one row per (account, hero).
QUALITY_SAMPLE = int(os.environ.get("ORBIT_SAMPLE") or 400)
SQL_PAUSE_S = int(os.environ.get("SQL_PAUSE_S") or 40)

_sql_calls = [0]


def get(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-orbits/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sql(q, label="", tries=4):
    """
    The quota is 2 requests per 60s and the window SLIDES; a 429 response still
    consumes a slot, so retrying inside the window makes things worse. Space
    calls past half the window and drain it fully on a 429.
    """
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)
    if len(url) > MAX_URL:
        raise SystemExit("query URL %d chars, over %d — chunk further"
                         % (len(url), MAX_URL))
    if _sql_calls[0]:
        print("  [sql] pausing %ds" % SQL_PAUSE_S, file=sys.stderr)
        time.sleep(SQL_PAUSE_S)
    _sql_calls[0] += 1
    print("  [sql] #%d %s (%d char url)" % (_sql_calls[0], label, len(url)),
          file=sys.stderr)
    for attempt in range(tries):
        try:
            rows = get(url)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < tries - 1:
                print("  [sql] 429, waiting 65s for the window to drain",
                      file=sys.stderr)
                time.sleep(65)
                continue
            raise SystemExit("SQL failed (%s): %s" % (e.code, body[:300]))
    else:
        raise SystemExit("SQL still rate limited")
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] %d rows" % len(rows), file=sys.stderr)
    return rows


Q_ORBIT = """
SELECT DISTINCT account_id
FROM match_player
WHERE match_id IN (
    SELECT match_id FROM match_player
    WHERE account_id IN ({ids})
      AND match_mode = '{mode}' AND game_mode = 'Normal'
      AND start_time >= now() - INTERVAL {days} DAY
)
"""


def expand(ids, label):
    """
    One orbit outward. DISTINCT account_id keeps the response small — the
    intermediate row count would otherwise be matches x 12.
    """
    ids = sorted(set(ids))
    out = set()
    # size the chunk against the real URL, not the id list alone
    fixed = len(BASE + "/v1/sql?format=json&query=" +
                urllib.parse.quote(Q_ORBIT.format(ids="", mode=MATCH_MODE,
                                                  days=LOOKBACK_DAYS)))
    per = 12          # ~11 chars per id once encoded, plus the comma
    chunk = max(50, (MAX_URL - fixed - 100) // per)
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        rows = sql(Q_ORBIT.format(ids=",".join(str(a) for a in part),
                                  mode=MATCH_MODE, days=LOOKBACK_DAYS),
                   "%s %d-%d of %d" % (label, i + 1, i + len(part), len(ids)))
        for r in rows:
            out.add(int(r["account_id"]))
    return out


def hero_stats(ids):
    """(account, hero) -> {games, wins} over ranked play. Free, batched."""
    ids = sorted(set(int(a) for a in ids if a))
    out = {}
    if not ids:
        return out
    base = "%s/v1/players/hero-stats?match_mode=%s" % (BASE, urllib.parse.quote(MATCH_MODE))
    chunk = max(20, (MAX_URL - len(base) - 40) // 24)
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            rows = get(base + "".join("&account_ids=%d" % a for a in part), timeout=180)
        except Exception as e:
            print("  [hs] chunk failed: %s" % e, file=sys.stderr)
            continue
        for r in rows or []:
            a, h = r.get("account_id"), r.get("hero_id")
            if a is None or h is None:
                continue
            g = r.get("matches_played")
            if g is None:
                m = r.get("matches")
                g = len(m) if isinstance(m, list) else (m or 0)
            w = r.get("wins") or 0
            if isinstance(w, list):
                w = len(w)
            out[(int(a), int(h))] = {"games": int(g), "wins": int(w)}
        time.sleep(0.05)
    return out


def quality(ids, stats):
    """Account-level ranked record for a set of ids."""
    per = defaultdict(lambda: {"games": 0, "wins": 0})
    for (a, _h), rec in stats.items():
        if a in ids:
            per[a]["games"] += rec["games"]
            per[a]["wins"] += rec["wins"]
    wr = [v["wins"] / v["games"] for v in per.values() if v["games"] >= 10]
    gm = [v["games"] for v in per.values() if v["games"]]
    return {
        "with_ranked_games": len(gm),
        "with_10plus": len(wr),
        "median_games": statistics.median(gm) if gm else None,
        "median_winrate": round(statistics.median(wr), 3) if wr else None,
        "p90_winrate": round(sorted(wr)[int(0.9 * len(wr))], 3) if wr else None,
        "over_55pct": sum(1 for x in wr if x > 0.55),
        "over_60pct": sum(1 for x in wr if x > 0.60),
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    cpath = os.path.join("output", "ceiling.csv")
    if not os.path.exists(cpath):
        raise SystemExit("no output/ceiling.csv — run the pipeline first")
    seeds = []
    for r in csv.DictReader(open(cpath, newline="", encoding="utf-8-sig")):
        if r.get("region") != REGION:
            continue
        a = (r.get("account_id") or "").strip()
        if a.isdigit():
            seeds.append(int(a))
    seeds = sorted(set(seeds))[:SEEDS]
    if not seeds:
        raise SystemExit("no ceiling accounts for %s" % REGION)

    report = {"region": REGION, "lookback_days": LOOKBACK_DAYS,
              "orbit0": len(seeds)}
    print("[orbit 0] %d ceiling accounts" % len(seeds), file=sys.stderr)

    orbits = [set(seeds)]
    seen = set(seeds)
    for k in range(1, ORBITS + 1):
        print("[orbit %d] expanding from %d accounts" % (k, len(orbits[-1])),
              file=sys.stderr)
        got = expand(orbits[-1], "orbit%d" % k)
        fresh = got - seen
        seen |= got
        orbits.append(fresh)
        report["orbit%d" % k] = {"reached": len(got), "new": len(fresh),
                                 "cumulative": len(seen)}
        print("  [orbit %d] %d reached, %d new, %d cumulative"
              % (k, len(got), len(fresh), len(seen)), file=sys.stderr)
        if not fresh:
            break

    report["sql_calls"] = _sql_calls[0]
    report["growth"] = [len(o) for o in orbits]

    # ---- quality by orbit ------------------------------------------------
    print("[quality] hero-stats per orbit", file=sys.stderr)
    q = {}
    sampled = set()
    for k, o in enumerate(orbits):
        s = sorted(o)[:QUALITY_SAMPLE]
        sampled |= set(s)
    stats = hero_stats(sampled)
    for k, o in enumerate(orbits):
        s = set(sorted(o)[:QUALITY_SAMPLE])
        q["orbit%d" % k] = quality(s, stats)
    report["quality"] = q

    # ---- coverage of thin hero-regions ----------------------------------
    thin = []
    for r in csv.DictReader(open(cpath, newline="", encoding="utf-8-sig")):
        if r.get("region") != REGION:
            continue
        try:
            if int(r.get("scored_candidates") or 0) < 8:
                thin.append((int(r["hero_id"]), r["hero"],
                             int(r.get("scored_candidates") or 0)))
        except ValueError:
            continue
    cov = []
    for hid, hero, cur in thin:
        n = sum(1 for (a, h), rec in stats.items()
                if h == hid and rec["games"] >= 5 and a in sampled)
        cov.append({"hero": hero, "current": cur, "in_orbit_sample": n})
    report["thin_coverage"] = cov

    json.dump(report, open(os.path.join(OUT, "orbits.json"), "w"), indent=2)

    print("\n=== Q1  orbit sizes (%s, %d-day window) ===" % (REGION, LOOKBACK_DAYS))
    print("  orbit 0 (seeds)   %6d" % len(orbits[0]))
    for k in range(1, len(orbits)):
        b = report["orbit%d" % k]
        print("  orbit %d           %6d new   (%d reached, %d cumulative)"
              % (k, b["new"], b["reached"], b["cumulative"]))
    print("  SQL calls used: %d" % _sql_calls[0])

    print("\n=== Q2  does standing decay with distance? ===")
    print("  %-9s %-8s %-9s %-9s %-9s %s"
          % ("orbit", "sampled", "med games", "med wr", "p90 wr", ">60%"))
    for k in range(len(orbits)):
        b = q["orbit%d" % k]
        print("  %-9d %-8s %-9s %-9s %-9s %s"
              % (k, b["with_ranked_games"], b["median_games"], b["median_winrate"],
                 b["p90_winrate"], b["over_60pct"]))
    print("  -> orbit 2 looking like orbit 1 means distance carries no signal")

    print("\n=== Q3  thin hero-regions, candidates in the orbit sample ===")
    for c in sorted(cov, key=lambda c: c["current"])[:12]:
        print("  %-14s current %-3d  in sample %d" % (c["hero"], c["current"],
                                                      c["in_orbit_sample"]))
    print("\nwrote %s/orbits.json" % OUT)


if __name__ == "__main__":
    main()
