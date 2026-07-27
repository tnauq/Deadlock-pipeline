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

# Accept only name+id confirmed matches. Set STRICT=0 to also allow entries
# whose name is unique on the board but whose candidate id list omits our id.
STRICT = (os.environ.get("STRICT") or "1") != "0"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-ceiling/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_board(region):
    """Return (entries_by_name, depth) for one region's cross-hero board.

    Position is the LIST INDEX, not the 'rank' field. Measured on the live
    board: 1001 entries carry only 634 distinct 'rank' values, and rank 1 is
    shared by 14 players, but the list is monotonic in rank — so list order is
    the real ordering and 'rank' is a tied/rounded label.
    """
    data = get("%s/v1/leaderboard/%s" % (BASE, region))
    entries = data.get("entries") if isinstance(data, dict) else data
    if not entries:
        raise SystemExit("no entries for %s; response keys: %s"
                         % (region, list(data)[:8] if isinstance(data, dict) else type(data)))

    by_name = defaultdict(list)
    for i, e in enumerate(entries):
        name = e.get("account_name")
        if not name:
            continue
        by_name[name].append({
            "pos": i + 1,
            "name": name,
            "badge": e.get("badge_level"),
            "ranked_rank": e.get("ranked_rank"),
            "top_heroes": e.get("top_hero_ids") or [],
            "ids": {int(a) for a in (e.get("possible_account_ids") or []) if a},
        })

    print("  [board] %-9s %d entries, %d named, depth %d"
          % (region, len(entries), sum(len(v) for v in by_name.values()), len(entries)),
          file=sys.stderr)
    return by_name, len(entries)


def locate(row, boards):
    """Find a pool member on their region's cross-hero board.

    possible_account_ids is a CANDIDATE list, not an identity — the 1001 NA
    entries between them reference ~34,700 distinct ids, and one name can carry
    30+. Matching on it alone put the wrong player in 110 of 371 slots. So an
    accepted match needs BOTH the display name and the id to agree.
    """
    by_name, _ = boards[row["region"]]
    aid = int(row["account_id"])
    named = by_name.get(row["account_name"] or "", [])
    if not named:
        return None, "name_absent"

    confirmed = [r for r in named if aid in r["ids"]]
    if len(confirmed) == 1:
        return confirmed[0], "confirmed"
    if len(confirmed) > 1:
        # same name, several entries, all listing this id — cannot separate them
        return None, "ambiguous"
    if len(named) == 1:
        # name is unique on the board but the id isn't in its candidate list
        return named[0], "name_only"
    return None, "unresolved"


def main():
    cands = list(csv.DictReader(open(os.path.join(OUT_DIR, "candidates.csv"))))
    tier = {r["hero"]: r for r in csv.DictReader(open(os.path.join(OUT_DIR, "tierlist.csv")))}

    print("[1/2] leaderboards", file=sys.stderr)
    boards = {}
    for region in REGIONS:
        boards[region] = fetch_board(region)
    depth = {r: boards[r][1] for r in REGIONS}

    print("[2/2] locating pool members", file=sys.stderr)
    by_hero = defaultdict(list)
    tally = defaultdict(int)
    hits = []
    for row in cands:
        if row["region"] not in boards:
            continue
        rec, how = locate(row, boards)
        tally[how] += 1
        if rec and not (STRICT and how == "name_only"):
            hits.append((row, rec, how))

    # One board entry is one player. Two pool accounts sharing a display name
    # can both confirm against the same entry, because that entry's candidate
    # list holds both ids — which made one name the ceiling for two heroes.
    # Neither claim can be preferred, so drop both.
    claimed = defaultdict(list)
    for row, rec, how in hits:
        claimed[(row["region"], rec["pos"])].append(row)
    contested = {k for k, v in claimed.items()
                 if len({r["account_id"] for r in v}) > 1}
    for row, rec, how in hits:
        if (row["region"], rec["pos"]) in contested:
            tally["contested_entry"] += 1
            continue
        by_hero[row["hero"]].append((row, rec, how))
    if contested:
        print("  [match] dropped %d entries claimed by more than one pool account"
              % len(contested), file=sys.stderr)
    print("  [match] " + "  ".join("%s=%d" % kv for kv in sorted(tally.items())),
          file=sys.stderr)
    if STRICT:
        print("  [match] strict mode: name_only matches excluded", file=sys.stderr)

    # Each region is ranked against its own board. Nothing is compared across
    # regions, so there is no percentile normalisation and no tiebreak convention.
    per_region = defaultdict(list)
    for hero, pairs in by_hero.items():
        best = defaultdict(list)
        for row, rec, how in pairs:
            best[row["region"]].append((row, rec, how))
        for rg, lst in best.items():
            lst.sort(key=lambda p: p[1]["pos"])
            row, rec, how = lst[0]
            t = tier.get(hero, {})
            hid = int(t.get("hero_id") or row["hero_id"])
            per_region[rg].append({
                "hero": hero,
                "hero_id": hid,
                "region": rg,
                "ceiling_player": rec["name"] or row["account_name"],
                "global_pos": rec["pos"],
                "region_depth": depth[rg],
                "pct": round(100.0 * rec["pos"] / max(depth[rg], 1), 3),
                "badge_level": rec["badge"],
                "hero_ladder_pos": row["ladder_pos"],
                "mmr": row["mmr"],
                "match": how,
                # Valve's own view of what this account mains — a sanity check
                # on the one-trick assignment, not used in the ordering
                "valve_top_hero": "YES" if hid in (rec["top_heroes"] or []) else "",
                "pool_located": len(lst),
                "winrate_rank": t.get("rank", ""),
                "elite_winrate": t.get("elite_winrate", ""),
            })

    out = []
    for rg in REGIONS:
        rows = sorted(per_region.get(rg, []), key=lambda d: d["global_pos"])
        for i, d in enumerate(rows, 1):
            d["ceiling_rank"] = i
        out.extend(rows)

    for region in REGIONS:
        have = {d["hero"] for d in out if d["region"] == region}
        gap = sorted(set(tier) - have)
        if gap:
            print("  [warn] %s: no located pool member for %d heroes: %s"
                  % (region, len(gap), ", ".join(gap)), file=sys.stderr)

    cols = ["region", "ceiling_rank", "hero", "hero_id", "ceiling_player", "global_pos",
            "region_depth", "pct", "badge_level", "hero_ladder_pos", "mmr", "match",
            "valve_top_hero", "pool_located", "winrate_rank", "elite_winrate"]
    path = os.path.join(OUT_DIR, "ceiling.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    print("  -> %s (%d hero-region rows)" % (path, len(out)), file=sys.stderr)

    for region in REGIONS:
        rows = [d for d in out if d["region"] == region][:10]
        if not rows:
            continue
        print("\n  %s — top 10 by ceiling (%d heroes ranked)"
              % (region, sum(1 for d in out if d["region"] == region)), file=sys.stderr)
        print("  %-4s %-12s %-16s %6s %-10s %s" %
              ("#", "hero", "ceiling player", "pos", "match", "wr#"), file=sys.stderr)
        for d in rows:
            print("  %-4d %-12s %-16s %6d %-10s %s" %
                  (d["ceiling_rank"], d["hero"][:12], (d["ceiling_player"] or "?")[:16],
                   d["global_pos"], d["match"], d["winrate_rank"]), file=sys.stderr)
    dup = defaultdict(list)
    for d in out:
        dup[(d["ceiling_player"], d["region"])].append(d["hero"])
    shared = {k: v for k, v in dup.items() if len(v) > 1}
    if shared:
        print("\n  [warn] one player is the ceiling for several heroes:", file=sys.stderr)
        for (nm, reg), hs in shared.items():
            print("    %-16s %-8s %s" % (nm, reg, ", ".join(hs)), file=sys.stderr)


if __name__ == "__main__":
    main()
