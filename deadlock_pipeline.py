#!/usr/bin/env python3
"""
Deadlock tier-list + item-frequency pipeline.

Candidates are the top qualifying players from each region's hero ladder
(Valve's own leaderboard, passed through by deadlock-api), CROSS-REFERENCED
against that region's general cross-hero board: the general board supplies the
standing that orders selection (global_pos) and a second, independent
constraint on identity, since Valve publishes no account ids and both boards
carry only deadlock-api's fuzzy name-to-id guesses. Item data comes from SQL.

MODE. 2026-09-02: reverted from the ranked-only cohort back to standard play.
Ranked volume never grew into what the ranked-native selector assumed and the
ladder feeding it has been degrading, so ordering on ranked win rate or ranked
net wins now reads a shrinking, unrepresentative slice. MATCH_MODE defaults to
"" (no match_mode filter, game_mode='Normal' still applied) and every filter in
the file — hero-stats, orbit, pool net wins — follows it. MATCH_MODE=Ranked
restores the old behaviour in one variable if ranked ever recovers.

Outputs (./output/):
    tierlist.csv         heroes ranked by decay-weighted pool net wins, with
                         pooled win rate ON THE HERO as the tiebreak
    pool_audit.csv       sampled players at or below POOL_LOW_NET_WINS net wins
    candidates.csv       the sampled players, per hero per region
    item_frequency.csv   hold rates per hero per net-worth snapshot
    hero_splits.csv      V/G/S soul share and split classification, per snapshot
    excluded.csv         ladder entries dropped, with the reason
    roster.csv           every RELEASED hero, with or without data this run —
                         how a new hero reaches the site before it has a pool,
                         plus first_seen / new for the new-hero fallback

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
import datetime
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

# ---- general-board cross-reference (the old, restored selector) -----------
# Each region publishes ONE cross-hero board (/v1/leaderboard/{region}) of
# 1,001 entries, plus a per-hero board per hero. The hero boards run deeper
# than the general board, so a hero-board entry ranked ~1,400th overall has
# nowhere to be found on it — the ~42% location rate is that, not a bug.
#
# Cross-referencing does two jobs at once:
#   1. STANDING. global_pos is the player's position on the region's overall
#      board, which is what "elite" meant before the ranked detour. It orders
#      selection, and it is the number ceiling_rank.py wants back.
#   2. IDENTITY. Valve publishes no account_id (PROBES 2026-08-07), so both
#      boards carry only deadlock-api's fuzzy possible_account_ids. Taking the
#      INTERSECTION of the hero entry's id list and the general entry's id list
#      for the same display name is a second independent constraint on a match
#      that name-alone put wrong in 110 of 371 slots.
#
# Both boards live in the 100 req/s leaderboard bucket, so this costs
# len(REGIONS) extra calls and nothing at all against the SQL budget.
GENERAL_XREF = _env("GENERAL_XREF", 1)
# 1 = drop hero-board entries that do not appear on the general board. OFF by
# default: at ~42% location it would halve the pool, and a short pool is a
# worse failure than a loosely-confirmed one. When off, unlocated entries are
# kept but sort BEHIND every located entry.
REQUIRE_GENERAL = _env("REQUIRE_GENERAL", 0)
# What orders candidates within a hero-region.
#   "general"  general-board position, unlocated entries last  (the old method)
#   "winrate"  shrunk win rate on the hero                      (the ranked-era
#              selector, kept so the two can be compared on one run)
#   "ladder"   raw per-hero board position, no cross-reference
SELECTION_ORDER = os.environ.get("SELECTION_ORDER") or "general"

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

# MATCH MODE — REVERTED TO STANDARD PLAY, 2026-09-02.
#
# The ranked-native cohort (MATCH_MODE=Ranked everywhere, selection ordered by
# shrunk ranked win rate, ceiling ordered by ranked net wins) is being retired.
# Ranked volume never grew into the thing it was sized for and the ladder that
# fed it has been degrading, so a ranked-gated selector is now reading a
# shrinking, unrepresentative slice. The old selector — Valve's GENERAL
# leaderboard cross-referenced against the per-hero boards, over standard-mode
# play — is back as the default. See GENERAL_XREF below.
#
# "" (default) = no match_mode filter, i.e. every mode the general board is
# itself built from, with game_mode='Normal' still applied. Set
# MATCH_MODE=Unranked to exclude ranked games outright, or MATCH_MODE=Ranked to
# restore the ranked-only behaviour for comparison. Nothing else needs changing
# to switch — every filter below reads this one variable.
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
# Measured 2026-08-08 (NAmerica, 3-day window, 12 seeds — the 12 lowest
# account ids, see ORBIT_SEEDS): orbit 1 is 949
# players with median win rate 0.530 and p90 0.629, against the seeds' 0.568
# and 0.634. Orbit 2 sits at the population mean (0.504); it is used only as
# the last tier for NEW heroes, filtered and measured (see ORBIT2_* below).
# In a 400-account sample, hero coverage was ample even for unpopular heroes:
# Lady Geist 82, Mirage 98, Grey Talon 41, Vyper 23.
#
# Cost: one SQL call per region, and only when that region has a short
# hero-region. Orbit players are appended AFTER board members, so a full pool
# never changes.
ORBIT_FILL = _env("ORBIT_FILL", 1)
# SEEDS (2026-10-04). Until now the seeds were the 12 LOWEST ACCOUNT IDS among
# this run's board players — sorted(set(ids))[:12] — which is the dozen OLDEST
# Steam accounts on the boards, not the strongest and not necessarily active.
# They were recomputed every run, but the oldest accounts on the boards barely
# change, so in practice the same dozen seeded the orbit for weeks whether or
# not they still played. That sort has been there since the orbit landed on
# 2026-08-07 (ceiling_rank.py's caller ordered its seeds by board position and
# the same sort inside its fetch_orbit1 threw the order away). The
# best-match-first order in PROBES.md is a different list — each leaderboard
# entry's possible_account_ids — and is still kept as received.
#
# Now: the ORBIT_SEEDS strongest board players in the region — by general-board
# position, id-confirmed accounts first — who actually played in the
# ORBIT_DAYS window. Activity costs nothing extra: the orbit query is sent the
# first ORBIT_SEEDS x ORBIT_SEED_OVERFETCH candidates and any candidate with no
# match in the window is skipped. Still one SQL call per region; the longer IN
# list (~10 chars an id) is nowhere near MAX_URL. The `[orbit] seeds:` lines
# print how many candidates were active and the seeds' board positions.
ORBIT_SEEDS = _env("ORBIT_SEEDS", 24)
ORBIT_SEED_OVERFETCH = _env("ORBIT_SEED_OVERFETCH", 3)
ORBIT_DAYS = _env("ORBIT_DAYS", 3)
ORBIT_MIN_HERO_GAMES = _env("ORBIT_MIN_HERO_GAMES", 5)
ORBIT_MIN_SEEDS_MET = _env("ORBIT_MIN_SEEDS_MET", 1)
# What orders orbit candidates for a build slot.
#   "breadth"  distinct seeds met first, hero win rate as the tiebreak
#   "winrate"  the reverse
# Breadth measures STANDING — meeting several different top players in a
# 3-day window is hard to do by accident — while hero win rate measures being
# good AT THE HERO, which is not the same thing. With 12 seeds breadth was a
# 1-3 valued signal for most players; with 24 active ones it has more room.
# The `[orbit] seeds met:` line prints the distribution; if it is
# overwhelmingly {1: ...} this choice barely matters.
ORBIT_SORT = os.environ.get("ORBIT_SORT", "breadth")

# ---- new-hero fallback (2026-10-03) ---------------------------------------
# THE GOAL DOES NOT CHANGE: each region's top 20 players ON THE HERO, one build
# each, their most recent game on it. A hero released days ago has an empty
# per-hero board, so "top on the hero" cannot be read off Valve's board, and
# the orbit alone produced 3 builds for Rat King on his first day. While a
# hero is NEW, its shortfall after its own board players is filled, in order:
#
#   1. SWEEP — accounts this run already resolved from ANY hero's board or the
#      region's general board, with NEW_HERO_MIN_GAMES+ games on the new hero,
#      ordered by general-board position (the site's own definition of top).
#      Their hero-stats come back with every hero in one row set, so this is
#      free: no SQL, plus a few hero-stats calls for general-board ids.
#   2. ORBIT — orbit-1 players at the same relaxed bar, by seeds met.
#
# One build per player, unique players across regions, exactly as for board
# players. Established heroes are untouched: same board selection, same
# 5-game orbit. A hero is NEW for NEW_HERO_DAYS after it first appears; the
# first-seen date lives in the committed docs/data.json (see
# load_first_seen). NEW_HERO_IDS forces heroes into the set by id.
NEW_HERO_DAYS = _env("NEW_HERO_DAYS", 14)
# Early players sit at 2-4 games: Rat King's first 3 qualifiers had 16 games
# between them, all just over the orbit's 5-game bar.
NEW_HERO_MIN_GAMES = _env("NEW_HERO_MIN_GAMES", 3)
# Sweep ids are resolved from display names; an account with almost no games
# cannot be the player standing on that board. Same floor ceiling_rank.py uses.
NEW_HERO_MIN_ACCOUNT_GAMES = _env("NEW_HERO_MIN_ACCOUNT_GAMES", 100)
NEW_HERO_FORCE = {int(x) for x in (os.environ.get("NEW_HERO_IDS") or "").split(",")
                  if x.strip().isdigit()}
SITE_DATA = os.environ.get("SITE_DATA") or os.path.join("docs", "data.json")
# Seeds the one-time migration (load_first_seen). Rat King was released in
# build 6736 on 2026-10-02 (GameTracking-Deadlock heroes.vdata). Listing him
# here keeps him NEW even if a run of the previous code ranks him somewhere
# before this one first runs. Later heroes need no entry: they are absent from
# the published data.json the first time they appear, which is what marks them.
KNOWN_RELEASES = {84: "2026-10-02"}

# ---- ring 2 of the orbit, new heroes only (2026-10-03) ---------------------
# Players who shared a match with ring-1 players, in the same ORBIT_DAYS
# window, from the same seeds. It was set aside on 2026-08-08 because its
# median win rate sat at the population mean (0.504) — but that is the wrong
# test. Matchmaking pulls everyone toward 50% AT THEIR OWN MMR, so a ~50% win
# rate says "correctly matched", not "average player"; only the very top runs
# out of equal opponents, which is why the seeds read 0.568.
#
# The real risk is DRIFT: co-play means similar MMR in that lobby, each hop
# widens the spread, and the drift runs downward on average because the
# leaders sit in the top tail. So:
#   * FILTER: ORBIT2_MIN_SHARED+ ring-1 matches — players who keep landing in
#     the leaders' lobbies, not ones who met them once.
#   * ORDER: by the share of the player's own games in the window that were
#     ring-1 lobbies, not by the raw count. A raw count rewards volume: 4 of
#     30 games (a grinder brushing the band) would outrank 4 of 4. The share
#     is discounted for small samples by ORBIT2_SHARE_K phantom games outside
#     the band, so 2 of 2 (0.50) does not outrank 18 of 20 (0.82).
# Each run that uses it also MEASURES it: the fraction of ring-2 players on
# the region's top-1000 board, by shared matches and by share, against ring 1
# — see the [orbit2] lines. If closeness predicts standing, those fractions
# climb across the buckets.
#
# Last tier: players are taken only when a new hero is still short after the
# sweep and ring 1. One SQL call per region (the query is computed server-side
# from the 12 seeds, so the URL stays short). Non-fatal: if the query fails,
# the run carries on without ring 2 and says so. Hero-stats (free bucket) are
# fetched only for the ORBIT2_STATS_MAX closest players who clear the filter:
# ~20 builds are needed, not thousands of records.
#
# ORBIT2_MEASURE: while any hero is NEW, run the query every run even when no
# gap is left, to print the measurement — players are still only taken where
# there is a gap. That is how the filter gets judged on real lobbies BEFORE a
# release that needs it (Rat King's fallback was full by its second run). The
# key (DEADLOCK_API_KEY) lifts the 20/hr IP cap, so this costs 2 SQL calls and
# ~80 s per run while a hero is new, nothing otherwise.
ORBIT2_FILL = _env("ORBIT2_FILL", 1)
ORBIT2_MEASURE = _env("ORBIT2_MEASURE", 1)
ORBIT2_MIN_SHARED = _env("ORBIT2_MIN_SHARED", 2)
ORBIT2_SHARE_K = _env("ORBIT2_SHARE_K", 2)
ORBIT2_LIMIT = _env("ORBIT2_LIMIT", 20000)
ORBIT2_STATS_MAX = _env("ORBIT2_STATS_MAX", 1000)

# Unkeyed /v1/sql allows 2 req/min AND 20 req/hr (SCHEMA.md quirk #5). The
# hourly cap is the binding one for chunked queries — it is what killed the
# 2026-07-31 runs at chunk 21 both times. An X-API-Key raises this.
# ---- recency decay + pool net wins ---------------------------------------
# net wins = wins - losses = games x (2p - 1). It is CUMULATIVE, so a nerfed
# hero keeps every win already banked and only stops adding to the pile; it
# cannot fall. A heavily nerfed hero therefore holds its rank until a rival
# accumulates an equal total from scratch, which at ~300 games per
# hero-region takes most of a lookback window.
#
# Fix: weight each ranked game by 0.5 ** (age_days / DECAY_HALFLIFE_DAYS), so
# a game today counts 1.0 and one a half-life ago counts 0.5. Decayed net
# wins can FALL, because post-patch losses arrive at full weight while the
# pre-patch stock is already discounted. Set DECAY_HALFLIFE_DAYS=0 to
# disable and fall back to the flat count.
#
# Half-life choice: 14 days puts a 90-day-old game at 1.2% of a fresh one and
# reaches 90% of its response to a patch in ~46 days. Shorter reacts faster
# and is noisier.
# DEFAULT 0 = OFF. Below Eternus the ladder is a PROGRESSION system: a ranked
# win is about +250 Rank Points and a loss about -250, with 1000 RP per
# Subrank, so accumulated RP is roughly 250 x net wins. Net wins is therefore
# not a proxy for rank — it is a rescaled copy of the currency itself. A
# decayed net-wins figure corresponds to no rank any player holds, so decay is
# WRONG for anything meant to track standing. It is left available for
# exploring hero form, where "recent games only" is the actual question.
DECAY_HALFLIFE_DAYS = float(os.environ.get("DECAY_HALFLIFE_DAYS") or 0)
# Was pinned to Ranked on the argument that net wins is a ladder-success
# statistic. That argument dies with the ranked cohort: pool net wins is now a
# sample-quality diagnostic over the same games everything else is measured
# on, so it follows MATCH_MODE rather than overriding it. "" = no filter.
# Set DECAY_MATCH_MODE=Ranked to pin it back.
DECAY_MATCH_MODE = os.environ.get("DECAY_MATCH_MODE")
if DECAY_MATCH_MODE is None:
    DECAY_MATCH_MODE = MATCH_MODE
# Costs 1-3 SQL calls against the 20/hr cap. Set 0 to skip; tierlist then
# keeps elite_winrate as the primary sort.
POOL_NET_WINS = _env("POOL_NET_WINS", 1)
# Pool players at or below this net-wins figure are listed in pool_audit.csv.
# 0 means "has lost at least as often as won on the hero", which is the case
# worth eyeballing: an elite pool should not contain break-even players.
POOL_LOW_NET_WINS = _env("POOL_LOW_NET_WINS", 0)
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


def _hero_released(h):
    """True when the hero can actually be picked in matchmaking.

    BUILD 6711 (2026-09-29) removed m_bPlayerSelectable from heroes.vdata in
    favour of m_eHeroDevelopmentState (Release / PreRelease / DebugOnly).
    deadlock-api now derives `player_selectable` as development_state ==
    release and also publishes `development_state` itself.

    disabled/in_development alone stopped being enough at that build: the six
    hero-vote candidates (Rat King, Deadman Danny, Solomon, Violet, Nurse
    Harrow, Baba) shipped as PreRelease with BOTH flags false, so they passed
    the old filter while nobody could play them. Rat King flipped to Release
    in build 6736 (2026-10-02); the other five are still PreRelease and join
    the roster on their own the moment Valve flips them — nothing here needs
    editing per hero.
    """
    if h.get("disabled") or h.get("in_development"):
        return False
    ps = h.get("player_selectable")
    if ps is not None:
        return bool(ps)
    ds = str(h.get("development_state") or "").strip().lower()
    if ds:
        return ds == "release"
    return True        # a payload with neither field predates both; trust the old flags


def load_assets():
    heroes, hero_icon, hero_meta = {}, {}, {}
    _hero_sig_classes = {}
    hero_recs = [h for h in _get(BASE + "/v1/assets/heroes")
                 if h.get("id") is not None
                 and not h.get("disabled") and not h.get("in_development")]
    released = [h for h in hero_recs if _hero_released(h)]
    # GUARD. When build 6711 first shipped, the API parsed EVERY hero as
    # non-selectable until it was patched (deadlock-api commit 3475ae2, "every
    # hero parsed as non-selectable"). Trusting that
    # would empty the roster and the run would die with nothing to show. A
    # sudden collapse of the released set is an upstream flag glitch, not 30
    # heroes leaving the game — fall back to the old filter and say so loudly.
    if hero_recs and len(released) < 0.5 * len(hero_recs):
        print("  [assets] WARNING: only %d of %d non-disabled heroes read as "
              "released — treating that as an upstream flag glitch and using "
              "disabled/in_development alone this run"
              % (len(released), len(hero_recs)), file=sys.stderr)
        released = hero_recs
    released_ids = {int(h["id"]) for h in released}
    held_back = sorted((h.get("name") or str(h.get("id"))) for h in hero_recs
                       if int(h["id"]) not in released_ids)
    if held_back:
        print("  [assets] %d hero(es) not yet released, skipped until Valve flips "
              "them: %s" % (len(held_back), ", ".join(held_back)), file=sys.stderr)
    for h in released:
        hid = int(h["id"])
        heroes[hid] = h.get("name") or ("hero_%s" % hid)
        hero_icon[hid] = _pick(h.get("images"),
                               ("icon_hero_card", "icon_image_small",
                                "icon_hero_card_webp", "icon_image_small_webp",
                                "minimap_image", "top_bar_vertical_image"))
        hero_meta[hid] = {"class_name": h.get("class_name") or "",
                          "hero_type": h.get("hero_type") or "",
                          "development_state": h.get("development_state") or ""}
        _hero_sig_classes[hid] = [(h.get("items") or {}).get("signature%d" % k)
                                  for k in (1, 2, 3, 4)]

    _assets = _get(BASE + "/v1/assets/items")
    # DISABLED RECORDS ARE EXCLUDED. Without this the dump's 78
    # disabled/unshopable upgrades entered `items`, so the `iid not in items`
    # guard in the aggregation loop never rejected them and four items removed
    # in May 2025 rendered on the site with counts of 2-4. `by_class` is built
    # from this same list, so a live item's component_items could also resolve
    # onto a dead id and put it into the component graph.
    #
    # `shopable` is absent on some records; only an explicit False disqualifies.
    # Anything filtered here now falls to the skipped_abilities branch below,
    # which prints a count — so the run states plainly whether these ids appear
    # in match_player at all.
    raw = [it for it in _assets
           if it.get("type") == "upgrade" and it.get("id") is not None
           and not it.get("disabled") and it.get("shopable") is not False]
    _dead_ids = {int(it["id"]) for it in _assets
                 if it.get("type") == "upgrade" and it.get("id") is not None
                 and (it.get("disabled") or it.get("shopable") is False)}
    print("  [assets] excluded %d disabled/unshopable upgrade records"
          % len(_dead_ids), file=sys.stderr)

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
    return (heroes, hero_icon, items, component_of, abilities, hero_sigs, _dead_ids,
            hero_meta)


# --------------------------------------------------------------------------
# LEADERBOARDS
# --------------------------------------------------------------------------


def _norm_name(s):
    """Display names are matched case- and whitespace-insensitively.

    Nothing stronger is safe: the two boards are the same upstream strings, so
    an exact match is nearly always available, and anything fuzzier (prefix,
    edit distance) would start joining distinct players who picked similar
    names — which is the failure mode the id intersection exists to catch.
    """
    return " ".join((s or "").split()).casefold()


def fetch_general_boards():
    """region -> {"by_name": {norm_name: entry}, "size": n}.

    One call per region, in the 100 req/s leaderboard bucket. A name claimed by
    two different general-board entries is DROPPED rather than resolved: an
    ambiguous standing figure is worse than none, because it silently promotes
    the wrong player to the front of a hero's pool.
    """
    out = {}
    for region in REGIONS:
        url = "%s/v1/leaderboard/%s" % (BASE, urllib.parse.quote(region))
        try:
            payload = _get(url)
        except urllib.error.HTTPError as e:
            print("  [gb] %s -> HTTP %s — cross-reference disabled for this region"
                  % (region, e.code), file=sys.stderr)
            out[region] = {"by_name": {}, "size": 0}
            continue
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        by_name, dupes = {}, set()
        for pos, e in enumerate(entries or [], 1):
            key = _norm_name(e.get("account_name"))
            if not key:
                continue
            if key in by_name:
                dupes.add(key)
                continue
            ids = []
            for a in (e.get("possible_account_ids") or []):
                a = int(a)
                if a not in ids:
                    ids.append(a)
            # `rank` is NOT a unique position — 1,001 NA entries carried 634
            # distinct values and rank 1 was shared by 14 players. The list is
            # monotonic in rank, so the INDEX is the position.
            by_name[key] = {"global_pos": pos, "ids": ids,
                            "top_hero_ids": [int(h) for h in
                                             (e.get("top_hero_ids") or [])]}
        for k in dupes:
            by_name.pop(k, None)
        out[region] = {"by_name": by_name, "size": len(entries or [])}
        print("  [gb] %-9s general board %d entries, %d usable names (%d dropped "
              "as duplicate names)" % (region, len(entries or []), len(by_name),
                                       len(dupes)), file=sys.stderr)
    return out


def fetch_ladders(heroes, general=None):
    general = general or {}
    ladder = {}
    located = Counter()
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
                # ---- general-board cross-reference ------------------------
                gb = (general.get(region) or {}).get("by_name", {})
                g = gb.get(_norm_name(e.get("account_name"))) if gb else None
                # The intersection is the confirmed identity: an id that both
                # boards independently offer for this name. Where it is empty
                # (either the player is off the general board, or the two id
                # lists disagree entirely) fall back to the hero board's own
                # list, which is what the pipeline used before this change.
                confirmed = [a for a in all_ids if g and a in g["ids"]]
                if g:
                    located[region] += 1
                # Valve's own view of what the account plays. Free agreement
                # check on the hero assignment — no query, no cost.
                valve_top = bool(g and hid in (g.get("top_hero_ids") or []))
                ranked_ids = confirmed + [a for a in all_ids if a not in confirmed]
                rows.append({"hero_id": hid, "region": region, "ladder_pos": pos,
                             "account_name": e.get("account_name", ""),
                             "global_pos": g["global_pos"] if g else None,
                             "located_on_general": bool(g),
                             "valve_top_hero": valve_top,
                             "confirmed_ids": confirmed,
                             "badge_level": e.get("badge_level")
                                            or e.get("ranked_rank") or 0,
                             # A minority of names carry very long id lists (218
                             # of 1001 NA entries averaged 153 ids each) and
                             # inflated a ~7,600-entry leaderboard into 87,351
                             # SQL candidates — 470 chunks, 4.5+ hours at the
                             # rate limit (2026-07-31). Most entries have 1-2.
                             #
                             # ORDERING CHANGE 2026-09-02: ids the general
                             # board also claims for this name come FIRST, so
                             # the MAX_IDS_PER_ENTRY truncation can no longer
                             # discard a cross-confirmed id in favour of an
                             # unconfirmed one that merely sat earlier in the
                             # hero board's list. Within each group the API's
                             # native order is preserved — it is
                             # best-match-first (92% of resolutions came from
                             # slot 0) and re-sorting would throw that away.
                             "ids_ordered": ranked_ids[:MAX_IDS_PER_ENTRY],
                             "ids": set(ranked_ids[:MAX_IDS_PER_ENTRY]),
                             "ids_truncated": len(ranked_ids) > MAX_IDS_PER_ENTRY})
            ladder[(hid, region)] = rows
            total += len(rows)
        print("  [lb] hero %-3d %-22s %d entries" % (hid, heroes[hid][:22], total),
              file=sys.stderr)
    if general:
        seen = sum(len(rows) for rows in ladder.values())
        hit = sum(located.values())
        # ~42% is the expected figure and is NOT contamination: hero boards run
        # deeper than the 1,001-entry general board, so an entry ranked past
        # 1,000 overall has nowhere to be located. A sharp DROP from ~42%,
        # though, is the board degrading and is worth alarming on.
        print("  [gb] %d of %d hero-board entries located on a general board "
              "(%.0f%%)" % (hit, seen, 100.0 * hit / seen if seen else 0),
              file=sys.stderr)
        if seen and hit < 0.20 * seen:
            print("  [gb] WARNING: location rate is far below the ~42%% baseline. "
                  "The general board may be degrading; selection is falling back "
                  "to per-hero ladder order for most entries.", file=sys.stderr)
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
# Follows MATCH_MODE. Empty means the parameter is omitted entirely, which is
# what returns standard-mode play — sending match_mode= with an empty value is
# a 400, and sending "ranked" is the behaviour being retired.
MODE_API = MATCH_MODE.lower()
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
      AND {mode}game_mode = 'Normal'
      AND start_time >= now() - INTERVAL {days} DAY
)
"""


def _ranges(nums):
    """[1, 2, 3, 5, 9, 10] -> "1-3, 5, 9-10" (for log lines)."""
    out, run = [], []
    for n in sorted(set(nums)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append("%d-%d" % (run[0], run[-1]) if len(run) > 1 else "%d" % run[0])
        run = [n]
    if run:
        out.append("%d-%d" % (run[0], run[-1]) if len(run) > 1 else "%d" % run[0])
    return ", ".join(out)


def fetch_orbit1(candidates, label=""):
    """
    (members, seeds). members: account_id -> {"seeds_met": n, "shared": n} for
    everyone who shared a match with a seed; seeds: the accounts used. ONE SQL
    call.

    `candidates` is in order of preference, strongest first. The query is sent
    the first ORBIT_SEEDS x ORBIT_SEED_OVERFETCH of them, and the seeds are the
    first ORBIT_SEEDS that played at least one match in the window. Matches
    with no seed in them are dropped, so a candidate passed over never widens
    the orbit — though one who shared a match with a seed is a member like
    anyone else.

    Proximity is DISTINCT SEEDS met, not raw shared matches: a duo partner
    queuing with one seed all evening racks up matches without being of
    comparable standing, whereas meeting several different top players is hard
    to do by accident.
    """
    order = list(dict.fromkeys(int(a) for a in candidates if a))
    pool = order[:ORBIT_SEEDS * max(1, ORBIT_SEED_OVERFETCH)]
    if not pool:
        return {}, []
    mode_sql = "match_mode = '%s' AND " % MATCH_MODE if MATCH_MODE else ""
    q = Q_ORBIT.format(ids=",".join(str(a) for a in pool),
                       mode=mode_sql, days=ORBIT_DAYS)
    while len(sql_url(q)) > MAX_URL and len(pool) > ORBIT_SEEDS:
        pool = pool[:max(ORBIT_SEEDS, len(pool) * 2 // 3)]
        q = Q_ORBIT.format(ids=",".join(str(a) for a in pool),
                           mode=mode_sql, days=ORBIT_DAYS)
    try:
        rows = sql(q, "orbit1 %s from %d seed candidates" % (label, len(pool)))
    except SystemExit:
        raise
    except Exception as e:
        print("  [orbit] failed (%s) — continuing without the fill" % e,
              file=sys.stderr)
        return {}, []
    by_match = defaultdict(set)
    for r in rows:
        by_match[int(r["match_id"])].add(int(r["account_id"]))
    poolset = set(pool)
    played = Counter()
    for accts in by_match.values():
        for a in accts & poolset:
            played[a] += 1
    seeds = [a for a in pool if played[a]][:ORBIT_SEEDS]
    if not seeds:
        print("  [orbit] %s seeds: none of the first %d candidates played in the "
              "last %d days — no orbit this run" % (label, len(pool), ORBIT_DAYS),
              file=sys.stderr)
        return {}, []
    seedset = set(seeds)
    acc = defaultdict(lambda: {"seeds_met": set(), "shared": 0})
    n_matches = 0
    for accts in by_match.values():
        met = accts & seedset
        if not met:
            continue
        n_matches += 1
        for a in accts - seedset:
            acc[a]["seeds_met"] |= met
            acc[a]["shared"] += 1
    out = {a: {"seeds_met": len(v["seeds_met"]), "shared": v["shared"]}
           for a, v in acc.items()}
    looked = pool.index(seeds[-1]) + 1
    per = sorted(played[a] for a in seeds)
    print("  [orbit] %s seeds: %d active of the first %d candidates (%d with no "
          "match in the last %d days, skipped); matches per seed: min %d, "
          "median %d, max %d"
          % (label, len(seeds), looked, looked - len(seeds), ORBIT_DAYS,
             per[0], per[len(per) // 2], per[-1]), file=sys.stderr)
    if out:
        breadth = Counter(v["seeds_met"] for v in out.values())
        print("  [orbit] %d matches, %d players; seeds met: %s"
              % (n_matches, len(out), dict(sorted(breadth.items()))),
              file=sys.stderr)
    return out, seeds


# Ring 2, computed server-side from the seeds so the URL stays a few hundred
# characters: ring 1 is ~950 ids, which as a literal IN list would overflow
# MAX_URL and cost several calls. Restricted to accounts that have played one
# of the short new heroes, so the result is small. Per account: `shared` =
# distinct ring-1 matches it played in, `played` = distinct matches it played
# in the same window and mode (uniqExact: match_player is a ReplacingMergeTree,
# so a not-yet-merged duplicate row must not count twice). The hero filter
# leads with hero_id, game_mode, which is the order of deadlock-api's
# hero-led projection on match_player.
Q_ORBIT2 = """
WITH
  seed_m AS (
    SELECT DISTINCT match_id FROM match_player
    WHERE account_id IN ({seeds}) AND {mode}game_mode = 'Normal'
      AND start_time >= now() - INTERVAL {days} DAY),
  ring1 AS (
    SELECT DISTINCT account_id FROM match_player
    WHERE match_id IN (SELECT match_id FROM seed_m)),
  ring1_m AS (
    SELECT DISTINCT match_id FROM match_player
    WHERE account_id IN (SELECT account_id FROM ring1) AND {mode}game_mode = 'Normal'
      AND start_time >= now() - INTERVAL {days} DAY)
SELECT account_id,
       uniqExactIf(match_id, in_ring1 = 1) AS shared,
       uniqExact(match_id) AS played
FROM (
  SELECT account_id, match_id,
         match_id IN (SELECT match_id FROM ring1_m) AS in_ring1
  FROM match_player
  WHERE {mode}game_mode = 'Normal'
    AND start_time >= now() - INTERVAL {days} DAY
    AND account_id NOT IN (SELECT account_id FROM ring1)
    AND account_id IN (
        SELECT DISTINCT account_id FROM match_player
        WHERE hero_id IN ({heroes}) AND {mode}game_mode = 'Normal'
          AND start_time >= now() - INTERVAL {hero_days} DAY))
GROUP BY account_id
HAVING shared > 0
ORDER BY shared DESC, played ASC, account_id ASC
LIMIT {limit}
"""


def fetch_orbit2(seed_ids, hero_ids, label=""):
    """account_id -> (ring-1 matches shared, matches played), for ring-2
    players of the heroes.

    ONE SQL call. Never fatal: ring 2 is the last tier of an optional fill,
    so a refusal (the restricted SQL user may time out on a two-hop join)
    costs only ring 2, not the run.
    """
    seeds = list(dict.fromkeys(int(a) for a in seed_ids if a))[:ORBIT_SEEDS]
    if not seeds or not hero_ids:
        return {}
    mode_sql = "match_mode = '%s' AND " % MATCH_MODE if MATCH_MODE else ""
    q = Q_ORBIT2.format(seeds=",".join(str(a) for a in seeds), mode=mode_sql,
                        days=ORBIT_DAYS, heroes=",".join(str(h) for h in sorted(hero_ids)),
                        hero_days=NEW_HERO_DAYS + 1, limit=ORBIT2_LIMIT)
    try:
        rows = sql(q, "orbit2 %s from %d seeds" % (label, len(seeds)))
    except (SystemExit, Exception) as e:
        print("  [orbit2] %s query failed (%s) — continuing without ring 2"
              % (label, str(e).splitlines()[0][:160] if str(e) else type(e).__name__),
              file=sys.stderr)
        return {}
    out = {}
    for r in rows or []:
        try:
            n, p = int(float(r["shared"])), int(float(r["played"]))
            out[int(r["account_id"])] = (n, max(p, n))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def orbit2_closeness(shared, played):
    """Share of the player's games in the window that were ring-1 lobbies,
    discounted for small samples (see ORBIT2_SHARE_K)."""
    return shared / float(played + ORBIT2_SHARE_K)


Q_POOL_WINS = """
SELECT account_id,
       hero_id,
       count()    AS g,
       sum(won)   AS w,
       sum(pow(0.5, dateDiff('day', start_time, now()) / {hl}))       AS wg,
       sum(won * pow(0.5, dateDiff('day', start_time, now()) / {hl})) AS ww
FROM match_player
WHERE {mode}start_time >= now() - INTERVAL {days} DAY
  AND (account_id, hero_id) IN ({pairs})
GROUP BY account_id, hero_id
"""


def fetch_pool_wins(pairs):
    """Ranked games/wins per sampled (account, hero), raw and decay-weighted.

    Chunked under MAX_URL exactly like query_items. Returns
    (account_id, hero_id) -> {"g","w","wg","ww"}. hero_stats already gives a
    raw record, but it is unweighted and its match_mode handling is the API's
    rather than ours, so the ranked-only guarantee is asserted here in SQL.
    """
    hl = DECAY_HALFLIFE_DAYS if DECAY_HALFLIFE_DAYS > 0 else 1e9
    # "" means no match_mode filter at all — standard play, the same games the
    # general board is built from. A bare "match_mode = ''" would match nothing.
    mode_sql = "match_mode = '%s' AND " % DECAY_MATCH_MODE if DECAY_MATCH_MODE else ""

    out, chunk, i = {}, max(20, MAX_URL // 22), 0
    pairs = sorted(set(pairs))
    while i < len(pairs):
        part = pairs[i:i + chunk]
        body = ",".join("(%d,%d)" % (a, h) for a, h in part)
        q = Q_POOL_WINS.format(hl=hl, mode=mode_sql,
                               days=LOOKBACK_DAYS, pairs=body)
        n = len(sql_url(q))
        if n > MAX_URL:
            if chunk <= 20:
                raise SystemExit(
                    "pool-wins chunk of %d pairs is %d chars, over the %d limit."
                    % (len(part), n, MAX_URL))
            fixed = len(sql_url(Q_POOL_WINS.format(hl=hl, mode=mode_sql,
                                                   days=LOOKBACK_DAYS, pairs="")))
            per = max((n - fixed) / len(part), 1.0)
            chunk = max(20, min(chunk - 1, int((MAX_URL - fixed) / per * 0.97)))
            continue
        rows = sql(q, "pool wins %d-%d of %d" % (i + 1, i + len(part), len(pairs)))
        for r in rows:
            out[(int(r["account_id"]), int(r["hero_id"]))] = {
                "g": int(float(r["g"])), "w": int(float(r["w"])),
                "wg": float(r["wg"]), "ww": float(r["ww"]),
            }
        i += len(part)
    return out


def fetch_hero_stats(account_ids):
    """Per (account, hero) stats for every candidate, in MATCH_MODE. No SQL."""
    ids = sorted(account_ids)
    out, i = {}, 0
    # ~12 chars per id as a repeated query param; keep URLs well under the cap
    chunk = max(50, MAX_URL // 14)
    calls = 0
    while i < len(ids):
        part = ids[i:i + chunk]
        q = "&".join("account_ids=%d" % a for a in part)
        url = "%s/v1/players/hero-stats?%s%s" % (
            BASE, ("match_mode=%s&" % MODE_API) if MODE_API else "", q)
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


# --------------------------------------------------------------------------
# NEW HEROES
# --------------------------------------------------------------------------


def load_first_seen(heroes, today):
    """(hid -> first_seen, set of NEW hero ids).

    first_seen is "YYYY-MM-DD", or "" for a hero that predates tracking. The
    state lives in the committed docs/data.json, which build_site_data.py
    writes every run, so it needs no new file and no new commit step:

      * a hero absent from the last publish was released since — today
      * a hero carrying first_seen keeps it
      * MIGRATION, the first run after 2026-10-03 only: no hero carries the
        field yet. A hero in KNOWN_RELEASES gets its real release date; any
        other hero the last publish could not place in ANY region (tier NEW
        everywhere, or in no order at all) was not established either.
        Everything else predates tracking.

    Fails CLOSED. With no readable site data, or with an implausible share of
    the roster reading as new, the fallback is OFF for the run: a polluted
    established pool is worse than one thin new hero.
    """
    try:
        with open(SITE_DATA, encoding="utf-8") as f:
            prev = json.load(f)
        prev_heroes = prev.get("heroes") or {}
    except Exception as e:
        print("  [new] no readable %s (%s) — new-hero fallback OFF this run"
              % (SITE_DATA, e), file=sys.stderr)
        return {hid: "" for hid in heroes}, set()
    if not prev_heroes:
        print("  [new] %s lists no heroes — new-hero fallback OFF this run" % SITE_DATA,
              file=sys.stderr)
        return {hid: "" for hid in heroes}, set()

    by_id = {}
    for h in prev_heroes.values():
        try:
            by_id[int(h.get("id"))] = h
        except (TypeError, ValueError):
            continue
    tracked = any("first_seen" in h for h in prev_heroes.values())
    unplaced = set()
    if not tracked:
        regions = prev.get("regions") or {}
        for s in prev_heroes:
            tiers = [o.get("tier") for b in regions.values()
                     for o in (b.get("order") or []) if o.get("slug") == s]
            if not tiers or all(t == "NEW" for t in tiers):
                unplaced.add(s)

    first = {}
    for hid in heroes:
        h = by_id.get(hid)
        if h is None:
            first[hid] = today
        elif "first_seen" in h:
            first[hid] = h.get("first_seen") or ""
        elif not tracked and hid in KNOWN_RELEASES:
            first[hid] = KNOWN_RELEASES[hid]
        elif not tracked and h.get("slug") in unplaced:
            first[hid] = today
        else:
            first[hid] = ""

    new = set(h for h in NEW_HERO_FORCE if h in heroes)
    t0 = datetime.date.fromisoformat(today)
    for hid, d in first.items():
        try:
            if d and (t0 - datetime.date.fromisoformat(d)).days <= NEW_HERO_DAYS:
                new.add(hid)
        except ValueError:
            continue
    if len(new) > max(8, len(heroes) // 4):
        print("  [new] WARNING: %d of %d heroes read as new — that is missing site "
              "state, not a release wave; new-hero fallback OFF this run"
              % (len(new), len(heroes)), file=sys.stderr)
        # and record no first-seen dates from it: stamping today on heroes
        # that are only absent because the state was bad would make them all
        # "new" for the next fortnight
        return {h: ("" if d == today else d) for h, d in first.items()}, set()
    if new:
        print("  [new] %d new hero(es), fallback ON (within %d days of first seen): %s"
              % (len(new), NEW_HERO_DAYS,
                 ", ".join("%s (since %s)" % (heroes[h], first.get(h) or "forced")
                           for h in sorted(new))), file=sys.stderr)
    return first, new


def new_hero_fill(new_heroes, heroes, ladder, general, stats, chosen,
                  orbit_members, orbit_stats, home, orbit_seeds):
    """Top up each NEW hero's pool, per region, after its own board players.

    Order of preference, one build per player, unique players across regions
    (see NEW_HERO_* and ORBIT2_* above for the why):
      1. the hero's own board players — already in `chosen`, untouched
      2. SWEEP: accounts resolved from any board this run, NEW_HERO_MIN_GAMES+
         games on the hero, by general-board position; players not on the
         general board follow, by their best position on a hero board
      3. ORBIT: ring-1 players at the same relaxed bar, by seeds met
      4. ORBIT 2: ring-2 players with ORBIT2_MIN_SHARED+ shared ring-1
         matches, same bar, closest first (orbit2_closeness) — only if 2 and
         3 left a gap

    Fetches hero-stats for general-board ids not already known (free bucket,
    capped at MAX_IDS_PER_ENTRY per name in native best-match-first order),
    and for ring-2 players. Returns builds added.
    """
    # ---- who is near the top of each region, and as which account --------
    near = {rg: {} for rg in REGIONS}

    def note(rg, aid, gp=None, lpos=None, name="", confirmed=False):
        cur = near[rg].setdefault(aid, {"pos": None, "lpos": None, "name": name,
                                        "confirmed": False})
        if gp and (cur["pos"] is None or gp < cur["pos"]):
            cur["pos"] = gp
        if lpos and (cur["lpos"] is None or lpos < cur["lpos"]):
            cur["lpos"] = lpos
        cur["confirmed"] = cur["confirmed"] or confirmed
        cur["name"] = cur["name"] or name

    for (_h, rg), rows in ladder.items():
        if rg not in near:
            continue
        for r in rows:
            aid = r.get("account_id")
            if aid is not None:
                note(rg, aid, r.get("global_pos"), r.get("ladder_pos"),
                     r.get("account_name") or "", aid in (r.get("confirmed_ids") or []))

    gen = []
    board_ids = {rg: set() for rg in REGIONS}   # every id any top-1000 name claims
    for rg in REGIONS:
        for key, e in ((general.get(rg) or {}).get("by_name") or {}).items():
            ids = list(e.get("ids") or [])[:MAX_IDS_PER_ENTRY]
            if ids:
                gen.append((rg, key, e.get("global_pos"), ids))
                board_ids[rg].update(ids)
    known = {a for (a, _h) in stats} | {a for (a, _h) in orbit_stats}
    want = {a for (_rg, _k, _gp, ids) in gen for a in ids} - known
    merged = dict(stats)
    merged.update(orbit_stats)
    if want:
        print("  [new] hero-stats for %d general-board ids not already known"
              % len(want), file=sys.stderr)
        merged.update(fetch_hero_stats(want))
    totals = account_totals(merged)
    for rg, key, gp, ids in gen:
        # the account this name most plausibly is: most games overall, ties to
        # the earlier slot (the list is best-match-first; PROBES.md)
        best = None
        for a in ids:
            g = totals[a][0] if a in totals else 0
            if g and (best is None or g > best[0]):
                best = (g, a)
        if best:
            note(rg, best[1], gp, None, key)

    added = 0
    got = defaultdict(lambda: {"sweep": 0, "orbit": 0, "orbit2": 0})
    located = defaultdict(int)
    eligible = {}
    taken = {hid: {c["account_id"] for c in chosen[hid]} for hid in new_heroes}

    def take(hid, rg, src, aid, s, m=None, met=""):
        nonlocal added
        taken[hid].add(aid)
        gp = m["pos"] if m else None
        tg, tw = totals[aid] if aid in totals else (0, 0)
        chosen[hid].append({
            "hero_id": hid, "region": rg, "ladder_pos": None,
            "account_name": (m or {}).get("name", ""), "badge_level": None,
            "global_pos": gp, "located_on_general": "YES" if gp else "",
            "valve_top_hero": "",
            "id_confirmed": "YES" if (m or {}).get("confirmed") else "",
            "account_id": aid, "mmr": None,
            "ranked_rating": round(shrunk(s["hero_wins"], s["hero_games"]), 4),
            "last_match_id": int(s["last_match_id"]),
            "last_played": s["last_played"],
            "hero_games": s["hero_games"], "hero_wins": s["hero_wins"],
            # sweep players have a full record in hand, so their off-hero
            # baseline is real; orbit rows keep the orbit fill's convention
            "offhero_games": max(tg - s["hero_games"], 0) if src == "sweep" else 0,
            "offhero_wins": max(tw - s["hero_wins"], 0) if src == "sweep" else 0,
            "ambiguous": False, "source": src,
            "orbit_seeds_met": met if src == "orbit" else "",
            "orbit2_shared": met if src == "orbit2" else "",
        })
        got[(hid, rg)][src] += 1
        located[(hid, rg)] += 1 if gp else 0
        added += 1

    def need(hid, rg):
        return PER_REGION - sum(1 for c in chosen[hid] if c["region"] == rg)

    def playable(aid, hid):
        s = merged.get((aid, hid))
        if not s or s["last_match_id"] is None or s["hero_games"] < NEW_HERO_MIN_GAMES:
            return None
        return s

    board = {(hid, rg): sum(1 for c in chosen[hid] if c["region"] == rg)
             for hid in new_heroes for rg in REGIONS}

    # ---- 2 + 3: the sweep, then ring 1 -----------------------------------
    for hid in sorted(new_heroes):
        for rg in REGIONS:
            if need(hid, rg) <= 0:
                continue
            sweep = []
            for aid, m in near[rg].items():
                if aid in taken[hid] or (EXCLUSIVITY and home.get(aid, hid) != hid):
                    continue
                s = playable(aid, hid)
                if not s or totals[aid][0] < NEW_HERO_MIN_ACCOUNT_GAMES:
                    continue
                rank = (0, m["pos"]) if m["pos"] else (1, m["lpos"] or 10 ** 9)
                sweep.append((rank, -s["hero_games"], aid, m, s))
            sweep.sort(key=lambda t: t[:3])
            orbit = []
            for aid, prox in (orbit_members.get(rg) or {}).items():
                if aid in taken[hid] or prox["seeds_met"] < ORBIT_MIN_SEEDS_MET:
                    continue
                s = playable(aid, hid)
                if s:
                    orbit.append((prox["seeds_met"],
                                  shrunk(s["hero_wins"], s["hero_games"]), aid, s))
            if ORBIT_SORT == "winrate":
                orbit.sort(key=lambda t: (-t[1], -t[0], t[2]))
            else:
                orbit.sort(key=lambda t: (-t[0], -t[1], t[2]))
            eligible[(hid, rg)] = [len(sweep), len(orbit), 0]
            for _r, _g, aid, m, s in sweep:
                if need(hid, rg) <= 0:
                    break
                if aid not in taken[hid]:
                    take(hid, rg, "sweep", aid, s, m)
            for met, _w, aid, s in orbit:
                if need(hid, rg) <= 0:
                    break
                if aid not in taken[hid]:
                    take(hid, rg, "orbit", aid, s, None, met)

    # ---- 4: ring 2 — players taken only where a gap is left; measured on
    # every run while a hero is new (ORBIT2_MEASURE) ------------------------
    def frac(on, n):
        return "%.0f%% of %d" % (100.0 * on / n, n) if n else "none"

    for rg in REGIONS:
        short = sorted(h for h in new_heroes if need(h, rg) > 0) if ORBIT2_FILL else []
        if not (short or ORBIT2_MEASURE):
            continue
        target = short or sorted(new_heroes)
        if not orbit_seeds.get(rg):
            print("  [orbit2] %-9s no seeds this run — ring 2 skipped" % rg, file=sys.stderr)
            continue
        if not short:
            print("  [orbit2] %-9s measurement only — %s, nobody is taken"
                  % (rg, ("%s already full here" % ", ".join(heroes[h] for h in target))
                     if ORBIT2_FILL else "ORBIT2_FILL=0"), file=sys.stderr)
        raw = fetch_orbit2(orbit_seeds[rg], target, rg)
        # the query already leaves out ring 1 and the seeds; this is a guard
        ring1 = set(orbit_members.get(rg) or {})
        inner = ring1 | set(orbit_seeds[rg])
        ring2 = {a: v for a, v in raw.items() if a not in inner}
        if not ring2:
            if raw:
                print("  [orbit2] %-9s every row returned was ring 1 or a seed — "
                      "nothing to add" % rg, file=sys.stderr)
            continue
        # THE MEASUREMENT: how close does ring 2 sit to the leaders? The one
        # standing measure trusted here is Valve's top-1000 board. Needs no
        # hero-stats, so it covers every ring-2 player returned.
        by_n = defaultdict(lambda: [0, 0])
        by_share = defaultdict(lambda: [0, 0])
        for a, (n, pl) in ring2.items():
            on = 1 if a in board_ids[rg] else 0
            for b in (by_n["1" if n == 1 else "2" if n == 2 else "3-5" if n <= 5 else "6+"],
                      by_share["<1/3" if 3 * n < pl else "1/3-2/3" if 3 * n < 2 * pl
                               else "2/3+"]):
                b[0] += 1
                b[1] += on
        r1 = [a for a in ring1 if any((a, h) in merged for h in target)]
        r1_on = sum(1 for a in r1 if a in board_ids[rg])
        print("  [orbit2] %-9s %d ring-2 players of %s%s. On the top-1000 board:"
              % (rg, len(ring2), ", ".join(heroes[h] for h in target),
                 " (query cap reached)" if len(raw) >= ORBIT2_LIMIT else ""),
              file=sys.stderr)
        print("  [orbit2]   by ring-1 matches shared:   %s"
              % "  |  ".join("%s: %s" % (k, frac(by_n[k][1], by_n[k][0]))
                             for k in ("1", "2", "3-5", "6+")), file=sys.stderr)
        print("  [orbit2]   by share of their games:    %s"
              % "  |  ".join("%s: %s" % (k, frac(by_share[k][1], by_share[k][0]))
                             for k in ("<1/3", "1/3-2/3", "2/3+")), file=sys.stderr)
        print("  [orbit2]   ring 1, for comparison:     %s (players of the same hero(es))"
              % frac(r1_on, len(r1)), file=sys.stderr)
        if not short:
            continue
        # only players who clear the filter can be used, so only they need
        # hero-stats; closest first
        usable = sorted((a for a, (n, _p) in ring2.items() if n >= ORBIT2_MIN_SHARED),
                        key=lambda a: (-orbit2_closeness(*ring2[a]), -ring2[a][0], a)
                        )[:ORBIT2_STATS_MAX]
        unknown = set(usable) - {a for (a, _h) in merged}
        if unknown:
            merged.update(fetch_hero_stats(unknown))
        for hid in short:
            cands = []
            for aid in usable:
                if aid in taken[hid]:
                    continue
                s = playable(aid, hid)
                if s:
                    n, pl = ring2[aid]
                    cands.append((-orbit2_closeness(n, pl), -n,
                                  -shrunk(s["hero_wins"], s["hero_games"]), aid, s, n, pl))
            cands.sort(key=lambda t: t[:4])
            eligible.setdefault((hid, rg), [0, 0, 0])[2] = len(cands)
            picked = []
            for _c, _n, _w, aid, s, n, pl in cands:
                if need(hid, rg) <= 0:
                    break
                take(hid, rg, "orbit2", aid, s, None, "%d/%d" % (n, pl))
                picked.append("%d/%d" % (n, pl))
            if picked:
                print("  [orbit2] %-9s %s: took %d of %d eligible; ring-1 lobbies / games "
                      "played: %s" % (rg, heroes[hid], len(picked), len(cands),
                                      " ".join(picked[:20])), file=sys.stderr)

    for hid in sorted(new_heroes):
        for rg in REGIONS:
            g = got[(hid, rg)]
            e = eligible.get((hid, rg), [0, 0, 0])
            have = PER_REGION - need(hid, rg)
            print("  [new] %-14s %-9s %d board + %d sweep (%d on the general board) "
                  "+ %d orbit + %d ring 2 = %d/%d   [bar %d games; eligible %d sweep, "
                  "%d orbit, %d ring 2]"
                  % (heroes[hid][:14], rg, board[(hid, rg)], g["sweep"],
                     located[(hid, rg)], g["orbit"], g["orbit2"], have, PER_REGION,
                     NEW_HERO_MIN_GAMES, e[0], e[1], e[2]), file=sys.stderr)
    return added


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
    (heroes, hero_icon, items, component_of, abilities, hero_sigs, dead_ids,
     hero_meta) = load_assets()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    first_seen, new_heroes = load_first_seen(heroes, today)

    print("[2/5] ladders (%s), depth %d, target %d per region, exclusivity %s, "
          "mode %s, selection by %s"
          % (", ".join(REGIONS), LEADERBOARD_DEPTH, PER_REGION,
             "ON" if EXCLUSIVITY else "OFF", MATCH_MODE or "standard (unfiltered)",
             SELECTION_ORDER), file=sys.stderr)
    # The general board is fetched FIRST so the hero boards can be cross-
    # referenced against it as they arrive. Free bucket, one call per region.
    general = fetch_general_boards() if GENERAL_XREF else {}
    ladder = fetch_ladders(heroes, general)
    # A hero released hours ago has no per-hero board yet: Valve's board is
    # built from games already played. That is where every NEW hero starts,
    # and until 2026-10-03 such a hero was silently dropped here — the orbit
    # fill below only topped up heroes that already had a board player, so
    # Rat King (released in build 6736) never reached tierlist.csv, never got
    # an icon fetched, and never appeared on the site.
    no_board = sorted(hid for hid in heroes
                      if not any(ladder.get((hid, rg)) for rg in REGIONS))
    if no_board:
        print("  [lb] %d hero(es) have NO board entries in any region: %s — a "
              "new hero starts here; its pool comes from the orbit fill until "
              "its own leaderboard fills in"
              % (len(no_board), ", ".join("%s (%d)" % (heroes[h], h)
                                          for h in no_board)), file=sys.stderr)
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
        # SELECTION ORDER — REVERTED 2026-09-02, see SELECTION_ORDER.
        #
        # The ranked-era selector ordered by shrunk ranked win rate, on the
        # argument that Valve's board ranks all-mode standing while the sampled
        # builds were ranked-only. With the cohort back on standard play that
        # mismatch is gone, and the ranked win rate is now computed over a
        # shrinking and unrepresentative set of games. General-board position
        # is the original selector: a single cross-hero standing, published by
        # Valve, that does not depend on any of the ratings that died.
        #
        # Unlocated entries sort behind every located one rather than being
        # dropped — see REQUIRE_GENERAL. Within each group the per-hero board's
        # own position breaks ties, so the exclusion reasons below still read
        # in a sensible order.
        def _order(r):
            gp = r.get("global_pos")
            if SELECTION_ORDER == "ladder":
                return (0, 0, r["ladder_pos"])
            if SELECTION_ORDER == "winrate":
                s_ = (stats.get((r.get("account_id"), hid))
                      if r.get("account_id") is not None else None)
                return (0, -shrunk(s_["hero_wins"], s_["hero_games"]) if s_ else 1.0,
                        r["ladder_pos"])
            # "general" — located first, then general-board position
            return (0 if gp else 1, gp or 0, r["ladder_pos"])

        rows = sorted(rows, key=_order)
        for r in rows:
            if taken >= PER_REGION:
                break
            base = {"hero_id": hid, "region": region, "ladder_pos": r["ladder_pos"],
                    "account_name": r["account_name"], "badge_level": r["badge_level"],
                    "global_pos": r.get("global_pos"),
                    "located_on_general": "YES" if r.get("located_on_general") else "",
                    "valve_top_hero": "YES" if r.get("valve_top_hero") else "",
                    "id_confirmed": "YES" if r.get("confirmed_ids") else ""}
            if REQUIRE_GENERAL and not r.get("located_on_general"):
                excluded.append(dict(base, reason="not on the region's general board"))
                continue
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
                excluded.append(dict(base, reason="no match id on this hero in the sampled mode"))
                continue
            # off-hero = every hero this account played, minus this one
            tg, tw = acct_tot[aid]
            og, ow = max(tg - hg, 0), max(tw - hw, 0)
            # base["id_confirmed"] said only that SOME id was cross-confirmed;
            # what matters downstream is whether the id actually chosen was.
            base["id_confirmed"] = "YES" if aid in (r.get("confirmed_ids") or []) else ""
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
    orbit_members, orbit_stats, orbit_seeds = {}, {}, {}
    if ORBIT_FILL:
        short = defaultdict(list)
        # EVERY released hero, not just the ones already in `chosen`. Looping
        # over chosen.items() skipped any hero with zero board players in
        # both regions — exactly a newly released hero — so the one case the
        # fill matters most was the one it could never reach. Same bug class
        # ceiling_rank.py fixed on 2026-08-18. Costs no extra SQL: the orbit
        # query is one call per region and runs whenever ANY hero-region is
        # short, and the hero-stats lookup for orbit members already returns
        # every hero they play.
        #
        # NEW heroes are filled below by new_hero_fill instead, sweep FIRST:
        # left in here, the 5-game orbit would take their slots before the
        # higher-standing sweep players got a look.
        new_short = set()
        for hid in sorted(heroes):
            lst = chosen.get(hid, [])
            for rg in REGIONS:
                have = [c for c in lst if c["region"] == rg]
                if len(have) < PER_REGION:
                    if hid in new_heroes:
                        new_short.add(rg)
                    else:
                        short[rg].append((hid, PER_REGION - len(have)))
        for rg in sorted(set(short) | new_short):
            gaps = short.get(rg, [])
            # seed candidates: this region's board players, strongest first —
            # id-confirmed accounts by general-board position, then the other
            # general-board accounts, then hero-board-only ones by their best
            # ladder position (see ORBIT_SEEDS)
            standing = {}
            for lst in chosen.values():
                for c in lst:
                    if c["region"] != rg or c.get("source"):
                        continue
                    gp, lp = c.get("global_pos"), c.get("ladder_pos")
                    key = ((0 if c.get("id_confirmed") == "YES" else 1, gp) if gp
                           else (2, lp if lp is not None else 10 ** 9))
                    a = c["account_id"]
                    if a not in standing or key < standing[a]:
                        standing[a] = key
            members, seeds = fetch_orbit1(sorted(standing, key=lambda a: (standing[a], a)),
                                          rg)
            orbit_seeds[rg] = seeds        # ring 2 starts from the same seeds
            if seeds:
                on_gen = [standing[a][1] for a in seeds if standing[a][0] < 2]
                print("  [orbit] %-9s seeds' general-board positions: %s%s"
                      % (rg, _ranges(on_gen) or "none",
                         " (+%d off the general board)" % (len(seeds) - len(on_gen))
                         if len(seeds) > len(on_gen) else ""), file=sys.stderr)
            if not members:
                continue
            # hero-stats for the orbit members, so their hero record and most
            # recent match on the hero are known. Free, batched.
            known = {a for (a, _h) in stats}
            extra = fetch_hero_stats(set(members) - known)
            orbit_members[rg] = members
            orbit_stats.update(extra)
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
                        # orbit players never came off a board, so they have no
                        # standing to cross-reference. Blank, not zero — zero
                        # would sort them to the FRONT of a general-board order.
                        "global_pos": None, "located_on_general": "",
                        "valve_top_hero": "", "id_confirmed": "",
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

    # ---- new heroes: sweep, then the orbit at a relaxed bar ---------------
    new_added = 0
    if new_heroes:
        new_added = new_hero_fill(new_heroes, heroes, ladder, general, stats, chosen,
                                  orbit_members, orbit_stats, home, orbit_seeds)
        print("  [new] added %d builds across %d new hero(es)"
              % (new_added, len(new_heroes)), file=sys.stderr)

    # ---- pool net wins, ranked only, with recency decay -------------------
    # Attaches to every sampled player BEFORE the tier rollup, so the tier
    # statistic and the per-player audit come from the same numbers.
    pool_low = []
    if POOL_NET_WINS:
        pw_pairs = [(c["account_id"], hid)
                    for hid, lst in chosen.items() for c in lst]
        print("[3b/5] pool net wins (%d players, %s only, half-life %s d)"
              % (len(pw_pairs), DECAY_MATCH_MODE,
                 DECAY_HALFLIFE_DAYS or "off"), file=sys.stderr)
        try:
            pw = fetch_pool_wins(pw_pairs)
        except SystemExit:
            raise
        except Exception as e:
            print("  [pool] failed (%s) - continuing without decay" % e,
                  file=sys.stderr)
            pw = {}
        missing = 0
        for hid, lst in chosen.items():
            for c in lst:
                v = pw.get((c["account_id"], hid))
                if not v:
                    missing += 1
                    # No ranked rows in the window. Recorded as blank, NOT as
                    # zero: zero would read as "played and broke even" and
                    # would drag the hero's total down as if it were a real
                    # break-even record.
                    c["ranked_games"] = c["ranked_wins"] = ""
                    c["net_wins"] = c["net_wins_decayed"] = ""
                    continue
                g, w = v["g"], v["w"]
                c["ranked_games"], c["ranked_wins"] = g, w
                c["net_wins"] = w - (g - w)
                c["net_wins_decayed"] = round(2 * v["ww"] - v["wg"], 2)
                c["_wg"], c["_ww"] = v["wg"], v["ww"]
                if c["net_wins"] <= POOL_LOW_NET_WINS:
                    pool_low.append(dict(
                        hero=heroes.get(hid, ""), hero_id=hid,
                        region=c["region"], account_id=c["account_id"],
                        account_name=c.get("account_name", ""),
                        source=c.get("source", "board"),
                        orbit_seeds_met=c.get("orbit_seeds_met", ""),
                        ranked_games=g, ranked_wins=w,
                        net_wins=c["net_wins"],
                        net_wins_decayed=c["net_wins_decayed"],
                        ranked_rating=c["ranked_rating"]))
        if missing:
            print("  [pool] %d of %d sampled players had no ranked rows in the "
                  "%d-day window" % (missing, len(pw_pairs), LOOKBACK_DAYS),
                  file=sys.stderr)
        if pool_low:
            byhero = Counter(r["hero"] for r in pool_low)
            bysrc = Counter(r["source"] for r in pool_low)
            print("  [pool] %d players at or below %d net wins (%s); worst "
                  "heroes: %s" % (len(pool_low), POOL_LOW_NET_WINS,
                                  dict(bysrc), dict(byhero.most_common(5))),
                  file=sys.stderr)

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
    # Postgame holdings split by where the build CAME from, so the orbit fill
    # can be audited without anything account-level leaving the runner.
    # candidates.csv carries account ids and is never uploaded or committed;
    # this is the aggregate that answers "do orbit builds differ?" on its own.
    src_of = {(c["last_match_id"], c["account_id"]): c.get("source") or "board"
              for lst in chosen.values() for c in lst}
    src_holds = defaultdict(lambda: defaultdict(int))
    src_builds = defaultdict(int)

    for (hid, _m, _a), purchases in per_build.items():
        rg = build_region.get((_m, _a), "")
        src = src_of.get((_m, _a), "board")
        builds[hid] += 1
        builds_rg[(hid, rg)] += 1
        src_builds[(hid, rg, src)] += 1
        for snap, held in snapshot_holdings(purchases, component_of).items():
            if snap == "postgame":
                for iid in held:
                    src_holds[(hid, rg, src)][iid] += 1
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
        # Items held by a single build are noise in a 20-build sample, so they
        # are dropped — but on a NEW hero with 1-2 builds that rule hides
        # everything (Rat King NA, 2026-10-03: one build, empty panel). Below 3
        # builds a new hero keeps them; the site shows them as "1 of 1".
        min_c = 1 if (hid in new_heroes and n < 3) else 2
        for iid, c in counter.items():
            if c < min_c:                  # exclude single-instance items
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
        # Pooled ranked record. Blank entries (no ranked rows in the window)
        # are skipped rather than counted as 0-0.
        rk = [c for c in lst if c.get("ranked_games") not in (None, "")]
        rg_ = sum(c["ranked_games"] for c in rk)
        rw_ = sum(c["ranked_wins"] for c in rk)
        net = rw_ - (rg_ - rw_)
        wg_ = sum(c.get("_wg", 0.0) for c in rk)
        ww_ = sum(c.get("_ww", 0.0) for c in rk)
        net_dec = 2 * ww_ - wg_
        # Effective sample size after weighting, for the decayed win rate's SE.
        dec_wr = _wr(ww_, wg_) if wg_ else None
        top5 = [c["ranked_rating"] for c in by_rating[:5]]
        per_reg = {rg: sum(1 for c in lst if c["region"] == rg) for rg in REGIONS}
        tier.append({"hero_id": hid, "hero": heroes.get(hid, ""),
                     "top_rating": round(by_rating[0]["ranked_rating"], 4),
                     "top5_rating": round(sum(top5) / len(top5), 4),
                     "median_rating": round(
                         sorted(c["ranked_rating"] for c in lst)[len(lst) // 2], 4),
                     "pool_net_wins_decayed": round(net_dec, 1) if rk else "",
                     "pool_net_wins": net if rk else "",
                     "pool_ranked_games": rg_, "pool_ranked_wins": rw_,
                     "pool_decayed_winrate": round(dec_wr, 2) if dec_wr is not None else "",
                     "pool_decayed_games": round(wg_, 1),
                     "pool_decayed_se": round(_se(wg_), 2) if wg_ else "",
                     "pool_players_rated": len(rk),
                     "pool_low_net_wins": sum(
                         1 for c in rk if c["net_wins"] <= POOL_LOW_NET_WINS),
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
    # NOT the tier ordering. The tier list is ONE player per hero, ranked by
    # that player's overall account standing, and it is built by
    # ceiling_rank.py / consumed by build_site_data.py, which orders on
    # ceiling_rank and ignores this file's ordering entirely.
    #
    # A pooled statistic here answers a different question — how the hero's
    # WHOLE elite cohort does — and briefly sorted this file, which put Vyper
    # 2nd on the strength of 40 players while its ceiling player sat last at
    # 8 net wins. The pool columns are RETAINED as a sample-quality
    # diagnostic (see pool_low_net_wins) and are deliberately not the key.
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
          ["rank", "hero_id", "hero",
           "elite_winrate", "elite_games", "elite_se",
           # pool-quality diagnostics, NOT a ranking. Prefixed so nothing
           # downstream mistakes them for the ceiling player's figures.
           "pool_net_wins", "pool_net_wins_decayed", "pool_decayed_winrate",
           "pool_decayed_games", "pool_decayed_se",
           "pool_ranked_games", "pool_ranked_wins",
           "pool_players_rated", "pool_low_net_wins",
           "offhero_winrate", "offhero_games", "offhero_players",
           "winrate_delta", "delta_se", "delta_rank",
           "lane_split", "lane_weak", "lane_role", "median_rating", "top5_rating",
           "top_rating", "players", "builds_sampled", "thin", "by_region",
           "top_account_id", "icon_url"])

    # ROSTER — every released hero, WHETHER OR NOT it produced data this run.
    # tierlist.csv only carries heroes with a sampled pool, so a hero with no
    # pool yet (a new release) had no row anywhere and the site could not even
    # show its art. build_site_data.py lists roster heroes it cannot rank as
    # NEW, and fetch_icons.py downloads their icons from here. Aggregates only
    # — no account ids — so it is safe to upload with the other CSVs.
    in_tier = {t["hero_id"] for t in tier}
    roster = []
    for hid in sorted(heroes, key=lambda h: heroes[h]):
        lst = chosen.get(hid, [])
        roster.append({
            "hero_id": hid, "hero": heroes[hid],
            "class_name": hero_meta.get(hid, {}).get("class_name", ""),
            "hero_type": hero_meta.get(hid, {}).get("hero_type", ""),
            "development_state": hero_meta.get(hid, {}).get("development_state", ""),
            "icon_url": hero_icon.get(hid, ""),
            "board_entries": sum(len(ladder.get((hid, rg)) or []) for rg in REGIONS),
            "players": len(lst),
            "orbit_players": sum(1 for c in lst if c.get("source") == "orbit"),
            "sweep_players": sum(1 for c in lst if c.get("source") == "sweep"),
            "orbit2_players": sum(1 for c in lst if c.get("source") == "orbit2"),
            "builds_sampled": builds.get(hid, 0),
            "in_tierlist": "YES" if hid in in_tier else "",
            # carried into docs/data.json by build_site_data.py, which is
            # where the next run's load_first_seen reads it back from
            "first_seen": first_seen.get(hid, ""),
            "new": "YES" if hid in new_heroes else "",
        })
    write("roster.csv", roster,
          ["hero_id", "hero", "class_name", "hero_type", "development_state",
           "icon_url", "board_entries", "players", "orbit_players", "sweep_players",
           "orbit2_players", "builds_sampled", "in_tierlist", "first_seen", "new"])
    missing = [r for r in roster if not r["in_tierlist"]]
    if missing:
        print("  [roster] %d released hero(es) produced no pool this run and will "
              "show as NEW on the site: %s"
              % (len(missing), ", ".join("%s (%d board entries)"
                                         % (r["hero"], r["board_entries"])
                                         for r in missing)), file=sys.stderr)

    # sorted by ranked_rating now, since mmr is dead while badge reads 0
    write("candidates.csv",
          [dict(c, hero=heroes.get(c["hero_id"], ""))
           for lst in chosen.values()
           for c in sorted(lst, key=lambda x: -x["ranked_rating"])],
          ["hero_id", "hero", "region", "ladder_pos",
           # the restored cross-reference: standing on the region's general
           # board, whether the entry was found there at all, whether Valve's
           # own top_hero_ids agrees with the assignment, and whether the id
           # was claimed by BOTH boards. ceiling_rank.py orders on global_pos.
           "global_pos", "located_on_general", "valve_top_hero", "id_confirmed",
           "account_id", "account_name",
           "ranked_rating", "hero_games", "hero_wins",
           "ranked_games", "ranked_wins", "net_wins", "net_wins_decayed",
           "offhero_games", "offhero_wins",
           "last_match_id", "last_played", "ambiguous",
           # blank for board-sourced rows; "sweep" for a new hero's top-ladder
           # fill (new_hero_fill); "orbit2" plus "shared/played" (ring-1
           # matches / all matches in the window) for its ring-2 tier; "orbit"
           # plus the breadth of contact
           # for anyone the orbit fill supplied
           "source", "orbit_seeds_met", "orbit2_shared"])
    if POOL_NET_WINS:
        write("pool_audit.csv",
              sorted(pool_low, key=lambda r: (r["net_wins"], r["hero"])),
              ["hero", "hero_id", "region", "account_id", "account_name",
               "source", "orbit_seeds_met", "ranked_games", "ranked_wins",
               "net_wins", "net_wins_decayed", "ranked_rating"])

    # A disabled id reaching this point means the filter in load_assets() has
    # been bypassed or the asset dump changed shape. Fail loudly: shipping a
    # dead item quietly is exactly what happened before, and it survived
    # several runs because nothing ever objected.
    _leaked = sorted({r["item_id"] for r in freq if r["item_id"] in dead_ids})
    if _leaked:
        raise SystemExit(
            "%d disabled item id(s) reached item_frequency.csv: %s\n"
            "load_assets() should have excluded these. Not writing CSVs."
            % (len(_leaked), ", ".join(str(i) for i in _leaked[:12])))

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
    src_rows = []
    for (hid, rg, src), counter in sorted(src_holds.items()):
        for iid, c in sorted(counter.items(), key=lambda kv: -kv[1]):
            src_rows.append({
                "hero_id": hid, "hero": heroes.get(hid, ""), "region": rg,
                "source": src, "item_id": iid,
                "item": items.get(iid, {}).get("name", ""),
                "category": (items.get(iid, {}).get("cat") or "?"),
                "count": c, "of_builds": src_builds[(hid, rg, src)],
            })
    write("source_items.csv", src_rows,
          ["hero_id", "hero", "region", "source", "item_id", "item",
           "category", "count", "of_builds"])

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
          ["hero_id", "region", "ladder_pos", "global_pos", "located_on_general",
           "account_name", "badge_level", "reason"])

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
        # restval, because rows now come from several sources — board-sourced,
        # orbit-sourced, exclusion records — and a source that legitimately has
        # no general-board standing should write a blank cell, not raise.
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    print("  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)


if __name__ == "__main__":
    main()
