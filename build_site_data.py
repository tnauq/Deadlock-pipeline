#!/usr/bin/env python3
"""
Turn the pipeline CSVs into one JSON file the static site reads.

    python3 build_site_data.py

Reads  ./output/tierlist.csv, ./output/item_frequency.csv, ./output/ceiling.csv
       ./output/ability_frequency.csv, ./output/ability_order.csv  (both optional)
Writes ./docs/data.json

MERGES BY REGION. Both regions are processed in one run again as of
2026-08-03 — the candidate pool moved off /v1/sql onto /v1/players/hero-stats,
taking a run from ~16 SQL calls to ~4, so the alternating-region split that
existed purely to halve SQL usage is no longer needed.

The merge is KEPT anyway: it costs nothing, and it means a run that processes
only one region (a manual REGIONS override, or a partial failure) still leaves
the other region's block intact instead of wiping it.

Heroes are ordered by CEILING — the ladder position of each hero's strongest
player, computed per region. Win rate is carried through for reference but is
not what the ordering or the tiers are built from.

Everything is per region: NAmerica and Europe get their own ordering, their own
tiers, and their own item counts. Nothing is pooled across regions.
"""

import csv
import datetime
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from math import erf, sqrt

OUT = "output"
DOCS = "docs"
SNAPSHOTS = ["4.8k", "9.6k", "14.4k", "20.8k", "postgame"]
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
ALL_REGIONS = ["NAmerica", "Europe"]   # display order, independent of what this run built
REGION_LABEL = {"NAmerica": "NA", "Europe": "EU"}

# Tiers are a bell: the normal split into five equal-width z bands, heroes
# allocated by area. S 11.5%, A 23.0%, B 31.1%, C 23.0%, D 11.5% — so at 38
# heroes that is 4 / 9 / 12 / 9 / 4. Ranking is by position, so unlike the old
# fixed win-rate bands these are RELATIVE: a hero can change tier because
# others moved.
TIER_NAMES = ["S", "A", "B", "C", "D"]
_Z_CUTS = [1.2, 0.4, -0.4, -1.2]


def _phi(z):
    return 0.5 * (1 + erf(z / sqrt(2)))


_EDGES = [1.0] + [_phi(z) for z in _Z_CUTS] + [0.0]
TIER_AREAS = [_EDGES[i] - _EDGES[i + 1] for i in range(5)]

MIN_HEROES = 30


def allocate(n):
    """Bell-shaped tier sizes for n heroes; largest remainder so it sums to n."""
    raw = [a * n for a in TIER_AREAS]
    base = [int(x) for x in raw]
    order = sorted(range(5), key=lambda i: -(raw[i] - base[i]))
    for i in order[:n - sum(base)]:
        base[i] += 1
    return base


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


def read(name, required=True):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        if required:
            raise SystemExit("missing %s" % path)
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    tier_rows = [r for r in read("tierlist.csv") if r.get("elite_winrate")]
    item_rows = read("item_frequency.csv")
    ceil_rows = read("ceiling.csv")
    # Optional, so a run against an older pipeline still publishes. The site
    # simply hides the ability panel when the block is missing.
    abil_rows = read("ability_frequency.csv", required=False)
    order_rows = read("ability_order.csv", required=False)

    if len(tier_rows) < MIN_HEROES:
        raise SystemExit("refusing to build: only %d heroes" % len(tier_rows))
    if "region" not in (item_rows[0] if item_rows else {}):
        raise SystemExit("item_frequency.csv has no region column — pipeline is out of date")

    # ---- hero facts, shared across regions -------------------------------
    heroes = {}
    for r in tier_rows:
        s = slug(r["hero"])
        heroes[s] = {
            "id": int(r["hero_id"]),
            "name": r["hero"],
            "slug": s,
            "icon": icon_ref(r["icon_url"]),
            # carried for reference, not used for ordering or tiers
            "winrate": float(r["elite_winrate"]),
            "winrate_rank": int(r["rank"]),
        }

    # ---- deduped item lookup ---------------------------------------------
    meta = {}
    for r in item_rows:
        iid = r["item_id"]
        if iid not in meta:
            meta[iid] = {
                "name": r["item"],
                "cat": r["category"] or "?",
                "icon": icon_ref(r["icon_url"]),
            }

    # ---- deduped ability lookup -------------------------------------------
    abil_meta = {}
    for r in abil_rows:
        if not (r.get("slot") or "").strip():
            continue
        aid = r["ability_id"]
        if aid not in abil_meta:
            abil_meta[aid] = {"name": r["ability"], "icon": icon_ref(r.get("icon_url", ""))}

    # ---- per-region ability points ----------------------------------------
    # Two products from the same rows, kept apart on purpose:
    #   count      bare, denominated in builds, for display. Same convention as
    #              item hold counts — "3 of 4" shows the thin sample that 75%
    #              would hide.
    #   seed_rank  mean pick position ranked 1..16. NEVER displayed; it exists
    #              only so the build calculator can seed picks in a plausible
    #              and legally purchasable order.
    # tier 0 is an UNLOCK and spends its own currency (4 of them, at levels
    # 1/3/5/8). Tiers 1-3 are upgrades costing 1, 2 and 5 ability points, 32 in
    # total. Two budgets, not one 16-step track — the site needs the split.
    abilities = {rg: defaultdict(lambda: {"slots": {}, "of_builds": 0}) for rg in REGIONS}
    for r in abil_rows:
        rg, hs = r["region"], slug(r["hero"])
        if rg not in abilities:
            continue
        # Rows with no slot are abilities outside signature1-4. In practice
        # that is only Silver's werewolf form, whose upgrades mirror her base
        # kit, so showing both would duplicate the same choices.
        if not (r.get("slot") or "").strip():
            continue
        blk = abilities[rg][hs]
        entry = blk["slots"].setdefault(str(r.get("slot") or ""),
                                        {"id": r["ability_id"], "steps": {}})
        # [modal position, builds agreeing, of_builds denominator is on the
        # block, seed_rank, point cost]. The plain count is deliberately NOT
        # carried: "took it eventually" is ~everything and says nothing, while
        # the ORDER is the actual decision being made.
        entry["steps"][str(r["tier"])] = [
            int(r["modal_pos"]) if (r.get("modal_pos") or "").isdigit() else 0,
            int(r.get("modal_count") or 0),
            int(r["seed_rank"]) if (r.get("seed_rank") or "").isdigit() else 0,
            int(r.get("point_cost") or 0),
        ]
        blk["of_builds"] = max(blk["of_builds"], int(r.get("of_builds") or 0))

    # ---- the ceiling player's own pick order ------------------------------
    # ceiling.csv carries a confirmed account_id — the id present on both the
    # hero board and the general board. Join it to the sampled builds to
    # publish that one player's sequence. The ACCOUNT ID IS NOT PUBLISHED, only
    # the ordered picks, consistent with withholding ladder position and name.
    def parse_seq(row):
        seq = []
        for tok in (row.get("sequence") or "").split():
            aid, _, t = tok.partition(":")
            if t.isdigit():
                seq.append([aid, int(t)])
        return seq

    # Two orders per hero-region, shown one at a time behind a 1/2 toggle:
    #   1  the ceiling player's own build
    #   2  the most REPRESENTATIVE cohort build
    #
    # "Most common" cannot mean an exact duplicate: a sequence is ~16 steps, so
    # essentially every build is unique and a modal count would be 1 of 20.
    # Representativeness is scored instead — for each position, how many other
    # builds made the same pick there — so the winner is the build that agrees
    # most with the cohort rather than one that happens to repeat.
    #
    # If the top-scoring build IS the ceiling player's, the runner-up takes
    # slot 2, so the toggle always shows two different things.
    by_hr = defaultdict(list)
    for o in order_rows:
        seq = parse_seq(o)
        if seq:
            by_hr[(o["region"], slug(o["hero"]))].append((o, seq))

    def representative(entries, exclude=None):
        at = defaultdict(Counter)
        for _o, seq in entries:
            for i, step in enumerate(seq):
                at[i][tuple(step)] += 1
        best = None
        for o, seq in entries:
            if exclude is not None and seq == exclude:
                continue
            score = sum(at[i][tuple(step)] for i, step in enumerate(seq))
            # longer builds see more positions, so normalise by length
            score = score / max(len(seq), 1)
            if best is None or score > best[0]:
                best = (score, seq)
        return best[1] if best else None

    orders, seq_source = {}, {}
    for r in ceil_rows:
        key = (r["region"], slug(r["hero"]))
        entries = by_hr.get(key) or []
        if not entries:
            continue
        acct = (r.get("account_id") or "").strip()
        ceil = None
        for o, seq in entries:
            if acct and (o.get("account_id") or "").strip() == acct:
                ceil = seq
                break
        first = ceil if ceil else representative(entries)
        second = representative(entries, exclude=first)
        got = [x for x in (first, second) if x]
        if got:
            orders[key] = got
            seq_source[key] = "ceiling" if ceil else "cohort"

    if abil_rows:
        n_ceil = sum(1 for v in seq_source.values() if v == "ceiling")
        n_two = sum(1 for v in orders.values() if len(v) > 1)
        print("  [abilities] %d rows, %d hero-regions with an order "
              "(%d ceiling player, %d cohort only), %d with both"
              % (len(abil_rows), len(orders), n_ceil,
                 len(orders) - n_ceil, n_two), file=sys.stderr)

    # ---- per-region builds ------------------------------------------------
    builds = {rg: defaultdict(lambda: {s: [] for s in SNAPSHOTS}) for rg in REGIONS}
    of_builds = {rg: {} for rg in REGIONS}
    for r in item_rows:
        rg, s = r["region"], slug(r["hero"])
        if rg not in builds or r["snapshot"] not in SNAPSHOTS:
            continue
        if int(r["count"]) < 2:
            continue
        builds[rg][s][r["snapshot"]].append([int(r["count"]), r["item_id"]])
        of_builds[rg][s] = int(r["of_builds"])
    for rg in REGIONS:
        for hero in builds[rg].values():
            for s in SNAPSHOTS:
                hero[s].sort(key=lambda p: (-p[0], meta[p[1]]["name"]))

    # ---- per-region ordering, from the ceiling ---------------------------
    # ceiling_rank is the hero's best player's position on the region's
    # cross-hero board, computed by cross-referencing the hero board against
    # the general board (see ceiling_rank.py). Until 2026-08-03 that lookup ran
    # over the pipeline's chosen 20-player pool instead, which missed the true
    # ceiling on 4 of 12 sampled hero-regions — worst case Mirage NA at
    # position 379 when its top player sat at position 2.
    regions = {}
    for rg in REGIONS:
        rows = sorted((r for r in ceil_rows if r["region"] == rg),
                      key=lambda r: int(r["global_pos"]))
        if not rows:
            raise SystemExit("no ceiling rows for %s" % rg)
        sizes = allocate(len(rows))
        order, i = [], 0
        for name, n in zip(TIER_NAMES, sizes):
            for r in rows[i:i + n]:
                s = slug(r["hero"])
                if s not in heroes:
                    continue
                # Only what the page renders. Ladder position, percentile and
                # the ceiling player's display name stay in output/ceiling.csv
                # and are deliberately NOT published — the site never shows
                # them, and data.json is served publicly.
                order.append({
                    "slug": s,
                    "tier": name,
                    "rank": len(order) + 1,
                    "of_builds": of_builds[rg].get(s, 0),
                })
            i += n
        regions[rg] = {
            "label": REGION_LABEL[rg],
            "depth": int(rows[0]["region_depth"]),
            "order": order,
            "builds": {k: v for k, v in builds[rg].items()},
            "abilities": {k: {"slots": v["slots"], "of_builds": v["of_builds"],
                              "orders": orders.get((rg, k), []),
                              "seq_from": seq_source.get((rg, k), "")}
                          for k, v in abilities[rg].items()},
        }
        print("  [%s] %d heroes, tiers %s" % (rg, len(order), dict(zip(TIER_NAMES, sizes))),
              file=sys.stderr)

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for rg in regions:
        regions[rg]["generated_at"] = now      # per region: they refresh on different runs

    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, "data.json")

    # ---- merge with whatever is already published --------------------------
    # This run may have built only one region. Carry the other region's block
    # forward from the existing file rather than dropping it: its CSVs no
    # longer exist on this runner.
    prev = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception as e:
            print("  [warn] could not read existing %s (%s) — writing fresh" % (path, e),
                  file=sys.stderr)
            prev = {}

    merged_regions = dict(prev.get("regions") or {})
    for rg, block in regions.items():
        merged_regions[rg] = block
    carried = [rg for rg in merged_regions if rg not in regions]
    if carried:
        for rg in carried:
            stamp = merged_regions[rg].get("generated_at", "?")
            print("  [%s] carried forward unchanged (built %s)" % (rg, stamp), file=sys.stderr)

    # heroes/items are shared across regions, so union them rather than
    # replacing — a region-only run still sees every hero, but this keeps an
    # icon or item that only the other region's CSVs mentioned.
    merged_heroes = dict(prev.get("heroes") or {})
    merged_heroes.update(heroes)
    merged_items = dict(prev.get("items") or {})
    merged_items.update(meta)
    merged_abilities = dict(prev.get("ability_meta") or {})
    merged_abilities.update(abil_meta)

    # only advertise regions that actually have data
    order = [rg for rg in ALL_REGIONS if rg in merged_regions]

    data = {
        "generated_at": now,
        "snapshots": SNAPSHOTS,
        "tiers": TIER_NAMES,
        "region_order": order,
        "heroes": merged_heroes,
        "items": merged_items,
        "ability_meta": merged_abilities,
        "regions": merged_regions,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print("  -> %s (%.0f KB, %d heroes, %d items, %d abilities, regions %s)"
          % (path, os.path.getsize(path) / 1024, len(merged_heroes), len(merged_items),
             len(merged_abilities), ", ".join(order)), file=sys.stderr)


if __name__ == "__main__":
    main()
