#!/usr/bin/env python3
"""
Cross-reference the per-hero leaderboard against the general leaderboard.

    python3 probe_board_xref.py

ZERO SQL. The leaderboard endpoint is 100 req/s and a separate bucket from
/v1/sql, so this is safe to run any time, including alongside the pipeline.

WHY. ceiling_rank.py currently locates OUR CHOSEN POOL on the general board.
Anyone on the hero board who fails identity resolution, or falls outside the
per-region cap, is invisible no matter how highly they rank. That is not
hypothetical: on 2026-08-03 the pipeline reported Mirage NA's ceiling as
`thirkl` at general position 379, while `rocaine` sits at #1 on the Mirage
hero board AND inside the general board's top 8. Five of the hero board's top
six never reached the pool.

The fix is to cross-reference the two boards directly - every hero-board entry,
located on the general board, best position wins - with no pool and no cap.

This probe measures whether that is actually feasible:
  1. how deep the hero boards are, per region
  2. what fraction of hero-board entries can be located on the general board
  3. whether id-intersection beats name matching for the join
  4. what ceiling each hero would get, vs what the pipeline currently reports

MATCHING. Both boards carry possible_account_ids, which is a CANDIDATE list,
not an identity - one name can carry 30+ ids, and name-only matching put the
wrong player in 110 of 371 slots historically. Intersecting the two id lists
should be far stronger than either name or id alone, because it requires the
same account to appear on both boards. This probe reports all three so the
join can be chosen on evidence.
"""

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
# a spread of board depths: Mirage/Grey Talon are short, Haze/Bebop are deep
HERO_IDS = [int(x) for x in
            (os.environ.get("PROBE_HEROES") or "52,17,6,13,2,20").split(",")]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-xref/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:
        return None, str(e)


def entries(data):
    return (data.get("entries") if isinstance(data, dict) else data) or []


def board(region, hero_id=None):
    url = "%s/v1/leaderboard/%s" % (BASE, region)
    if hero_id is not None:
        url += "/%d" % hero_id
    data, err = get(url)
    if err:
        print("    %s -> %s" % (url.split("/v1/")[1], err), file=sys.stderr)
        return []
    return entries(data)


def main():
    heroes, _ = get(BASE + "/v1/assets/heroes")
    name = {}
    for h in heroes or []:
        if h.get("id") is not None:
            name[int(h["id"])] = h.get("name") or str(h["id"])

    for region in REGIONS:
        gen = board(region)
        if not gen:
            continue
        # list index is the position - the `rank` field ties heavily on the
        # general board (1,001 NA entries across 634 distinct values)
        pos_by_name, pos_by_id = {}, {}
        for i, e in enumerate(gen, 1):
            nm = e.get("account_name")
            if nm and nm not in pos_by_name:
                pos_by_name[nm] = i
            for a in (e.get("possible_account_ids") or []):
                pos_by_id.setdefault(int(a), i)
        print("\n" + "=" * 70, file=sys.stderr)
        print("%s - general board: %d entries, %d named, %d distinct candidate ids"
              % (region, len(gen), len(pos_by_name), len(pos_by_id)), file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print("  %-13s %5s %8s %8s %8s   %-18s %s"
              % ("hero", "board", "by name", "by id", "by both", "best located", "at pos"),
              file=sys.stderr)

        for hid in HERO_IDS:
            hb = board(region, hid)
            time.sleep(0.2)
            if not hb:
                continue
            n_name = n_id = n_both = 0
            best = None
            for j, e in enumerate(hb, 1):
                nm = e.get("account_name")
                ids = [int(a) for a in (e.get("possible_account_ids") or [])]
                p_name = pos_by_name.get(nm)
                p_ids = [pos_by_id[a] for a in ids if a in pos_by_id]
                p_id = min(p_ids) if p_ids else None
                if p_name:
                    n_name += 1
                if p_id:
                    n_id += 1
                # strongest join: the same account appears on both boards AND
                # the display name agrees
                if p_name and p_id and p_name == p_id:
                    n_both += 1
                    if best is None or p_name < best[0]:
                        best = (p_name, nm, j)
            print("  %-13s %5d %8d %8d %8d   %-18s %s"
                  % (name.get(hid, hid), len(hb), n_name, n_id, n_both,
                     (best[1][:18] if best else "-"),
                     ("%d (hero #%d)" % (best[0], best[2])) if best else "-"),
                  file=sys.stderr)

    print("\n  'by both' is the join to use if it stays close to 'board' - it "
          "requires the", file=sys.stderr)
    print("  same account on both boards AND the same display name, which is the "
          "dual", file=sys.stderr)
    print("  confirmation the pipeline already relies on elsewhere.", file=sys.stderr)
    print("\n  Compare 'best located' against ceiling_player in output/ceiling.csv. "
          "Where they", file=sys.stderr)
    print("  differ, the pool-based ceiling was missing the hero's actual best "
          "player.", file=sys.stderr)


if __name__ == "__main__":
    main()
