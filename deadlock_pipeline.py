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
from collections import Counter, defaultdict

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
LEADERBOARD_DEPTH = _env("LEADERBOARD_DEPTH", 40)  # hard ceiling on entries read per hero-region
# Adaptive depth (see fetch_ladders): read ~PER_REGION/LADDER_YIELD entries plus
# a margin, rather than always reading to LEADERBOARD_DEPTH. Measured yield was
# 1,259 players from 3,674 entries = 34%.
LADDER_YIELD = float(os.environ.get("LADDER_YIELD") or 0.34)
LADDER_MARGIN = _env("LADDER_MARGIN", 15)
LADDER_MIN_READ = _env("LADDER_MIN_READ", 40)

POOL_PER_HERO = _env("POOL_PER_HERO", 500)       # rows SQL returns per hero
RECENCY_WINDOW = _env("RECENCY_WINDOW", 25)
MAX_IDS_PER_ENTRY = _env("MAX_IDS_PER_ENTRY", 2)  # see fetch_ladders — caps the
                                                   # SQL candidate-id explosion
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

MAX_URL = _env("MAX_URL", 9000)   # ~9KB is documented as working (SCHEMA.md quirk #4)
# The SQL quota is 2 requests per 60s and the window SLIDES. A 429 response
# still counts as a request, so retrying inside the window burns another slot
# and pushes the window forward instead of clearing it — a 2026-08-07 run died
# after retries at 9s, 17s and 35s, each of which made things worse.
#
# 35s spacing puts two calls inside one 60s window by construction: #1 at
# t=0 and #2 at t=35 leaves 2 requests in the trailing minute, so #2 is at the
# limit and any retry is refused. Spacing must exceed HALF the window with
# margin; 32s is the theoretical floor and 40s is the safe one.
SQL_PAUSE_S = _env("SQL_PAUSE_S", 40)
# On a 429 the only reliable recovery is to let the whole window drain.
SQL_429_WAIT_S = _env("SQL_429_WAIT_S", 65)

# ---- orbit fill -----------------------------------------------------------
# Only 49 of 76 hero-regions reach a full 20 builds from the leaderboards; the
# rest run out of qualifying board members. Orbit 1 — everyone who shared a
# ranked match with a top board player — supplies the shortfall. Account ids
# come straight from match_player, so identity is exact rather than resolved
# from a display name.
#
# Measured 2026-08-08 (NAmerica, 3-day window, 12 seeds): orbit 1 is 949
# players with median win rate 0.530 and p90 0.629, against the seeds' 0.568
# and 0.634. Orbit 2 sits at the population mean (0.504) and is NOT used.
# In a 400-account sample, hero coverage was ample even for unpopular heroes:
# Lady Geist 82, Mirage 98, Grey Talon 41, Vyper 23.
#
# Cost: one SQL call per region, and only when that region has a short
# hero-region. Orbit players are appended AFTER board members, so a full pool
# never changes.
ORBIT_FILL = _env("ORBIT_FILL", 1)
ORBIT_SEEDS = _env("ORBIT_SEEDS", 12)
ORBIT_DAYS = _env("ORBIT_DAYS", 3)
ORBIT_MIN_HERO_GAMES = _env("ORBIT_MIN_HERO_GAMES", 5)
ORBIT_MIN_SEEDS_MET = _env("ORBIT_MIN_SEEDS_MET", 1)
# What orders orbit candidates for a build slot.
#   "breadth"  distinct seeds met first, hero win rate as the tiebreak
#   "winrate"  the reverse
# Breadth measures STANDING — meeting several different top players in a
# 3-day window is hard to do by accident — while hero win rate measures being
# good AT THE HERO, which is not the same thing. Breadth is only a 1-3 valued
# signal though, so if almost everyone sits at 1 the two orderings differ only
# for the few who met 2+. The `[orbit] seeds met:` line prints the
# distribution; if it is overwhelmingly {1: ...} this choice barely matters.
ORBIT_SORT = os.environ.get("ORBIT_SORT", "breadth")
# Unkeyed /v1/sql allows 2 req/min AND 20 req/hr (SCHEMA.md quirk #5). The
# hourly cap is the binding one for chunked queries — it is what killed the
# 2026-07-31 runs at chunk 21 both times. An X-API-Key raises this.
HOURLY_SQL_BUDGET = _env("HOURLY_SQL_BUDGET", 20)
OUT_DIR = "output"


def _label(t):
    return ("%.1fk" % (t / 1000.0)).replace(".0k", "k")


SNAPSHOT_ORDER = [_label(t) for t in SNAPSHOTS] + ["postgame"]
TARGET_BUILDS = PER_REGION * len(REGIONS)

# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _get(url, tries=_env("HTTP_TRIES", 4)):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-pipeline/5.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                wait = 2 ** attempt
                if e.code == 429:
                    # `next_request_in` is when the NEXT slot frees, not when
                    # the window is clear. Waiting exactly that long lands at
                    # the limit again, and the failed attempt has meanwhile
                    # consumed a slot of its own — which is how a 2026-08-07 run
                    # died after retries at 9s, 17s and 35s. Wait for the whole
                    # window to drain, taking the hint only if it is LONGER.
                    wait = SQL_429_WAIT_S
                    try:
                        body = json.loads(e.read().decode("utf-8", "replace"))
                        err = body.get("error", {}) or {}
                        hint = err.get("next_request_in")
                        period = (err.get("quota") or {}).get("period")
                        if period:
                            wait = max(wait, int(period) + 5)
                        if hint:
                            wait = max(wait, int(hint) + 2)
                    except Exception:
                        pass
                    print("  [http] 429, waiting %ds for the window to drain"
                          % wait, file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            # A dropped connection never reaches an HTTP status, so the block
            # above never saw it and one reset killed a whole run — a 2026-08-07
            # run died on hero 52 of 38 with "[Errno 104] Connection reset by
            # peer" during the TLS handshake, 24 ladders in. Transient network
            # faults get the same treatment as a 503.
            if attempt < tries - 1:
                wait = 2 ** attempt + 1
                print("  [http] %s — retrying in %ds (%d/%d)"
                      % (e, wait, attempt + 1, tries - 1), file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


_sql_calls = [0]


# The exact string sql() measures, so callers can size a chunk against the same
# number instead of guessing at the prefix. query_items() measured quote(q)
# alone, which is ~50 chars short of the real URL — a chunk could clear its own
# check at 8,997 and then die inside sql() at 9,007.
def sql_url(query):
    return BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query)


def sql(query, label=""):
    url = sql_url(query)
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
    _hero_sig_classes = {}
    for h in _get(BASE + "/v1/assets/heroes"):
        hid = h.get("id")
        if hid is None or h.get("disabled") or h.get("in_development"):
            continue
        heroes[int(hid)] = h.get("name") or ("hero_%s" % hid)
        hero_icon[int(hid)] = _pick(h.get("images"),
                                    ("icon_hero_card", "icon_image_small",
                                     "icon_hero_card_webp", "icon_image_small_webp",
                                     "minimap_image"))
        _hero_sig_classes[int(hid)] = [(h.get("items") or {}).get("signature%d" % k)
                                       for k in (1, 2, 3, 4)]

    _assets = _get(BASE + "/v1/assets/items")
    raw = [it for it in _assets
           if it.get("type") == "upgrade" and it.get("id") is not None]

    # Hero abilities share the items.item_id space in match_player (PROBES.md
    # finding 6). They used to be discarded as noise; they are the ability
    # point data. Probed 2026-08-06: 4,093 of 4,093 sampled ability rows fell
    # inside the hero's own signature1-4, so the join below is exact.
    abilities, abil_by_class = {}, {}
    for it in _assets:
        if it.get("type") != "ability" or it.get("id") is None:
            continue
        aid = int(it["id"])
        abilities[aid] = {"name": it.get("name") or ("ability_%d" % aid),
                          "upgrades": len(it.get("upgrades") or []),
                          "icon": _pick(it, ("image", "image_webp",
                                             "shop_image", "shop_image_small"))}
        if it.get("class_name"):
            abil_by_class[it["class_name"]] = aid

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
    # signature class names -> ability ids, in slot order (1-4)
    hero_sigs = {}
    for hid, classes in _hero_sig_classes.items():
        if hid in heroes:
            hero_sigs[hid] = [abil_by_class.get(c) for c in classes]
    n_sig = sum(1 for v in hero_sigs.values() if all(x is not None for x in v))
    print("  [assets] %d abilities, %d/%d heroes with all 4 signatures resolved"
          % (len(abilities), n_sig, len(hero_sigs)), file=sys.stderr)

    have_icons = sum(1 for v in items.values() if v["icon"])
    print("  [assets] icons: %d/%d heroes, %d/%d items"
          % (sum(1 for v in hero_icon.values() if v), len(heroes), have_icons, len(items)),
          file=sys.stderr)
    return heroes, hero_icon, items, component_of, abilities, hero_sigs


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
            # ADAPTIVE DEPTH. Reading 100 deep everywhere is wasted on boards
            # that fill easily and useless on boards that are short. Measured
            # 2026-07-31: every hero with a deep board (Bebop/Haze/Lash/Shiv at
            # ~100/region) filled its full 20/region, while the starved heroes
            # (Mirage 22/region, Grey Talon 17) have boards shorter than any
            # cap. So depth only ever bites heroes that don't need it.
            #
            # Yield is ~34% of entries -> qualifying players, so PER_REGION*3
            # plus a margin is enough to fill the pool. A short board is taken
            # whole. This keeps the SQL id count flat as ranked grows: at full
            # saturation a fixed depth of 100 projects to ~30 SQL calls against
            # the 20/hr unkeyed cap, which would break the run outright.
            want = min(LEADERBOARD_DEPTH,
                       max(LADDER_MIN_READ, int(PER_REGION / LADDER_YIELD) + LADDER_MARGIN))
            for pos, e in enumerate(entries[:want], 1):
                # NATIVE ORDER IS PRESERVED DELIBERATELY. The API does not return
                # possible_account_ids sorted numerically (observed: "wander" ->
                # [17403205, 56217724, 243091796, 1296699245, 884669372, ...]),
                # so the ordering is something the service chose — most likely
                # best-match-first. Sorting here would discard whatever signal
                # that carries and keep the numerically smallest ids instead,
                # which is arbitrary. Truncation therefore keeps the API's own
                # first N. resolved_idx below measures whether this assumption
                # actually holds.
                all_ids = []
                for a in (e.get("possible_account_ids") or []):
                    a = int(a)
                    if a not in all_ids:
                        all_ids.append(a)
                rows.append({"hero_id": hid, "region": region, "ladder_pos": pos,
                             "account_name": e.get("account_name", ""),
                             "badge_level": e.get("badge_level")
                                            or e.get("ranked_rank") or 0,
                             # A minority of names carry very long id lists (218
                             # of 1001 NA entries averaged 153 ids each) and
                             # inflated a ~7,600-entry leaderboard into 87,351
                             # SQL candidates — 470 chunks, 4.5+ hours at the
                             # rate limit (2026-07-31). Most entries have 1-2.
                             "ids_ordered": all_ids[:MAX_IDS_PER_ENTRY],
                             "ids": set(all_ids[:MAX_IDS_PER_ENTRY]),
                             "ids_truncated": len(all_ids) > MAX_IDS_PER_ENTRY})
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
# Q_POOL removed 2026-08-03 — the candidate pool now comes from
# /v1/players/hero-stats (batched, no SQL). See fetch_hero_stats().


# imbued_ability_id rides along in the SAME array join, so it costs no extra
# SQL call. Nine of the 156 items are imbue items whose effect applies to one
# chosen ability; this is the only place the game records which one was picked.
Q_ITEMS = """
SELECT account_id, hero_id, match_id,
       item_id, nwb AS net_worth_at_buy, bought AS game_time_s, sold AS sold_time_s,
       imbued AS imbued_ability_id
FROM match_player
ARRAY JOIN
    items.item_id           AS item_id,
    items.net_worth_at_buy  AS nwb,
    items.game_time_s       AS bought,
    items.sold_time_s       AS sold,
    items.imbued_ability_id AS imbued
WHERE match_id IN ({mids}) AND account_id IN ({aids})
"""


# --------------------------------------------------------------------------
# RANKED PER-HERO STATS  (replaces the chunked SQL candidate pool)
# --------------------------------------------------------------------------

# /v1/players/hero-stats gained a match_mode filter in the 2026-08-03 spec.
# Batched via repeated account_ids params, 100 req/s, and NOT /v1/sql — so the
# whole candidate pool now costs nothing against the 20/hr SQL budget. It
# returns everything the old Q_POOL query did:
#
#   matches_played -> hero_games      wins        -> hero_wins
#   max(matches)   -> last_match_id   last_played -> last_played
#
# match_mode is case-insensitive here (both 'ranked' and 'Ranked' returned
# identical rows when probed), unlike /v1/analytics/* where the wrong casing
# returns a bare 400. Lowercase matches the documented enum.
MODE_API = (os.environ.get("MATCH_MODE") or "Ranked").lower()
SHRINK_K = float(os.environ.get("SHRINK_K") or 25)


def shrunk(wins, games, k=None):
    """Win rate pulled toward 0.5 by sample size.

    Raw win rate is unusable as a selector at this volume — median ranked games
    per (account, hero) was 4 when probed, so a 3-0 record would outrank a
    46-35 one. k is the number of phantom coin-flips added; at k=25 a 10-0
    record lands below a 46-35, which is the ordering we want.
    """
    k = SHRINK_K if k is None else k
    return (wins + k * 0.5) / (games + k) if (games + k) else 0.5


Q_ORBIT = """
SELECT match_id, account_id
FROM match_player
WHERE match_id IN (
    SELECT match_id FROM match_player
    WHERE account_id IN ({ids})
      AND match_mode = '{mode}' AND game_mode = 'Normal'
      AND start_time >= now() - INTERVAL {days} DAY
)
"""


def fetch_orbit1(seed_ids):
    """
    account_id -> {"seeds_met": n, "shared": n} for everyone who shared a
    ranked match with a seed. ONE SQL call.

    Proximity is DISTINCT SEEDS met, not raw shared matches: a duo partner
    queuing with one seed all evening racks up matches without being of
    comparable standing, whereas meeting several different top players is hard
    to do by accident.
    """
    seeds = sorted(set(int(a) for a in seed_ids if a))[:ORBIT_SEEDS]
    if not seeds:
        return {}
    q = Q_ORBIT.format(ids=",".join(str(a) for a in seeds),
                       mode=MATCH_MODE, days=ORBIT_DAYS)
    if len(sql_url(q)) > MAX_URL:
        seeds = seeds[:max(4, len(seeds) // 2)]
        q = Q_ORBIT.format(ids=",".join(str(a) for a in seeds),
                           mode=MATCH_MODE, days=ORBIT_DAYS)
    try:
        rows = sql(q, "orbit1 from %d seeds" % len(seeds))
    except SystemExit:
        raise
    except Exception as e:
        print("  [orbit] failed (%s) — continuing without the fill" % e,
              file=sys.stderr)
        return {}
    by_match = defaultdict(set)
    for r in rows:
        by_match[int(r["match_id"])].add(int(r["account_id"]))
    seedset = set(seeds)
    acc = defaultdict(lambda: {"seeds_met": set(), "shared": 0})
    for accts in by_match.values():
        met = accts & seedset
        if not met:
            continue
        for a in accts - seedset:
            acc[a]["seeds_met"] |= met
            acc[a]["shared"] += 1
    out = {a: {"seeds_met": len(v["seeds_met"]), "shared": v["shared"]}
           for a, v in acc.items()}
    if out:
        breadth = Counter(v["seeds_met"] for v in out.values())
        print("  [orbit] %d matches, %d players; seeds met: %s"
              % (len(by_match), len(out), dict(sorted(breadth.items()))),
              file=sys.stderr)
    return out


def fetch_hero_stats(account_ids):
    """Per (account, hero) ranked stats for every candidate. No SQL."""
    ids = sorted(account_ids)
    out, i = {}, 0
    # ~12 chars per id as a repeated query param; keep URLs well under the cap
    chunk = max(50, MAX_URL // 14)
    calls = 0
    while i < len(ids):
        part = ids[i:i + chunk]
        q = "&".join("account_ids=%d" % a for a in part)
        url = "%s/v1/players/hero-stats?match_mode=%s&%s" % (BASE, MODE_API, q)
        if len(url) > MAX_URL and chunk > 50:
            chunk = max(50, chunk // 2)   # url here is already the full string
            continue
        try:
            rows = _get(url)
        except Exception as e:
            print("  [hs] chunk %d-%d failed: %s" % (i + 1, i + len(part), e),
                  file=sys.stderr)
            rows = []
        calls += 1
        for r in rows or []:
            aid, hid = r.get("account_id"), r.get("hero_id")
            if aid is None or hid is None:
                continue
            m = r.get("matches") or []
            out[(int(aid), int(hid))] = {
                "account_id": int(aid),
                "hero_id": int(hid),
                "hero_games": int(r.get("matches_played") or len(m) or 0),
                "hero_wins": int(r.get("wins") or 0),
                # match ids increase monotonically, so the largest is the most
                # recent — no extra query needed to find the build to sample
                "last_match_id": max(m) if m else None,
                "last_played": r.get("last_played"),
            }
        i += len(part)
        time.sleep(0.2)          # 100 req/s allowed; stay well clear
    print("  [hs] %d (account,hero) rows from %d calls (no SQL used)"
          % (len(out), calls), file=sys.stderr)
    return out


def account_totals(stats):
    """Per-account games/wins across ALL heroes, for the off-hero baseline."""
    tot = defaultdict(lambda: [0, 0])
    for (aid, _hid), v in stats.items():
        tot[aid][0] += v["hero_games"]
        tot[aid][1] += v["hero_wins"]
    return tot


def query_items(pairs):
    """Chunked so no single URL exceeds MAX_URL."""
    # ~22 encoded chars per (match_id, account_id) pair; //25 leaves room for
    # the query body. The old //40 was undersized and cost 3 extra requests —
    # which matters against the 20 req/HR unkeyed cap, not the 2/min one.
    # Halving on overflow wasted requests: a 360-pair chunk that missed by ten
    # characters dropped to 180 and left the URL half empty, turning 4 calls
    # into 8 against a 20/HOUR cap. Measure the real URL and resize in
    # proportion instead, so a chunk lands just under the limit.
    rows, chunk, i = [], max(20, MAX_URL // 25), 0
    while i < len(pairs):
        part = pairs[i:i + chunk]
        q = Q_ITEMS.format(mids=",".join(str(m) for m, _ in part),
                           aids=",".join(str(a) for _, a in part))
        n = len(sql_url(q))
        if n > MAX_URL:
            if chunk <= 20:
                raise SystemExit(
                    "items chunk of %d pairs is %d chars, over the %d limit, and "
                    "cannot be shrunk further. Raise MAX_URL or shorten Q_ITEMS."
                    % (len(part), n, MAX_URL))
            fixed = len(sql_url(Q_ITEMS.format(mids="", aids="")))
            per = max((n - fixed) / len(part), 1.0)
            chunk = max(20, min(chunk - 1, int((MAX_URL - fixed) / per * 0.97)))
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
    heroes, hero_icon, items, component_of, abilities, hero_sigs = load_assets()

    print("[2/5] ladders (%s), depth %d, target %d per region, exclusivity %s"
          % (", ".join(REGIONS), LEADERBOARD_DEPTH, PER_REGION,
             "ON" if EXCLUSIVITY else "OFF"), file=sys.stderr)
    ladder = fetch_ladders(heroes)
    ladder_ids = {a for rows in ladder.values() for r in rows for a in r["ids"]}
    n_truncated = sum(1 for rows in ladder.values() for r in rows if r["ids_truncated"])
    print("  [lb] %d distinct candidate ids across all entries (%d entries hit the "
          "%d-id cap)" % (len(ladder_ids), n_truncated, MAX_IDS_PER_ENTRY), file=sys.stderr)
    if not ladder_ids:
        raise SystemExit("No account ids resolved from any leaderboard entry. Not writing "
                         "CSVs — leaving yesterday's output/data.json in place.")

    # Candidate stats now come from /v1/players/hero-stats (batched, 100 req/s,
    # NOT /v1/sql), so the only SQL left in the run is the item query. That took
    # a run from ~16 SQL calls to ~4 and is why both regions can share one run
    # again instead of alternating.
    est_items = -(-(TARGET_BUILDS * len(heroes)) // max(20, MAX_URL // 25))
    print("  [lb] 0 pool + ~%d item = ~%d SQL requests this run (hourly cap %d)"
          % (est_items, est_items, HOURLY_SQL_BUDGET), file=sys.stderr)

    print("[3/5] ranked hero stats for %d ladder-sourced ids (no SQL)"
          % len(ladder_ids), file=sys.stderr)
    stats = fetch_hero_stats(ladder_ids)
    if not stats:
        raise SystemExit("hero-stats returned nothing for %d ladder-sourced ids. "
                         "Not writing CSVs — leaving yesterday's output/data.json "
                         "in place." % len(ladder_ids))
    acct_tot = account_totals(stats)

    # resolve each ladder entry to one account: of the possible ids, keep the one
    # with the most games on THIS hero inside the window
    for (hid, _rg), rows in ladder.items():
        for r in rows:
            # most ranked games on THIS hero wins the id. hero_matches (a
            # recency count over the last N matches) no longer exists — the
            # hero-stats equivalent is total ranked games on the hero.
            scored = [(stats[(a, hid)]["hero_games"], a)
                      for a in r["ids"] if (a, hid) in stats]
            r["account_id"] = max(scored)[1] if scored else None
            r["ambiguous"] = len(scored) > 1
            # Which slot in the API's native ordering won? If the API really is
            # returning best-match-first, this should cluster hard at 0 and
            # MAX_IDS_PER_ENTRY can drop to 1-2, removing the whole id-explosion
            # problem. If it's spread evenly, the order carries no signal and
            # truncating is a genuine accuracy cost that has to be justified
            # on cost grounds alone.
            r["resolved_idx"] = (r["ids_ordered"].index(r["account_id"])
                                 if r["account_id"] in r["ids_ordered"] else None)

    idxs = [r["resolved_idx"] for rows in ladder.values() for r in rows
            if r.get("resolved_idx") is not None]
    if idxs:
        hist = Counter(idxs)
        print("  [ids] resolved account's slot in the API's native order: %s"
              % dict(sorted(hist.items())), file=sys.stderr)
        print("  [ids] %d of %d resolved from slot 0 (%.0f%%)"
              % (hist.get(0, 0), len(idxs), 100.0 * hist.get(0, 0) / len(idxs)),
              file=sys.stderr)

    played = defaultdict(dict)
    for (hid, _rg), rows in ladder.items():
        for r in rows:
            if r["account_id"] is not None:
                played[r["account_id"]][hid] = stats[(r["account_id"], hid)]["hero_games"]

    home = {}
    for aid, counts in played.items():
        top = max(counts.values())
        home[aid] = sorted(h for h, n in counts.items() if n == top)[0]

    chosen = defaultdict(list)
    for (hid, region), rows in sorted(ladder.items()):
        taken = 0
        # SELECTION ORDER. Previously this was ladder order, i.e. Valve's
        # leaderboard position — but that board is not ranked-gated (~1,000
        # entries per region while the season needs 60 normal wins to enter),
        # so it ranks on all-mode standing while the builds we sample are
        # ranked-only. Ordering by shrunk ranked win rate makes selection
        # ranked-native. Entries with no resolved account keep ladder order so
        # the exclusion reasons below still read sensibly.
        rows = sorted(rows, key=lambda r: (
            -shrunk(stats[(r["account_id"], hid)]["hero_wins"],
                    stats[(r["account_id"], hid)]["hero_games"])
            if r.get("account_id") is not None and (r["account_id"], hid) in stats
            else 1.0,
            r["ladder_pos"]))
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
            hg, hw = s["hero_games"], s["hero_wins"]
            if s["last_match_id"] is None:
                excluded.append(dict(base, reason="no ranked match id on this hero"))
                continue
            # off-hero = every hero this account played, minus this one
            tg, tw = acct_tot[aid]
            og, ow = max(tg - hg, 0), max(tw - hw, 0)
            chosen[hid].append(dict(base, account_id=aid,
                                    # mmr is badge-derived and badge has read 0 on
                                    # every ranked row since 2026-07-31, so it is
                                    # recorded as None rather than a fake -6.0
                                    mmr=None,
                                    ranked_rating=round(shrunk(hw, hg), 4),
                                    last_match_id=int(s["last_match_id"]),
                                    last_played=s["last_played"],
                                    hero_games=hg, hero_wins=hw,
                                    offhero_games=og, offhero_wins=ow,
                                    ambiguous=r["ambiguous"]))
            taken += 1

    # ---- orbit fill -------------------------------------------------------
    # Top up any hero-region short of PER_REGION with orbit-1 players who
    # actually play that hero. Board members are never displaced: the orbit
    # only ever appends to a pool that came up short.
    orbit_added = 0
    if ORBIT_FILL:
        short = defaultdict(list)
        for hid, lst in chosen.items():
            for rg in REGIONS:
                have = [c for c in lst if c["region"] == rg]
                if len(have) < PER_REGION:
                    short[rg].append((hid, PER_REGION - len(have)))
        for rg, gaps in sorted(short.items()):
            seeds = [c["account_id"] for lst in chosen.values() for c in lst
                     if c["region"] == rg]
            seeds = sorted(set(seeds))[:ORBIT_SEEDS]
            members = fetch_orbit1(seeds)
            if not members:
                continue
            # hero-stats for the orbit members, so their hero record and most
            # recent match on the hero are known. Free, batched.
            known = {a for (a, _h) in stats}
            extra = fetch_hero_stats(set(members) - known)
            print("  [orbit] %-9s %d short hero-regions, %d orbit players, "
                  "%d (account,hero) rows" % (rg, len(gaps), len(members), len(extra)),
                  file=sys.stderr)
            for hid, need in gaps:
                taken = {c["account_id"] for c in chosen[hid]}
                cands = []
                for aid, prox in members.items():
                    if aid in taken or prox["seeds_met"] < ORBIT_MIN_SEEDS_MET:
                        continue
                    s_ = extra.get((aid, hid)) or stats.get((aid, hid))
                    if not s_ or s_["last_match_id"] is None:
                        continue
                    if s_["hero_games"] < ORBIT_MIN_HERO_GAMES:
                        continue
                    cands.append((shrunk(s_["hero_wins"], s_["hero_games"]),
                                  prox["seeds_met"], aid, s_))
                if ORBIT_SORT == "winrate":
                    cands.sort(key=lambda t: (-t[0], -t[1]))
                else:
                    cands.sort(key=lambda t: (-t[1], -t[0]))
                for rating, met, aid, s_ in cands[:need]:
                    chosen[hid].append({
                        "hero_id": hid, "region": rg, "ladder_pos": None,
                        "account_name": "", "badge_level": None,
                        "account_id": aid, "mmr": None,
                        "ranked_rating": round(rating, 4),
                        "last_match_id": int(s_["last_match_id"]),
                        "last_played": s_["last_played"],
                        "hero_games": s_["hero_games"], "hero_wins": s_["hero_wins"],
                        "offhero_games": 0, "offhero_wins": 0,
                        "ambiguous": False, "source": "orbit",
                        "orbit_seeds_met": met,
                    })
                    orbit_added += 1
        print("  [orbit] added %d builds across all short hero-regions"
              % orbit_added, file=sys.stderr)

    wanted = [(c["last_match_id"], c["account_id"])
              for lst in chosen.values() for c in lst]
    # which region each sampled build came from, so item frequencies can be
    # reported per region rather than pooled
    build_region = {(c["last_match_id"], c["account_id"]): c["region"]
                    for lst in chosen.values() for c in lst}
    # shrunk ranked win rate per sampled build, so the ability display can fall
    # back to the strongest player in the cohort when the ceiling player's own
    # match was not among the 20 sampled. Volume-aware by construction:
    # (wins + k*0.5)/(games + k) with k=25 pulls a 5-0 player to 0.583.
    build_rating = {(c["last_match_id"], c["account_id"]): c["ranked_rating"]
                    for lst in chosen.values() for c in lst}
    print("[4/5] item query (%d player-matches)" % len(wanted), file=sys.stderr)
    item_rows = query_items(wanted) if wanted else []

    print("[5/5] aggregating", file=sys.stderr)
    keep = {(m, a) for m, a in wanted}
    per_build, skipped_abilities = defaultdict(list), set()
    imbue_pick = Counter()      # (hid, rg, item, ability) -> builds
    imbue_item = Counter()      # (hid, rg, item)          -> builds holding it
    per_build_abil = defaultdict(list)
    for r in item_rows:
        key = (int(r["match_id"]), int(r["account_id"]))
        if key not in keep:          # the IN-pair filter is done here, not in SQL
            continue
        iid = int(r["item_id"])
        if iid in abilities:
            # One row per ACQUISITION: an ability appears up to 4 times — the
            # unlock, then its three upgrades. Probed 2026-08-06 — repeats run
            # 1-4, mean 14.2 per build, max 16 = 4 abilities x 4.
            #
            # The unlock and the upgrades are DIFFERENT CURRENCIES, confirmed
            # from level_info: 36 levels grant 4 EAbilityUnlocks (levels 1, 3,
            # 5, 8) and 32 EAbilityPoints. Upgrades cost 1/2/5 points, so
            # 8 per ability x 4 = exactly 32. `step` below records which:
            # step 0 is an unlock and costs no points.
            # `upgrade_id` is 0 on the unlock and a distinct id after, but the
            # asset `upgrades` array carries no ids to join it against, so TIER
            # IS THE OCCURRENCE INDEX in game_time_s order. Upgrades can only
            # be bought in sequence, which is what makes that sound.
            per_build_abil[(int(r["hero_id"]),) + key].append(
                (int(r["game_time_s"] or 0), iid))
            continue
        if iid not in items:     # anything else that is not a shop item
            skipped_abilities.add(iid)
            continue
        imb = int(r.get("imbued_ability_id") or 0)
        if imb:
            imbue_pick[(int(r["hero_id"]), build_region.get(key, ""), iid, imb)] += 1
            imbue_item[(int(r["hero_id"]), build_region.get(key, ""), iid)] += 1
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

    # ---- ability points -------------------------------------------------
    # Two products from the same rows. `count` is bare, of_builds-denominated,
    # exactly like item_hold_count (ONTOLOGY.yml) — 3 of 4 shows the thin
    # sample that 75% would hide. `seed_rank` is the derived one: mean pick
    # position across builds, ranked 1..16, never displayed, used only to seed
    # the build calculator in a plausible order.
    # tier 0 is an UNLOCK (its own currency, no point cost); 1-3 are upgrades
    # costing 1, 2 and 5 ability points.
    POINT_COST = {0: 0, 1: 1, 2: 2, 3: 5}
    abil_counts = defaultdict(int)          # (hid, rg, aid, tier) -> builds
    abil_pos = defaultdict(list)            # (hid, rg, aid, tier) -> [order idx]
    abil_at = defaultdict(Counter)          # (hid, rg, aid, tier) -> {order idx: n}
    abil_builds = defaultdict(int)          # (hid, rg) -> builds with any points
    abil_order = []                         # one row per sampled build
    for (hid, _m, _a), picks in per_build_abil.items():
        rg = build_region.get((_m, _a), "")
        picks.sort()                        # by game_time_s
        abil_builds[(hid, rg)] += 1
        seen = defaultdict(int)
        seq = []
        for order_idx, (_t, aid) in enumerate(picks):
            tier = seen[aid]                # 0 = unlock, 1-3 = upgrades
            seen[aid] += 1
            if tier > 3:                    # defensive: never observed
                continue
            abil_counts[(hid, rg, aid, tier)] += 1
            abil_pos[(hid, rg, aid, tier)].append(order_idx)
            abil_at[(hid, rg, aid, tier)][order_idx] += 1
            seq.append("%d:%d" % (aid, tier))
        abil_order.append({"hero_id": hid, "hero": heroes.get(hid, ""), "region": rg,
                           "account_id": _a, "match_id": _m,
                           "ranked_rating": build_rating.get((_m, _a), ""),
                           "points": len(seq), "sequence": " ".join(seq)})

    sig_slot = {}                           # (hid, aid) -> 1-4
    for hid, sig in hero_sigs.items():
        for k, aid in enumerate(sig, 1):
            if aid is not None:
                sig_slot[(hid, aid)] = k

    # rank every (ability, tier) within a hero-region by mean pick position
    # Abilities outside signature1-4 (only Silver's werewolf form, whose
    # upgrades mirror her base kit) are excluded before ranking, so every hero
    # gets a clean 1..16 rather than Silver's 1..19 with gaps.
    mean_pos = {k: sum(v) / len(v) for k, v in abil_pos.items()
                if (k[0], k[2]) in sig_slot}
    seed_rank = {}
    by_hr = defaultdict(list)
    for k in mean_pos:
        by_hr[(k[0], k[1])].append(k)
    # Mean position alone produces impossible orders on a thin sample — one
    # build upgrading an ability early can rank tier 1 ahead of its own unlock.
    # Unlocks and upgrades spend different currencies, but the dependency still
    # holds in one direction: an ability must be unlocked before it can be
    # upgraded, and upgrades are bought 1 -> 2 -> 3.
    # Emit in mean-position order but hold anything whose previous tier has not
    # been emitted yet, so a seeded build is always legally purchasable.
    for hr, keys in by_hr.items():
        pending = sorted(keys, key=lambda x: mean_pos[x])
        done, rank = set(), 1
        while pending:
            progressed = False
            for k in list(pending):
                aid, tier = k[2], k[3]
                if tier == 0 or (hr[0], hr[1], aid, tier - 1) in done:
                    seed_rank[k] = rank
                    done.add(k)
                    pending.remove(k)
                    rank += 1
                    progressed = True
                    break
            if not progressed:            # unreachable tier, e.g. a gap in the
                for k in pending:         # data; emit the rest in place order
                    seed_rank[k] = rank
                    rank += 1
                break

    abil_freq = []
    for (hid, rg, aid, tier), c in abil_counts.items():
        meta = abilities.get(aid, {})
        # WHEN a step is taken, and how much builds agree on it. The plain
        # count says "took it eventually", which is ~everything and carries no
        # information; the order is the actual decision. modal_pos is the most
        # common position in the pick sequence (1-based) and modal_count is how
        # many builds put it exactly there.
        at = abil_at[(hid, rg, aid, tier)]
        pos, agree = (at.most_common(1)[0] if at else (0, 0))
        abil_freq.append({
            "hero_id": hid, "hero": heroes.get(hid, ""), "region": rg,
            "ability_id": aid, "ability": meta.get("name", "ability_%d" % aid),
            "slot": sig_slot.get((hid, aid), ""),
            "tier": tier,
            "kind": "unlock" if tier == 0 else "upgrade",
            "point_cost": POINT_COST[tier],
            "count": c,
            "modal_pos": pos + 1 if at else "",
            "modal_count": agree,
            "of_builds": abil_builds[(hid, rg)],
            "seed_rank": seed_rank.get((hid, rg, aid, tier), ""),
            "icon_url": meta.get("icon", ""),
        })
    abil_freq.sort(key=lambda d: (d["hero"], d["region"],
                                  d["slot"] if d["slot"] != "" else 9, d["tier"]))
    abil_order.sort(key=lambda d: (d["hero"], d["region"], -d["points"]))

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
        # mmr was badge-derived and badge has read 0 on every ranked row since
        # 2026-07-31, so the *_mmr columns are replaced by the ranked rating.
        by_rating = sorted(lst, key=lambda c: -c["ranked_rating"])
        n = builds.get(hid, 0)
        g = sum(c.get("hero_games", 0) for c in lst)
        w = sum(c.get("hero_wins", 0) for c in lst)
        # Only players with a usable off-hero sample contribute to the baseline.
        thick = [c for c in lst if c.get("offhero_games", 0) >= MIN_OFFHERO_GAMES]
        og = sum(c["offhero_games"] for c in thick)
        ow = sum(c["offhero_wins"] for c in thick)
        hero_wr, off_wr = _wr(w, g), _wr(ow, og)
        top5 = [c["ranked_rating"] for c in by_rating[:5]]
        per_reg = {rg: sum(1 for c in lst if c["region"] == rg) for rg in REGIONS}
        tier.append({"hero_id": hid, "hero": heroes.get(hid, ""),
                     "top_rating": round(by_rating[0]["ranked_rating"], 4),
                     "top5_rating": round(sum(top5) / len(top5), 4),
                     "median_rating": round(
                         sorted(c["ranked_rating"] for c in lst)[len(lst) // 2], 4),
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
                     "top_account_id": by_rating[0]["account_id"],
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

    # Full item manifest, INCLUDING items nobody built. An item patch will add,
    # remove, rename, recost and recategorise items; without a dated record of
    # what the catalogue looked like each day, a chart spanning the patch
    # silently treats a reworked item as continuous with its old self. This is
    # free — load_assets() already fetched all of it.
    try:
        manifest = {str(iid): {"name": m["name"], "cat": m["cat"],
                               "cost": m["cost"], "tier": m["tier"]}
                    for iid, m in sorted(items.items())}
        with open(os.path.join(OUT_DIR, "items_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        print("  -> %s/items_manifest.json (%d items)" % (OUT_DIR, len(manifest)),
              file=sys.stderr)
    except Exception as e:
        print("  [warn] could not write items_manifest.json (%s)" % e, file=sys.stderr)

    write("tierlist.csv", tier,
          ["rank", "hero_id", "hero", "elite_winrate", "elite_games", "elite_se",
           "offhero_winrate", "offhero_games", "offhero_players",
           "winrate_delta", "delta_se", "delta_rank",
           "lane_split", "lane_weak", "lane_role", "median_rating", "top5_rating",
           "top_rating", "players", "builds_sampled", "thin", "by_region",
           "top_account_id", "icon_url"])
    # sorted by ranked_rating now, since mmr is dead while badge reads 0
    write("candidates.csv",
          [dict(c, hero=heroes.get(c["hero_id"], ""))
           for lst in chosen.values()
           for c in sorted(lst, key=lambda x: -x["ranked_rating"])],
          ["hero_id", "hero", "region", "ladder_pos", "account_id", "account_name",
           "ranked_rating", "hero_games", "hero_wins",
           "offhero_games", "offhero_wins",
           "last_match_id", "last_played", "ambiguous",
           # blank for board-sourced rows; "orbit" plus the breadth of contact
           # for anyone the orbit fill supplied
           "source", "orbit_seeds_met"])
    write("item_frequency.csv", freq,
          ["hero_id", "hero", "region", "snapshot", "item_id", "item", "category", "tier",
           "count", "of_builds", "icon_url"])
    imbue_rows = []
    for (hid, rg, iid, aid), c in sorted(imbue_pick.items()):
        imbue_rows.append({
            "hero_id": hid, "hero": heroes.get(hid, ""), "region": rg,
            "item_id": iid, "item": items.get(iid, {}).get("name", ""),
            "ability_id": aid,
            "ability": abilities.get(aid, {}).get("name", "ability_%d" % aid),
            "slot": sig_slot.get((hid, aid), ""),
            "count": c, "of_holders": imbue_item[(hid, rg, iid)],
        })
    imbue_rows.sort(key=lambda d: (d["hero"], d["region"], d["item"], -d["count"]))
    write("imbue_frequency.csv", imbue_rows,
          ["hero_id", "hero", "region", "item_id", "item", "ability_id", "ability",
           "slot", "count", "of_holders"])

    write("ability_frequency.csv", abil_freq,
          ["hero_id", "hero", "region", "ability_id", "ability", "slot", "tier",
           "kind", "point_cost", "count", "modal_pos", "modal_count",
           "of_builds", "seed_rank", "icon_url"])
    # account_id is here so build_site_data.py can pick out the ceiling
    # player's own sequence. output/ is gitignored; nothing account-level is
    # published to the site.
    write("ability_order.csv", abil_order,
          ["hero_id", "hero", "region", "account_id", "match_id", "ranked_rating",
           "points", "sequence"])
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

    print("\nDone. %d heroes, %d builds, %d item rows, %d ability rows, %d SQL calls."
          % (len(tier), sum(builds.values()), len(freq), len(abil_freq),
             _sql_calls[0]), file=sys.stderr)


def write(name, rows, cols):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
