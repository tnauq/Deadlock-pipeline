#!/usr/bin/env python3
"""
probe_ceiling_delta.py — who does dropping the general-board requirement let in?

Measured 2026-08-07: dropping it takes the candidate pool from 813 to 2,112,
median per hero-region from 9.5 to 23.5, and hero-regions with under five
candidates from 11 to zero. The new set is a SUPERSET of the old, so a ceiling
can only move upward — it changed on 39 of 76.

The risk is specific and worth measuring rather than arguing about: net wins
rewards VOLUME. A lower-ranked player who grinds 300 ranked games at 55% has
more net wins than a genuinely top player with 120 games at 60%. The old rule
excluded anyone outside the region's top 1,000 by construction; the new one
does not, so it could promote grinders over the actual ceiling.

For every hero-region where the ceiling changed, this reports both players
side by side:

  * position on the HERO's own board — a real ceiling player should sit near
    the top of the board they were drawn from
  * whether they appear on the region's general board at all, and where —
    absent means outside the region's top 1,000
  * games, win rate and net wins, so volume and rate can be separated

Decision rule this is meant to inform: if new ceilings are mostly high-board,
present-on-general players, the old rule was hiding them and the swap is safe.
If they are mostly absent from the general board with large game counts, net
wins is buying volume and the general-board floor was doing real work.

Cost: ZERO SQL. Leaderboard and hero-stats only.

    python3 probe_ceiling_delta.py

Writes probe_out/ceiling_delta.json. Stdlib only.
"""

import csv
import json
import os
import statistics
import sys
import time
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
MAX_IDS_PER_ENTRY = int(os.environ.get("MAX_IDS_PER_ENTRY") or 2)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


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


def hero_stats(ids):
    ids = sorted(set(int(a) for a in ids if a))
    out = {}
    if not ids:
        return out
    base = "%s/v1/players/hero-stats?match_mode=%s" % (BASE, urllib.parse.quote(MATCH_MODE))
    chunk = max(20, (MAX_URL - len(base) - 40) // 24)
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            rows = get(base + "".join("&account_ids=%d" % a for a in part))
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


def main():
    os.makedirs(OUT, exist_ok=True)
    # Prefer the pipeline's hero list, but fall back to /v1/assets/heroes so
    # this stays runnable on a free_only run, where output/ does not exist.
    heroes = {}
    tier_path = os.path.join("output", "tierlist.csv")
    if os.path.exists(tier_path):
        for r in csv.DictReader(open(tier_path, newline="", encoding="utf-8-sig")):
            try:
                heroes[int(r["hero_id"])] = r["hero"]
            except (KeyError, ValueError):
                continue
    if not heroes:
        print("  [heroes] no tierlist.csv — using /v1/assets/heroes", file=sys.stderr)
        for h in get(BASE + "/v1/assets/heroes") or []:
            hid = h.get("id", h.get("hero_id"))
            if hid is None or h.get("disabled"):
                continue
            heroes[int(hid)] = h.get("name") or ("hero_%s" % hid)
    if not heroes:
        raise SystemExit("could not source a hero list")

    changed, unchanged = [], 0
    all_new_pos, all_old_pos = [], []

    for rg in REGIONS:
        print("[%s] boards" % rg, file=sys.stderr)
        gen = board(rg)
        gen_by_name = defaultdict(list)
        gen_pos_by_id = {}
        for e in gen:
            gen_by_name[e["name"]].append(e)
            for a in e["ids"][:MAX_IDS_PER_ENTRY]:
                gen_pos_by_id.setdefault(int(a), e["pos"])

        per_hero = {}
        want = set()
        for hid, hero in sorted(heroes.items(), key=lambda kv: kv[1]):
            hb = board(rg, hid)
            time.sleep(0.15)
            if not hb:
                continue
            old, new = {}, {}
            for he in hb:
                hids = he["ids"][:MAX_IDS_PER_ENTRY]
                for a in hids:
                    new.setdefault(int(a), he)
                for cand in gen_by_name.get(he["name"], []):
                    common = set(hids) & set(cand["ids"])
                    if common:
                        old.setdefault(min(common), he)
                        break
            per_hero[hid] = (old, new, len(hb))
            want |= set(new)

        print("  hero-stats for %d ids" % len(want), file=sys.stderr)
        stats = hero_stats(want)

        for hid, (old, new, bsize) in per_hero.items():
            def top(pool):
                best = None
                for a, he in pool.items():
                    rec = stats.get((a, hid))
                    if not rec or not rec["games"]:
                        continue
                    net = 2 * rec["wins"] - rec["games"]
                    key = (-net, he["pos"])
                    if best is None or key < best[0]:
                        best = (key, a, he, rec)
                return best
            bo, bn = top(old), top(new)
            if not bo or not bn:
                continue
            if bo[1] == bn[1]:
                unchanged += 1
                continue
            def pack(b):
                _k, a, he, rec = b
                return {"account_id": a, "name": he["name"], "hero_pos": he["pos"],
                        "general_pos": gen_pos_by_id.get(a),
                        "games": rec["games"], "wins": rec["wins"],
                        "winrate": round(rec["wins"] / rec["games"], 3),
                        "net": 2 * rec["wins"] - rec["games"]}
            rec = {"region": rg, "hero": heroes[hid], "board_size": bsize,
                   "old": pack(bo), "new": pack(bn)}
            changed.append(rec)
            all_new_pos.append(bn[2]["pos"])
            all_old_pos.append(bo[2]["pos"])

    report = {"changed": changed, "unchanged": unchanged}
    n_on_gen = sum(1 for c in changed if c["new"]["general_pos"])
    report["summary"] = {
        "changed": len(changed), "unchanged": unchanged,
        "new_on_general_board": n_on_gen,
        "new_absent_from_general": len(changed) - n_on_gen,
        "median_new_hero_pos": statistics.median(all_new_pos) if all_new_pos else None,
        "median_old_hero_pos": statistics.median(all_old_pos) if all_old_pos else None,
        "median_new_games": statistics.median([c["new"]["games"] for c in changed]) if changed else None,
        "median_old_games": statistics.median([c["old"]["games"] for c in changed]) if changed else None,
        "median_new_winrate": statistics.median([c["new"]["winrate"] for c in changed]) if changed else None,
        "median_old_winrate": statistics.median([c["old"]["winrate"] for c in changed]) if changed else None,
        "new_worse_winrate": sum(1 for c in changed if c["new"]["winrate"] < c["old"]["winrate"]),
    }
    json.dump(report, open(os.path.join(OUT, "ceiling_delta.json"), "w"), indent=1)

    s = report["summary"]
    print("\n=== ceilings that changed: %d (unchanged %d) ===" % (s["changed"], s["unchanged"]))
    print("  new player IS on the general board : %d" % s["new_on_general_board"])
    print("  new player is NOT (outside top1000): %d" % s["new_absent_from_general"])
    print("  median hero-board position   old %s -> new %s"
          % (s["median_old_hero_pos"], s["median_new_hero_pos"]))
    print("  median ranked games          old %s -> new %s"
          % (s["median_old_games"], s["median_new_games"]))
    print("  median win rate              old %s -> new %s"
          % (s["median_old_winrate"], s["median_new_winrate"]))
    print("  new ceiling has a WORSE win rate than the old on %d of %d"
          % (s["new_worse_winrate"], s["changed"]))

    print("\n  %-13s %-9s %-22s %-22s" % ("hero", "region", "old", "new"))
    for c in changed[:14]:
        o, n = c["old"], c["new"]
        print("  %-13s %-9s %-22s %-22s" % (
            c["hero"][:13], c["region"],
            "h%-3d g%-3d wr%.2f n%-3d" % (o["hero_pos"], o["games"], o["winrate"], o["net"]),
            "h%-3d g%-3d wr%.2f n%-3d%s" % (n["hero_pos"], n["games"], n["winrate"], n["net"],
                                            "" if n["general_pos"] else " OFF-BOARD")))
    print("\n  h = hero-board position, g = ranked games, wr = win rate, n = net wins")
    print("\nwrote %s/ceiling_delta.json" % OUT)


if __name__ == "__main__":
    main()
