#!/usr/bin/env python3
"""
probe_pool_expansion.py — two ways to fix thin hero-regions, measured.

The problem, from ceiling.csv on 2026-08-07:

    2,881 hero-board entries
    1,205 located on the general board   (42%)
      889 with ranked games              (31%)

    median 10 scored candidates per hero-region, but 7 of 76 have fewer
    than 5 — Infernus EU has exactly ONE, which is why its ceiling reads -7.

Two candidate fixes:

A. DROP THE GENERAL-BOARD REQUIREMENT (free, no SQL).
   The cross-reference exists only to confirm identity: `possible_account_ids`
   is deadlock-api's own fuzzy name resolution, and name-only matching put the
   wrong player in 110 of 371 slots. But it costs 58% of the pool, and it no
   longer contributes to the ORDERING now that net wins does that.

   A different confirmation is available and arguably stronger: ask
   hero-stats whether a candidate id has RANKED GAMES ON THAT HERO. An id
   that plays the hero whose board it appeared on is evidence of the right
   account, and unlike a name match it cannot be confounded by two players
   sharing a display name.

   This measures what the pool becomes, and how often the two rules disagree
   about who the ceiling player is — the number that decides whether the
   swap is safe.

B. LOBBY EXPANSION (~2 SQL calls).
   A ceiling player's ranked lobby holds 11 others matched against them.
   Volume was already measured — 498 distinct mates from 12 seeds over 3 days,
   238 recurring — but comparability was NOT, because the test depended on
   player_score, which is gone. Net wins gives a way to test it now.

   The claim under test is that lobby-mates are of comparable standing. A wide
   spread means the lobby is not a comparable population and the idea should
   be dropped rather than quietly widening the cohort.

    python3 probe_pool_expansion.py          # both parts
    POOL_SKIP_SQL=1 python3 probe_pool_expansion.py   # part A only, free

Writes probe_out/pool_expansion.json. Stdlib only.
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
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
MATCH_MODE = os.environ.get("MATCH_MODE") or "Ranked"
MAX_URL = int(os.environ.get("MAX_URL") or 9000)
SKIP_SQL = os.environ.get("POOL_SKIP_SQL") == "1"
SEEDS = int(os.environ.get("PROBE_SEEDS") or 12)
LOOKBACK_DAYS = int(os.environ.get("PROBE_LOOKBACK_DAYS") or 3)
MAX_IDS_PER_ENTRY = int(os.environ.get("MAX_IDS_PER_ENTRY") or 2)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def sql(q, label="", tries=4):
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)
    print("  [sql] %s (%d char url)" % (label, len(url)), file=sys.stderr)
    for attempt in range(tries):
        try:
            rows = get(url)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < tries - 1:
                wait = 35
                try:
                    wait = int(json.loads(body)["error"].get("next_request_in", 33)) + 2
                except Exception:
                    pass
                print("  [sql] 429, waiting %ds" % wait, file=sys.stderr)
                time.sleep(wait)
                continue
            raise SystemExit("SQL failed (%s): %s" % (e.code, body[:300]))
    else:
        raise SystemExit("SQL still rate limited")
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] %d rows" % len(rows), file=sys.stderr)
    return rows


def hero_stats(ids):
    """(account_id, hero_id) -> {games, wins} over ranked play."""
    ids = sorted(set(int(a) for a in ids if a))
    out = {}
    if not ids:
        return out
    base = "%s/v1/players/hero-stats?match_mode=%s" % (BASE, urllib.parse.quote(MATCH_MODE))
    chunk = max(20, (MAX_URL - len(base) - 40) // 24)
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        url = base + "".join("&account_ids=%d" % a for a in part)
        try:
            rows = get(url)
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


def net_of(recs):
    g = sum(v["games"] for v in recs)
    w = sum(v["wins"] for v in recs)
    return 2 * w - g, g, w


def board(region, hero_id=None):
    url = "%s/v1/leaderboard/%s" % (BASE, region)
    if hero_id is not None:
        url += "/%d" % hero_id
    try:
        data = get(url)
    except Exception as e:
        print("  [lb] %s %s -> %s" % (region, hero_id, e), file=sys.stderr)
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    out = []
    for i, e in enumerate(entries or [], 1):
        nm = e.get("account_name")
        if not nm:
            continue
        out.append({"pos": i, "name": nm,
                    "ids": [int(a) for a in (e.get("possible_account_ids") or []) if a]})
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}

    tier_path = os.path.join("output", "tierlist.csv")
    if not os.path.exists(tier_path):
        raise SystemExit("no output/tierlist.csv — run the pipeline first")
    heroes = {}
    for r in csv.DictReader(open(tier_path, newline="", encoding="utf-8-sig")):
        try:
            heroes[int(r["hero_id"])] = r["hero"]
        except (KeyError, ValueError):
            continue

    # ---- A. what does dropping the general board give? ------------------
    print("[A] pool with and without the general-board requirement", file=sys.stderr)
    per_hr = {}
    for rg in REGIONS:
        gen = board(rg)
        gen_by_name = defaultdict(list)
        for e in gen:
            gen_by_name[e["name"]].append(e)
        for hid, hero in sorted(heroes.items(), key=lambda kv: kv[1]):
            hb = board(rg, hid)
            time.sleep(0.15)
            if not hb:
                continue
            # OLD rule: name on both boards AND ids intersect
            old_ids, new_ids = set(), set()
            for he in hb:
                hids = set(he["ids"][:MAX_IDS_PER_ENTRY])
                for cand in gen_by_name.get(he["name"], []):
                    common = hids & set(cand["ids"])
                    if common:
                        old_ids.add(min(common))
                        break
                # NEW rule: every candidate id, confirmed later by whether it
                # has ranked games ON THIS HERO
                new_ids |= hids
            per_hr[(rg, hid)] = {"hero": hero, "board": len(hb),
                                 "old_ids": old_ids, "new_ids": new_ids}
        print("  [A] %s done" % rg, file=sys.stderr)

    every = set()
    for v in per_hr.values():
        every |= v["new_ids"]
    print("  [A] hero-stats for %d candidate ids" % len(every), file=sys.stderr)
    stats = hero_stats(every)

    rowsA, disagree = [], 0
    for (rg, hid), v in per_hr.items():
        def scored(ids):
            out = []
            for a in ids:
                rec = stats.get((a, hid))
                if rec and rec["games"]:
                    out.append((2 * rec["wins"] - rec["games"], a, rec))
            out.sort(key=lambda t: -t[0])
            return out
        old = scored(v["old_ids"])
        new = scored(v["new_ids"])
        if old and new and old[0][1] != new[0][1]:
            disagree += 1
        rowsA.append({"region": rg, "hero": v["hero"], "board": v["board"],
                      "old_scored": len(old), "new_scored": len(new),
                      "old_net": old[0][0] if old else None,
                      "new_net": new[0][0] if new else None,
                      "same_ceiling": bool(old and new and old[0][1] == new[0][1])})
    report["A_rows"] = rowsA
    o = [r["old_scored"] for r in rowsA]
    n = [r["new_scored"] for r in rowsA]
    report["A_summary"] = {
        "hero_regions": len(rowsA),
        "old_total": sum(o), "new_total": sum(n),
        "old_median": statistics.median(o), "new_median": statistics.median(n),
        "old_under5": sum(1 for x in o if x < 5),
        "new_under5": sum(1 for x in n if x < 5),
        "ceiling_changed": disagree,
    }

    # ---- B. are lobby-mates comparable? ---------------------------------
    if not SKIP_SQL:
        print("[B] lobby expansion", file=sys.stderr)
        seeds = []
        cpath = os.path.join("output", "ceiling.csv")
        if os.path.exists(cpath):
            for r in csv.DictReader(open(cpath, newline="", encoding="utf-8-sig")):
                a = (r.get("account_id") or "").strip()
                if a.isdigit():
                    seeds.append(int(a))
        seeds = sorted(set(seeds))[:SEEDS]
        if seeds:
            time.sleep(35)
            q = ("SELECT match_id, account_id FROM match_player WHERE match_id IN ("
                 "SELECT match_id FROM match_player WHERE account_id IN (%s) "
                 "AND match_mode = 'Ranked' AND game_mode = 'Normal' "
                 "AND start_time >= now() - INTERVAL %d DAY)"
                 % (",".join(str(s) for s in seeds), LOOKBACK_DAYS))
            rows = sql(q, "rosters")
            mates = set()
            matches = set()
            for r in rows:
                matches.add(int(r["match_id"]))
                a = int(r["account_id"])
                if a not in seeds:
                    mates.add(a)
            mate_stats = hero_stats(list(mates)[:600])
            by_acct = defaultdict(list)
            for (a, _h), rec in mate_stats.items():
                by_acct[a].append(rec)
            seed_stats = hero_stats(seeds)
            by_seed = defaultdict(list)
            for (a, _h), rec in seed_stats.items():
                by_seed[a].append(rec)
            sn = [net_of(v)[0] for v in by_seed.values()]
            mn = [net_of(v)[0] for v in by_acct.values()]
            report["B"] = {
                "seeds": len(seeds), "matches": len(matches),
                "distinct_mates": len(mates), "mates_scored": len(mn),
                "seed_net": {"min": min(sn), "median": statistics.median(sn),
                             "max": max(sn)} if sn else None,
                "mate_net": {"min": min(mn), "median": statistics.median(mn),
                             "max": max(mn)} if mn else None,
                "mates_at_or_above_seed_floor": sum(1 for x in mn if sn and x >= min(sn)),
                "mates_above_seed_median": sum(1 for x in mn if sn and x >= statistics.median(sn)),
            }
    else:
        report["B"] = {"skipped": "POOL_SKIP_SQL=1"}

    json.dump(report, open(os.path.join(OUT, "pool_expansion.json"), "w"),
              indent=1, default=str)

    a = report["A_summary"]
    print("\n=== A  dropping the general-board requirement ===")
    print("  scored candidates   old %5d   ->  new %5d" % (a["old_total"], a["new_total"]))
    print("  median per hero-reg old %5.1f   ->  new %5.1f" % (a["old_median"], a["new_median"]))
    print("  hero-regions <5     old %5d   ->  new %5d" % (a["old_under5"], a["new_under5"]))
    print("  ceiling player changes on %d of %d hero-regions"
          % (a["ceiling_changed"], a["hero_regions"]))
    print("\n  thinnest under the NEW rule:")
    for r in sorted(rowsA, key=lambda r: r["new_scored"])[:6]:
        print("    %-13s %-9s board %-4d old %-3d new %-3d" %
              (r["hero"], r["region"], r["board"], r["old_scored"], r["new_scored"]))

    b = report.get("B") or {}
    print("\n=== B  lobby expansion ===")
    if b.get("skipped"):
        print("  skipped (%s)" % b["skipped"])
    else:
        print("  %s seeds -> %s matches -> %s mates (%s scored)"
              % (b["seeds"], b["matches"], b["distinct_mates"], b["mates_scored"]))
        print("  seed net wins: %s" % b["seed_net"])
        print("  mate net wins: %s" % b["mate_net"])
        print("  mates at or above the LOWEST seed: %s" % b["mates_at_or_above_seed_floor"])
        print("  mates above the seed MEDIAN:       %s" % b["mates_above_seed_median"])
        print("\n  -> a mate distribution close to the seeds supports using them as")
        print("     candidates; far below means the lobby is not comparable.")

    print("\nwrote %s/pool_expansion.json" % OUT)


if __name__ == "__main__":
    main()
