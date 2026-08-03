#!/usr/bin/env python3
"""
Can /v1/players/hero-stats?match_mode=ranked replace leaderboard position as the
candidate selector?

    python3 probe_ranked_stats.py

ZERO /v1/sql queries. hero-stats is 100 req/s and batched via account_ids, so
this costs nothing against the 20/hr SQL budget and is safe to run any time,
including alongside the scheduled pipeline.

WHY THIS MATTERS. Candidate selection is currently leaderboard-driven, but the
leaderboard is not ranked-gated: ~1,000 entries per region while the season
requires 60 wins and started days ago. So the pool is picked on all-mode
standing while builds are filtered to ranked, and pool members average only
~7.5 ranked games on their hero (min 3.1). If hero-stats can return ranked-only
per-account-per-hero numbers, selection can be ranked-native for free.

CASING. match_mode wants lowercase snake_case - `ranked`, `unranked`,
`private_lobby` - NOT the ClickHouse enum `Ranked`. Sending the wrong casing to
/v1/analytics/* previously returned a bare HTTP 400 and cost a run.

WHAT IT CHECKS
  1. does match_mode=ranked work, and what casing is accepted
  2. does it actually restrict (ranked totals should be << all-mode totals)
  3. is there enough ranked volume per player to rank on
  4. how a shrinkage estimator behaves on that volume
"""

import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
N_ACCOUNTS = int(os.environ.get("PROBE_ACCOUNTS") or 40)
SHRINK_K = float(os.environ.get("SHRINK_K") or 25)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-rankedstats/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
    except Exception as e:
        return None, str(e)


def account_ids(n):
    """Real ids the pipeline already resolved."""
    p = os.path.join("output", "candidates.csv")
    if os.path.exists(p):
        ids, seen = [], set()
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            a = r.get("account_id")
            if a and a not in seen:
                seen.add(a)
                ids.append(int(a))
            if len(ids) >= n:
                break
        if ids:
            print("  [ids] %d from candidates.csv" % len(ids), file=sys.stderr)
            return ids
    p = os.path.join("output", "board.json")
    if os.path.exists(p):
        board = json.load(open(p, encoding="utf-8"))
        ids = [e["ids"][0] for rg in board for e in board[rg]["entries"]
               if len(e.get("ids") or []) == 1][:n]
        print("  [ids] %d from board.json" % len(ids), file=sys.stderr)
        return ids
    raise SystemExit("no output/candidates.csv or board.json - run the pipeline first")


def fetch(ids, mode=None):
    q = [("account_ids", str(a)) for a in ids]
    if mode:
        q.append(("match_mode", mode))
    data, err = get(BASE + "/v1/players/hero-stats?" + urllib.parse.urlencode(q))
    return data, err


def summarise(rows, label):
    if not rows:
        print("    %s: no rows" % label, file=sys.stderr)
        return None
    m = sum(int(r.get("matches_played") or r.get("matches") or 0) for r in rows)
    w = sum(int(r.get("wins") or 0) for r in rows)
    accts = len({r.get("account_id") for r in rows})
    print("    %-22s %5d rows | %3d accounts | %6d matches | %5d wins"
          % (label, len(rows), accts, m, w), file=sys.stderr)
    return {"rows": len(rows), "matches": m, "wins": w, "accounts": accts}


def main():
    ids = account_ids(N_ACCOUNTS)
    print("probing hero-stats for %d accounts (key: %s)\n"
          % (len(ids), "yes" if API_KEY else "no"), file=sys.stderr)

    print("=" * 64, file=sys.stderr)
    print("1. does match_mode work, and which casing?", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    results = {}
    for mode in (None, "ranked", "Ranked", "unranked"):
        lbl = mode or "(no filter)"
        data, err = fetch(ids, mode)
        if err:
            print("    %-22s %s" % (lbl, err), file=sys.stderr)
            continue
        rows = data if isinstance(data, list) else data.get("data", [])
        results[lbl] = summarise(rows, lbl)
        time.sleep(1)

    base = results.get("(no filter)")
    rk = results.get("ranked")
    print("", file=sys.stderr)
    if rk is None:
        print("  >>> lowercase 'ranked' REJECTED - try the other casing above",
              file=sys.stderr)
    elif base and rk["matches"] >= base["matches"]:
        print("  >>> WARNING: ranked total is not smaller than unfiltered. The "
              "filter may be", file=sys.stderr)
        print("      silently ignored - do not trust it without checking a known "
              "account.", file=sys.stderr)
    elif base:
        print("  >>> filter works: ranked is %.1f%% of all-mode matches"
              % (100.0 * rk["matches"] / max(base["matches"], 1)), file=sys.stderr)

    if not rk:
        return

    print("\n" + "=" * 64, file=sys.stderr)
    print("2. is there enough ranked volume to select on?", file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    data, err = fetch(ids, "ranked")
    rows = data if isinstance(data, list) else (data or {}).get("data", [])
    per = defaultdict(lambda: [0, 0])
    for r in rows:
        k = (r.get("account_id"), r.get("hero_id"))
        per[k][0] += int(r.get("matches_played") or r.get("matches") or 0)
        per[k][1] += int(r.get("wins") or 0)
    vals = sorted(v[0] for v in per.values() if v[0] > 0)
    if not vals:
        print("  no ranked games at all for these accounts", file=sys.stderr)
        return
    print("  %d (account,hero) pairs with >=1 ranked game" % len(vals), file=sys.stderr)
    print("  games per pair: min %d  median %d  max %d"
          % (vals[0], vals[len(vals) // 2], vals[-1]), file=sys.stderr)
    for t in (5, 10, 20):
        print("     pairs with >=%2d games: %d (%.0f%%)"
              % (t, sum(1 for v in vals if v >= t),
                 100.0 * sum(1 for v in vals if v >= t) / len(vals)), file=sys.stderr)

    print("\n" + "=" * 64, file=sys.stderr)
    print("3. shrinkage estimator on this volume (k=%.0f)" % SHRINK_K, file=sys.stderr)
    print("=" * 64, file=sys.stderr)
    print("  rating = (wins + k*0.5) / (games + k) - pulls thin samples to 50%%",
          file=sys.stderr)
    ranked = sorted(((w + SHRINK_K * 0.5) / (g + SHRINK_K), g, w, k)
                    for k, (g, w) in per.items() if g > 0)
    print("\n  top 8 by shrunk rating:", file=sys.stderr)
    print("    %-8s %-8s %6s %6s %8s %8s" % ("account", "hero", "games", "wins",
                                             "raw", "shrunk"), file=sys.stderr)
    for rating, g, w, k in ranked[::-1][:8]:
        print("    %-8s %-8s %6d %6d %7.3f %8.3f"
              % (str(k[0])[:8], str(k[1])[:8], g, w, w / max(g, 1), rating),
              file=sys.stderr)
    print("\n  >>> if the top of this list is dominated by 3-5 game samples, raise "
          "SHRINK_K.", file=sys.stderr)
    print("      if it tracks high-volume winners, the estimator is usable as a "
          "selector.", file=sys.stderr)


if __name__ == "__main__":
    main()
