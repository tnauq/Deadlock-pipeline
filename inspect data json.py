#!/usr/bin/env python3
"""
inspect_data_json.py — which builds in docs/data.json hold DISABLED items?

Two dead items surfaced on the site (Ammo Scavenger on Haze, Enduring Spirit on
Lady Geist). Both were removed from the game well outside the 90-day lookback,
so the counts cannot be genuine current play. Two mechanisms in
build_site_data.py can produce this and they have opposite fixes:

  A. CARRY-FORWARD. A run that builds one region copies the other region's
     whole block — builds included — from the previous data.json. That block
     can be arbitrarily old and is never re-examined. Fix: prune on merge.

  B. AGGREGATION. The pipeline's shop-id allowlist is laxer than the 156-item
     one build_calc_data.py asserts, so disabled items survive every run and
     the CSVs themselves carry them. Fix: filter at aggregation.

The discriminator is WHICH REGION and HOW OLD. A dead item only in a region
with a stale generated_at is A. One in the freshly-built region is B.

Truth for "disabled" comes from /v1/assets/items, which is free — no key, no
SQL budget, separate bucket. Pass --offline to skip it and check only the two
named items.

    python3 inspect_data_json.py
    python3 inspect_data_json.py --offline
    python3 inspect_data_json.py --path docs/data.json
"""

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"

# Fallback set when running --offline: the two items observed on the site.
KNOWN_DEAD_NAMES = {"ammo scavenger", "enduring spirit"}


def get(path):
    req = urllib.request.Request(BASE + path,
                                 headers={"User-Agent": "inspect-data-json/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def dead_ids_from_assets():
    """Every asset id that is disabled or unshopable, by the pipeline's own
    id resolution so the keys line up with what data.json stores."""
    items = get("/v1/assets/items")
    dead = {}
    for it in items:
        v = it.get("id", it.get("item_id"))
        if v is None:
            continue
        disabled = bool(it.get("disabled"))
        # `shopable` is absent on some records; only count an explicit False
        unshopable = it.get("shopable") is False
        if disabled or unshopable:
            dead[str(int(v))] = {
                "name": it.get("name") or it.get("class_name") or "<unnamed>",
                "class_name": it.get("class_name"),
                "disabled": disabled,
                "shopable": it.get("shopable"),
                "cost": it.get("cost"),
            }
    print("[assets] %d records, %d disabled/unshopable" % (len(items), len(dead)),
          file=sys.stderr)
    return dead


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=os.path.join("docs", "data.json"))
    ap.add_argument("--offline", action="store_true",
                    help="skip the assets fetch; check only the two known dead items")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        raise SystemExit("no %s — run build_site_data.py, or pass --path" % args.path)

    with open(args.path, encoding="utf-8") as f:
        d = json.load(f)

    items = d.get("items") or {}
    regions = d.get("regions") or {}
    heroes = d.get("heroes") or {}

    print("=== 1. FRESHNESS ===")
    print("  file generated_at   %s" % d.get("generated_at"))
    stamps = {}
    for rg in d.get("region_order") or sorted(regions):
        b = regions.get(rg) or {}
        stamps[rg] = b.get("generated_at") or ""
        print("  %-10s %s   (%d heroes with builds)"
              % (rg, stamps[rg] or "<none>", len(b.get("builds") or {})))
    if len(set(v for v in stamps.values() if v)) > 1:
        print("  -> regions carry DIFFERENT stamps; the older one is carried forward")
    else:
        print("  -> regions agree; both were built by the same run")

    # ---- which ids count as dead ----------------------------------------
    if args.offline:
        dead = {iid: {"name": v["name"]} for iid, v in items.items()
                if str(v.get("name", "")).lower() in KNOWN_DEAD_NAMES}
        print("\n=== 2. DEAD ITEMS (offline: the two known names) ===")
    else:
        try:
            dead = dead_ids_from_assets()
        except Exception as e:
            print("\n[warn] assets fetch failed (%s) — falling back to offline mode" % e,
                  file=sys.stderr)
            dead = {iid: {"name": v["name"]} for iid, v in items.items()
                    if str(v.get("name", "")).lower() in KNOWN_DEAD_NAMES}
        print("\n=== 2. DEAD ITEMS PRESENT IN THE data.json LOOKUP ===")

    present = {iid: info for iid, info in dead.items() if iid in items}
    print("  %d of the dead set have a lookup entry in data.json" % len(present))
    for iid, info in sorted(present.items(), key=lambda kv: kv[1]["name"]):
        local = items[iid]["name"]
        flag = "" if local == info["name"] else "   (data.json calls it %r)" % local
        print("    %-12s %s%s" % (iid, info["name"], flag))

    # ---- where they actually appear -------------------------------------
    print("\n=== 3. BUILDS HOLDING A DEAD ITEM ===")
    hits = defaultdict(list)          # region -> rows
    for rg, b in regions.items():
        for hslug, cols in (b.get("builds") or {}).items():
            for snap, lst in (cols or {}).items():
                for pair in lst:
                    count, iid = pair[0], str(pair[1])
                    if iid in present:
                        hits[rg].append((hslug, snap, present[iid]["name"], count))

    if not hits:
        print("  none — every dead item is only a stale LOOKUP entry, never held.")
        print("  That is harmless: merged_items unions and never prunes, so a")
        print("  name survives after its builds are gone. The site would only")
        print("  render it if a build referenced it, and none does.")
    for rg in sorted(hits):
        rows = sorted(hits[rg])
        n_heroes = len(set(r[0] for r in rows))
        print("\n  %s  (%s)  — %d rows across %d heroes"
              % (rg, stamps.get(rg) or "<no stamp>", len(rows), n_heroes))
        for hslug, snap, name, count in rows:
            hname = (heroes.get(hslug) or {}).get("name", hslug)
            print("    %-14s %-9s %-22s count %s" % (hname, snap, name, count))

    # ---- read ------------------------------------------------------------
    print("\n=== 4. READ ===")
    if not hits:
        print("  Nothing to fix in the builds. If the site still SHOWS a dead")
        print("  item, it is coming from somewhere other than data.json.")
    elif len(hits) == 1 and len(stamps) > 1:
        rg = next(iter(hits))
        others = [r for r in stamps if r != rg]
        older = stamps.get(rg, "") and all(stamps.get(rg, "") < stamps.get(o, "")
                                           for o in others if stamps.get(o))
        if older:
            print("  Confined to %s, which carries the OLDER stamp." % rg)
            print("  -> mechanism A, carry-forward. Fix in build_site_data.py:")
            print("     prune dead ids when merging a carried region, or stop")
            print("     carrying builds forward at all.")
        else:
            print("  Confined to %s, which is NOT the stale region." % rg)
            print("  -> mechanism B, aggregation. Fix in the pipeline: tighten")
            print("     the shop-id allowlist to the 156-item catalogue that")
            print("     build_calc_data.py already asserts.")
    else:
        print("  Present in BOTH regions, including freshly built data.")
        print("  -> mechanism B, aggregation. The pipeline's shop-id allowlist")
        print("     is admitting disabled items on every run. Tighten it to the")
        print("     156-item catalogue and the CSVs stop carrying them.")
    print("\n  Either way, adding a disabled-id filter at the point data.json is")
    print("  written is the cheap belt-and-braces fix: it cannot regress and it")
    print("  covers carried blocks the pipeline will never rebuild.")


if __name__ == "__main__":
    main()
