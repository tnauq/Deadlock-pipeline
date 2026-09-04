#!/usr/bin/env python3
"""
Build a hero ordering by walking the general board from position 1 and giving
each hero to the highest-ranked player who plays it.

    python3 assign_tiers.py

Reads  ./output/top_heroes.csv   (written by top_heroes.py)
Writes ./output/tier_assignment.csv
       ./output/tier_sensitivity.csv

--------------------------------------------------------------------------
WHAT THIS IS

Exclusive assignment: walk board positions ascending, hand each player ONE
unclaimed hero from their most-played list, stop when all heroes are claimed.
A hero's rank is the board position of the player who claimed it.

This IS exclusivity, which was measured and rejected for the pipeline's BUILD
pools (fill 98% -> 83%, hero-regions under 15 builds 2 -> 18). It is being
used here for a different purpose — an ordering, not a sample — where the
cost is different: no hero loses builds, but one player's near-tie moves two
heroes at once.

--------------------------------------------------------------------------
THE NEAR-TIE PROBLEM, AND WHY SCARCITY_PREF EXISTS

Measured on the NA board: 28% of resolved players have their #1 and #2 hero
within 3 games, and 98 players are within a single game. Under strict
"most-played wins", which hero those players main is a coin flip, and the flip
propagates straight into the ordering — Konr at board position 4 reads
Rem 33 / Sinclair 28, so Rem takes him on a five-game margin and Sinclair
falls from 1st to 164th.

SCARCITY_PREF breaks those near-ties toward the RARER hero rather than the
arbitrary winner. Within MARGIN games of a player's top count, the hero with
the fewest appearances across the whole board is taken.

This deliberately biases scarce heroes UP and common heroes DOWN. Be clear
about what that means: rarity is not strength. A hero played by few people
will rank higher under this rule than its play would justify, and the
ordering should be read as "how high on the ladder do you have to go to find
this hero's best representative", not as a strength claim. It is a defensible
thing to want — it makes the thin end of the list legible instead of noise —
but it is a normative choice, not a measurement.

--------------------------------------------------------------------------
SENSITIVITY IS THE POINT

Every variant is computed on every run and written to tier_sensitivity.csv:
a hero that sits at the same rank under all of them is solidly placed, and a
hero that swings 100+ places is being decided by one player's coin flip. Read
the range before trusting any single ordering.
"""

import csv
import os
import sys
from collections import Counter, defaultdict

OUT_DIR = os.environ.get("OUT_DIR") or "output"
SRC = os.path.join(OUT_DIR, os.environ.get("TOP_HEROES_CSV") or "top_heroes.csv")


def _env(name, default):
    v = os.environ.get(name)
    return type(default)(v) if v not in (None, "") else default


# How far down a player's most-played list to look for an unclaimed hero.
# 1 = strict (skip the player if their #1 is taken). 3 = use the full list.
FALLBACK_DEPTH = _env("FALLBACK_DEPTH", 3)
# Games within the top count that count as a tie for that player.
# ABSOLUTE tie band, in games. Kept for comparability with earlier runs, but
# it behaves inconsistently across activity levels: median hero1_games over a
# 14-day window is 20 (quartiles 12-30), so MARGIN=5 is 25% of a typical top
# count and over 40% for a low-activity player. That — not co-mains — is why
# 33 of 38 EU placements came back flagged near_tie.
MARGIN = _env("MARGIN", 3)
# PROPORTIONAL tie band, as a fraction of the player's OWN top count, so it
# means the same thing for a 12-game player and a 60-game one. Only 13% of
# players have #1 and #2 within 10% of each other, so this fires far less
# often than the absolute band at its usual settings.
MARGIN_PCT = float(os.environ.get("MARGIN_PCT") or 0.15)
# Break near-ties toward the rarer hero. See the header.
SCARCITY_PREF = os.environ.get("SCARCITY_PREF", "1") == "1"
# Regions are ranked SEPARATELY by default. NA and EU ceiling orderings are
# uncorrelated (Spearman 0.004 and 0.017 on two run pairs) and nine heroes
# swing 18+ places between them, so pooling positions across regions compares
# numbers that are not on the same scale.
POOL_REGIONS = os.environ.get("POOL_REGIONS", "0") == "1"
# Rows whose identity could not be resolved carry no heroes and are skipped.
# They are counted, because a gap at the TOP of the board matters far more
# than one at the bottom: 19 of the NA top 50 had no hero data.
TOP_GAP_WINDOW = _env("TOP_GAP_WINDOW", 50)


def load():
    if not os.path.exists(SRC):
        raise SystemExit("%s not found. Run top_heroes.py first." % SRC)
    rows = list(csv.DictReader(open(SRC)))
    for r in rows:
        r["board_pos"] = int(r["board_pos"])
        r["picks"] = []
        for i in (1, 2, 3):
            h = r.get("hero%d" % i) or ""
            if not h:
                continue
            try:
                g = int(r.get("hero%d_games" % i) or 0)
            except ValueError:
                g = 0
            r["picks"].append((h, g))
    return rows


def rarity(rows):
    """How many players list each hero anywhere in their top 3.

    The scarcity signal. Counted over the WHOLE board rather than the assigned
    set, so it does not shift as assignment proceeds — a tiebreak that changed
    based on what had already been claimed would make the result depend on
    board order twice over.
    """
    c = Counter()
    for r in rows:
        for h, _g in r["picks"]:
            c[h] += 1
    return c


def assign(rows, depth, margin, scarcity, rare, margin_pct=None):
    """Greedy walk down the board. Returns hero -> assignment record.

    margin_pct, when set, replaces the absolute game band with a fraction of
    the player's own top count — the same band for a 12-game player and a
    60-game one.
    """
    claimed = {}
    used = set()
    for r in sorted(rows, key=lambda x: x["board_pos"]):
        picks = r["picks"][:depth]
        if not picks or r.get("account_id") in used:
            continue
        top = picks[0][1]
        band = top * margin_pct if margin_pct is not None else margin
        free = [(h, g) for h, g in picks if h not in claimed]
        if not free:
            continue
        if scarcity:
            # among heroes this player plays within MARGIN of their top count,
            # take the rarest; outside the margin, most-played still wins
            tied = [(h, g) for h, g in free if top - g <= band]
            pick = min(tied, key=lambda hg: (rare[hg[0]], -hg[1], hg[0])) if tied \
                else max(free, key=lambda hg: (hg[1], hg[0]))
        else:
            pick = max(free, key=lambda hg: (hg[1], hg[0]))
        h, g = pick
        claimed[h] = {
            "hero": h, "board_pos": r["board_pos"],
            "account_name": r["account_name"], "account_id": r.get("account_id", ""),
            "hero_games": g,
            "via": [p[0] for p in r["picks"]].index(h) + 1,
            "top_games": top,
            "margin_to_top": top - g,
            # a claim taken on a near-tie is the fragile kind: this player
            # could as easily have been assigned elsewhere
            "near_tie": "YES" if (top - g) <= band and len(r["picks"]) > 1 else "",
            "rarity": rare[h],
        }
        used.add(r.get("account_id"))
    return claimed


VARIANTS = [
    ("strict_1",     dict(depth=1, scarcity=False)),
    ("fallback_2",   dict(depth=2, scarcity=False)),
    ("fallback_3",   dict(depth=3, scarcity=False)),
    ("scarcity_3",   dict(depth=3, scarcity=True)),
    ("scarcity_pct", dict(depth=3, scarcity=True, pct=True)),
]
# "expected_pos" is a sixth ordering and is NOT a greedy walk — it reranks
# fallback_3. See expected_position_rerank().


def expected_position_rerank(claimed, rare, board_n):
    """Rerank by observed position DIVIDED BY the position headcount predicts.

    THE POPULARITY CORRECTION, done smoothly instead of by a tie band.

    Under "walk down the board and take the first player who plays hero X", a
    hero's rank is the MINIMUM of n draws, where n is how many board players
    play it. For n draws over a board of N the expected best position is about
    (N+1)/(n+1) — so popularity buys position for free, with no reference to
    the hero at all. Lash appeared in 111 NA top-3 lists and landed at
    position 7 against an expectation of ~9: essentially nothing. Lady Geist
    appeared in 13 and landed at 29 against an expectation of ~72, which is
    the largest overperformance on the board.

    ratio = observed_pos / expected_pos. Below 1 means the hero's best player
    sits higher up the ladder than headcount alone predicts; that is the
    quantity worth ranking on, and it needs no threshold.

    TWO LIMITS, both real:
      - It assumes board position is INDEPENDENT of which hero you play. If a
        hero genuinely helps you climb, this correction absorbs part of the
        very effect being measured. The correction is conservative for that
        reason, not neutral.
      - At n = 13 the estimate is noisy. expected_se is written alongside so a
        thin hero's ratio is not read with the same confidence as Lash's. The
        minimum of n draws has a standard error of roughly its own expectation
        for small n, so a 13-player hero's interval is enormous.
    """
    out = {}
    for hero, d in claimed.items():
        n = max(rare.get(hero, 0), 1)
        exp = (board_n + 1.0) / (n + 1.0)
        out[hero] = {
            "expected_pos": round(exp, 1),
            "ratio": round(d["board_pos"] / exp, 3),
            # spread of the minimum of n draws, in the same units as exp
            "expected_se": round(exp * (n / (n + 2.0)) ** 0.5, 1),
            "draws": n,
        }
    order = sorted(out, key=lambda h: (out[h]["ratio"], claimed[h]["board_pos"], h))
    for i, h in enumerate(order, 1):
        out[h]["rank"] = i
    return out


def ranks(claimed):
    order = sorted(claimed.values(), key=lambda d: (d["board_pos"], d["hero"]))
    return {d["hero"]: i + 1 for i, d in enumerate(order)}


def spearman(a, b):
    hs = [h for h in a if h in b]
    n = len(hs)
    if n < 3:
        return float("nan")
    d = sum((a[h] - b[h]) ** 2 for h in hs)
    return 1 - 6.0 * d / (n * (n * n - 1))


def main():
    rows = load()
    regions = ["ALL"] if POOL_REGIONS else sorted({r["region"] for r in rows})
    out_rows, sens_rows = [], []

    for rg in regions:
        sub = rows if rg == "ALL" else [r for r in rows if r["region"] == rg]
        rare = rarity(sub)
        universe = set(rare)

        gap = sum(1 for r in sub
                  if r["board_pos"] <= TOP_GAP_WINDOW and not r["picks"])
        print("\n=== %s — %d board entries, %d with heroes, %d distinct heroes"
              % (rg, len(sub), sum(1 for r in sub if r["picks"]), len(universe)),
              file=sys.stderr)
        if gap:
            print("  [gap] %d of the top %d board positions have no hero data. "
                  "This ordering is most sensitive exactly there — an "
                  "unresolved position 1 contributes nothing."
                  % (gap, TOP_GAP_WINDOW), file=sys.stderr)

        results = {}
        for name, kw in VARIANTS:
            c = assign(sub, kw["depth"], MARGIN, kw["scarcity"], rare,
                       MARGIN_PCT if kw.get("pct") else None)
            results[name] = c
            missing = universe - set(c)
            via = Counter(d["via"] for d in c.values())
            print("  %-12s %2d/%d heroes, depth needed %4d, via #1/#2/#3 = %d/%d/%d%s"
                  % (name, len(c), len(universe),
                     max([d["board_pos"] for d in c.values()] or [0]),
                     via[1], via[2], via[3],
                     "  MISSING: %s" % ", ".join(sorted(missing)) if missing else ""),
                  file=sys.stderr)

        chosen_name = os.environ.get("ORDERING") or (
            "scarcity_3" if SCARCITY_PREF else "fallback_%d" % FALLBACK_DEPTH)
        if chosen_name not in results and chosen_name != "expected_pos":
            chosen_name = "fallback_3"
        # expected_pos reranks fallback_3's assignment rather than producing
        # its own, so the claim records come from there either way
        chosen = results["fallback_3" if chosen_name == "expected_pos"
                         else chosen_name]
        print("  ordering written from: %s" % chosen_name, file=sys.stderr)

        exp = expected_position_rerank(results["fallback_3"], rare, len(sub))
        rk = {n: ranks(c) for n, c in results.items()}
        rk["expected_pos"] = {h: v["rank"] for h, v in exp.items()}
        top_exp = sorted(exp, key=lambda h: exp[h]["rank"])[:6]
        print("  expected_pos top6: %s"
              % ", ".join("%s(ratio %.2f, n=%d)"
                          % (h, exp[h]["ratio"], exp[h]["draws"]) for h in top_exp),
              file=sys.stderr)
        base = rk[chosen_name]
        print("  spearman vs %s:" % chosen_name, file=sys.stderr)
        for n in rk:
            if n != chosen_name:
                print("    %-12s %.3f" % (n, spearman(base, rk[n])), file=sys.stderr)

        for hero in sorted(universe, key=lambda h: base.get(h, 10 ** 9)):
            d = chosen.get(hero)
            if d:
                out_rows.append(dict(d, region=rg, rank=base[hero],
                                     variant=chosen_name))
            e = exp.get(hero, {})
            seen = [rk[n][hero] for n in rk if hero in rk[n]]
            sens_rows.append({
                "region": rg, "hero": hero, "rank": base.get(hero, ""),
                "rarity": rare[hero],
                "best_rank": min(seen) if seen else "",
                "worst_rank": max(seen) if seen else "",
                # the fragility number. A hero swinging 100+ places across
                # variants is being placed by one player's coin flip, not by
                # anything about the hero.
                "rank_range": (max(seen) - min(seen)) if seen else "",
                "near_tie": d["near_tie"] if d else "",
                "expected_pos": e.get("expected_pos", ""),
                "obs_over_expected": e.get("ratio", ""),
                "expected_se": e.get("expected_se", ""),
                **{"rank_" + n: rk[n].get(hero, "") for n in rk},
            })

        vol = sorted((s for s in sens_rows if s["region"] == rg),
                     key=lambda s: -(s["rank_range"] or 0))[:6]
        print("  most variant-sensitive: %s"
              % ", ".join("%s(%d)" % (s["hero"], s["rank_range"]) for s in vol),
              file=sys.stderr)

    cols = ["region", "rank", "hero", "board_pos", "account_name", "account_id",
            "hero_games", "top_games", "via", "margin_to_top", "near_tie",
            "rarity", "variant"]
    _write("tier_assignment.csv", out_rows, cols)
    scols = (["region", "hero", "rank", "best_rank", "worst_rank", "rank_range",
              "near_tie", "rarity", "expected_pos", "obs_over_expected",
              "expected_se"]
             + ["rank_" + n for n, _ in VARIANTS] + ["rank_expected_pos"])
    _write("tier_sensitivity.csv", sens_rows, scols)


def _write(name, rows, cols):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    print("\n  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
