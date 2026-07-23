#!/usr/bin/env python3
"""
Deadlock tier-list + item-frequency pipeline.

Replaces the screenshot workflow. Two SQL queries against deadlock-api's
ClickHouse endpoint, plus two static asset fetches. No API key required.

Outputs (written to ./output/):
    tierlist.csv        heroes ranked by the max MMR among their top one-tricks
    candidates.csv      every qualifying player considered, with their MMR
    item_frequency.csv  item pick rates per hero, bucketed by purchase phase
    hero_splits.csv     each hero's stat split derived from soul spend by category

Only stdlib. Run:  python3 deadlock_pipeline.py
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")  # optional; raises rate limits

LOOKBACK_DAYS = 90     # how far back to scan matches
MIN_BADGE = 100        # only consider lobbies at/above this average badge (0-116)
EMA_WINDOW = 50        # matches in the MMR EMA (matches deadlock-api's definition)
EMA_ALPHA = 2.0 / (EMA_WINDOW + 1)
RECENCY_WINDOW = 25    # your rule: hero must appear in the player's last N games
PLAYERS_PER_HERO = 10  # candidates kept per hero
MIN_HERO_MATCHES = 5   # minimum games on the hero inside the lookback

# When the most recent qualifying game has no parsed item data:
#   "step_back" -> use their next-most-recent game on that hero still inside
#                  the recency window
#   "drop"      -> discard the player, the next candidate takes their place
MISSING_ITEMS_FALLBACK = "step_back"

# Purchase-phase boundaries, in souls at time of purchase (net_worth_at_buy).
PHASES = [(0, 4800, "early"), (4800, 8000, "mid"), (8000, 10**9, "late")]

SQL_PAUSE_S = 35       # /v1/sql allows 2 req/min without a key
OUT_DIR = "output"

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-pipeline/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def sql(query):
    """Run one ClickHouse query. Returns a list of dicts."""
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query)
    print("  [sql] %d chars ..." % len(query), file=sys.stderr)
    try:
        rows = _get(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        raise SystemExit("SQL request failed (%s):\n%s\n\nQuery was:\n%s"
                         % (e.code, body, query))
    if isinstance(rows, dict):          # some formats wrap results
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] %d rows" % len(rows), file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# ASSETS
# --------------------------------------------------------------------------

# /v1/assets/items field names aren't pinned in the OpenAPI spec, so probe.
_CATEGORY_KEYS = ("item_slot_type", "slot_type", "type", "category", "item_type")
_NAME_KEYS = ("name", "display_name", "class_name")
# deadlock uses weapon/vitality/spirit; map onto your G / V / S notation
_CATEGORY_MAP = {"weapon": "G", "vitality": "V", "spirit": "S",
                 "tech": "S", "armor": "V", "gun": "G"}


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def load_assets():
    heroes, items = {}, {}
    for h in _get(BASE + "/v1/assets/heroes"):
        hid = h.get("id", h.get("hero_id"))
        if hid is not None:
            heroes[int(hid)] = _first(h, _NAME_KEYS) or ("hero_%s" % hid)

    raw_items = _get(BASE + "/v1/assets/items")
    unmapped = set()
    for it in raw_items:
        iid = it.get("id", it.get("item_id"))
        if iid is None:
            continue
        cat_raw = _first(it, _CATEGORY_KEYS)
        cat = None
        if cat_raw:
            key = str(cat_raw).lower().replace("eitemslottype_", "").strip()
            cat = _CATEGORY_MAP.get(key)
            if cat is None:
                unmapped.add(key)
        items[int(iid)] = {
            "name": _first(it, _NAME_KEYS) or ("item_%s" % iid),
            "cat": cat,
            "cost": it.get("cost") or it.get("item_cost"),
        }
    if unmapped:
        print("  [assets] unmapped categories (extend _CATEGORY_MAP): %s"
              % sorted(unmapped)[:12], file=sys.stderr)
    print("  [assets] %d heroes, %d items" % (len(heroes), len(items)), file=sys.stderr)
    return heroes, items


# --------------------------------------------------------------------------
# QUERY 1 — candidates + MMR
# --------------------------------------------------------------------------

Q_CANDIDATES = """
WITH recent AS (
    SELECT
        account_id,
        hero_id,
        match_id,
        start_time,
        if(team = 'Team0', average_badge_team0, average_badge_team1) AS team_badge,
        row_number() OVER (PARTITION BY account_id ORDER BY start_time DESC) AS rn
    FROM match_player
    WHERE match_mode = 'Ranked'
      AND game_mode  = 'Normal'
      AND start_time >= now() - INTERVAL {lookback} DAY
      AND average_badge_team0 IS NOT NULL
      AND average_badge_team1 IS NOT NULL
      AND greatest(average_badge_team0, average_badge_team1) >= {min_badge}
),
mmr AS (
    SELECT
        account_id,
        sum(score * w) / sum(w) AS mmr
    FROM (
        SELECT
            account_id,
            (intDiv(team_badge, 10) - 1) * 6 + (team_badge % 10) AS score,
            pow({keep}, rn - 1) AS w
        FROM recent
        WHERE rn <= {ema_window}
    )
    GROUP BY account_id
),
hero_recent AS (
    SELECT
        account_id,
        hero_id,
        count()                        AS hero_matches,
        min(rn)                        AS best_rn,
        argMin(match_id, rn)           AS last_match_id,
        argMin(start_time, rn)         AS last_played,
        groupArray(64)(match_id)       AS recent_match_ids
    FROM recent
    WHERE rn <= {recency}
    GROUP BY account_id, hero_id
)
SELECT
    h.hero_id            AS hero_id,
    h.account_id         AS account_id,
    m.mmr                AS mmr,
    h.hero_matches       AS hero_matches,
    h.best_rn            AS best_rn,
    h.last_match_id      AS last_match_id,
    h.last_played        AS last_played,
    h.recent_match_ids   AS recent_match_ids
FROM hero_recent AS h
INNER JOIN mmr AS m USING (account_id)
WHERE h.hero_matches >= {min_hero_matches}
ORDER BY hero_id ASC, mmr DESC
LIMIT {per_hero} BY hero_id
"""


def query_candidates():
    q = Q_CANDIDATES.format(
        lookback=LOOKBACK_DAYS,
        min_badge=MIN_BADGE,
        keep=repr(1.0 - EMA_ALPHA),
        ema_window=EMA_WINDOW,
        recency=RECENCY_WINDOW,
        min_hero_matches=MIN_HERO_MATCHES,
        per_hero=PLAYERS_PER_HERO * 2,   # headroom for the items fallback
    )
    return sql(q)


# --------------------------------------------------------------------------
# QUERY 2 — item purchases for the chosen matches
# --------------------------------------------------------------------------

Q_ITEMS = """
SELECT
    account_id,
    hero_id,
    match_id,
    item_id,
    nwb   AS net_worth_at_buy,
    sold  AS sold_time_s
FROM match_player
ARRAY JOIN
    items.item_id          AS item_id,
    items.net_worth_at_buy AS nwb,
    items.sold_time_s      AS sold
WHERE (match_id, account_id) IN ({pairs})
"""


def query_items(pairs):
    literal = ",".join("(%d,%d)" % (m, a) for m, a in pairs)
    return sql(Q_ITEMS.format(pairs=literal))


# --------------------------------------------------------------------------
# ASSEMBLY
# --------------------------------------------------------------------------


def phase_of(souls):
    for lo, hi, label in PHASES:
        if lo <= souls < hi:
            return label
    return PHASES[-1][2]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    print("[1/4] assets", file=sys.stderr)
    heroes, items = load_assets()

    print("[2/4] candidate query", file=sys.stderr)
    cands = query_candidates()
    if not cands:
        raise SystemExit("No candidates. Try lowering MIN_BADGE or raising LOOKBACK_DAYS.")

    by_hero = defaultdict(list)
    for r in cands:
        by_hero[int(r["hero_id"])].append(r)

    # first pass: most recent qualifying match per (hero, player)
    wanted, attempt = [], {}
    for hid, rows in by_hero.items():
        for r in rows[:PLAYERS_PER_HERO]:
            mid, aid = int(r["last_match_id"]), int(r["account_id"])
            wanted.append((mid, aid))
            attempt[(hid, aid)] = r

    print("[3/4] item query (%d player-matches)" % len(wanted), file=sys.stderr)
    time.sleep(SQL_PAUSE_S)
    item_rows = query_items(wanted)

    have = {(int(r["match_id"]), int(r["account_id"])) for r in item_rows}
    missing = [k for k in wanted if k not in have]
    if missing:
        print("  [warn] %d of %d player-matches have no parsed item data (%s)"
              % (len(missing), len(wanted), MISSING_ITEMS_FALLBACK), file=sys.stderr)

    # ---- tier list -------------------------------------------------------
    tier = []
    for hid, rows in by_hero.items():
        usable = rows
        if MISSING_ITEMS_FALLBACK == "drop":
            usable = [r for r in rows
                      if (int(r["last_match_id"]), int(r["account_id"])) in have]
        usable = usable[:PLAYERS_PER_HERO]
        if not usable:
            continue
        tier.append({
            "hero_id": hid,
            "hero": heroes.get(hid, "hero_%d" % hid),
            "max_mmr": round(float(usable[0]["mmr"]), 4),
            "top_account_id": int(usable[0]["account_id"]),
            "median_mmr": round(
                sorted(float(r["mmr"]) for r in usable)[len(usable) // 2], 4),
            "players_used": len(usable),
        })
    tier.sort(key=lambda d: -d["max_mmr"])
    for i, row in enumerate(tier, 1):
        row["rank"] = i

    write_csv("tierlist.csv", tier,
              ["rank", "hero_id", "hero", "max_mmr", "median_mmr",
               "top_account_id", "players_used"])

    write_csv("candidates.csv", [{
        "hero_id": int(r["hero_id"]),
        "hero": heroes.get(int(r["hero_id"]), ""),
        "account_id": int(r["account_id"]),
        "mmr": round(float(r["mmr"]), 4),
        "hero_matches": r["hero_matches"],
        "games_ago": r["best_rn"],
        "last_match_id": r["last_match_id"],
        "last_played": r["last_played"],
        "has_items": (int(r["last_match_id"]), int(r["account_id"])) in have,
    } for rows in by_hero.values() for r in rows],
        ["hero_id", "hero", "account_id", "mmr", "hero_matches", "games_ago",
         "last_match_id", "last_played", "has_items"])

    # ---- item frequency + splits ----------------------------------------
    print("[4/4] aggregating", file=sys.stderr)
    picks = defaultdict(lambda: defaultdict(int))   # (hero, phase) -> item -> n
    builds = defaultdict(set)                       # hero -> {(match, account)}
    souls = defaultdict(lambda: defaultdict(int))   # hero -> cat -> souls

    for r in item_rows:
        hid, iid = int(r["hero_id"]), int(r["item_id"])
        nwb = int(r["net_worth_at_buy"] or 0)
        builds[hid].add((int(r["match_id"]), int(r["account_id"])))
        picks[(hid, phase_of(nwb))][iid] += 1
        meta = items.get(iid)
        if meta and meta["cat"] and meta["cost"]:
            souls[hid][meta["cat"]] += int(meta["cost"])

    freq = []
    for (hid, phase), counter in picks.items():
        n = max(len(builds[hid]), 1)
        for iid, c in sorted(counter.items(), key=lambda kv: -kv[1]):
            freq.append({
                "hero_id": hid,
                "hero": heroes.get(hid, ""),
                "phase": phase,
                "item_id": iid,
                "item": items.get(iid, {}).get("name", "item_%d" % iid),
                "category": items.get(iid, {}).get("cat") or "?",
                "count": c,
                "pct_of_builds": round(100.0 * c / n, 1),
                "builds": n,
            })
    freq.sort(key=lambda d: (d["hero"], d["phase"], -d["count"]))
    write_csv("item_frequency.csv", freq,
              ["hero_id", "hero", "phase", "item_id", "item", "category",
               "count", "pct_of_builds", "builds"])

    splits = []
    for hid, cats in souls.items():
        total = sum(cats.values()) or 1
        ordered = sorted(("V", "G", "S"), key=lambda c: -cats.get(c, 0))
        splits.append({
            "hero_id": hid,
            "hero": heroes.get(hid, ""),
            "V_pct": round(100.0 * cats.get("V", 0) / total, 1),
            "G_pct": round(100.0 * cats.get("G", 0) / total, 1),
            "S_pct": round(100.0 * cats.get("S", 0) / total, 1),
            "split": "".join(sorted(ordered[:2])),
            "weak": ordered[2],
        })
    splits.sort(key=lambda d: d["hero"])
    write_csv("hero_splits.csv", splits,
              ["hero_id", "hero", "split", "weak", "V_pct", "G_pct", "S_pct"])

    print("\nDone. %d heroes ranked, %d item rows, %d splits."
          % (len(tier), len(freq), len(splits)), file=sys.stderr)


def write_csv(name, rows, cols):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
