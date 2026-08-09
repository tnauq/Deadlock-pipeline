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

Reads  ./output/candidates.csv, ./output/source_items.csv
Writes ./output/orbit_audit.csv, and prints a summary

A RAW JACCARD IS NOT INTERPRETABLE on its own. It falls when either group is
small, because fewer items clear the "held by 2 or more builds" threshold — so
a 5-vs-15 split scores low whether or not the groups differ. Two controls fix
that:

  * a BOARD-vs-BOARD baseline, splitting the board builds in half and
    comparing those. That is the noise floor: whatever two halves of the SAME
    population score is what "no difference" looks like at that sample size.
  * size matching, subsampling the larger group down to the smaller one.

If orbit-vs-board lands at the baseline, the two sources are indistinguishable
and a rising orbit share is harmless.

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
import random
import sys
from collections import defaultdict

random.seed(20260809)          # a stable baseline between runs
TRIALS = int(os.environ.get("ORBIT_AUDIT_TRIALS") or 40)

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
    # item -> how many builds of that source held it, so a threshold can be
    # applied at whatever sample size the comparison is run at
    counts = defaultdict(dict)
    for r in src:
        counts[(r["region"], r["hero"], r["source"])][r["item_id"]] = int(r["count"])

    def jac(a, b):
        u = len(a | b)
        return len(a & b) / u if u else None

    def at_least(d, k):
        return {i for i, c in d.items() if c >= k}

    def baseline(hero_key, n_small, n_large):
        """
        Board-vs-board at the same sizes. source_items.csv is aggregated, so
        the two halves cannot be drawn from real builds — instead each item is
        kept with probability proportional to how often it appeared, which
        reproduces the thresholding effect that drives the jaccard down.
        """
        d = counts.get(hero_key + ("board",), {})
        tot = sum(1 for _ in d) and max(
            (c for c in d.values()), default=0)
        if not d or not tot:
            return None
        out = []
        for _ in range(TRIALS):
            ha, hb = {}, {}
            for i, c in d.items():
                # split c holdings between two halves binomially
                a = sum(1 for _ in range(c) if random.random() < 0.5)
                ha[i], hb[i] = a, c - a
            sa = at_least(ha, 2)
            sb = at_least(hb, 2)
            v = jac(sa, sb)
            if v is not None:
                out.append(v)
        return sum(out) / len(out) if out else None

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
            "jaccard": "", "baseline": "", "vs_baseline": "",
            "orbit_only": "", "board_only": "",
        }
        if have_builds and n_o and n_b:
            oi = at_least(counts.get((rg, hero, "orbit"), {}), 2)
            bi = at_least(counts.get((rg, hero, "board"), {}), 2)
            v = jac(oi, bi)
            rec["jaccard"] = round(v, 3) if v is not None else ""
            rec["orbit_only"] = len(oi - bi)
            rec["board_only"] = len(bi - oi)
            base = baseline((rg, hero), min(n_o, n_b), max(n_o, n_b))
            rec["baseline"] = round(base, 3) if base is not None else ""
            if base and v is not None:
                # above 1.0 means the two sources agree MORE than two halves
                # of the board pool agree with each other
                rec["vs_baseline"] = round(v / base, 2)
        rows.append(rec)

    cols = ["region", "hero", "board_builds", "orbit_builds", "orbit_share",
            "mean_seeds_met", "jaccard", "baseline", "vs_baseline",
            "orbit_only", "board_only"]
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
    bs = [r["baseline"] for r in rows if isinstance(r.get("baseline"), float)]
    vb = [r["vs_baseline"] for r in rows if isinstance(r.get("vs_baseline"), float)]
    if js:
        js.sort()
        print("  [orbit] jaccard orbit vs board : min %.2f  median %.2f  max %.2f"
              % (js[0], js[len(js) // 2], js[-1]), file=sys.stderr)
    if bs:
        bs.sort()
        print("  [orbit] jaccard board vs board : min %.2f  median %.2f  max %.2f"
              "   <- the noise floor" % (bs[0], bs[len(bs) // 2], bs[-1]),
              file=sys.stderr)
    if vb:
        vb.sort()
        print("  [orbit] ratio to baseline      : median %.2f  (1.00 means the "
              "two sources are indistinguishable)" % vb[len(vb) // 2],
              file=sys.stderr)
    else:
        print("  [orbit] no per-build item file, so item overlap was not "
              "computed — only the build split is reported", file=sys.stderr)


if __name__ == "__main__":
    main()
