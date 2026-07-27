#!/usr/bin/env python3
"""
Diagnose the cross-hero leaderboard response before trusting any ordering.

ceiling_rank.py produced ten heroes tied at position 1, so an assumption in it
is wrong. This prints the board's actual structure. No CSVs required beyond
candidates.csv, no writes.

    python3 diagnose_board.py
"""

import csv
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict

BASE = "https://api.deadlock-api.com"
REGION = os.environ.get("REGION") or "NAmerica"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-diag/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    data = get("%s/v1/leaderboard/%s" % (BASE, REGION))
    entries = data.get("entries") if isinstance(data, dict) else data
    print("region=%s  entries=%d" % (REGION, len(entries)))
    if isinstance(data, dict):
        print("top-level keys: %s" % list(data))

    print("\n--- first 3 entries, verbatim ---")
    for e in entries[:3]:
        print(json.dumps(e)[:400])

    print("\n--- field presence ---")
    keys = Counter()
    for e in entries:
        for k, v in e.items():
            if v not in (None, "", []):
                keys[k] += 1
    for k, n in keys.most_common():
        print("  %-22s %d/%d" % (k, n, len(entries)))

    # Is 'rank' a unique position?
    print("\n--- is 'rank' a unique ordering? ---")
    ranks = [e.get("rank") for e in entries]
    nn = [r for r in ranks if r is not None]
    print("  non-null: %d/%d" % (len(nn), len(ranks)))
    if nn:
        c = Counter(nn)
        dup = {k: v for k, v in c.items() if v > 1}
        print("  min=%s max=%s distinct=%d" % (min(nn), max(nn), len(c)))
        print("  values used more than once: %d (e.g. %s)"
              % (len(dup), sorted(dup.items(), key=lambda kv: -kv[1])[:5]))
        print("  monotonic in list order: %s"
              % (nn == sorted(nn) or nn == sorted(nn, reverse=True)))

    # How ambiguous is possible_account_ids?
    print("\n--- possible_account_ids ---")
    sizes = Counter(len(e.get("possible_account_ids") or []) for e in entries)
    print("  list size -> entries: %s" % dict(sorted(sizes.items())[:10]))
    claims = defaultdict(list)
    for i, e in enumerate(entries):
        pos = e.get("rank") if e.get("rank") else i + 1
        for aid in (e.get("possible_account_ids") or []):
            claims[int(aid)].append(pos)
    multi = {a: p for a, p in claims.items() if len(p) > 1}
    print("  distinct account ids: %d" % len(claims))
    print("  ids claimed by >1 entry: %d (%.1f%%)"
          % (len(multi), 100.0 * len(multi) / max(len(claims), 1)))
    if multi:
        worst = sorted(multi.items(), key=lambda kv: -len(kv[1]))[:5]
        print("  worst offenders (id -> positions): %s"
              % [(a, p[:6]) for a, p in worst])

    # What does this mean for the actual pool?
    path = os.path.join("output", "candidates.csv")
    if os.path.exists(path):
        pool = [r for r in csv.DictReader(open(path)) if r["region"] == REGION]
        print("\n--- effect on the %d pool members in this region ---" % len(pool))
        spread = []
        for r in pool:
            p = claims.get(int(r["account_id"]))
            if p and len(p) > 1:
                spread.append((r["account_name"], r["hero"], min(p), max(p), len(p)))
        print("  pool ids claimed by >1 entry: %d/%d" % (len(spread), len(pool)))
        for nm, h, lo, hi, n in sorted(spread, key=lambda x: -x[4])[:8]:
            print("    %-16s %-12s %d entries, positions %d..%d" % (nm[:16], h[:12], n, lo, hi))

        # Does the entry's own name agree with what the pipeline resolved?
        by_pos = {}
        for i, e in enumerate(entries):
            by_pos[e.get("rank") if e.get("rank") else i + 1] = e.get("account_name")
        agree = sum(1 for r in pool
                    if (p := claims.get(int(r["account_id"])))
                    and by_pos.get(min(p)) == r["account_name"])
        print("  name agrees at the claimed top position: %d/%d" % (agree, len(pool)))


if __name__ == "__main__":
    main()
