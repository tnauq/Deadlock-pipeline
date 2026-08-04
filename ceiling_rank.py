#!/usr/bin/env python3
"""
Ceiling ranking: order heroes by their single strongest player.

The per-hero leaderboard gives position WITHIN a hero, so it cannot compare
heroes against each other — every hero has a rank 1. The cross-hero board for a
region gives one ordering over all players, so a hero's ceiling is its best
board player's position on the cross-hero board.

This CROSS-REFERENCES the two boards directly: every player on a hero's board,
located on the region's cross-hero board, lowest position wins. Until
2026-08-03 it instead located the pipeline's chosen 20-player pool, so any
hero-board player who failed identity resolution or fell outside the per-region
cap was invisible however highly they ranked. Measured across 12 hero-regions,
that missed the true ceiling on 4 — worst case Mirage NA, reported at position
379 when the hero's top player sat at position 2.

    python3 ceiling_rank.py

Reads  ./output/tierlist.csv (for hero ids and the win-rate reference columns)
Writes ./output/ceiling.csv

Requests: one cross-hero board per region, plus one board per hero per region
(~78 total). The leaderboard endpoint is 100 req/s and a separate bucket from
/v1/sql, so this costs nothing against the SQL budget. No API key needed.
"""

import csv
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
OUT_DIR = "output"

# Matching is always name+id confirmed now: an entry counts only when the same
# display name appears on both boards AND their candidate id lists intersect.
# The old STRICT toggle relaxed this to name-only, which is unsafe here — the
# cross-hero board carries ~40,000 distinct candidate ids across 1,000 entries.

# How many board entries to keep for the archive (player bar-chart-race data).
# Costs no extra API calls — the full board is already fetched.
BOARD_ARCHIVE_TOP = int(os.environ.get("BOARD_ARCHIVE_TOP") or 100)


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

    ordered = []          # raw board order, kept for the archive
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
        ordered.append({
            "pos": i + 1,
            "name": name,
            "top_heroes": e.get("top_hero_ids") or [],
            # CANDIDATE list, not an identity — one name can carry 30+ ids.
            # Only trustworthy once cross-checked against a resolved pool
            # member, which archive_snapshot.py does.
            "ids": [int(a) for a in (e.get("possible_account_ids") or []) if a][:8],
        })

    print("  [board] %-9s %d entries, %d named, depth %d"
          % (region, len(entries), sum(len(v) for v in by_name.values()), len(entries)),
          file=sys.stderr)
    return by_name, len(entries), ordered


def fetch_hero_board(region, hero_id):
    """One hero's board for one region. Position is the list index.

    Unlike the cross-hero board, the per-hero board's `rank` field appears to
    be unique and sequential (1..N with no ties), but list index is used
    anyway for consistency.
    """
    try:
        data = get("%s/v1/leaderboard/%s/%d" % (BASE, region, hero_id))
    except Exception as e:
        print("  [hb] %s hero %d -> %s" % (region, hero_id, e), file=sys.stderr)
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    out = []
    for i, e in enumerate(entries or [], 1):
        nm = e.get("account_name")
        if not nm:
            continue
        out.append({"hero_pos": i, "name": nm,
                    "ids": {int(a) for a in (e.get("possible_account_ids") or []) if a}})
    return out


def best_on_board(hero_entries, by_name):
    """Best cross-hero position among a hero board's players.

    This is the ceiling, done directly: every player on the hero's board,
    located on the region's cross-hero board, lowest position wins.

    The previous approach located OUR CHOSEN POOL instead, so any hero-board
    player who failed identity resolution or fell outside the per-region cap
    was invisible however highly they ranked. Measured 2026-08-03 on 12
    hero-regions, that missed the true ceiling on 4 of them — worst case
    Mirage NA, which reported position 379 when the hero's top player sat at
    position 2.

    A match needs BOTH the display name and an account id to agree, the same
    dual confirmation used elsewhere: possible_account_ids is a candidate list
    (~40,000 distinct ids across 1,000 NA entries) and name-only matching put
    the wrong player in 110 of 371 slots historically.
    """
    best = None
    located = 0
    for he in hero_entries:
        for cand in by_name.get(he["name"], []):
            if he["ids"] & cand["ids"]:
                located += 1
                if best is None or cand["pos"] < best[0]["pos"]:
                    best = (cand, he["hero_pos"])
                break
    return best, located


def main():
    tier = {r["hero"]: r for r in csv.DictReader(open(os.path.join(OUT_DIR, "tierlist.csv")))}

    print("[1/2] leaderboards", file=sys.stderr)
    boards = {}
    for region in REGIONS:
        boards[region] = fetch_board(region)
    depth = {r: boards[r][1] for r in REGIONS}

    # Dump the raw ordered board for archive_snapshot.py. The leaderboard
    # endpoint allows 100 req/s — a different bucket from /v1/sql's 20/hr —
    # and this reuses the fetch already made, so it costs no extra requests.
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "board.json"), "w", encoding="utf-8") as f:
            json.dump({rg: {"depth": boards[rg][1],
                            "entries": boards[rg][2][:BOARD_ARCHIVE_TOP]}
                       for rg in REGIONS}, f, separators=(",", ":"), ensure_ascii=False)
        print("  [board] wrote %s/board.json (top %d per region)"
              % (OUT_DIR, BOARD_ARCHIVE_TOP), file=sys.stderr)
    except Exception as e:
        print("  [warn] could not write board.json (%s)" % e, file=sys.stderr)

    print("[2/2] cross-referencing hero boards against the general board",
          file=sys.stderr)

    # hero_id -> name, from tierlist (which the pipeline just wrote)
    hero_ids = {}
    for h, t in tier.items():
        try:
            hero_ids[int(t["hero_id"])] = h
        except (KeyError, TypeError, ValueError):
            continue
    if not hero_ids:
        raise SystemExit("no hero ids in tierlist.csv")

    per_region = defaultdict(list)
    for rg in REGIONS:
        by_name = boards[rg][0]
        n_missing = 0
        for hid, hero in sorted(hero_ids.items(), key=lambda kv: kv[1]):
            hb = fetch_hero_board(rg, hid)
            time.sleep(0.15)          # 100 req/s allowed; stay well clear
            if not hb:
                n_missing += 1
                continue
            best, located = best_on_board(hb, by_name)
            if best is None:
                # nobody on this hero's board appears in the region's top-N
                n_missing += 1
                continue
            rec, hero_pos = best
            t = tier.get(hero, {})
            per_region[rg].append({
                "hero": hero,
                "hero_id": hid,
                "region": rg,
                "ceiling_player": rec["name"],
                "global_pos": rec["pos"],
                "region_depth": depth[rg],
                "pct": round(100.0 * rec["pos"] / max(depth[rg], 1), 3),
                "badge_level": rec["badge"],
                # position on the hero's OWN board - now read directly rather
                # than inherited from whichever pool member happened to resolve
                "hero_ladder_pos": hero_pos,
                "match": "confirmed",
                "valve_top_hero": "YES" if hid in (rec["top_heroes"] or []) else "",
                # how many of the hero board's players are on the general board
                "located_on_general": located,
                "board_size": len(hb),
                "winrate_rank": t.get("rank", ""),
                "elite_winrate": t.get("elite_winrate", ""),
            })
        print("  [xref] %-9s %d heroes ranked, %d with no locatable player"
              % (rg, len(per_region[rg]), n_missing), file=sys.stderr)

    out = []
    for rg in REGIONS:
        # global_pos ties happen: one account can be the located ceiling player
        # for two heroes at the identical board position (e.g. a McGinnis/Ivy
        # dual-main). Without a tiebreak, sort stability alone decided which
        # hero showed first — an artifact of dict iteration order, not a
        # ranking. Break ties by hero_ladder_pos (their standing on THAT
        # hero's own per-hero board — lower is better, so this asks "which
        # hero do they actually rank higher on"), then by pool elite_winrate
        # if that's tied too (e.g. wander sits at ladder_pos 1 on both
        # McGinnis and Ivy). Missing values sort last, not first.
        def _lp(d):
            try:
                return int(d["hero_ladder_pos"])
            except (TypeError, ValueError):
                return 10**9
        def _wr(d):
            try:
                return float(d["elite_winrate"])
            except (TypeError, ValueError):
                return -1.0
        rows = sorted(per_region.get(rg, []),
                      key=lambda d: (d["global_pos"], _lp(d), -_wr(d)))
        for i, d in enumerate(rows, 1):
            d["ceiling_rank"] = i

        out.extend(rows)

    for region in REGIONS:
        have = {d["hero"] for d in out if d["region"] == region}
        gap = sorted(set(tier) - have)
        if gap:
            print("  [warn] %s: no located pool member for %d heroes: %s"
                  % (region, len(gap), ", ".join(gap)), file=sys.stderr)

    cols = ["region", "ceiling_rank", "hero", "hero_id", "ceiling_player",
            "global_pos", "region_depth", "pct", "badge_level", "hero_ladder_pos",
            "match", "valve_top_hero", "located_on_general", "board_size",
            "winrate_rank", "elite_winrate"]
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
