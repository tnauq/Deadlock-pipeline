#!/usr/bin/env python3
"""
Ceiling ranking: order heroes by their single strongest player.

The per-hero leaderboard gives position WITHIN a hero, so it cannot compare
heroes against each other — every hero has a rank 1. The cross-hero board for a
region gives one ordering over all players, and positions are unique, so the
13 heroes whose best player is pinned at the mmr ceiling separate cleanly.

    python3 ceiling_rank.py

Reads  ./output/candidates.csv, ./output/tierlist.csv
Writes ./output/ceiling.csv

Two requests total (one per region). No API key needed.
"""

import csv
import json
import os
import sys
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
REGIONS = ["NAmerica", "Europe"]      # enum values match candidates.csv exactly
OUT_DIR = "output"

# How to compare a position in one region against a position in another.
#   "percentile" — position / region_depth. Neutral, but assumes the two
#                  ladders are equally deep in talent, not just in headcount.
#   "raw"        — compare positions directly, then TIEBREAK_REGION wins ties.
CROSS_REGION = os.environ.get("CROSS_REGION") or "percentile"
TIEBREAK_REGION = os.environ.get("TIEBREAK_REGION") or "Europe"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-ceiling/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_board(region):
    """Return (by_account_id, by_name, depth) for one region's cross-hero board."""
    data = get("%s/v1/leaderboard/%s" % (BASE, region))
    entries = data.get("entries") if isinstance(data, dict) else data
    if not entries:
        raise SystemExit("no entries for %s; response keys: %s"
                         % (region, list(data)[:8] if isinstance(data, dict) else type(data)))

    by_id, by_name = {}, defaultdict(list)
    for i, e in enumerate(entries):
        # 'rank' is the board position; fall back to order if it's ever null
        pos = e.get("rank")
        pos = int(pos) if pos else i + 1
        rec = {
            "pos": pos,
            "name": e.get("account_name"),
            "badge": e.get("badge_level"),
            "ranked_rank": e.get("ranked_rank"),
            "top_heroes": e.get("top_hero_ids") or [],
        }
        for aid in (e.get("possible_account_ids") or []):
            # a lower position wins if an id somehow appears twice
            if aid and (aid not in by_id or pos < by_id[aid]["pos"]):
                by_id[int(aid)] = rec
        if rec["name"]:
            by_name[rec["name"]].append(rec)

    print("  [board] %-9s %d entries, depth %d"
          % (region, len(entries), max(r["pos"] for r in by_id.values()) if by_id else 0),
          file=sys.stderr)
    return by_id, by_name, len(entries)


def locate(row, boards):
    """Find a pool member on their region's cross-hero board.

    Exact = the account id the pipeline resolved appears in that entry's
    possible_account_ids. Name-only matches are returned but flagged, and a
    name matching several entries is refused rather than guessed.
    """
    by_id, by_name, _ = boards[row["region"]]
    aid = int(row["account_id"])
    if aid in by_id:
        return by_id[aid], "exact"
    hits = by_name.get(row["account_name"] or "", [])
    if len(hits) == 1:
        return hits[0], "name"
    if len(hits) > 1:
        return None, "ambiguous_name"
    return None, "absent"


def main():
    cands = list(csv.DictReader(open(os.path.join(OUT_DIR, "candidates.csv"))))
    tier = {r["hero"]: r for r in csv.DictReader(open(os.path.join(OUT_DIR, "tierlist.csv")))}

    print("[1/2] leaderboards", file=sys.stderr)
    boards = {}
    for region in REGIONS:
        boards[region] = fetch_board(region)
    depth = {r: boards[r][2] for r in REGIONS}

    print("[2/2] locating pool members", file=sys.stderr)
    by_hero = defaultdict(list)
    tally = defaultdict(int)
    for row in cands:
        if row["region"] not in boards:
            continue
        rec, how = locate(row, boards)
        tally[how] += 1
        if rec:
            by_hero[row["hero"]].append((row, rec))
    print("  [match] " + "  ".join("%s=%d" % kv for kv in sorted(tally.items())),
          file=sys.stderr)

    def key(pair):
        """Lower is better. This is the whole cross-region decision."""
        row, rec = pair
        if CROSS_REGION == "percentile":
            return (rec["pos"] / max(depth[row["region"]], 1), 0)
        # raw positions, with the tiebreak region winning exact ties
        return (rec["pos"], 0 if row["region"] == TIEBREAK_REGION else 1)

    out = []
    for hero, pairs in by_hero.items():
        pairs.sort(key=key)
        row, rec = pairs[0]
        t = tier.get(hero, {})
        hid = int(t.get("hero_id") or row["hero_id"])
        out.append({
            "hero": hero,
            "hero_id": hid,
            "ceiling_player": rec["name"] or row["account_name"],
            "region": row["region"],
            "global_pos": rec["pos"],
            "region_depth": depth[row["region"]],
            "pct": round(100.0 * rec["pos"] / max(depth[row["region"]], 1), 3),
            "badge_level": rec["badge"],
            "hero_ladder_pos": row["ladder_pos"],
            "mmr": row["mmr"],
            # Valve's own view of what this account mains — a free sanity check
            # on the one-trick assignment, not used in the ordering
            "valve_top_hero": "YES" if hid in (rec["top_heroes"] or []) else "",
            "pool_located": len(pairs),
            "winrate_rank": t.get("rank", ""),
            "elite_winrate": t.get("elite_winrate", ""),
        })

    out.sort(key=lambda d: d["pct"] if CROSS_REGION == "percentile" else d["global_pos"])
    for i, d in enumerate(out, 1):
        d["ceiling_rank"] = i

    missing = sorted(set(tier) - {d["hero"] for d in out})
    if missing:
        print("  [warn] no located pool member for: %s" % ", ".join(missing), file=sys.stderr)

    cols = ["ceiling_rank", "hero", "hero_id", "ceiling_player", "region", "global_pos",
            "region_depth", "pct", "badge_level", "hero_ladder_pos", "mmr",
            "valve_top_hero", "pool_located", "winrate_rank", "elite_winrate"]
    path = os.path.join(OUT_DIR, "ceiling.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    print("  -> %s (%d heroes, cross_region=%s)" % (path, len(out), CROSS_REGION),
          file=sys.stderr)

    print("\n  %-4s %-12s %-16s %-8s %6s %7s" %
          ("#", "hero", "ceiling player", "region", "pos", "pct"), file=sys.stderr)
    for d in out[:12]:
        print("  %-4d %-12s %-16s %-8s %6d %6.2f%%" %
              (d["ceiling_rank"], d["hero"][:12], (d["ceiling_player"] or "?")[:16],
               d["region"][:8], d["global_pos"], d["pct"]), file=sys.stderr)


if __name__ == "__main__":
    main()
