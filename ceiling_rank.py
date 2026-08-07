#!/usr/bin/env python3
"""
Ceiling ranking: order heroes by their single strongest player.

The per-hero leaderboard gives position WITHIN a hero, so it cannot compare
heroes against each other — every hero has a rank 1. A hero's ceiling is
therefore its best board player's standing in the region as a whole.

WHAT ORDERS THE CEILING — changed 2026-08-07
--------------------------------------------
It used to be the player's position on Valve's cross-hero board. That board is
not ranked-gated, so it ranked all-mode standing while every build we sample is
ranked-only. Worse, it cannot be filtered: the OpenAPI spec exposes no
match_mode parameter, and match_mode=Ranked, ranked=true and only_ranked=true
all return byte-identical results.

Every upstream RATING was then checked and none survived:

    average_badge_team0/1   0 on every ranked row since 2026-07-31
    /v1/players/mmr         200 with an empty list — including for an
                            account id that cannot exist, so an empty
                            response says nothing about the account
    mmr-history             404
    /v1/players/scoreboard  match_mode accepted and IGNORED (top account has
                            7,651 matches in a window opening 2026-07-08;
                            ranked launched 2026-07-30), and sort_by=winrate
                            and sort_by=wins both return 500

So the ordering has to come from ranked match outcomes. Checked against
statlocker's NA ranked ladder over the twelve players visible on both, by
pairwise concordance:

    net wins (w - l)        84%
    shrunk win rate k=100   80%
    shrunk win rate k=50    73%
    shrunk win rate k=25    71%
    Valve board position    79%
    raw win rate            62%

NET WINS WINS, and the reason matters more than the number. Ranked MMR is
currently inflationary — loss protection and win-streak bonuses against fixed
rank thresholds — which makes the ladder below Eternus a progression system
rather than a ranking one. Under progression, accumulated net wins IS rank, so
the two coincide. The shrunk-win-rate figures converge on net wins from below
as k rises, which is why bigger k scored better; they were approximating it.

*** THIS METRIC HAS AN EXPIRY. *** At Eternus the system reverts to percentile
ranking, where inflation stops moving anyone and net wins decouples from rank.
One player is at Eternus as of 2026-08-07. When enough arrive that the top is
percentile-ranked, `ceiling_value()` below is the single function to replace.
Badge is not expected to return: it reads 0 across every ranked row, and being
a team average it would be non-zero if any Eternus player were in the lobby.

WHAT THE BOARD IS STILL FOR
---------------------------
Eligibility and identity, both of which it does fine even unfiltered. Valve's
leaderboard publishes no account ids at all — /raw is bare protobuf carrying
only account_name and rank — so `possible_account_ids` is deadlock-api's own
name resolution, and it is fuzzy: ~40,000 distinct ids across 1,000 NA entries,
17.5% claimed by more than one entry, and name-only matching put the wrong
player in 110 of 371 slots. A player therefore counts only when their display
name appears on BOTH the hero board and the cross-hero board AND their
candidate id lists intersect. That dual confirmation is unchanged.

    python3 ceiling_rank.py

Reads  ./output/tierlist.csv (hero ids and the win-rate reference columns)
Writes ./output/ceiling.csv, ./output/board.json

Requests: one cross-hero board per region, one board per hero per region (~78),
plus ~10 batched hero-stats calls. The leaderboard and hero-stats endpoints are
100 req/s and a separate bucket from /v1/sql, so this still costs NOTHING
against the 20/hour SQL budget.
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
OUT_DIR = "output"
MATCH_MODE = os.environ.get("MATCH_MODE") or "Ranked"
MAX_URL = int(os.environ.get("MAX_URL") or 9000)

# Shrinkage for the SECONDARY win-rate column. Not the ordering — kept so the
# two can be compared when the ladder switches to percentile ranking and net
# wins expires. k=25 matches the pipeline's pool selection.
SHRINK_K = int(os.environ.get("SHRINK_K") or 25)

# How many board entries to keep for the archive (player bar-chart-race data).
# Costs no extra API calls — the full board is already fetched.
BOARD_ARCHIVE_TOP = int(os.environ.get("BOARD_ARCHIVE_TOP") or 100)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-ceiling/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_board(region):
    """Return (entries_by_name, depth, ordered) for one region's cross-hero board.

    Position is the LIST INDEX, not the 'rank' field. Measured on the live
    board: 1001 entries carry only 634 distinct 'rank' values, and rank 1 is
    shared by 14 players, but the list is monotonic in rank — so list order is
    the real ordering and 'rank' is a tied/rounded label.
    """
    data = get("%s/v1/leaderboard/%s" % (BASE, region))
    entries = data.get("entries") if isinstance(data, dict) else data
    if not entries:
        raise SystemExit("no entries for %s; response keys: %s"
                         % (region, list(data)[:8] if isinstance(data, dict) else type(data)))

    ordered = []          # raw board order, kept for the archive
    by_name = defaultdict(list)
    for i, e in enumerate(entries):
        name = e.get("account_name")
        if not name:
            continue
        by_name[name].append({
            "pos": i + 1,
            "name": name,
            "badge": e.get("badge_level"),
            "ranked_rank": e.get("ranked_rank"),
            "top_heroes": e.get("top_hero_ids") or [],
            "ids": {int(a) for a in (e.get("possible_account_ids") or []) if a},
        })
        ordered.append({
            "pos": i + 1,
            "name": name,
            "top_heroes": e.get("top_hero_ids") or [],
            # CANDIDATE list, not an identity — one name can carry 30+ ids.
            # Only trustworthy once cross-checked against a resolved pool
            # member, which archive_snapshot.py does.
            "ids": [int(a) for a in (e.get("possible_account_ids") or []) if a][:8],
        })

    print("  [board] %-9s %d entries, %d named, depth %d"
          % (region, len(entries), sum(len(v) for v in by_name.values()), len(entries)),
          file=sys.stderr)
    return by_name, len(entries), ordered


def fetch_hero_board(region, hero_id):
    """One hero's board for one region. Position is the list index."""
    try:
        data = get("%s/v1/leaderboard/%s/%d" % (BASE, region, hero_id))
    except Exception as e:
        print("  [hb] %s hero %d -> %s" % (region, hero_id, e), file=sys.stderr)
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    out = []
    for i, e in enumerate(entries or [], 1):
        nm = e.get("account_name")
        if not nm:
            continue
        out.append({"hero_pos": i, "name": nm,
                    "ids": {int(a) for a in (e.get("possible_account_ids") or []) if a}})
    return out


def all_on_board(hero_entries, by_name):
    """
    EVERY dual-confirmed player on a hero's board, not just the best one.

    Previously this returned only the lowest cross-hero position, because
    position WAS the ordering. Net wins is not known at this stage, so all
    confirmed candidates are returned and the winner is chosen after their
    ranked records arrive.

    A match needs BOTH the display name and an account id to agree —
    possible_account_ids is a candidate list, and name-only matching put the
    wrong player in 110 of 371 slots historically.
    """
    found = []
    for he in hero_entries:
        for cand in by_name.get(he["name"], []):
            common = he["ids"] & cand["ids"]
            if common:
                found.append({
                    "name": cand["name"],
                    # the id on BOTH boards — the strongest identity available
                    "account_id": min(common),
                    "pos": cand["pos"],
                    "hero_pos": he["hero_pos"],
                    "badge": cand["badge"],
                    "top_heroes": cand["top_heroes"],
                })
                break
    return found


# --------------------------------------------------------------------------
# RANKED RECORDS  (batched, no SQL)
# --------------------------------------------------------------------------


def fetch_ranked_records(account_ids):
    """
    account_id -> {"games": n, "wins": n} over RANKED play, all heroes.

    /v1/players/hero-stats is batched via repeated account_ids params, runs at
    100 req/s, and is a different bucket from /v1/sql. It returns one row per
    (account, hero); totalling across heroes gives the account-level record,
    which is what a ladder position reflects — rank is an account property, not
    a per-hero one.
    """
    ids = sorted(set(int(a) for a in account_ids if a))
    if not ids:
        return {}
    out = defaultdict(lambda: {"games": 0, "wins": 0})
    base = "%s/v1/players/hero-stats?match_mode=%s" % (BASE, urllib.parse.quote(MATCH_MODE))
    # ~22.7 encoded chars per id (SCHEMA.md quirk 4); size the chunk against the
    # REAL url length, prefix included, rather than the query string alone
    per = 24
    chunk = max(20, (MAX_URL - len(base) - 40) // per)
    calls = 0
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        url = base + "".join("&account_ids=%d" % a for a in part)
        if len(url) > MAX_URL:
            part = part[:max(20, len(part) // 2)]
            url = base + "".join("&account_ids=%d" % a for a in part)
        try:
            rows = get(url)
        except Exception as e:
            print("  [hs] chunk %d-%d failed: %s" % (i + 1, i + len(part), e),
                  file=sys.stderr)
            continue
        calls += 1
        for r in rows or []:
            a = r.get("account_id")
            if a is None:
                continue
            # `matches` is a LIST OF MATCH IDS on this endpoint; the count is
            # `matches_played`. Taking `matches` first handed a list to int().
            g = r.get("matches_played")
            if g is None:
                m = r.get("matches")
                g = len(m) if isinstance(m, list) else (m or 0)
            w = r.get("wins") or 0
            if isinstance(w, list):
                w = len(w)
            rec = out[int(a)]
            rec["games"] += int(g)
            rec["wins"] += int(w)
        time.sleep(0.05)
    print("  [hs] %d accounts over %d calls (no SQL used)" % (len(out), calls),
          file=sys.stderr)
    return out


def ceiling_value(rec):
    """
    THE ORDERING. Net wins over ranked play: wins minus losses.

    Replace this one function when the ladder stops being progression-based —
    see the module docstring. Everything else keys off whatever it returns.
    """
    return 2 * rec["wins"] - rec["games"]


def shrunk(rec, k=None):
    """Secondary reference column, not the ordering."""
    k = SHRINK_K if k is None else k
    return (rec["wins"] + k * 0.5) / (rec["games"] + k)


# --------------------------------------------------------------------------


def main():
    tier = {r["hero"]: r for r in csv.DictReader(open(os.path.join(OUT_DIR, "tierlist.csv")))}

    print("[1/3] leaderboards", file=sys.stderr)
    boards = {}
    for region in REGIONS:
        boards[region] = fetch_board(region)
    depth = {r: boards[r][1] for r in REGIONS}

    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "board.json"), "w", encoding="utf-8") as f:
            json.dump({rg: {"depth": boards[rg][1],
                            "entries": boards[rg][2][:BOARD_ARCHIVE_TOP]}
                       for rg in REGIONS}, f, separators=(",", ":"), ensure_ascii=False)
        print("  [board] wrote %s/board.json (top %d per region)"
              % (OUT_DIR, BOARD_ARCHIVE_TOP), file=sys.stderr)
    except Exception as e:
        print("  [warn] could not write board.json (%s)" % e, file=sys.stderr)

    hero_ids = {}
    for h, t in tier.items():
        try:
            hero_ids[int(t["hero_id"])] = h
        except (KeyError, TypeError, ValueError):
            continue
    if not hero_ids:
        raise SystemExit("no hero ids in tierlist.csv")

    print("[2/3] cross-referencing hero boards against the general board",
          file=sys.stderr)
    confirmed = {}            # (region, hero_id) -> [candidate, ...]
    for rg in REGIONS:
        by_name = boards[rg][0]
        n_missing = 0
        for hid, hero in sorted(hero_ids.items(), key=lambda kv: kv[1]):
            hb = fetch_hero_board(rg, hid)
            time.sleep(0.15)          # 100 req/s allowed; stay well clear
            cands = all_on_board(hb, by_name) if hb else []
            confirmed[(rg, hid)] = (cands, len(hb))
            if not cands:
                n_missing += 1
        print("  [xref] %-9s %d heroes with a confirmed player, %d without"
              % (rg, sum(1 for k, v in confirmed.items()
                         if k[0] == rg and v[0]), n_missing), file=sys.stderr)

    print("[3/3] ranked records for the confirmed players", file=sys.stderr)
    every_id = {c["account_id"] for (cands, _n) in confirmed.values() for c in cands}
    records = fetch_ranked_records(every_id)

    per_region = defaultdict(list)
    for (rg, hid), (cands, board_size) in confirmed.items():
        if not cands:
            continue
        hero = hero_ids[hid]
        scored = []
        for c in cands:
            rec = records.get(c["account_id"])
            if not rec or not rec["games"]:
                continue
            scored.append((ceiling_value(rec), rec, c))
        if not scored:
            continue
        # the ceiling: the confirmed player with the most net ranked wins.
        # Ties break on the better cross-hero board position, then on their
        # standing on this hero's own board.
        scored.sort(key=lambda t: (-t[0], t[2]["pos"], t[2]["hero_pos"]))
        net, rec, c = scored[0]
        t = tier.get(hero, {})
        per_region[rg].append({
            "hero": hero,
            "hero_id": hid,
            "region": rg,
            "ceiling_player": c["name"],
            "account_id": c["account_id"],
            # THE ORDERING
            "net_wins": net,
            "ranked_games": rec["games"],
            "ranked_wins": rec["wins"],
            "shrunk_winrate": round(shrunk(rec), 4),
            # reference only — what the ceiling used to be ordered by
            "global_pos": c["pos"],
            "region_depth": depth[rg],
            "pct": round(100.0 * c["pos"] / max(depth[rg], 1), 3),
            "badge_level": c["badge"],
            "hero_ladder_pos": c["hero_pos"],
            "match": "confirmed",
            "valve_top_hero": "YES" if hid in (c["top_heroes"] or []) else "",
            "located_on_general": len(cands),
            "scored_candidates": len(scored),
            "board_size": board_size,
            "winrate_rank": t.get("rank", ""),
            "elite_winrate": t.get("elite_winrate", ""),
        })

    out = []
    for rg in REGIONS:
        # Order heroes by their ceiling player's net wins, descending. Ties
        # fall back to the cross-hero board position and then to the pool win
        # rate, so ordering never depends on dict iteration order — one account
        # can be the ceiling for two heroes (a McGinnis/Ivy dual-main) and
        # would otherwise be sorted arbitrarily.
        def _wr(d):
            try:
                return float(d["elite_winrate"])
            except (TypeError, ValueError):
                return -1.0
        rows = sorted(per_region.get(rg, []),
                      key=lambda d: (-d["net_wins"], d["global_pos"], -_wr(d)))
        for i, d in enumerate(rows, 1):
            d["ceiling_rank"] = i
        out.extend(rows)

    for region in REGIONS:
        have = {d["hero"] for d in out if d["region"] == region}
        gap = sorted(set(tier) - have)
        if gap:
            print("  [warn] %s: no confirmed player with ranked games for %d heroes: %s"
                  % (region, len(gap), ", ".join(gap)), file=sys.stderr)

    cols = ["region", "ceiling_rank", "hero", "hero_id", "ceiling_player",
            "account_id", "net_wins", "ranked_games", "ranked_wins",
            "shrunk_winrate", "global_pos", "region_depth", "pct", "badge_level",
            "hero_ladder_pos", "match", "valve_top_hero", "located_on_general",
            "scored_candidates", "board_size", "winrate_rank", "elite_winrate"]
    path = os.path.join(OUT_DIR, "ceiling.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    print("  -> %s (%d hero-region rows)" % (path, len(out)), file=sys.stderr)

    for region in REGIONS:
        rows = [d for d in out if d["region"] == region][:10]
        if not rows:
            continue
        print("\n  %s — top 10 by ceiling (%d heroes ranked)"
              % (region, sum(1 for d in out if d["region"] == region)), file=sys.stderr)
        print("  %-4s %-12s %-16s %6s %7s %8s %s" %
              ("#", "hero", "ceiling player", "net", "games", "shrunk", "boardpos"),
              file=sys.stderr)
        for d in rows:
            print("  %-4d %-12s %-16s %6d %7d %8.3f %s" %
                  (d["ceiling_rank"], d["hero"][:12], (d["ceiling_player"] or "?")[:16],
                   d["net_wins"], d["ranked_games"], d["shrunk_winrate"],
                   d["global_pos"]), file=sys.stderr)

    dup = defaultdict(list)
    for d in out:
        dup[(d["ceiling_player"], d["region"])].append(d["hero"])
    shared = {k: v for k, v in dup.items() if len(v) > 1}
    if shared:
        print("\n  [warn] one player is the ceiling for several heroes:", file=sys.stderr)
        for (nm, reg), hs in shared.items():
            print("    %-16s %-8s %s" % (nm, reg, ", ".join(hs)), file=sys.stderr)


if __name__ == "__main__":
    main()
