#!/usr/bin/env python3
"""
Deadlock tier-list + item-frequency pipeline.

Candidates are the top qualifying players from each region's hero ladder
(Valve's own leaderboard, passed through by deadlock-api). Ratings and item
data come from SQL.

Outputs (./output/):
    tierlist.csv         heroes ranked by their pool's pooled win rate ON THE HERO
    candidates.csv       the sampled players, per hero per region
    item_frequency.csv   hold rates per hero per net-worth snapshot
    hero_splits.csv      V/G/S soul share and split classification, per snapshot
    excluded.csv         ladder entries dropped, with the reason

NAMING NOTE. hero_games / hero_wins count a player's games and wins ON THE HERO
named in the same row, across the whole lookback. They were called games_all /
wins_all, which read as "all heroes" and caused a downstream misreading.
offhero_games / offhero_wins are that player's record on EVERY OTHER hero, and
exist so hero strength can be separated from the general skill of whoever mains
the hero. hero_matches is a different quantity again: games on the hero within
the player's last RECENCY_WINDOW matches, so it caps at that value.

Stdlib only.  Run:  python3 deadlock_pipeline.py
"""

import csv
import json
import math
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

PER_REGION = _env("PER_REGION", 10)              # qualifying players per region per hero
LEADERBOARD_DEPTH = _env("LEADERBOARD_DEPTH", 40)  # ladder entries read, for backfill

POOL_PER_HERO = _env("POOL_PER_HERO", 500)       # rows SQL returns per hero
RECENCY_WINDOW = _env("RECENCY_WINDOW", 25)
MIN_HERO_MATCHES = _env("MIN_HERO_MATCHES", 1)
LOOKBACK_DAYS = _env("LOOKBACK_DAYS", 90)
EMA_WINDOW = _env("EMA_WINDOW", 50)
EMA_ALPHA = 2.0 / (EMA_WINDOW + 1)

# A player needs at least this many off-hero games before their off-hero win
# rate is worth pooling; below it the baseline is noise.
MIN_OFFHERO_GAMES = _env("MIN_OFFHERO_GAMES", 20)

# Assign each account to exactly one hero (the one it played most in the recency
# window) and drop it from every other hero's pool.
#
# OFF by default. Deadlock requires a minimum of 3 selected heroes and Eternus
# forces at least 2 at high priority, so single-hero mains do not exist at this
# rank — the rule was deleting genuine specialists from a hero's pool because
# they were one game busier on their other high-priority pick. With it off, an
# account can appear under several heroes; its rows stay hero-specific either
# way, since every stat is computed from that account's games ON that hero.
EXCLUSIVITY = (os.environ.get("EXCLUSIVITY") or "0") == "1"

# A ranked mode update shipped ~2026-07-30. Confirmed live and non-trivial:
# match_mode='Ranked' now returns real rows (924-1,080 over a 3-day sample,
# vs. ~841k Unranked), and ~97% of those Ranked rows also carry
# game_mode='Normal' — so the cohort currently pools Ranked and Unranked
# together at roughly 1,400:1 with no way to tell them apart downstream.
# Decide deliberately: MATCH_MODE=Unranked excludes ranked games (closest to
# today's existing cohort), MATCH_MODE=Ranked switches to ranked-only once
# there's enough volume, and "" (default) keeps pooling both as now.
MATCH_MODE = os.environ.get("MATCH_MODE") or ""
GAME_MODE = os.environ.get("GAME_MODE") or "Normal"
MODE_SQL = ("match_mode = '%s' AND " % MATCH_MODE if MATCH_MODE else "") + \
           ("game_mode = '%s' AND " % GAME_MODE if GAME_MODE else "")

SNAPSHOTS = [int(x) for x in
             (os.environ.get("SNAPSHOTS") or "4800,9600,14400,20800").split(",")]

MAX_URL = _env("MAX_URL", 6000)                  # encoded-URL ceiling; Cloudflare 414s well above
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
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-pipeline/5.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


_sql_calls = [0]


def sql(query, label=""):
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query)
    if len(url) > MAX_URL:
        raise SystemExit("Query URL is %d chars (limit %d) — %s would 414. "
                         "Reduce the batch size." % (len(url), MAX_URL, label or "query"))
    if _sql_calls[0]:
        print("  [sql] pausing %ds for the rate limit" % SQL_PAUSE_S, file=sys.stderr)
        time.sleep(SQL_PAUSE_S)
    _sql_calls[0] += 1
    print("  [sql] #%d %s (%d char url)" % (_sql_calls[0], label, len(url)), file=sys.stderr)
    try:
        rows = _get(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:800]
        raise SystemExit("SQL failed (%s) on %s:\n%s\n\nQuery:\n%s"
                         % (e.code, label, body, query[:2000]))
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] -> %d rows" % len(rows), file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# ASSETS
# --------------------------------------------------------------------------

# ItemSlotType enum from the spec: weapon | spirit | vitality
_CATEGORY_MAP = {"weapon": "G", "spirit": "S", "vitality": "V"}


def _pick(d, keys):
    for k in keys:
        v = (d or {}).get(k)
        if v:
            return v
    return ""


def load_assets():
    heroes, hero_icon = {}, {}
    for h in _get(BASE + "/v1/assets/heroes"):
        hid = h.get("id")
        if hid is None or h.get("disabled") or h.get("in_development"):
            continue
        heroes[int(hid)] = h.get("name") or ("hero_%s" % hid)
        hero_icon[int(hid)] = _pick(h.get("images"),
                                    ("icon_hero_card", "icon_image_small",
                                     "icon_hero_card_webp", "icon_image_small_webp",
                                     "minimap_image"))

    raw = [it for it in _get(BASE + "/v1/assets/items")
           if it.get("type") == "upgrade" and it.get("id") is not None]

    by_class = {}
    for it in raw:
        cn = it.get("class_name")
        if cn:
            by_class[cn] = int(it["id"])

    items, component_of, unmapped, unresolved = {}, defaultdict(set), set(), 0
    for it in raw:
        iid = int(it["id"])
        slot = (it.get("item_slot_type") or "").lower()
        cat = _CATEGORY_MAP.get(slot)
        if slot and cat is None:
            unmapped.add(slot)
        items[iid] = {"name": it.get("name") or ("item_%d" % iid),
                      "cat": cat,
                      "cost": int(it.get("cost") or 0),
                      "tier": it.get("item_tier"),
                      "icon": _pick(it, ("shop_image_small", "shop_image", "image",
                                         "shop_image_small_webp", "shop_image_webp",
                                         "image_webp"))}
        # component_items is an array of CLASS NAMES per the spec
        for comp in (it.get("component_items") or []):
            cid = by_class.get(comp) if isinstance(comp, str) else (
                comp if isinstance(comp, int) else None)
            if cid is None:
                unresolved += 1
            else:
                component_of[cid].add(iid)

    if unmapped:
        print("  [assets] UNMAPPED slot types: %s" % sorted(unmapped), file=sys.stderr)
    print("  [assets] %d heroes, %d shop items, %d with a parent (%d component names "
          "unresolved)" % (len(heroes), len(items), len(component_of), unresolved),
          file=sys.stderr)
    if not component_of:
        sample = next((it for it in raw if it.get("component_items")), None)
        print("  [assets] WARNING: no component linkage. Sample item keys: %s"
              % (sorted(sample.keys())[:20] if sample else "none had component_items"),
              file=sys.stderr)
    have_icons = sum(1 for v in items.values() if v["icon"])
    print("  [assets] icons: %d/%d heroes, %d/%d items"
          % (sum(1 for v in hero_icon.values() if v), len(heroes), have_icons, len(items)),
          file=sys.stderr)
    return heroes, hero_icon, items, component_of


# --------------------------------------------------------------------------
# LEADERBOARDS
# --------------------------------------------------------------------------


def fetch_ladders(heroes):
    ladder = {}
    for hid in sorted(heroes):
        total = 0
        for region in REGIONS:
            url = "%s/v1/leaderboard/%s/%d" % (BASE, urllib.parse.quote(region), hid)
            try:
                payload = _get(url)
            except urllib.error.HTTPError as e:
                print("  [lb] %s hero %d -> HTTP %s" % (region, hid, e.code), file=sys.stderr)
                ladder[(hid, region)] = []
                continue
            entries = payload.get("entries", []) if isinstance(payload, dict) else payload
            rows = []
            for pos, e in enumerate(entries[:LEADERBOARD_DEPTH], 1):
                rows.append({"hero_id": hid, "region": region, "ladder_pos": pos,
                             "account_name": e.get("account_name", ""),
                             "badge_level": e.get("badge_level")
                                            or e.get("ranked_rank") or 0,
                             "ids": {int(a) for a in
                                     (e.get("possible_account_ids") or [])}})
            ladder[(hid, region)] = rows
            total += len(rows)
        print("  [lb] hero %-3d %-22s %d entries" % (hid, heroes[hid][:22], total),
              file=sys.stderr)
    return ladder


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

# No account-id list: an IN clause of ~90k ids produced a 908KB URL and a 414.
# Instead SQL picks the elite population itself and Python intersects with the
# ladder afterwards, which also prunes most of the ambiguous name matches.
#
# hero_perf is grouped by (account_id, hero_id) -> a player's record ON ONE HERO.
# acct_perf is grouped by account_id alone -> the same player across EVERY hero.
# Subtracting gives the off-hero baseline, which is what lets a hero's win rate
# be separated from the general skill of the players who main it. Both read the
# same `recent` CTE, so this costs no extra SQL call.

# The candidate population is now the accounts that showed up on Valve's own
# per-hero leaderboards (fetch_ladders) — that IS the "elite" definition; SQL
# no longer reconstructs it from badge. badge/mmr are still computed here for
# reference (median_mmr etc. in tierlist.csv) but nothing is FILTERED on them
# anymore, so the badge-zero issue (2026-07-31, upstream on match_player,
# unrelated to the leaderboard endpoint) can't zero out the candidate pool —
# only degrade the mmr column, which the site doesn't show.
#
# An IN clause of the full ~90k ladder ids produced a 908KB URL and a 414
# (2026-07-25). query_pool() chunks the id list the same way query_items()
# chunks (match_id, account_id) pairs, self-halving on a 414.
Q_POOL = """
WITH recent AS (
    SELECT account_id, hero_id, match_id, start_time, won,
        if(team = 'Team0', average_badge_team0, average_badge_team1) AS team_badge,
        row_number() OVER (PARTITION BY account_id ORDER BY start_time DESC) AS rn
    FROM match_player
    WHERE account_id IN ({ids})
      AND {mode}start_time >= now() - INTERVAL {lookback} DAY
),
mmr AS (
    SELECT account_id, sum(score * w) / sum(w) AS mmr
    FROM (
        SELECT account_id,
               (intDiv(team_badge, 10) - 1) * 6 + (team_badge % 10) AS score,
               pow({keep}, rn - 1) AS w
        FROM recent WHERE rn <= {ema} AND team_badge IS NOT NULL
    )
    GROUP BY account_id
),
hero_perf AS (
    SELECT account_id, hero_id, count() AS hero_games, sum(won) AS hero_wins
    FROM recent GROUP BY account_id, hero_id
),
acct_perf AS (
    SELECT account_id, count() AS acct_games, sum(won) AS acct_wins
    FROM recent GROUP BY account_id
),
hero_recent AS (
    SELECT account_id, hero_id,
           count()                AS hero_matches,
           min(rn)                AS best_rn,
           argMin(match_id, rn)   AS last_match_id,
           argMin(start_time, rn) AS last_played
    FROM recent WHERE rn <= {recency}
    GROUP BY account_id, hero_id
)
SELECT h.account_id AS account_id, h.hero_id AS hero_id, m.mmr AS mmr,
       h.hero_matches AS hero_matches, h.best_rn AS best_rn,
       h.last_match_id AS last_match_id, h.last_played AS last_played,
       p.hero_games AS hero_games, p.hero_wins AS hero_wins,
       a.acct_games AS acct_games, a.acct_wins AS acct_wins
FROM hero_recent AS h
INNER JOIN mmr AS m USING (account_id)
INNER JOIN hero_perf AS p USING (account_id, hero_id)
INNER JOIN acct_perf AS a USING (account_id)
WHERE h.hero_matches >= {minhero}
ORDER BY hero_id ASC, mmr DESC
LIMIT {pool} BY hero_id
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
WHERE match_id IN ({mids}) AND account_id IN ({aids})
"""


def query_pool(account_ids):
    """Chunked so no single URL exceeds MAX_URL — same pattern as query_items()."""
    ids = sorted(account_ids)
    rows, chunk, i = [], max(50, MAX_URL // 8), 0
    while i < len(ids):
        part = ids[i:i + chunk]
        q = Q_POOL.format(lookback=LOOKBACK_DAYS, ids=",".join(str(a) for a in part),
                          keep=repr(1.0 - EMA_ALPHA), ema=EMA_WINDOW,
                          recency=RECENCY_WINDOW, minhero=MIN_HERO_MATCHES,
                          pool=POOL_PER_HERO, mode=MODE_SQL)
        if len(urllib.parse.quote(q)) > MAX_URL and chunk > 50:
            chunk = max(50, chunk // 2)
            continue
        rows.extend(sql(q, "candidate pool %d-%d of %d" % (i + 1, i + len(part), len(ids))))
        i += len(part)
    return rows


def query_items(pairs):
    """Chunked so no single URL exceeds MAX_URL."""
    rows, chunk, i = [], max(20, MAX_URL // 40), 0
    while i < len(pairs):
        part = pairs[i:i + chunk]
        q = Q_ITEMS.format(mids=",".join(str(m) for m, _ in part),
                           aids=",".join(str(a) for _, a in part))
        if len(urllib.parse.quote(q)) > MAX_URL and chunk > 20:
            chunk = max(20, chunk // 2)
            continue
        rows.extend(sql(q, "items %d-%d of %d" % (i + 1, i + len(part), len(pairs))))
        i += len(part)
    return rows


# --------------------------------------------------------------------------
# SNAPSHOTS
# --------------------------------------------------------------------------


def snapshot_holdings(purchases, component_of):
    """(item_id, net_worth_at_buy, game_time_s, sold_time_s) -> {label: held set}.

    An item counts at a threshold if bought at or below that net worth and not
    sold by the time it was reached. Components are suppressed when the item
    they build into is also held: an 800 upgraded to a 1600 is 1600, not 2400.
    """
    def prune(held):
        return {i for i in held if not (component_of.get(i, set()) & held)}

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
# STATS HELPERS
# --------------------------------------------------------------------------


def _wr(wins, games):
    return 100.0 * wins / games if games else None


def _se(games):
    """Standard error of a win rate near 50%, in percentage points."""
    return 100.0 * math.sqrt(0.25 / games) if games else None


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    excluded = []

    print("[1/5] assets", file=sys.stderr)
    heroes, hero_icon, items, component_of = load_assets()

    print("[2/5] ladders (%s), depth %d, target %d per region, exclusivity %s"
          % (", ".join(REGIONS), LEADERBOARD_DEPTH, PER_REGION,
             "ON" if EXCLUSIVITY else "OFF"), file=sys.stderr)
    ladder = fetch_ladders(heroes)
    ladder_ids = {a for rows in ladder.values() for r in rows for a in r["ids"]}
    print("  [lb] %d distinct candidate ids across all entries" % len(ladder_ids),
          file=sys.stderr)
    if not ladder_ids:
        raise SystemExit("No account ids resolved from any leaderboard entry. Not writing "
                         "CSVs — leaving yesterday's output/data.json in place.")

    print("[3/5] candidate pool query (%d ladder-sourced ids)" % len(ladder_ids),
          file=sys.stderr)
    stats = {}
    for r in query_pool(ladder_ids):
        stats[(int(r["account_id"]), int(r["hero_id"]))] = r
    print("  [pool] %d (account,hero) rows" % len(stats), file=sys.stderr)
    if not stats:
        raise SystemExit("0 rows back for %d ladder-sourced ids — match_player may be down "
                         "or LOOKBACK_DAYS too narrow. Not writing CSVs — leaving yesterday's "
                         "output/data.json in place." % len(ladder_ids))

    # resolve each ladder entry to one account: of the possible ids, keep the one
    # with the most games on THIS hero inside the window
    for (hid, _rg), rows in ladder.items():
        for r in rows:
            scored = [(int(stats[(a, hid)]["hero_matches"]), a)
                      for a in r["ids"] if (a, hid) in stats]
            r["account_id"] = max(scored)[1] if scored else None
            r["ambiguous"] = len(scored) > 1

    played = defaultdict(dict)
    for (hid, _rg), rows in ladder.items():
        for r in rows:
            if r["account_id"] is not None:
                played[r["account_id"]][hid] = int(stats[(r["account_id"], hid)]["hero_matches"])

    home = {}
    for aid, counts in played.items():
        top = max(counts.values())
        home[aid] = sorted(h for h, n in counts.items() if n == top)[0]

    chosen = defaultdict(list)
    for (hid, region), rows in sorted(ladder.items()):
        taken = 0
        for r in rows:
            if taken >= PER_REGION:
                break
            base = {"hero_id": hid, "region": region, "ladder_pos": r["ladder_pos"],
                    "account_name": r["account_name"], "badge_level": r["badge_level"]}
            aid = r["account_id"]
            if aid is None:
                excluded.append(dict(base, reason=(
                    "no account id (likely private)" if not r["ids"]
                    else "no candidate id in the elite pool, or hero not in last %d games"
                         % RECENCY_WINDOW)))
                continue
            if EXCLUSIVITY and home[aid] != hid:
                excluded.append(dict(base, reason="duplicate; assigned to %s (%d games)"
                                     % (heroes.get(home[aid], home[aid]),
                                        played[aid][home[aid]])))
                continue
            if any(c["account_id"] == aid for c in chosen[hid]):
                excluded.append(dict(base, reason="already sampled in another region"))
                continue
            s = stats[(aid, hid)]
            hg = int(s.get("hero_games") or 0)
            hw = int(s.get("hero_wins") or 0)
            # off-hero = everything the account played minus this hero
            og = max(int(s.get("acct_games") or 0) - hg, 0)
            ow = max(int(s.get("acct_wins") or 0) - hw, 0)
            chosen[hid].append(dict(base, account_id=aid, mmr=float(s["mmr"]),
                                    hero_matches=int(s["hero_matches"]),
                                    best_rn=int(s["best_rn"]),
                                    last_match_id=int(s["last_match_id"]),
                                    last_played=s["last_played"],
                                    hero_games=hg, hero_wins=hw,
                                    offhero_games=og, offhero_wins=ow,
                                    ambiguous=r["ambiguous"]))
            taken += 1

    wanted = [(c["last_match_id"], c["account_id"])
              for lst in chosen.values() for c in lst]
    # which region each sampled build came from, so item frequencies can be
    # reported per region rather than pooled
    build_region = {(c["last_match_id"], c["account_id"]): c["region"]
                    for lst in chosen.values() for c in lst}
    print("[4/5] item query (%d player-matches)" % len(wanted), file=sys.stderr)
    item_rows = query_items(wanted) if wanted else []

    print("[5/5] aggregating", file=sys.stderr)
    keep = {(m, a) for m, a in wanted}
    per_build, skipped_abilities = defaultdict(list), set()
    for r in item_rows:
        key = (int(r["match_id"]), int(r["account_id"]))
        if key not in keep:          # the IN-pair filter is done here, not in SQL
            continue
        iid = int(r["item_id"])
        if iid not in items:     # hero abilities also appear in items.item_id
            skipped_abilities.add(iid)
            continue
        per_build[(int(r["hero_id"]),) + key].append(
            (iid, int(r["net_worth_at_buy"] or 0),
             int(r["game_time_s"] or 0), int(r["sold_time_s"] or 0)))

    if skipped_abilities:
        print("  [items] skipped %d non-shop ids (hero abilities)"
              % len(skipped_abilities), file=sys.stderr)

    builds = defaultdict(int)
    builds_rg = defaultdict(int)
    holds = defaultdict(lambda: defaultdict(int))
    souls = defaultdict(lambda: defaultdict(int))

    for (hid, _m, _a), purchases in per_build.items():
        rg = build_region.get((_m, _a), "")
        builds[hid] += 1
        builds_rg[(hid, rg)] += 1
        for snap, held in snapshot_holdings(purchases, component_of).items():
            for iid in held:
                holds[(hid, rg, snap)][iid] += 1
                meta = items.get(iid)
                if meta and meta["cat"] and meta["cost"]:
                    souls[(hid, snap)][meta["cat"]] += meta["cost"]

    freq = []
    for (hid, rg, snap), counter in holds.items():
        n = builds_rg[(hid, rg)] or 1
        for iid, c in counter.items():
            if c < 2:                      # exclude single-instance items
                continue
            m = items.get(iid, {})
            freq.append({"hero_id": hid, "hero": heroes.get(hid, ""), "region": rg,
                         "snapshot": snap,
                         "item_id": iid, "item": m.get("name", "item_%d" % iid),
                         "category": m.get("cat") or "?", "tier": m.get("tier"),
                         "icon_url": m.get("icon", ""),
                         "count": c, "of_builds": n})
    freq.sort(key=lambda d: (d["hero"], d["region"],
                             SNAPSHOT_ORDER.index(d["snapshot"]), -d["count"]))

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

    # Lane positioning is an early-game construct, so the split that slots a hero
    # into the composition framework is the FIRST snapshot, not postgame.
    early = {s["hero_id"]: s for s in splits if s["snapshot"] == SNAPSHOT_ORDER[0]}

    tier = []
    for hid, lst in chosen.items():
        if not lst:
            continue
        by_mmr = sorted(lst, key=lambda c: -c["mmr"])
        n = builds.get(hid, 0)
        g = sum(c.get("hero_games", 0) for c in lst)
        w = sum(c.get("hero_wins", 0) for c in lst)
        # Only players with a usable off-hero sample contribute to the baseline.
        thick = [c for c in lst if c.get("offhero_games", 0) >= MIN_OFFHERO_GAMES]
        og = sum(c["offhero_games"] for c in thick)
        ow = sum(c["offhero_wins"] for c in thick)
        hero_wr, off_wr = _wr(w, g), _wr(ow, og)
        top5 = [c["mmr"] for c in by_mmr[:5]]
        per_reg = {rg: sum(1 for c in lst if c["region"] == rg) for rg in REGIONS}
        tier.append({"hero_id": hid, "hero": heroes.get(hid, ""),
                     "top_mmr": round(by_mmr[0]["mmr"], 4),
                     "top5_mmr": round(sum(top5) / len(top5), 4),
                     "median_mmr": round(sorted(c["mmr"] for c in lst)[len(lst) // 2], 4),
                     "elite_winrate": round(hero_wr, 2) if hero_wr is not None else "",
                     "elite_games": g,
                     "elite_se": round(_se(g), 2) if g else "",
                     "offhero_winrate": round(off_wr, 2) if off_wr is not None else "",
                     "offhero_games": og,
                     "winrate_delta": (round(hero_wr - off_wr, 2)
                                       if (hero_wr is not None and off_wr is not None)
                                       else ""),
                     "delta_se": (round(math.sqrt(_se(g) ** 2 + _se(og) ** 2), 2)
                                  if (g and og) else ""),
                     "offhero_players": len(thick),
                     "players": len(lst), "builds_sampled": n,
                     "thin": "YES" if n < TARGET_BUILDS else "",
                     "by_region": " ".join("%s=%d" % (r, per_reg[r]) for r in REGIONS),
                     "top_account_id": by_mmr[0]["account_id"],
                     "lane_split": early.get(hid, {}).get("split", ""),
                     "lane_weak": early.get(hid, {}).get("weak", ""),
                     "lane_role": {"GS": "damage"}.get(
                         early.get(hid, {}).get("split", ""), "frontline/support"),
                     "icon_url": hero_icon.get(hid, "")})
    # ranked by pooled elite win rate: badge saturates at the top, win rate does not
    tier.sort(key=lambda d: -(d["elite_winrate"] or 0))
    for i, t in enumerate(tier, 1):
        t["rank"] = i
    # a second ordering, by how much the pool outperforms its own off-hero baseline
    for i, t in enumerate(sorted(tier, key=lambda d: -(d["winrate_delta"]
                                                       if d["winrate_delta"] != "" else -99)), 1):
        t["delta_rank"] = i

    write("tierlist.csv", tier,
          ["rank", "hero_id", "hero", "elite_winrate", "elite_games", "elite_se",
           "offhero_winrate", "offhero_games", "offhero_players",
           "winrate_delta", "delta_se", "delta_rank",
           "lane_split", "lane_weak", "lane_role", "median_mmr", "top5_mmr",
           "top_mmr", "players", "builds_sampled", "thin", "by_region",
           "top_account_id", "icon_url"])
    write("candidates.csv",
          [dict(c, hero=heroes.get(c["hero_id"], ""), mmr=round(c["mmr"], 4))
           for lst in chosen.values() for c in sorted(lst, key=lambda x: -x["mmr"])],
          ["hero_id", "hero", "region", "ladder_pos", "account_id", "account_name",
           "badge_level", "mmr", "hero_games", "hero_wins",
           "offhero_games", "offhero_wins", "hero_matches", "best_rn",
           "last_match_id", "last_played", "ambiguous"])
    write("item_frequency.csv", freq,
          ["hero_id", "hero", "region", "snapshot", "item_id", "item", "category", "tier",
           "count", "of_builds", "icon_url"])
    write("hero_splits.csv", splits,
          ["hero_id", "hero", "snapshot", "split", "weak", "V_pct", "G_pct", "S_pct"])
    write("excluded.csv", excluded,
          ["hero_id", "region", "ladder_pos", "account_name", "badge_level", "reason"])

    thin = [t["hero"] for t in tier if t["thin"]]
    if thin:
        print("  [warn] thin heroes (<%d builds): %s"
              % (TARGET_BUILDS, ", ".join(thin[:12])), file=sys.stderr)

    # The band widths in PROMPT-tierlist assume a particular sample size. If the
    # pool or lookback changes, the bands have to move with the standard error.
    ses = [t["elite_se"] for t in tier if t["elite_se"] != ""]
    if ses:
        mean_se = sum(ses) / len(ses)
        print("  [bands] mean elite_se %.2f pts -> a 2.0-pt band is %.1f SE"
              % (mean_se, 2.0 / mean_se), file=sys.stderr)
        if 2.0 / mean_se < 1.3:
            print("  [bands] WARNING: bands are finer than the data supports; "
                  "widen them or enlarge the pool", file=sys.stderr)

    moved = [t for t in tier if t["delta_rank"] != "" and abs(t["delta_rank"] - t["rank"]) >= 8]
    if moved:
        print("  [delta] heroes shifting >=8 places on the off-hero baseline: %s"
              % ", ".join("%s %d->%d" % (t["hero"], t["rank"], t["delta_rank"])
                          for t in moved[:10]), file=sys.stderr)

    print("\nDone. %d heroes, %d builds, %d item rows, %d SQL calls."
          % (len(tier), sum(builds.values()), len(freq), _sql_calls[0]), file=sys.stderr)


def write(name, rows, cols):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
