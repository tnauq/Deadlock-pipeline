#!/usr/bin/env python3
"""
probe_item_ids.py — why does a Haze tile read "Ammo Scavenger"?

Hypothesis: id collision in /v1/assets/items. `it.get("id", it.get("item_id"))`
resolves two records to the same key, last-write-wins, and a dead legacy record
overwrites the live name.

No SQL. Assets endpoint only (no key, no rate-limit budget spent).

    python3 probe_item_ids.py

Optionally reads ./output/item_frequency.csv to name the exact ids Haze holds.
"""

import csv
import json
import os
import sys
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
TARGET_NAME = os.environ.get("PROBE_ITEM") or "ammo scavenger"
TARGET_HERO = os.environ.get("PROBE_HERO") or "Haze"


def get(path):
    req = urllib.request.Request(BASE + path,
                                 headers={"User-Agent": "probe-item-ids/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def resolve_id(it):
    """Exactly what deadlock_pipeline.py does, so a collision here is the bug."""
    v = it.get("id", it.get("item_id"))
    return None if v is None else int(v)


def name_of(it):
    for k in ("name", "display_name", "class_name"):
        v = it.get(k)
        if v not in (None, ""):
            return v
    return "<unnamed>"


def dump(it, prefix="    "):
    keep = ("id", "item_id", "class_name", "name", "cost", "item_tier",
            "item_slot_type", "disabled", "shopable", "shop_filters",
            "is_active_item", "imbue", "components")
    for k in keep:
        if k in it:
            print("%s%-16s %s" % (prefix, k, it[k]))
    extra = sorted(set(it) - set(keep))
    print("%sother keys: %s" % (prefix, ", ".join(extra)))


def main():
    items = get("/v1/assets/items")
    print("[assets] %d raw records\n" % len(items))

    # ---- 1. id collisions under the pipeline's own resolution ------------
    by_id = defaultdict(list)
    for it in items:
        iid = resolve_id(it)
        if iid is not None:
            by_id[iid].append(it)

    collisions = {k: v for k, v in by_id.items() if len(v) > 1}
    print("=== 1. ID COLLISIONS (pipeline resolution) ===")
    print("%d ids carry more than one record" % len(collisions))
    for iid, recs in sorted(collisions.items())[:20]:
        print("\n  id %s -> %d records" % (iid, len(recs)))
        for r in recs:
            print("    - %-34s class=%s cost=%s"
                  % (name_of(r), r.get("class_name"), r.get("cost")))
        print("    LAST WINS (what the pipeline keeps): %s" % name_of(recs[-1]))
    if len(collisions) > 20:
        print("\n  ... %d more" % (len(collisions) - 20))

    # ---- 2. every record matching the suspect name ----------------------
    print("\n=== 2. RECORDS NAMED LIKE %r ===" % TARGET_NAME)
    hits = [it for it in items if TARGET_NAME in name_of(it).lower()
            or TARGET_NAME.replace(" ", "_") in str(it.get("class_name", "")).lower()]
    if not hits:
        print("  none — the name is NOT coming from the asset dump at all.")
    for it in hits:
        print("\n  resolved id %s" % resolve_id(it))
        dump(it)

    # ---- 3. what the target hero actually holds -------------------------
    path = os.path.join("output", "item_frequency.csv")
    print("\n=== 3. %s ROWS IN item_frequency.csv ===" % TARGET_HERO)
    if not os.path.exists(path):
        print("  %s not found — skipping. Run the pipeline first, or set"
              " PROBE_HERO/PROBE_ITEM." % path)
    else:
        seen = {}
        for r in csv.DictReader(open(path)):
            if r.get("hero", "").lower() != TARGET_HERO.lower():
                continue
            if TARGET_NAME not in r.get("item", "").lower():
                continue
            seen.setdefault(r["item_id"], []).append((r["snapshot"], r["count"]))
        if not seen:
            print("  no matching rows — the label is being applied downstream"
                  " (calc/items.json or data.json), not in the CSV.")
        for iid, rows in seen.items():
            print("\n  item_id %s: %s" % (iid, rows))
            recs = by_id.get(int(iid), [])
            print("  -> %d asset record(s) for that id:" % len(recs))
            for rec in recs:
                print("     - %-34s class=%s cost=%s"
                      % (name_of(rec), rec.get("class_name"), rec.get("cost")))

    # ---- 4. same probe against the site's own calc data -----------------
    for cand in ("docs/calc/items.json", "calc/items.json"):
        if os.path.exists(cand):
            data = json.load(open(cand))
            match = [d for d in data
                     if TARGET_NAME in str(d.get("name", "")).lower()]
            print("\n=== 4. %s ===" % cand)
            print("  %d items; %d match %r" % (len(data), len(match), TARGET_NAME))
            for d in match:
                print("    id=%s name=%s class=%s cost=%s"
                      % (d.get("id"), d.get("name"),
                         d.get("class_name"), d.get("cost")))
            break
    else:
        print("\n=== 4. calc/items.json not found — skipped ===")

    print("\nRead: collision on the id Haze holds = mapping bug, fix by keying"
          " on class_name. No collision and the name is live in assets ="
          " the item was renamed, not removed.", file=sys.stderr)


if __name__ == "__main__":
    main()
