#!/usr/bin/env python3
"""
Is the type-composition result a real effect, or an artefact of pick rates?

    python3 type_pickrates.py

ZERO /v1/sql queries. Uses /v1/analytics/hero-stats (200 req/min) plus the
assets payload, so this is safe to run at any time, including alongside the
scheduled pipeline.

THE QUESTION. hero_type_comps.py found mystic-heavy teams winning more and
marksman-heavy teams winning less. But players do not choose their comp: they
select 3+ heroes and the matchmaker assigns ONE of them. So the observed type
mix is a convolution of three things:

    (a) how many heroes of each type exist in the roster
    (b) how often each hero is actually picked
    (c) matchmaker sampling from each player's pool of 3+

Observed share alone cannot separate these. A type can look rare because it has
few heroes, or because its heroes are unpopular, and those mean different
things. This script builds two baselines and compares:

    ROSTER baseline  — share if every hero were equally likely
    PICK baseline    — share implied by actual per-hero pick counts

If observed share ≈ pick baseline, the composition distribution is just pick
rates flowing through the matchmaker, and type share carries no extra signal.
The interesting case is a type whose WIN RATE per hero differs from the field:
that is a hero-strength effect masquerading as a composition effect, which is
the confound hero_type_comps.py could not rule out.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
TYPES = ["assassin", "brawler", "marksman", "mystic"]

# match hero_type_comps.py defaults so the comparison is like-for-like
LOOKBACK_DAYS = int(os.environ.get("COMP_LOOKBACK_DAYS") or 14)
BADGE_FLOOR = int(os.environ.get("COMP_BADGE_FLOOR") or 100)
# CASING DIFFERS BY ENDPOINT. /v1/sql uses the ClickHouse enum ('Normal'),
# but /v1/analytics/* uses lowercase snake_case ('normal', 'street_brawl').
# Sending 'Normal' here returns a bare HTTP 400 with no explanation — that
# cost a run on 2026-08-02. Normalise whatever COMP_GAME_MODE holds.
_GM = {"normal": "normal", "streetbrawl": "street_brawl",
       "street_brawl": "street_brawl", "explorenyc": "explore_n_y_c",
       "internal": "internal"}
GAME_MODE = _GM.get((os.environ.get("COMP_GAME_MODE") or "Normal")
                    .lower().replace("_", ""), "normal")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-picks/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        raise SystemExit("request failed (HTTP %s)\n  %s\n  url: %s"
                         % (e.code, body, url[:300]))


def main():
    import time
    heroes = get(BASE + "/v1/assets/heroes")
    if isinstance(heroes, dict):
        heroes = heroes.get("data", heroes.get("heroes", []))
    htype, hname = {}, {}
    for h in heroes:
        if h.get("disabled") or not h.get("player_selectable", True):
            continue
        hid = h.get("id")
        if hid is None:
            continue
        hid = int(hid)
        hname[hid] = h.get("name") or h.get("class_name") or str(hid)
        htype[hid] = h.get("hero_type")

    roster = defaultdict(list)
    for hid, t in htype.items():
        roster[t if t in TYPES else "untyped"].append(hid)

    if not 0 <= BADGE_FLOOR <= 116:
        raise SystemExit("COMP_BADGE_FLOOR must be 0-116 (schema limit), got %d"
                         % BADGE_FLOOR)
    since = int(time.time()) - LOOKBACK_DAYS * 86400
    q = urllib.parse.urlencode({
        "game_mode": GAME_MODE,
        "min_unix_timestamp": since,
        "min_average_badge": BADGE_FLOOR,
    })
    stats = get(BASE + "/v1/analytics/hero-stats?" + q)
    if isinstance(stats, dict):
        stats = stats.get("data", stats.get("hero_stats", []))
    if not stats:
        raise SystemExit("no rows from /v1/analytics/hero-stats")

    picks, wins = {}, {}
    for r in stats:
        hid = r.get("hero_id")
        if hid is None:
            continue
        hid = int(hid)
        m = r.get("matches") or r.get("matches_played") or 0
        picks[hid] = int(m)
        wins[hid] = int(r.get("wins") or 0)

    total_picks = sum(picks.get(h, 0) for h in htype)
    n_heroes = len(htype)
    print("roster: %d selectable heroes, %d total picks in the last %dd "
          "(badge>=%d, %s)\n" % (n_heroes, total_picks, LOOKBACK_DAYS,
                                 BADGE_FLOOR, GAME_MODE), file=sys.stderr)

    # OBSERVED slot share comes from hero_type_comps.py's averages per team,
    # but it is equivalently derivable here from pick counts — same data,
    # so this IS the pick baseline. The roster baseline is the contrast.
    print("%-10s %7s %9s %11s %11s %9s" %
          ("type", "heroes", "roster%", "pick share%", "per-hero%", "winrate"),
          file=sys.stderr)
    rows = []
    for t in TYPES:
        ids = roster[t]
        p = sum(picks.get(h, 0) for h in ids)
        w = sum(wins.get(h, 0) for h in ids)
        roster_share = 100.0 * len(ids) / n_heroes
        pick_share = 100.0 * p / total_picks if total_picks else 0
        per_hero = pick_share / len(ids) if ids else 0
        wr = 100.0 * w / p if p else 0
        rows.append((t, len(ids), roster_share, pick_share, per_hero, wr, p))
        print("%-10s %7d %8.1f%% %10.1f%% %10.2f%% %8.2f%%" %
              (t, len(ids), roster_share, pick_share, per_hero, wr),
              file=sys.stderr)

    if roster["untyped"]:
        print("\nUNTYPED (%d): %s" %
              (len(roster["untyped"]),
               ", ".join(hname[h] for h in roster["untyped"])), file=sys.stderr)
        print("  These are dropped from every signature in hero_type_comps.py,"
              " so teams containing them never appear.", file=sys.stderr)

    print("\n--- reading this ---", file=sys.stderr)
    for t, n, rs, ps, ph, wr, p in rows:
        gap = ps - rs
        note = ("picked MORE than roster share" if gap > 2 else
                "picked LESS than roster share" if gap < -2 else
                "picked in line with roster share")
        print("  %-10s %s (%+.1f pts)" % (t, note, gap), file=sys.stderr)

    field_wr = 100.0 * sum(wins.get(h, 0) for h in htype) / total_picks
    print("\n  field win rate %.2f%% (should be ~50 in a symmetric sample)"
          % field_wr, file=sys.stderr)
    print("  A type whose WIN RATE differs from the field is a hero-strength"
          " effect —", file=sys.stderr)
    print("  that is the confound the composition numbers cannot rule out."
          " Compare", file=sys.stderr)
    print("  these win rates against the mystic/marksman gradient before"
          " believing it.", file=sys.stderr)

    print("\n  Per-hero pick share is the fairest popularity measure: it"
          " divides out", file=sys.stderr)
    print("  roster size, so a type with few heroes is not automatically"
          " 'rare'.", file=sys.stderr)


if __name__ == "__main__":
    main()
