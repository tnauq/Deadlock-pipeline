#!/usr/bin/env python3
"""
Turn the pipeline CSVs into one JSON file the static site reads.

    python3 build_site_data.py

Reads  ./output/tierlist.csv, ./output/item_frequency.csv
Writes ./docs/data.json

Item metadata (name, category, tier, icon) is deduped into a lookup table
keyed by item_id, so the per-hero payload is just [count, item_id] pairs.
Icons are referenced by their sha1 filename in ./docs/icons/ (matching
fetch_icons.py) with the original URL kept as a fallback.
"""

import csv
import datetime
import hashlib
import json
import os
import sys
from collections import defaultdict

OUT = "output"
DOCS = "docs"
SNAPSHOTS = ["4.8k", "9.6k", "14.4k", "20.8k", "postgame"]

BANDS = [("S", 59.0, 200.0), ("A", 57.0, 59.0), ("B", 55.0, 57.0),
         ("C", 53.0, 55.0), ("D", 0.0, 53.0)]

# refuse to publish obviously broken data over a good file
MIN_HEROES = 30
WINRATE_SANE = (40.0, 75.0)


def slug(name):
    s = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_").lower()
    while "__" in s:
        s = s.replace("__", "_")
    return s


def icon_ref(url):
    """(local filename, original url) — matches fetch_icons.py naming."""
    if not url:
        return ["", ""]
    return [hashlib.sha1(url.encode()).hexdigest() + ".png", url]


def band_of(w):
    for name, lo, hi in BANDS:
        if lo <= w < hi:
            return name
    return "D"


def read(name):
    with open(os.path.join(OUT, name), newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    tier_rows = [r for r in read("tierlist.csv") if r.get("elite_winrate")]
    item_rows = read("item_frequency.csv")

    if len(tier_rows) < MIN_HEROES:
        raise SystemExit("refusing to build: only %d heroes (expected >=%d)"
                         % (len(tier_rows), MIN_HEROES))

    heroes = []
    for r in tier_rows:
        w = float(r["elite_winrate"])
        if not WINRATE_SANE[0] <= w <= WINRATE_SANE[1]:
            raise SystemExit("refusing to build: %s win rate %.1f out of range"
                             % (r["hero"], w))
        heroes.append({
            "id": int(r["hero_id"]),
            "name": r["hero"],
            "slug": slug(r["hero"]),
            "rank": int(r["rank"]),
            "winrate": w,
            "tier": band_of(w),
            "games": int(r["elite_games"]),
            "players": int(r["players"]),
            "builds": int(r["builds_sampled"]),
            "thin": r.get("thin", "").strip().upper() == "YES",
            "split": r.get("lane_split", ""),
            "weak": r.get("lane_weak", ""),
            "role": r.get("lane_role", ""),
            "regions": r.get("by_region", ""),
            "icon": icon_ref(r["icon_url"]),
        })
    heroes.sort(key=lambda h: -h["winrate"])

    # deduped item lookup
    meta = {}
    for r in item_rows:
        iid = r["item_id"]
        if iid not in meta:
            meta[iid] = {
                "name": r["item"],
                "cat": r["category"] or "?",
                "tier": int(r["tier"]) if r.get("tier") else 0,
                "icon": icon_ref(r["icon_url"]),
            }

    # hero slug -> snapshot -> [[count, item_id], ...] sorted by count desc
    builds = defaultdict(lambda: {s: [] for s in SNAPSHOTS})
    of_builds = {}
    singletons = 0
    for r in item_rows:
        c = int(r["count"])
        if c < 2:
            singletons += 1            # kept; the site filters these client-side
        s = r["snapshot"]
        if s not in SNAPSHOTS:
            continue
        builds[slug(r["hero"])][s].append([c, r["item_id"]])
        of_builds[slug(r["hero"])] = int(r["of_builds"])

    for hero in builds.values():
        for s in SNAPSHOTS:
            hero[s].sort(key=lambda p: (-p[0], meta[p[1]]["name"]))

    missing = [h["name"] for h in heroes if h["slug"] not in builds]
    if missing:
        print("  [warn] no item data for: %s" % ", ".join(missing), file=sys.stderr)

    for h in heroes:
        h["of_builds"] = of_builds.get(h["slug"], h["builds"])

    if not singletons:
        print("  [warn] no count==1 rows in item_frequency.csv — the pipeline is "
              "still filtering singletons, so the site's one-off toggle will be "
              "inert until that filter is removed.", file=sys.stderr)

    data = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "has_singletons": singletons > 0,
        "snapshots": SNAPSHOTS,
        "bands": [[n, lo, hi] for n, lo, hi in BANDS],
        "heroes": heroes,
        "items": meta,
        "builds": builds,
    }

    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    kb = os.path.getsize(path) / 1024
    print("  -> %s (%.0f KB, %d heroes, %d items)"
          % (path, kb, len(heroes), len(meta)), file=sys.stderr)


if __name__ == "__main__":
    main()
