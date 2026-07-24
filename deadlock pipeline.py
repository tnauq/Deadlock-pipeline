#!/usr/bin/env python3
"""
Deadlock tier-list + item-frequency pipeline.

Candidates are the top 10 qualifying players from each of the NAmerica and
Europe hero ladders (Valve's own leaderboard, passed through by deadlock-api).
Ratings and item data come from SQL.

Outputs (./output/):
    tierlist.csv         heroes ranked by the top MMR in their sampled pool
    candidates.csv       the sampled players, per hero per region
    item_frequency.csv   hold rates per hero per net-worth snapshot
    hero_splits.csv      V/G/S soul share and split classification, per snapshot
    excluded.csv         ladder entries dropped, with the reason

Stdlib only.  Run:  python3 deadlock_pipeline.py
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")


def _env(name, default, cast=int):
    v = os.environ.get(name)
    return cast(v) if v not in (None, "") else default


# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]

PER_REGION = _env("PER_REGION", 10)             # qualifying players per region per hero
LEADERBOARD_DEPTH = _env("LEADERBOARD_DEPTH", 60)  # ladder entries read, to backfill
MIN_BADGE = _env("MIN_BADGE", 0)                # 0 = trust the ladder; 113 to tighten
RECENCY_WINDOW = _env("RECENCY_WINDOW", 25)     # hero must appear in last N games
MIN_HERO_MATCHES = _env("MIN_HERO_MATCHES", 1)
LOOKBACK_DAYS = _env("LOOKBACK_DAYS", 90)
EMA_WINDOW = _env("EMA_WINDOW", 50)
EMA_ALPHA = 2.0 / (EMA_WINDOW + 1)

SNAPSHOTS = [int(x) for x in
             (os.environ.get("SNAPSHOTS") or "4800,9600,14400,20800").split(",")]

SQL_PAUSE_S = _env("SQL_PAUSE_S", 35)
OUT_DIR = "output"


def _label(t):
    return ("%.1fk" % (t / 1000.0)).replace(".0k", "k")


SNAPSHOT_ORDER = [_label(t) for t in SNAPSHOTS] + ["postgame"]
TARGET_BUILDS = PER_REGION * len(REGIONS)

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _get(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-pipeline/3.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def sql(query):
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query)
    print("  [sql] %d chars ..." % len(query), file=sys.stderr)
    try:
        rows = _get(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:1000]
        raise SystemExit("SQL failed (%s):\n%s\n\nQuery:\n%s" % (e.code, body, query))
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] %d rows" % len(rows), file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# ASSETS
# --------------------------------------------------------------------------

# ItemSlotType enum from the spec: weapon | spirit | vitality
_CATEGORY_MAP = {"weapon": "G", "spirit": "S", "vitality": "V"}


def load_assets():
    heroes = {}
    for h in _get(BASE + "/v1/assets/heroes"):
        hid = h.get("id")
        if hid is None or h.get("disabled") or h.get("in_development"):
            continue
        heroes[int(hid)] = h.get("name") or ("hero_%s" % hid)

    items, component_of, unmapped = {}, defaultdict(set), set()
    for it in _get(BASE + "/v1/assets/items"):
        iid = it.get("id")
        if iid is None or it.get("type") != "upgrade":   # shop items only
            continue
        iid = int(iid)
        slot = (it.get("item_slot_type") or "").lower()
        cat = _CATEGORY_MAP.get(slot)
        if slot and cat is None:
            unmapped.add(slot)
        items[iid] = {"name": it.get("name") or ("item_%d" % iid),
                      "cat": cat,
                      "cost": int(it.get("cost") or 0),
                      "tier": it.get("item_tier")}
        for comp in (it.get("component_items") or []):
            # component_items may be ids or class names; keep ints only
            if isinstance(comp, int):
                component_of[comp].add(iid)
            elif isinstance(comp, dict) and isinstance(comp.get("id"), int):
                component_of[comp["id"]].add(iid)

    if unmapped:
        print("  [assets] UNMAPPED slot types: %s" % sorted(unmapped), file=sys.stderr)
    print("  [assets] %d heroes, %d shop items, %d with a parent"
          % (len(heroes), len(items), len(component_of)), file=sys.stderr)
    if not component_of:
        print("  [assets] WARNING: no component_items linkage found — upgraded "
              "items may be double-counted in soul spend", file=sys.stderr)
    return heroes, items, component_of


# --------------------------------------------------------------------------
# LEADERBOARDS
# --------------------------------------------------------------------------


def fetch_ladders(heroes):
    """-> ladder[(hero_id, region)] = ordered list of entry dicts."""
    ladder = {}
    for hid in sorted(heroes):
        total = 0
        for region in REGIONS:
            url = "%s/v1/leaderboard/%s/%d" % (BASE, urllib.parse.quote(region), hid)
            try:
                payload = _get(url)
            except urllib.error.HTTPError as e:
                print("  [lb] %s hero %d -> HTTP %s" % (region, hid, e.code),
                      file=sys.stderr)
                ladder[(hid, region)] = []
                continue
            entries = payload.get("entries", []) if isinstance(payload, dict) else payload
            rows = []
            for pos, e in enumerate(entries[:LEADERBOARD_DEPTH], 1):
                rows.append({"hero_id": hid, "region": region, "ladder_pos": pos,
                             "account_name": e.get("account_name", ""),
                             "badge_level": e.get("badge_level")
                                            or e.get("ranked_rank") or 0,
                             "ids": [int(a) for a in
                                     (e.get("possible_account_ids") or [])]})
            ladder[(hid, region)] = rows
            total += len(rows)
        print("  [lb] hero %-3d %-22s %d entries"
              % (hid, heroes[hid][:22], total), file=sys.stderr)
    return ladder


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

Q_PLAYERS = """
WITH recent AS (
    SELECT
        account_id,
        hero_id,
        match_id,
        start_time,
        if(team = 'Team0', average_badge_team0, average_badge_team1) AS team_badge,
        row_number() OVER (PARTITION BY account_id ORDER BY start_time DESC) AS rn
    FROM match_player
    WHERE account_id IN ({accounts})
      AND match_mode = 'Ranked'
      AND game_mode  = 'Normal'
      AND start_time >= now() - INTERVAL {lookback} DAY
      AND average_badge_team0 IS NOT NULL
      AND average_badge_team1 IS NOT NULL
),
mmr AS (
    SELECT account_id, sum(score * w) / sum(w) AS mmr, count() AS rated_matches
    FROM (
        SELECT account_id,
               (intDiv(team_badge, 10) - 1) * 6 + (team_badge % 10) AS score,
               pow({keep}, rn - 1) AS w
        FROM recent
        WHERE rn <= {ema_window}
    )
    GROUP BY account_id
),
hero_recent AS (
    SELECT account_id, hero_id,
           count()                AS hero_matches,
           min(rn)                AS best_rn,
           argMin(match_id, rn)   AS last_match_id,
           argMin(start_time, rn) AS last_played
    FROM recent
    WHERE rn <= {recency}
    GROUP BY account_id, hero_id
)
SELECT h.account_id AS account_id, h.hero_id AS hero_id, m.mmr AS mmr,
       m.rated_matches AS rated_matches, h.hero_matches AS hero_matches,
       h.best_rn AS best_rn, h.last_match_id AS last_match_id,
       h.last_played AS last_played
FROM hero_recent AS h
INNER JOIN mmr AS m USING (account_id)
WHERE h.hero_matches >= {min_hero_matches}
"""

Q_ITEMS = """
SELECT account_id, hero_id, match_id,
       item_id, nwb AS net_worth_at_buy, bought AS game_time_s, sold AS sold_time_s
FROM match_player
ARRAY JOIN
    items.item_id          AS item_id,
    items.net_worth_at_buy AS nwb,
    items.game_time_s      AS bought,
    items.sold_time_s      AS sold
WHERE (match_id, account_id) IN ({pairs})
"""


def query_players(account_ids):
    return sql(Q_PLAYERS.format(
        accounts=",".join(str(a) for a in sorted(account_ids)),
        lookback=LOOKBACK_DAYS, keep=repr(1.0 - EMA_ALPHA), ema_window=EMA_WINDOW,
        recency=RECENCY_WINDOW, min_hero_matches=MIN_HERO_MATCHES))


def query_items(pairs):
    lit = ",".join("(%d,%d)" % (m, a) for m, a in pairs)
    return sql(Q_ITEMS.format(pairs=lit))


# --------------------------------------------------------------------------
# SNAPSHOTS
# --------------------------------------------------------------------------


def snapshot_holdings(purchases, component_of):
    """purchases: (item_id, net_worth_at_buy, game_time_s, sold_time_s).

    Returns {label: set(item_id)} of items HELD at each net-worth mark, with
    components suppressed where the item they build into is also held — an 800
    upgraded to a 1600 is 1600 spent, not 2400.
    """
    def prune(held):
        return {i for i in held
                if not (component_of.get(i, set()) & held)}

    out = {}
    for t in SNAPSHOTS:
        upto = [p for p in purchases if p[1] <= t]
        if not upto:
            out[_label(t)] = set()
            continue
        reached = max(p[2] for p in upto)
        out[_label(t)] = prune({p[0] for p in upto if not p[3] or p[3] > reached})
    out["postgame"] = prune({p[0] for p in purchases if not p[3]})
    return out


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    excluded = []

    print("[1/5] assets", file=sys.stderr)
    heroes, items, component_of = load_assets()

    print("[2/5] ladders (%s), depth %d, target %d per region"
          % (", ".join(REGIONS), LEADERBOARD_DEPTH, PER_REGION), file=sys.stderr)
    ladder = fetch_ladders(heroes)
    all_ids = {a for rows in ladder.values() for r in rows for a in r["ids"]}
    print("  [lb] %d candidate account ids" % len(all_ids), file=sys.stderr)
    if not all_ids:
        raise SystemExit("No ladder entries. Check REGIONS.")

    print("[3/5] rating + recency query", file=sys.stderr)
    stats = {(int(r["account_id"]), int(r["hero_id"])): r
             for r in query_players(all_ids)}

    # ---- resolve each ladder entry to one account -----------------------
    # ambiguity is broken by games on THIS hero inside the window
    for rows in ladder.values():
        for r in rows:
            scored = [(int(stats[(a, r["hero_id"])]["hero_matches"]), a)
                      for a in r["ids"] if (a, r["hero_id"]) in stats]
            r["account_id"] = max(scored)[1] if scored else None
            r["dropped_ids"] = [a for a in r["ids"] if a != r["account_id"]]

    # ---- cross-hero ownership: most games inside the window -------------
    played = defaultdict(dict)
    for (hid, _rg), rows in ladder.items():
        for r in rows:
            if r["account_id"] is None:
                continue
            s = stats[(r["account_id"], hid)]
            played[r["account_id"]][hid] = int(s["hero_matches"])

    home = {}
    for aid, counts in played.items():
        top = max(counts.values())
        tied = sorted(h for h, n in counts.items() if n == top)
        home[aid] = tied[0]

    # ---- fill the per-region quota in ladder order ----------------------
    chosen = defaultdict(list)     # hero_id -> selected candidate dicts
    for (hid, region), rows in sorted(ladder.items()):
        taken = 0
        for r in rows:
            if taken >= PER_REGION:
                break
            base = {"hero_id": hid, "region": region, "ladder_pos": r["ladder_pos"],
                    "account_name": r["account_name"], "badge_level": r["badge_level"]}
            if r["account_id"] is None:
                excluded.append(dict(base, reason=(
                    "no account id (likely private)" if not r["ids"]
                    else "hero not in player's last %d games" % RECENCY_WINDOW)))
                continue
            if MIN_BADGE and r["badge_level"] and r["badge_level"] < MIN_BADGE:
                excluded.append(dict(base, reason="below badge floor %d" % MIN_BADGE))
                continue
            aid = r["account_id"]
            if home[aid] != hid:
                excluded.append(dict(base, reason="duplicate; assigned to %s (%d games)"
                                     % (heroes.get(home[aid], home[aid]),
                                        played[aid][home[aid]])))
                continue
            if any(c["account_id"] == aid for c in chosen[hid]):
                excluded.append(dict(base, reason="already sampled in another region"))
                continue
            s = stats[(aid, hid)]
            chosen[hid].append(dict(base, account_id=aid, mmr=float(s["mmr"]),
                                    hero_matches=int(s["hero_matches"]),
                                    best_rn=int(s["best_rn"]),
                                    last_match_id=int(s["last_match_id"]),
                                    last_played=s["last_played"],
                                    ambiguous=bool(r["dropped_ids"])))
            taken += 1

    wanted = [(c["last_match_id"], c["account_id"])
              for lst in chosen.values() for c in lst]

    print("[4/5] item query (%d player-matches)" % len(wanted), file=sys.stderr)
    time.sleep(SQL_PAUSE_S)
    item_rows = query_items(wanted)

    # ---- aggregate -------------------------------------------------------
    print("[5/5] aggregating", file=sys.stderr)
    per_build = defaultdict(list)
    for r in item_rows:
        per_build[(int(r["hero_id"]), int(r["match_id"]), int(r["account_id"]))].append(
            (int(r["item_id"]), int(r["net_worth_at_buy"] or 0),
             int(r["game_time_s"] or 0), int(r["sold_time_s"] or 0)))

    builds = defaultdict(int)
    holds = defaultdict(lambda: defaultdict(int))
    souls = defaultdict(lambda: defaultdict(int))

    for (hid, _m, _a), purchases in per_build.items():
        builds[hid] += 1
        for snap, held in snapshot_holdings(purchases, component_of).items():
            for iid in held:
                holds[(hid, snap)][iid] += 1
                meta = items.get(iid)
                if meta and meta["cat"] and meta["cost"]:
                    souls[(hid, snap)][meta["cat"]] += meta["cost"]

    freq = []
    for (hid, snap), counter in holds.items():
        n = builds[hid] or 1
        for iid, c in counter.items():
            m = items.get(iid, {})
            freq.append({"hero_id": hid, "hero": heroes.get(hid, ""), "snapshot": snap,
                         "item_id": iid, "item": m.get("name", "item_%d" % iid),
                         "category": m.get("cat") or "?", "tier": m.get("tier"),
                         "builds_with_item": c, "builds": n,
                         "pct": round(100.0 * c / n, 1)})
    freq.sort(key=lambda d: (d["hero"], SNAPSHOT_ORDER.index(d["snapshot"]), -d["pct"]))

    splits = []
    for (hid, snap), cats in souls.items():
        total = sum(cats.values()) or 1
        order = sorted(("V", "G", "S"), key=lambda c: -cats.get(c, 0))
        splits.append({"hero_id": hid, "hero": heroes.get(hid, ""), "snapshot": snap,
                       "split": "".join(sorted(order[:2])), "weak": order[2],
                       "V_pct": round(100.0 * cats.get("V", 0) / total, 1),
                       "G_pct": round(100.0 * cats.get("G", 0) / total, 1),
                       "S_pct": round(100.0 * cats.get("S", 0) / total, 1)})
    splits.sort(key=lambda d: (d["hero"], SNAPSHOT_ORDER.index(d["snapshot"])))

    tier = []
    for hid, lst in chosen.items():
        if not lst:
            continue
        by_mmr = sorted(lst, key=lambda c: -c["mmr"])
        n = builds.get(hid, 0)
        per_reg = {rg: sum(1 for c in lst if c["region"] == rg) for rg in REGIONS}
        tier.append({"hero_id": hid, "hero": heroes.get(hid, ""),
                     "top_mmr": round(by_mmr[0]["mmr"], 4),
                     "median_mmr": round(
                         sorted(c["mmr"] for c in lst)[len(lst) // 2], 4),
                     "players": len(lst), "builds_sampled": n,
                     "thin": "YES" if n < TARGET_BUILDS else "",
                     "by_region": " ".join("%s=%d" % (r, per_reg[r]) for r in REGIONS),
                     "top_account_id": by_mmr[0]["account_id"]})
    tier.sort(key=lambda d: -d["top_mmr"])
    for i, t in enumerate(tier, 1):
        t["rank"] = i

    write("tierlist.csv", tier,
          ["rank", "hero_id", "hero", "top_mmr", "median_mmr", "players",
           "builds_sampled", "thin", "by_region", "top_account_id"])
    write("candidates.csv",
          [dict(c, hero=heroes.get(c["hero_id"], ""), mmr=round(c["mmr"], 4))
           for lst in chosen.values() for c in sorted(lst, key=lambda x: -x["mmr"])],
          ["hero_id", "hero", "region", "ladder_pos", "account_id", "account_name",
           "badge_level", "mmr", "hero_matches", "best_rn", "last_match_id",
           "last_played", "ambiguous"])
    write("item_frequency.csv", freq,
          ["hero_id", "hero", "snapshot", "item_id", "item", "category", "tier",
           "builds_with_item", "builds", "pct"])
    write("hero_splits.csv", splits,
          ["hero_id", "hero", "snapshot", "split", "weak", "V_pct", "G_pct", "S_pct"])
    write("excluded.csv", excluded,
          ["hero_id", "region", "ladder_pos", "account_name", "badge_level", "reason"])

    thin = [t["hero"] for t in tier if t["thin"]]
    if thin:
        print("  [warn] thin heroes (<%d builds): %s"
              % (TARGET_BUILDS, ", ".join(thin[:12])), file=sys.stderr)
    print("\nDone. %d heroes, %d builds, %d item rows."
          % (len(tier), sum(builds.values()), len(freq)), file=sys.stderr)


def write(name, rows, cols):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
