#!/usr/bin/env python3
"""
orbit_audit.py — do orbit-sourced builds actually look different?

The pipeline tops up short hero-regions with ORBIT players: accounts that
shared a ranked match with a top board player. On 2026-08-09 that was 211 of
1,520 builds, 13.9%, up from 165 the run before. Those players are near the
ceiling rather than demonstrably on the ladder, so the question is whether
their builds differ from the board-sourced ones — if they do not, the share
does not matter.

This reads output/candidates.csv, which carries Steam account ids and display
names and is deliberately never committed or uploaded. NOTHING account-level
leaves this script: it emits per-hero-region aggregates only.

    python3 orbit_audit.py

Reads  ./output/candidates.csv, ./output/item_frequency.csv
Writes ./output/orbit_audit.csv, and prints a summary

Per hero-region it reports:
    orbit_builds / board_builds     the split
    jaccard                         overlap of the two groups' postgame item
                                    sets — 1.00 means identical choices
    orbit_only / board_only         items unique to each group
    mean_seeds_met                  proximity of the orbit players used

A jaccard near 1 says the orbit players build the same things and the fill is
harmless. A low one, especially with a large orbit share, says the fill is
changing what the site reports.

Cost: ZERO API calls. Pure local aggregation.
"""

import csv
import os
import sys
from collections import defaultdict

OUT = "output"


def read(name, required=True):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        if required:
            raise SystemExit("missing %s — run the pipeline first" % path)
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    cands = read("candidates.csv")
    if "source" not in cands[0]:
        raise SystemExit("candidates.csv has no `source` column — pipeline is "
                         "older than the orbit fill")

    # (region, hero) -> {"orbit": {match_ids}, "board": {match_ids}}
    groups = defaultdict(lambda: {"orbit": set(), "board": set()})
    seeds_met = defaultdict(list)
    for r in cands:
        key = (r["region"], r["hero"])
        mid = (r.get("last_match_id") or "").strip()
        if not mid:
            continue
        which = "orbit" if (r.get("source") or "") == "orbit" else "board"
        groups[key][which].add(mid)
        if which == "orbit":
            v = (r.get("orbit_seeds_met") or "").strip()
            if v.isdigit():
                seeds_met[key].append(int(v))

    # source_items.csv is the pipeline's own postgame holdings split by where
    # the build came from — aggregate, no account ids. item_frequency.csv
    # cannot answer this because it pools both sources together.
    src = read("source_items.csv", required=False)
    have_builds = bool(src)
    by_src = defaultdict(set)
    for r in src:
        # a single build holding an item says little; two or more is a choice
        if int(r["count"]) >= 2:
            by_src[(r["region"], r["hero"], r["source"])].add(r["item_id"])

    rows = []
    for (rg, hero), g in sorted(groups.items()):
        n_o, n_b = len(g["orbit"]), len(g["board"])
        rec = {
            "region": rg, "hero": hero,
            "orbit_builds": n_o, "board_builds": n_b,
            "orbit_share": round(100.0 * n_o / max(n_o + n_b, 1), 1),
            "mean_seeds_met": round(sum(seeds_met[(rg, hero)]) /
                                    len(seeds_met[(rg, hero)]), 2)
                              if seeds_met[(rg, hero)] else "",
            "jaccard": "", "orbit_only": "", "board_only": "",
        }
        if have_builds and n_o and n_b:
            oi = by_src.get((rg, hero, "orbit"), set())
            bi = by_src.get((rg, hero, "board"), set())
            inter, union = len(oi & bi), len(oi | bi)
            rec["jaccard"] = round(inter / union, 3) if union else ""
            rec["orbit_only"] = len(oi - bi)
            rec["board_only"] = len(bi - oi)
        rows.append(rec)

    cols = ["region", "hero", "board_builds", "orbit_builds", "orbit_share",
            "mean_seeds_met", "jaccard", "orbit_only", "board_only"]
    path = os.path.join(OUT, "orbit_audit.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    tot_o = sum(r["orbit_builds"] for r in rows)
    tot_b = sum(r["board_builds"] for r in rows)
    touched = [r for r in rows if r["orbit_builds"]]
    print("  -> %s (%d hero-regions)" % (path, len(rows)), file=sys.stderr)
    print("  [orbit] %d of %d builds are orbit-sourced (%.1f%%), across %d "
          "hero-regions" % (tot_o, tot_o + tot_b,
                            100.0 * tot_o / max(tot_o + tot_b, 1), len(touched)),
          file=sys.stderr)
    if touched:
        worst = sorted(touched, key=lambda r: -r["orbit_share"])[:8]
        print("  [orbit] most orbit-dependent:", file=sys.stderr)
        for r in worst:
            print("     %-9s %-14s %2d board / %2d orbit  (%.0f%%)  seeds met %s"
                  % (r["region"], r["hero"], r["board_builds"], r["orbit_builds"],
                     r["orbit_share"], r["mean_seeds_met"]), file=sys.stderr)
    js = [r["jaccard"] for r in rows if isinstance(r["jaccard"], float)]
    if js:
        js.sort()
        print("  [orbit] item-set jaccard, orbit vs board: min %.2f  median %.2f  "
              "max %.2f" % (js[0], js[len(js) // 2], js[-1]), file=sys.stderr)
    else:
        print("  [orbit] no per-build item file, so item overlap was not "
              "computed — only the build split is reported", file=sys.stderr)


if __name__ == "__main__":
    main()
