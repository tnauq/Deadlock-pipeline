#!/usr/bin/env python3
"""
Ceiling ranking: order heroes by their single strongest player.

The per-hero leaderboard gives position WITHIN a hero, so it cannot compare
heroes against each other — every hero has a rank 1. A hero's ceiling is
therefore its best board player's standing in the region as a whole.

WHAT ORDERS THE CEILING — REVERTED 2026-09-02 to the cross-hero board
---------------------------------------------------------------------
CEILING_METRIC=board (the default again) orders a hero by its best confirmed
player's POSITION on the region's general leaderboard. CEILING_METRIC=net_wins
restores the ranked ordering described below.

Why the revert: the ranked ordering rested on ranked play being a growing,
representative slice. It is not. Ranked volume stalled and the ranked ladder
feeding it has been degrading run over run, so net wins is now accumulated over
a shrinking and unrepresentative sample, and the ranked_rank floor meant to
contain that has been unreadable on every row since 2026-08-18. The general
board is imperfect for the reasons set out immediately below — it is all-mode
and unfilterable — but it is PUBLISHED BY VALVE, is a single cross-hero
ordering by construction, and does not depend on any of the ratings that died.
An unfilterable board beats a filtered statistic computed over a slice that is
disappearing.

The ranked argument is kept verbatim below because it was correct when written
and will be correct again if ranked recovers; net_wins is one env var away.

--- ranked-era reasoning, retained ---
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

WHY NET WINS IS NOT A PROXY
---------------------------
A ranked win is about +250 Rank Points and a loss about -250; 1000 RP buys a
Subrank, 2000 for the last one before a new Rank. Accumulated RP is therefore
roughly 250 x net wins, so net wins is a RESCALED COPY OF THE CURRENCY, not a
correlate of it. That is the real reason the concordance table above came out
the way it did, and it is why DECAY MUST NOT BE APPLIED HERE: a decayed
net-wins figure corresponds to no rank any player holds.

Two known biases, both directional and neither correctable from the data:

    loss protection   5 games at a full-Rank boundary, 2 at a Subrank
                      boundary. Those losses subtract from net wins but cost
                      no real rank, so net wins UNDERSTATES the badge of
                      players who park at boundaries rather than climb.
    streak bonuses    wins on a streak award more than 250, so net wins
                      UNDERSTATES fast climbers.

Both push the same way, and together they are what makes the system
INFLATIONARY: even a 50:50 player climbs eventually. Inflation is harmless for
an ORDERING as long as everyone inflates on the same schedule — which is
exactly what stops being true at Eternus, where thresholds move relatively so
the inflation cancels out instead of carrying everyone upward.

*** THIS METRIC HAS AN EXPIRY. *** At Eternus the system reverts to percentile
ranking, where inflation stops moving anyone and net wins decouples from rank.
One player is at Eternus as of 2026-08-07. When enough arrive that the top is
percentile-ranked, `ceiling_value()` below is the single function to replace.
Badge is not expected to return: it reads 0 across every ranked row, and being
a team average it would be non-zero if any Eternus player were in the lobby.
The Eternus watch below is what tells you when that day has come.

THE RANK FLOOR — added 2026-08-18
---------------------------------
Net wins alone is an ACCUMULATION, and accumulation is reachable by volume at
any rank: a 51% player with 900 ranked games outscores a 60% player with 300.
Under a progression ladder those two really do hold different badges, so the
floor exists to keep grinders out of a statistic that is supposed to describe
the top of a hero's playerbase. A candidate must sit at or above
CEILING_MIN_RANK (default Phantom) to be eligible to BE a ceiling.

The floor gates ELIGIBILITY only. It does not touch `ceiling_value()`, and the
ordering among the survivors is unchanged — see the docstring section above for
why net wins must not be reweighted or decayed.

    *** READ THIS BEFORE TRUSTING THE FLOOR ***
The only rank signal that reaches this script is the board's `ranked_rank`
field, and on the 2026-08-18 aggregate run it was EMPTY on all 74 rows —
`rank_name` was blank for every ceiling player and the Eternus watch reported
no readable rank at all. `badge_level` is blank too. Applied strictly to that
data the floor drops every candidate and writes an empty ceiling.csv.

So CEILING_RANK_UNKNOWN decides what an unreadable rank means, and it defaults
to "keep" — floor the players it can read, pass the ones it cannot, and count
both in the log and in the CSV. This is deliberately the weak setting: it
degrades to current behaviour when the field is empty instead of silently
emptying the output. Set it to "drop" once `ranked_rank` is actually populated,
and check the [floor] log line to find out whether it is. Orbit candidates were
never on a board and so ALWAYS have an unknown rank; under "drop" they can
never be a ceiling, which defeats the fallback below.

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
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
OUT_DIR = "output"
# "" = no match_mode filter: standard play, which is what the general board is
# itself built from. Matches the pipeline's own default. Set MATCH_MODE=Ranked
# to go back to the ranked cohort (and set CEILING_METRIC=net_wins with it).
MATCH_MODE = os.environ.get("MATCH_MODE") or ""

# WHAT ORDERS THE CEILING.
#   "board"     general-board position, ascending — the restored default
#   "net_wins"  ranked net wins, descending — the 2026-08-07 to 2026-09-02
#               behaviour, kept for comparison and for if ranked recovers
# Whichever is not the key is still written to ceiling.csv as a reference
# column, so one run produces both orderings' inputs and they can be diffed.
CEILING_METRIC = (os.environ.get("CEILING_METRIC") or "board").strip().lower()
if CEILING_METRIC not in ("board", "net_wins"):
    raise SystemExit("CEILING_METRIC must be 'board' or 'net_wins', got %r"
                     % CEILING_METRIC)
MAX_URL = int(os.environ.get("MAX_URL") or 9000)

# Shrinkage for the SECONDARY win-rate column. Not the ordering — kept so the
# two can be compared when the ladder switches to percentile ranking and net
# wins expires. k=25 matches the pipeline's pool selection.
SHRINK_K = int(os.environ.get("SHRINK_K") or 25)

# ---- hero-play validation -------------------------------------------------
# Does the candidate actually PLAY the hero whose board they are on?
#
# The failure this catches: Valve publishes no account ids, so identity rests
# on deadlock-api's fuzzy name resolution, and a SHORT OR COMMON DISPLAY NAME
# collides. Several different players called "Snakes", each on a different
# hero board, all intersect the same general-board entry's id list and all
# resolve to the SAME account. The output looks like one dominant flex player
# holding five hero boards at #1; it may be five different people. Dual
# confirmation cannot see this, because both boards agree on a name that is
# not unique.
#
# The check costs NOTHING. fetch_ranked_records already returns per
# (account, hero) rows and they are currently used only to gate orbit
# candidates. A genuine flex player shows real games on every hero claimed;
# a collision shows games on one and nothing on the rest.
#
# This is deliberately NOT exclusivity. A player with real games on five
# heroes keeps all five — measured 2026-07-31, exclusivity dropped pool fill
# from 98% to 83% and pushed hero-regions under 15 builds from 2 to 18, so it
# stays off. This drops only claims with no play behind them.
VALIDATE_HERO_PLAY = os.environ.get("VALIDATE_HERO_PLAY", "1") == "1"
# Minimum games on the ACCOUNT, across all heroes, for its identity to be
# credible at all. Added 2026-09-02 after run 45, and it is the stronger of the
# two checks because it does not depend on which hero is claimed.
#
# A candidate asserted to be near the top of a 1,000-entry regional board whose
# entire record is eight games is internally contradictory — the id is wrong,
# not the player. Run 45 split cleanly: the 30 ceilings whose hero row came
# back missing had a median of 8 account games (1, 1, 1, 1, 1, 3, 3, ...) while
# the 46 verified ones started at 38 and ran to a median of 2,420. Two
# populations, not a gradient. A floor of 100 takes exactly the first group,
# 15 in each region, and nothing that verified.
#
# The misresolutions cluster at the TOP of the board — median global_pos 17
# against 49 for verified candidates — so this check matters most precisely
# where it does the most damage.
CEILING_MIN_ACCOUNT_GAMES = int(os.environ.get("CEILING_MIN_ACCOUNT_GAMES") or 100)
# Games on the hero required of a BOARD candidate. Orbit candidates have their
# own, stricter gate (ORBIT_MIN_HERO_GAMES) because board membership at least
# claims hero play whereas orbit membership claims nothing.
CEILING_MIN_HERO_GAMES = int(os.environ.get("CEILING_MIN_HERO_GAMES") or 5)
# Choose WHICH of a name's possible_account_ids to believe by hero play rather
# than by list order. Added 2026-09-03 after run 47.
#
# possible_account_ids is a candidate list, and slot 0 is only best-match-first
# on deadlock-api's own fuzzy name resolution — it knows nothing about which
# hero board the name was found on. Run 47's six worst rows were all slot-0
# picks that survived every existing check: account totals of 180-279 (above
# the 100 floor), hero games of 5-43 (above the 5 floor), identity_verdict
# pass on all six. Probes A/B/C/F ruled out truncation and a lookback window;
# the accounts are simply the wrong people. random kid's id does not carry
# Bebop in its top five heroes at all, and xD's does not carry Vyper.
#
# The fix costs no extra calls. fetch_ranked_records already returns a row per
# (account, hero), so once the ids are fetched the hero record for every
# candidate id is in hand and the one that actually plays the hero can be
# picked. Set to 0 to restore slot-0-wins ordering.
RESOLVE_ID_BY_PLAY = os.environ.get("RESOLVE_ID_BY_PLAY", "1") == "1"
# How many ids per name to carry forward for resolution. The measured
# distribution is 92% slot 0, 8% slot 1, 0% slot 2+, so 4 is generous; the cost
# is only that hero-stats is asked about more accounts, on a 100 req/s bucket.
ID_OPTIONS_MAX = int(os.environ.get("ID_OPTIONS_MAX") or 4)
# AUDIT ONLY by default. Share of an account's games that sit on the hero it
# was crowned for. Reported in ceiling.csv as hero_share so the threshold can
# be chosen from data rather than guessed; set above 0 to make it a filter.
# Run 47 reference points: the six bad rows ran 0.132-0.381, while a verified
# ceiling like account 123795813 on Wraith sits at 0.476. The populations
# overlap, which is exactly why this ships as a column and not a floor.
CEILING_MIN_HERO_SHARE = float(os.environ.get("CEILING_MIN_HERO_SHARE") or 0.0)
# What a MISSING hero-stats row means. "keep" (default) treats absence as
# absence of evidence — the endpoint can simply not return a row. "drop" treats
# it as evidence of absence. Only a row that EXISTS and falls under the
# threshold is positive evidence of no play, and that case is always dropped.
# DEFAULT FLIPPED TO "drop" 2026-09-02. The original "keep" rested on a
# missing row being absence of evidence. Run 45 showed it is not: a missing
# hero row alongside a near-empty account record is positive evidence the id
# is wrong. With the account-games floor in place this is now the weaker of
# the two checks and rarely the one that fires.
HERO_PLAY_MISSING = (os.environ.get("HERO_PLAY_MISSING") or "drop").strip().lower()

# NEVER LOSE A HERO. If every candidate for a hero-region fails identity
# validation, the best of the REJECTED candidates is used anyway rather than
# the hero-region vanishing from ceiling.csv, and the row is marked
# identity_verified=NO. Thinning the pool must not thin the OUTPUT: a flagged
# row a reader can discount is strictly better than a silent gap, which is how
# NAmerica/Mina and Europe/Warden disappeared from run 42. Set to 0 to make
# validation failures drop the hero-region outright.
IDENTITY_FALLBACK = os.environ.get("IDENTITY_FALLBACK", "1") == "1"

# ---- hero-board-only candidates -------------------------------------------
# Admit hero-board players who are not on the region's cross-hero board, as a
# second tier ranked strictly behind every dual-confirmed player. See
# all_on_board() for why their general position is CENSORED rather than
# imputed, and why no win-rate term is mixed in. 0 = old strict behaviour.
ALLOW_HERO_BOARD_ONLY = os.environ.get("ALLOW_HERO_BOARD_ONLY", "1") == "1"

# ---- orbit fallback -------------------------------------------------------
# Board-sourced candidates run thin on unpopular heroes — 11 of 76 hero-regions
# had under five before the general-board rule was relaxed. Orbit 1 is everyone
# who shared a ranked match with a ceiling player, taken straight from
# match_player, so identity is exact: no possible_account_ids, no name matching.
#
# Measured 2026-08-08 (NAmerica, 3-day window, 12 seeds):
#     orbit 0    12 accounts   median winrate 0.568   p90 0.634
#     orbit 1   949            median 0.530           p90 0.629
#     orbit 2  6,879           median 0.504           p90 0.607
# Orbit 2 sits at the population mean, so ONE HOP is the useful radius; two is
# dilution. Orbit 1's p90 matches the seeds' — its top is as strong as they
# are, which is what a ceiling (a maximum) actually needs.
#
# Proximity is measured as DISTINCT SEEDS shared with, not raw shared matches:
# a duo partner queuing with one seed all evening racks up matches without
# being of comparable standing, whereas meeting several different ceiling
# players is hard to do by accident.
ORBIT_FALLBACK = os.environ.get("ORBIT_FALLBACK", "1") == "1"
ORBIT_MIN_CANDIDATES = int(os.environ.get("ORBIT_MIN_CANDIDATES") or 8)
ORBIT_SEEDS = int(os.environ.get("ORBIT_SEEDS") or 12)
ORBIT_DAYS = int(os.environ.get("ORBIT_DAYS") or 3)
ORBIT_MIN_SEEDS_MET = int(os.environ.get("ORBIT_MIN_SEEDS_MET") or 1)
ORBIT_MIN_GAMES = int(os.environ.get("ORBIT_MIN_GAMES") or 10)
# Games ON THE HERO. Board candidates prove hero play by being on the hero's
# board; an orbit candidate proves nothing, so this is the only thing stopping
# someone who has never played Vyper from becoming Vyper's ceiling.
ORBIT_MIN_HERO_GAMES = int(os.environ.get("ORBIT_MIN_HERO_GAMES") or 5)

# How many board entries to keep for the archive (player bar-chart-race data).
# Costs no extra API calls — the full board is already fetched.
BOARD_ARCHIVE_TOP = int(os.environ.get("BOARD_ARCHIVE_TOP") or 100)

# ---- Eternus watch --------------------------------------------------------
# ceiling_value() is valid only while the ladder is a progression system. At
# Eternus I the Subrank thresholds stop being fixed and are recomputed daily on
# a PERCENTILE basis, at which point inflation stops carrying anyone upward and
# net wins decouples from rank.
#
# badge_level cannot detect this: it reads blank on every row. The board's own
# `ranked_rank` field is already fetched and thrown away, and it is the
# surviving signal, so it is counted here instead.
#
# Rank names in board order; Eternus is last. The field may come back as a
# rank index, a name, or a badge-style int (rank*10 + subrank) — _rank_name()
# normalises all three.
RANK_NAMES = ["Obscurus", "Initiate", "Seeker", "Alchemist", "Arcanist",
              "Ritualist", "Emissary", "Archon", "Oracle", "Phantom",
              "Ascendant", "Eternus"]
# Share of ranked board entries at Eternus that trips the warning.
ETERNUS_WARN_SHARE = float(os.environ.get("ETERNUS_WARN_SHARE") or 0.02)

# ---- rank floor -----------------------------------------------------------
# Minimum rank to be eligible as a ceiling. See the docstring section above.
# Set to "" to disable the floor entirely.
# `or "Phantom"` would swallow an explicit empty value, so the default is
# applied only when the variable is genuinely unset — CEILING_MIN_RANK= means
# OFF, not Phantom.
# DEFAULT CHANGED 2026-09-02: off under CEILING_METRIC=board. The floor exists
# because net wins is an ACCUMULATION that a high-volume player at a middling
# rank can reach by grinding. Board position is not an accumulation — it is
# already a ranking — so the failure the floor guards against does not arise,
# and applying an unreadable rank check on top of it can only drop good
# candidates. Under net_wins the default is Phantom exactly as before.
_MIN_RANK_ENV = os.environ.get("CEILING_MIN_RANK")
_MIN_RANK_DEFAULT = "" if CEILING_METRIC == "board" else "Phantom"
CEILING_MIN_RANK = (_MIN_RANK_DEFAULT if _MIN_RANK_ENV is None
                    else _MIN_RANK_ENV).strip()
# What an unreadable rank means: "keep" (default, degrades to no floor when the
# field is empty) or "drop" (strict — only use once ranked_rank is populated).
CEILING_RANK_UNKNOWN = (os.environ.get("CEILING_RANK_UNKNOWN") or "keep").strip().lower()


def _rank_name(v):
    """Board rank value -> rank name, or '' if it cannot be read."""
    if v is None or v == "":
        return ""
    if isinstance(v, str) and not v.strip().lstrip("-").isdigit():
        return v.strip().split()[0]
    try:
        n = int(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    # a badge-style value carries the subrank in the ones digit
    idx = n // 10 if n >= 10 else n
    return RANK_NAMES[idx] if 0 <= idx < len(RANK_NAMES) else ""


def _rank_index(v):
    """Board rank value -> index into RANK_NAMES, or None if unreadable."""
    nm = _rank_name(v)
    return RANK_NAMES.index(nm) if nm in RANK_NAMES else None


def _min_rank_index():
    """Configured floor as an index, or None when the floor is off/unknown."""
    if not CEILING_MIN_RANK:
        return None
    if CEILING_MIN_RANK in RANK_NAMES:
        return RANK_NAMES.index(CEILING_MIN_RANK)
    print("  [floor] CEILING_MIN_RANK=%r is not a rank name; floor DISABLED. "
          "Valid: %s" % (CEILING_MIN_RANK, ", ".join(RANK_NAMES)), file=sys.stderr)
    return None


def passes_floor(cand, floor_idx):
    """
    (eligible, verdict) for one candidate against the rank floor.

    verdict is "pass" / "below" / "unknown" so the caller can count how much of
    the floor is actually biting versus how much is unreadable — the difference
    between a working floor and a floor that is doing nothing at all.
    """
    if floor_idx is None:
        return True, "pass"
    idx = _rank_index(cand.get("ranked_rank"))
    if idx is None:
        # orbit candidates were never on a board and always land here
        return (CEILING_RANK_UNKNOWN != "drop"), "unknown"
    return (idx >= floor_idx), ("pass" if idx >= floor_idx else "below")


def passes_identity(cand, hero_id, rec, hero_records):
    """(eligible, verdict, hero_games) for one board candidate.

    Two checks, cheapest and strongest first. Both run off data already
    fetched — fetch_ranked_records returns account totals and per (account,
    hero) rows, so neither costs a call.

    1. ACCOUNT PLAUSIBILITY. Fewer than CEILING_MIN_ACCOUNT_GAMES games on the
       whole account contradicts the board standing being claimed for it. The
       id is wrong. Verdict "implausible_identity".
    2. HERO PLAY. The account is real, but has essentially no games on the hero
       whose board it was found on — so the board entry belongs to someone
       else with the same display name. Verdict "no_play", or "missing" when
       the endpoint returned no row for the pair at all.

    Orbit candidates are exempt from check 2: they were gated on hero games at
    admission and carry no board claim to falsify. They still face check 1.
    """
    if not VALIDATE_HERO_PLAY:
        return True, "pass", None
    if rec is None or rec.get("games", 0) < CEILING_MIN_ACCOUNT_GAMES:
        return False, "implausible_identity", None
    if cand.get("match") == "orbit":
        return True, "pass", None
    hrec = hero_records.get((cand["account_id"], hero_id))
    if hrec is None:
        return (HERO_PLAY_MISSING != "drop"), "missing", None
    g = hrec.get("games", 0)
    if g < CEILING_MIN_HERO_GAMES:
        return False, "no_play", g
    return True, "pass", g


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
            "ranked_rank": e.get("ranked_rank"),
            "top_heroes": e.get("top_hero_ids") or [],
            # CANDIDATE list, not an identity — one name can carry 30+ ids.
            # Only trustworthy once cross-checked against a resolved pool
            # member, which archive_snapshot.py does.
            "ids": [int(a) for a in (e.get("possible_account_ids") or []) if a][:8],
        })

    print("  [board] %-9s %d entries, %d named, depth %d"
          % (region, len(entries), sum(len(v) for v in by_name.values()), len(entries)),
          file=sys.stderr)

    # ---- Eternus watch, from ranked_rank -----------------------------------
    ranks = Counter(_rank_name(e.get("ranked_rank")) for e in entries)
    ranks.pop("", None)
    n_rank = sum(ranks.values())
    if n_rank:
        top = ", ".join(
            "%s=%d" % (k, v) for k, v in
            sorted(ranks.items(),
                   key=lambda kv: -(RANK_NAMES.index(kv[0])
                                    if kv[0] in RANK_NAMES else -1))[:4])
        print("  [rank]  %-9s %d of %d entries carry a rank; top: %s"
              % (region, n_rank, len(entries), top), file=sys.stderr)
        n_et = ranks.get("Eternus", 0)
        if n_et:
            share = n_et / n_rank
            print("  [eternus] %-9s %d entries at Eternus (%.2f%% of ranked entries)"
                  % (region, n_et, 100 * share), file=sys.stderr)
            if share >= ETERNUS_WARN_SHARE:
                print("  [eternus] *** %s is at %.1f%%, over the %.1f%% threshold. "
                      "Eternus is percentile-ranked, so net wins is decoupling "
                      "from rank — ceiling_value() needs replacing. See the "
                      "module docstring. ***"
                      % (region, 100 * share, 100 * ETERNUS_WARN_SHARE),
                      file=sys.stderr)
    else:
        print("  [rank]  %-9s no readable ranked_rank on any entry — Eternus "
              "cannot be detected from the board this run" % region,
              file=sys.stderr)
        if _min_rank_index() is not None:
            print("  [floor] %-9s and the %s floor has nothing to read here: "
                  "every board candidate will be 'unknown' (policy=%s)"
                  % (region, CEILING_MIN_RANK, CEILING_RANK_UNKNOWN),
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
        # ORDER MATTERS and must never be re-sorted: the id list is
        # best-match-first (92% of resolutions came from slot 0, 8% from slot 1,
        # 0% from slot 2+). It was a set here, which threw that away — harmless
        # while every candidate was dual-confirmed and the intersection was
        # usually a single id, but a hero-board-only candidate has no
        # intersection to fall back on and slot 0 is the whole of its identity.
        ids = []
        for a in (e.get("possible_account_ids") or []):
            a = int(a)
            if a and a not in ids:
                ids.append(a)
        out.append({"hero_pos": i, "name": nm, "ids": ids})
    return out


def resolve_identity(cand, hero_id, records, hero_records):
    """Pick which of a candidate's possible ids is actually this hero's player.

    Mutates `cand` in place and returns (chosen_id, slot, hero_share).

    Runs AFTER fetch_ranked_records, because the evidence it needs — games on
    THIS hero for each candidate id — only exists once hero-stats has come
    back. No extra requests: every id in id_options was already included in
    the fetch.

    Ordering, strongest signal first:
      1. games on the crowned hero. A board entry asserts hero play; the id
         that has none is not that entry, whatever its slot.
      2. share of the account's games on that hero, which separates a main
         from a big account that dabbles.
      3. total account games, as a last tiebreak.

    Slot order is preserved as the final term, so when nothing distinguishes
    two ids the historical behaviour stands and the run stays reproducible.
    """
    opts = cand.get("id_options") or [cand["account_id"]]
    if not RESOLVE_ID_BY_PLAY or len(opts) < 2:
        rec = records.get(cand["account_id"]) or {}
        hrec = hero_records.get((cand["account_id"], hero_id)) or {}
        tot = rec.get("games", 0)
        hg = hrec.get("games", 0)
        cand["hero_share"] = round(hg / tot, 4) if tot else ""
        cand["id_slot"] = 0
        return cand["account_id"], 0, cand["hero_share"]

    def score(slot_aid):
        slot, aid = slot_aid
        rec = records.get(aid) or {}
        hrec = hero_records.get((aid, hero_id)) or {}
        tot = rec.get("games", 0)
        hg = hrec.get("games", 0)
        share = (hg / tot) if tot else 0.0
        return (hg, share, tot, -slot)

    best_slot, best_aid = max(enumerate(opts), key=score)
    rec = records.get(best_aid) or {}
    hrec = hero_records.get((best_aid, hero_id)) or {}
    tot = rec.get("games", 0)
    share = round(hrec.get("games", 0) / tot, 4) if tot else ""
    if best_aid != cand["account_id"]:
        cand["id_reresolved"] = cand["account_id"]
    cand["account_id"] = best_aid
    cand["id_slot"] = best_slot
    cand["hero_share"] = share
    return best_aid, best_slot, share


def all_on_board(hero_entries, by_name, board_size=0, region_depth=0):
    """
    Every usable player on a hero's board, in two tiers.

    TIER 1 — "confirmed". Display name appears on BOTH this hero's board and
    the region's cross-hero board, AND the two candidate id lists intersect.
    possible_account_ids is deadlock-api's own fuzzy name resolution, not an
    identity Valve publishes, and name-only matching put the wrong player in
    110 of 371 slots — so both signals are required. Unchanged.

    TIER 2 — "hero_board_only", new 2026-09-02, gated by ALLOW_HERO_BOARD_ONLY.
    A hero-board player whose name is not on the cross-hero board at all. As
    the general board degrades this is most of the board: confirm rate on run
    43 was 23.7% NA / 29.3% EU, so roughly three quarters of entries were being
    discarded outright, and thin heroes were left with four or five candidates
    out of a board of twenty.

    HOW TIER 2 IS RANKED, since it has no cross-hero position. Being absent
    from the general board is not missing data — that board IS the region's
    top 1,000 (limit=2000 and limit=5000 both return 1,001), so an absent
    player is genuinely ranked below 1,000th. Their general position is
    therefore CENSORED at region_depth + 1 rather than imputed or guessed, and
    no second metric (win rate, net wins) is mixed into a positional ordering.
    They sort behind every confirmed player by construction.

    The censoring conflates two causes — genuinely below the cut, or a name
    deadlock-api failed to resolve — and cannot separate them. The error only
    ever pushes a real candidate DOWN, never promotes a weak one, so a tier-2
    ceiling is a FLOOR on that hero's ceiling, not an estimate of it. `match`
    carries the tier so the site can mark those rows.
    """
    censored_pos = (region_depth or 1000) + 1
    found = []
    for he in hero_entries:
        hit = None
        for cand in by_name.get(he["name"], []):
            # first id both boards offer, in the hero board's own order — NOT
            # min(), which picked the numerically smallest and silently
            # discarded best-match-first ordering
            common = [a for a in he["ids"] if a in cand["ids"]]
            if common:
                hit = {
                    "name": cand["name"],
                    # PROVISIONAL. resolve_identity() may replace this with
                    # another id from id_options once hero-stats has been
                    # fetched and it is known which of them plays the hero.
                    "account_id": common[0],
                    "id_options": common[:ID_OPTIONS_MAX],
                    "pos": cand["pos"],
                    "hero_pos": he["hero_pos"],
                    "badge": cand["badge"],
                    "ranked_rank": cand.get("ranked_rank"),
                    "top_heroes": cand["top_heroes"],
                    "match": "confirmed",
                    "board_size": board_size,
                }
                break
        if hit is None and ALLOW_HERO_BOARD_ONLY and he["ids"]:
            hit = {
                "name": he["name"],
                # slot 0: the only identity signal a tier-2 entry has, and the
                # tier that most needs resolve_identity — there is no
                # intersection with the general board to narrow the list
                "account_id": he["ids"][0],
                "id_options": he["ids"][:ID_OPTIONS_MAX],
                "pos": censored_pos,
                "hero_pos": he["hero_pos"],
                "badge": None,
                "ranked_rank": None,
                "top_heroes": [],
                "match": "hero_board_only",
                "board_size": board_size,
            }
        if hit is not None:
            found.append(hit)
    return found


def _hs_url(base, ids):
    """base already ends in '?' or '?match_mode=...'; join the ids correctly
    either way rather than assuming a leading '&' is safe."""
    q = "&".join("account_ids=%d" % a for a in ids)
    return base + ("&" if not base.endswith("?") else "") + q


def fetch_ranked_records(account_ids):
    """
    account_id -> {"games": n, "wins": n} over MATCH_MODE play, all heroes.

    /v1/players/hero-stats is batched via repeated account_ids params, runs at
    100 req/s, and is a different bucket from /v1/sql. It returns one row per
    (account, hero); totalling across heroes gives the account-level record,
    which is what a ladder position reflects — rank is an account property, not
    a per-hero one.
    """
    ids = sorted(set(int(a) for a in account_ids if a))
    if not ids:
        return {}, {}
    out = defaultdict(lambda: {"games": 0, "wins": 0})
    # per (account, hero) as well: an orbit candidate has no board membership
    # to prove they play the hero, so the hero-level record is the only check
    per_hero = {}
    # An empty match_mode is omitted rather than sent blank — hero-stats 400s
    # on match_mode= with no value.
    params = ["match_mode=%s" % urllib.parse.quote(MATCH_MODE)] if MATCH_MODE else []
    base = "%s/v1/players/hero-stats?%s" % (BASE, "&".join(params))
    # ~22.7 encoded chars per id (SCHEMA.md quirk 4); size the chunk against the
    # REAL url length, prefix included, rather than the query string alone
    per = 24
    chunk = max(20, (MAX_URL - len(base) - 40) // per)
    calls = 0
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        url = _hs_url(base, part)
        if len(url) > MAX_URL:
            part = part[:max(20, len(part) // 2)]
            url = _hs_url(base, part)
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
            h = r.get("hero_id")
            if h is not None:
                per_hero[(int(a), int(h))] = {"games": int(g), "wins": int(w)}
        time.sleep(0.05)
    print("  [hs] %d accounts, %d (account,hero) rows over %d calls (no SQL used)"
          % (len(out), len(per_hero), calls), file=sys.stderr)
    return out, per_hero


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


def sql(query, label=""):
    """One /v1/sql call. The quota is 2 per 60s on a SLIDING window and a 429
    still consumes a slot, so a refusal is waited out in full rather than
    retried immediately."""
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query)
    if len(url) > MAX_URL:
        raise RuntimeError("orbit query %d chars, over %d" % (len(url), MAX_URL))
    print("  [sql] %s (%d char url)" % (label, len(url)), file=sys.stderr)
    for attempt in range(3):
        try:
            rows = get(url)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                print("  [sql] 429, waiting 65s", file=sys.stderr)
                time.sleep(65)
                continue
            raise
    else:
        raise RuntimeError("rate limited")
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    return rows


def fetch_orbit1(seed_ids):
    """
    account_id -> {"seeds_met": n, "shared": n} for everyone who shared a
    ranked match with a seed. ONE SQL call for a dozen seeds.
    """
    seeds = sorted(set(int(a) for a in seed_ids if a))[:ORBIT_SEEDS]
    if not seeds:
        return {}
    try:
        mode_sql = "match_mode = '%s' AND " % MATCH_MODE if MATCH_MODE else ""
        rows = sql(Q_ORBIT.format(ids=",".join(str(a) for a in seeds),
                                  mode=mode_sql, days=ORBIT_DAYS),
                   "orbit1 from %d seeds" % len(seeds))
    except Exception as e:
        print("  [orbit] failed (%s) — continuing without the fallback" % e,
              file=sys.stderr)
        return {}
    by_match = defaultdict(set)
    for r in rows:
        by_match[int(r["match_id"])].add(int(r["account_id"]))
    seedset = set(seeds)
    out = defaultdict(lambda: {"seeds_met": set(), "shared": 0})
    for _mid, accts in by_match.items():
        met = accts & seedset
        if not met:
            continue
        for a in accts - seedset:
            out[a]["seeds_met"] |= met
            out[a]["shared"] += 1
    final = {a: {"seeds_met": len(v["seeds_met"]), "shared": v["shared"]}
             for a, v in out.items()}
    if final:
        breadth = Counter(v["seeds_met"] for v in final.values())
        print("  [orbit] %d matches, %d players; seeds met: %s"
              % (len(by_match), len(final), dict(sorted(breadth.items()))),
              file=sys.stderr)
    return final


NO_BOARD_POS = 10 ** 9      # the sentinel an orbit candidate carries


def hero_pct(cand):
    """Position on the hero's OWN board as a fraction, 0 = best.

    THE TIEBREAK FOR CENSORED CANDIDATES. Every hero-board-only player in a
    region shares the same censored general position, so without this all of
    them tie and two heroes whose ceilings are both their board's #1 would be
    ordered arbitrarily. Dividing by board size makes #1 of 204 beat #1 of 18,
    on the reasoning that the deeper board's leader has beaten more people.

    Two limits worth knowing. It rewards board DEPTH, which tracks hero
    popularity as much as difficulty, so it should stay a tiebreak and never
    become the primary key. And on the thinnest boards it is very coarse —
    EU Mirage has 18 entries, so consecutive positions differ by 5.6 points,
    and the term degenerates to "is this player #1 or not".

    Orbit candidates were on no board at all and return 1.0, behind everyone.
    """
    bs = cand.get("board_size") or 0
    if not bs or cand["hero_pos"] >= NO_BOARD_POS:
        return 1.0
    return cand["hero_pos"] / float(bs)


def ceiling_key(rec, cand):
    """
    THE ORDERING, as a sort key: lower is better.

    CEILING_METRIC=board (default) — the player's position on the region's
    cross-hero leaderboard, ascending. Position 1 is the region's best player,
    so a hero whose best confirmed player sits at 7 outranks one whose best
    sits at 308. This is a RANKING published by Valve, not a quantity we
    accumulate, which is the whole reason for coming back to it: it cannot be
    inflated by volume and it does not depend on any rating endpoint. Ties
    Ties break on hero-board percentile (see hero_pct — this is what separates
    two censored tier-2 candidates who are both their board's #1), then on net
    wins, then on raw hero-board position.

    Orbit candidates were never on a board and carry NO_BOARD_POS, so they sort
    behind every board player and can only become a ceiling for a hero-region
    with no confirmed board candidate at all — which is exactly the case the
    orbit fallback exists for.

    CEILING_METRIC=net_wins — ranked net wins, descending, negated here so the
    same "lower is better" convention holds. See the retained ranked-era
    reasoning in the module docstring; that ordering must not be decayed.
    """
    if CEILING_METRIC == "board":
        return (cand["pos"], hero_pct(cand), -ceiling_net_wins(rec),
                cand["hero_pos"])
    return (-ceiling_net_wins(rec), cand["pos"], hero_pct(cand),
            cand["hero_pos"])


def ceiling_net_wins(rec):
    """Net wins over MATCH_MODE play: wins minus losses.

    The ordering under CEILING_METRIC=net_wins, and the first tiebreak under
    "board". Still written to every row either way, so the two orderings can be
    compared from one run's output.
    """
    return 2 * rec["wins"] - rec["games"]


# Back-compat alias: this was the ordering function and is referenced by name
# in the docstring and in the [eternus] warnings.
ceiling_value = ceiling_net_wins


def shrunk(rec, k=None):
    """Secondary reference column, not the ordering."""
    k = SHRINK_K if k is None else k
    return (rec["wins"] + k * 0.5) / (rec["games"] + k)


# --------------------------------------------------------------------------


def main():
    tier = {r["hero"]: r for r in csv.DictReader(open(os.path.join(OUT_DIR, "tierlist.csv")))}

    print("[metric] ceiling ordered by %s; match_mode=%s"
          % ("general-board position (ascending)" if CEILING_METRIC == "board"
             else "ranked net wins (descending)",
             MATCH_MODE or "standard (unfiltered)"), file=sys.stderr)

    floor_idx = _min_rank_index()
    if floor_idx is None:
        print("[floor] no rank floor in effect", file=sys.stderr)
    else:
        print("[floor] minimum rank %s (index %d); unreadable rank -> %s"
              % (RANK_NAMES[floor_idx], floor_idx, CEILING_RANK_UNKNOWN.upper()),
              file=sys.stderr)

    print("[1/4] leaderboards", file=sys.stderr)
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

    print("[2/4] cross-referencing hero boards against the general board",
          file=sys.stderr)
    confirmed = {}            # (region, hero_id) -> ([candidate, ...], board_size)
    for rg in REGIONS:
        by_name = boards[rg][0]
        n_missing = 0
        n_tier2 = 0
        missing_heroes = []
        for hid, hero in sorted(hero_ids.items(), key=lambda kv: kv[1]):
            hb = fetch_hero_board(rg, hid)
            time.sleep(0.15)          # 100 req/s allowed; stay well clear
            cands = all_on_board(hb, by_name, board_size=len(hb),
                                 region_depth=depth[rg]) if hb else []
            confirmed[(rg, hid)] = (cands, len(hb))
            n_conf = sum(1 for c in cands if c["match"] == "confirmed")
            n_tier2 += len(cands) - n_conf
            # thinness is counted on TIER 1 only: a hero with no dual-confirmed
            # player is still the case the orbit fallback and the tier-2 tier
            # exist for, and counting tier-2 here would hide it
            if not n_conf:
                n_missing += 1
                missing_heroes.append(hero)
        print("  [xref] %-9s %d heroes with a dual-confirmed player, %d without; "
              "%d hero-board-only candidates admitted (%s)"
              % (rg, sum(1 for k, v in confirmed.items()
                         if k[0] == rg
                         and any(c["match"] == "confirmed" for c in v[0])),
                 n_missing, n_tier2,
                 "tier 2 ON" if ALLOW_HERO_BOARD_ONLY else "tier 2 OFF"),
              file=sys.stderr)
        if missing_heroes:
            # These are exactly the hero-regions the orbit fallback exists for.
            # Before 2026-08-18 they were skipped BEFORE the fallback ran and
            # silently vanished from ceiling.csv — that is how NAmerica/Mina
            # and Europe/Warden went missing from run 42.
            print("  [xref] %-9s zero board candidates: %s"
                  % (rg, ", ".join(missing_heroes)), file=sys.stderr)

    print("[3/4] orbit fallback for thin hero-regions", file=sys.stderr)
    # Only hero-regions that are actually short get expanded; everywhere else
    # the boards already supply enough candidates and the orbit would only add
    # weaker players for the maximum to ignore. ZERO counts as short — see the
    # [xref] note above.
    orbit = {}
    if ORBIT_FALLBACK:
        for rg in REGIONS:
            def _n_conf(cands):
                return sum(1 for c in cands if c["match"] == "confirmed")
            thin = [hid for (r, hid), (cands, _n) in confirmed.items()
                    if r == rg and _n_conf(cands) < ORBIT_MIN_CANDIDATES]
            if not thin:
                print("  [orbit] %-9s no thin hero-regions" % rg, file=sys.stderr)
                continue
            empty = [hid for (r, hid), (cands, _n) in confirmed.items()
                     if r == rg and not _n_conf(cands)]
            # seed from the strongest board positions in this region — the
            # accounts most likely to actually be near the ceiling
            # seed only from DUAL-CONFIRMED accounts: a tier-2 id is a slot-0
            # guess, and seeding the orbit query off a misresolved account
            # would propagate that error to every candidate it reaches
            seeds = sorted({c["account_id"]: c["pos"]
                            for (r, _h), (cands, _n) in confirmed.items()
                            if r == rg for c in cands
                            if c["match"] == "confirmed"}.items(),
                           key=lambda kv: kv[1])
            if not seeds:
                print("  [orbit] %-9s no seed accounts anywhere in this region "
                      "— fallback cannot run" % rg, file=sys.stderr)
                continue
            orbit[rg] = fetch_orbit1([a for a, _p in seeds])
            print("  [orbit] %-9s %d thin heroes (%d of them with zero board "
                  "candidates), %d orbit-1 players"
                  % (rg, len(thin), len(empty), len(orbit[rg])), file=sys.stderr)

    print("[4/4] ranked records for the confirmed players", file=sys.stderr)
    # Every id a name might resolve to, not just the provisional pick —
    # resolve_identity cannot choose between ids it has no records for, and
    # hero-stats is batched on a 100 req/s bucket so the extra breadth is
    # nearly free.
    every_id = {a for (cands, _n) in confirmed.values() for c in cands
                for a in (c.get("id_options") or [c["account_id"]])}
    for members in orbit.values():
        every_id |= set(members)
    records, hero_records = fetch_ranked_records(every_id)

    floor_stats = Counter()
    play_stats = Counter()
    identity_audit = []
    fallback_heroes = []
    per_region = defaultdict(list)
    for (rg, hid), (cands, board_size) in confirmed.items():
        hero = hero_ids[hid]
        pool = list(cands)
        from_orbit = 0
        # NOTE: no `if not cands: continue` here. A hero-region with zero board
        # candidates is the single case the orbit fallback was built for, and
        # skipping it early made the fallback unreachable exactly when it
        # mattered. The genuinely-empty case is handled by `if not scored`
        # below, which still drops the hero but only after the fallback has had
        # its turn.
        n_conf = sum(1 for c in cands if c["match"] == "confirmed")
        if n_conf < ORBIT_MIN_CANDIDATES and orbit.get(rg):
            have = {c["account_id"] for c in cands}
            for aid, prox in orbit[rg].items():
                if aid in have:
                    continue
                if prox["seeds_met"] < ORBIT_MIN_SEEDS_MET:
                    continue
                rec = records.get(aid)
                if not rec or rec["games"] < ORBIT_MIN_GAMES:
                    continue
                # and they must actually PLAY THIS HERO — reaching 949 players
                # is no use if none of them play Vyper, and an account-level
                # game count says nothing about that
                hrec = hero_records.get((aid, hid))
                if not hrec or hrec["games"] < ORBIT_MIN_HERO_GAMES:
                    continue
                pool.append({"name": "", "account_id": aid,
                             # no board position: they were not on it
                             "pos": NO_BOARD_POS, "hero_pos": NO_BOARD_POS,
                             "badge": None, "ranked_rank": None,
                             "top_heroes": [], "match": "orbit", "board_size": 0,
                             "seeds_met": prox["seeds_met"],
                             "shared": prox["shared"],
                             "hero_games": hrec["games"]})
                from_orbit += 1
        if not pool:
            continue
        scored = []
        reresolved = 0
        floored_out = 0
        unknown_rank = 0
        no_play = 0
        play_missing = 0
        reserve = []            # failed identity; used only if nothing passes
        for c in pool:
            # Resolve WHICH id this name is before judging whether it is
            # credible: passes_identity would otherwise validate slot 0 and
            # reject the entry when a sibling id was the real player.
            if c["match"] != "orbit":
                resolved, _slot, _share = resolve_identity(
                    c, hid, records, hero_records)
                if c.get("id_reresolved"):
                    reresolved += 1
            rec = records.get(c["account_id"])
            if not rec or not rec["games"]:
                continue
            play_ok, play_verdict, hgames = passes_identity(c, hid, rec, hero_records)
            play_stats[play_verdict] += 1
            if play_verdict == "missing":
                play_missing += 1
            if not play_ok:
                no_play += 1
                c["hero_games_checked"] = hgames
                c["identity_verdict"] = play_verdict
                reserve.append((ceiling_key(rec, c), rec, c))
                # record WHO was rejected and for what, so a name collision is
                # visible as a row rather than inferred from a count
                identity_audit.append({
                    "region": rg, "hero": hero, "hero_id": hid,
                    "account_name": c["name"], "account_id": c["account_id"],
                    "match": c["match"], "global_pos": c["pos"],
                    "hero_ladder_pos": c["hero_pos"], "board_size": c["board_size"],
                    "hero_games": hgames if hgames is not None else "",
                    "verdict": play_verdict})
                continue
            c["hero_games_checked"] = hgames
            c["identity_verdict"] = "pass"
            ok, verdict = passes_floor(c, floor_idx)
            floor_stats[verdict] += 1
            if verdict == "unknown":
                unknown_rank += 1
            if not ok:
                floored_out += 1
                continue
            scored.append((ceiling_key(rec, c), rec, c))
        # NEVER LOSE A HERO — see IDENTITY_FALLBACK. Validation thins the
        # POOL; it must not thin the OUTPUT. A hero-region whose every
        # candidate failed falls back to the best rejected one, flagged, so a
        # reader can discount the row instead of not seeing it.
        identity_verified = "YES"
        if not scored and reserve and IDENTITY_FALLBACK:
            scored = reserve
            identity_verified = "NO"
            fallback_heroes.append("%s/%s" % (rg, hero))
        if not scored:
            continue
        # the ceiling: the ELIGIBLE confirmed player who sorts first under
        # ceiling_key — best general-board position by default, most net wins
        # under CEILING_METRIC=net_wins. The key already carries its own
        # tiebreaks, so this sort is total and never depends on pool order.
        scored.sort(key=lambda t: t[0])
        _key, rec, c = scored[0]
        net = ceiling_net_wins(rec)
        t = tier.get(hero, {})
        per_region[rg].append({
            "hero": hero,
            "hero_id": hid,
            "region": rg,
            "ceiling_player": c["name"] or ("orbit:%d" % c["account_id"]),
            "account_id": c["account_id"],
            # THE ORDERING
            "net_wins": net,
            "ranked_games": rec["games"],
            "ranked_wins": rec["wins"],
            "shrunk_winrate": round(shrunk(rec), 4),
            # Under CEILING_METRIC=board this IS the ordering; under net_wins
            # it is the reference column. Either way both are always written.
            # An orbit player was never on a board, so a position would be a
            # lie. The 1e9 sentinel exists only to sort them last on ties.
            # blank for anyone not dual-confirmed: a tier-2 player's position
            # is CENSORED (known only to be past the general board's 1,000) and
            # an orbit player was never on a board, so printing either as a
            # number would state a standing neither one has
            "global_pos": c["pos"] if c["match"] == "confirmed" else "",
            "general_pos_censored": "YES" if c["match"] == "hero_board_only" else "",
            "hero_pct": round(hero_pct(c), 4) if c["match"] != "orbit" else "",
            # which ordering produced ceiling_rank in THIS file. build_site_data
            # sorts on ceiling_rank and cannot otherwise tell the two apart, so
            # a file is never ambiguous about what it was ranked by.
            "ceiling_metric": CEILING_METRIC,
            "region_depth": depth[rg],
            "pct": (round(100.0 * c["pos"] / max(depth[rg], 1), 3)
                    if c["match"] == "confirmed" else ""),
            "badge_level": c["badge"],
            # badge_level reads blank on every ranked row; ranked_rank is the
            # surviving rank signal and is what the Eternus watch and the rank
            # floor both key off.
            "ranked_rank": c.get("ranked_rank") if c.get("ranked_rank") is not None else "",
            "rank_name": _rank_name(c.get("ranked_rank")),
            "hero_ladder_pos": "" if c["hero_pos"] >= NO_BOARD_POS else c["hero_pos"],
            # confirmed | hero_board_only | orbit — how this ceiling was
            # sourced. A hero_board_only row is a FLOOR on the hero's ceiling,
            # not an estimate; the site should mark it as such.
            "match": c["match"],
            "valve_top_hero": "YES" if hid in (c["top_heroes"] or []) else "",
            "located_on_general": sum(1 for x in cands if x["match"] == "confirmed"),
            "hero_board_only_candidates": sum(1 for x in cands
                                              if x["match"] == "hero_board_only"),
            # the ceiling player's own games on THIS hero, and how many
            # candidates this hero-region rejected for having none
            "ceiling_hero_games": (c.get("hero_games_checked")
                                   if c.get("hero_games_checked") is not None else ""),
            # NO means every candidate failed identity validation and this row
            # is the best of a bad set — treat the ceiling player as unverified
            "identity_verified": identity_verified,
            "id_slot": c.get("id_slot", 0),
            "id_options_n": len(c.get("id_options") or [c["account_id"]]),
            "id_reresolved": c.get("id_reresolved", ""),
            "hero_share": c.get("hero_share", ""),
            "candidates_reresolved": reresolved,
            "identity_verdict": c.get("identity_verdict", ""),
            "ceiling_account_games": rec["games"],
            "rejected_no_hero_play": no_play,
            "hero_play_missing": play_missing,
            "scored_candidates": len(scored),
            # how much of the floor actually bit for this hero-region, and how
            # much of it was unreadable — a run where floored_out is 0 and
            # rank_unknown equals the pool size has NO EFFECTIVE FLOOR
            "floored_out": floored_out,
            "rank_unknown": unknown_rank,
            "min_rank": RANK_NAMES[floor_idx] if floor_idx is not None else "",
            "from_orbit": from_orbit,
            "ceiling_from_orbit": "YES" if c.get("seeds_met") else "",
            "orbit_seeds_met": c.get("seeds_met", ""),
            "orbit_hero_games": c.get("hero_games", ""),
            "board_size": board_size,
            "winrate_rank": t.get("rank", ""),
            "elite_winrate": t.get("elite_winrate", ""),
        })

    out = []
    for rg in REGIONS:
        # Order heroes by their ceiling player's standing — general-board
        # position ascending by default, net wins descending under
        # CEILING_METRIC=net_wins. The pool win rate is the last tiebreak so
        # ordering never depends on dict iteration order: one account can be
        # the ceiling for two heroes (a McGinnis/Ivy dual-main) and would
        # otherwise be sorted arbitrarily.
        def _wr(d):
            try:
                return float(d["elite_winrate"])
            except (TypeError, ValueError):
                return -1.0

        def _pos(d):
            # blank means censored (tier 2) or never on a board (orbit); both
            # sort behind every dual-confirmed ceiling
            v = d.get("global_pos")
            return v if isinstance(v, int) else NO_BOARD_POS

        def _hpct(d):
            v = d.get("hero_pct")
            return v if isinstance(v, float) else 1.0

        # hero name last so the order is TOTAL — two heroes can otherwise tie
        # on every term (same censored position, same board size, same record)
        # and fall back to dict insertion order, which is not reproducible
        if CEILING_METRIC == "board":
            key = lambda d: (_pos(d), _hpct(d), -d["net_wins"], -_wr(d), d["hero"])
        else:
            key = lambda d: (-d["net_wins"], _pos(d), _hpct(d), -_wr(d), d["hero"])
        rows = sorted(per_region.get(rg, []), key=key)
        for i, d in enumerate(rows, 1):
            d["ceiling_rank"] = i
        out.extend(rows)

    for region in REGIONS:
        have = {d["hero"] for d in out if d["region"] == region}
        gap = sorted(set(tier) - have)
        if gap:
            print("  [warn] %s: no eligible player with ranked games for %d heroes: %s"
                  % (region, len(gap), ", ".join(gap)), file=sys.stderr)

    cols = ["region", "ceiling_rank", "ceiling_metric", "hero", "hero_id",
            "ceiling_player",
            "account_id", "net_wins", "ranked_games", "ranked_wins",
            "shrunk_winrate", "global_pos", "general_pos_censored", "hero_pct",
            "region_depth", "pct", "badge_level",
            "ranked_rank", "rank_name",
            "hero_ladder_pos", "match", "valve_top_hero", "located_on_general",
            "hero_board_only_candidates", "identity_verified", "identity_verdict",
            "id_slot", "id_options_n", "id_reresolved", "hero_share",
            "candidates_reresolved",
            "ceiling_account_games", "ceiling_hero_games",
            "rejected_no_hero_play", "hero_play_missing",
            "scored_candidates", "floored_out", "rank_unknown", "min_rank",
            "from_orbit", "ceiling_from_orbit",
            "orbit_seeds_met", "orbit_hero_games", "board_size", "winrate_rank",
            "elite_winrate"]
    path = os.path.join(OUT_DIR, "ceiling.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)
    print("  -> %s (%d hero-region rows)" % (path, len(out)), file=sys.stderr)

    # ---- hero-play validation: did it bite, and on whom? ------------------
    if VALIDATE_HERO_PLAY:
        tot = sum(play_stats.values())
        print("\n  [play] %d candidates validated: %d passed, %d implausible "
              "identity (<%d account games), %d no hero play (<%d), %d no "
              "hero-stats row (policy=%s)"
              % (tot, play_stats["pass"], play_stats["implausible_identity"],
                 CEILING_MIN_ACCOUNT_GAMES, play_stats["no_play"],
                 CEILING_MIN_HERO_GAMES, play_stats["missing"],
                 HERO_PLAY_MISSING), file=sys.stderr)
        if tot and not play_stats["pass"]:
            print("  [play] *** NOTHING PASSED. Either hero-stats returned no "
                  "usable rows this run or the floors are set too high — check "
                  "before reading any ceiling. ***", file=sys.stderr)
        unver = [d for d in out if d.get("identity_verified") == "NO"]
        if unver:
            print("  [play] %d hero-regions had NO candidate pass validation and "
                  "fell back to their best rejected one (identity_verified=NO): "
                  "%s" % (len(unver), ", ".join(sorted(fallback_heroes)[:14])),
                  file=sys.stderr)
            print("  [play] those rows are kept so no hero vanishes, but their "
                  "ceiling player should not be trusted.", file=sys.stderr)

    # ---- multi-hero ceilings: real flex player, or a name collision? -------
    # This is the report the check exists for. One account holding several
    # hero boards is either a genuine flex player or several different players
    # sharing a display name that deadlock-api collapsed onto one id. Games on
    # each claimed hero is what separates the two.
    multi = defaultdict(list)
    for d in out:
        multi[(d["account_id"], d["region"])].append(d)
    multi = {k: v for k, v in multi.items() if len(v) > 1}
    if multi:
        print("\n  [identity] %d accounts are the ceiling for more than one hero:"
              % len(multi), file=sys.stderr)
        for (aid, rg), ds in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            games = ", ".join("%s=%s" % (d["hero"], d["ceiling_hero_games"]
                                         if d["ceiling_hero_games"] != "" else "?")
                              for d in ds)
            unver = sum(1 for d in ds if d["ceiling_hero_games"] == "")
            print("    %-9s %-22s id=%-11s %d heroes: %s%s"
                  % (rg, (ds[0]["ceiling_player"] or "?")[:22], aid, len(ds), games,
                     "   <-- %d UNVERIFIED" % unver if unver else ""),
                  file=sys.stderr)
        print("    A genuine flex player shows real games on every hero listed. "
              "A display-name collision shows them on one and little on the "
              "rest — that account is several people.", file=sys.stderr)

    if identity_audit:
        apath = os.path.join(OUT_DIR, "identity_audit.csv")
        acols = ["region", "hero", "hero_id", "account_name", "account_id",
                 "match", "global_pos", "hero_ladder_pos", "board_size",
                 "hero_games", "verdict"]
        with open(apath, "w", newline="", encoding="utf-8") as f:
            aw = csv.DictWriter(f, fieldnames=acols, extrasaction="ignore")
            aw.writeheader()
            aw.writerows(identity_audit)
        print("  -> %s (%d rejected board claims)" % (apath, len(identity_audit)),
              file=sys.stderr)

    # ---- did the floor do anything? ---------------------------------------
    if floor_idx is not None:
        tot = sum(floor_stats.values())
        print("\n  [floor] %d candidates checked against %s: %d passed, "
              "%d below, %d unreadable rank (%s)"
              % (tot, RANK_NAMES[floor_idx], floor_stats["pass"],
                 floor_stats["below"], floor_stats["unknown"],
                 CEILING_RANK_UNKNOWN), file=sys.stderr)
        if tot and not floor_stats["below"] and floor_stats["unknown"] == tot:
            print("  [floor] *** THE FLOOR IS NOT BITING. Every candidate had "
                  "an unreadable ranked_rank, so this ordering is unfloored "
                  "net wins exactly as before. Do not read the ceiling as "
                  "%s+ until this line changes. ***"
                  % RANK_NAMES[floor_idx], file=sys.stderr)
        elif floor_stats["unknown"]:
            print("  [floor] %d candidates passed on an unreadable rank rather "
                  "than a checked one" % floor_stats["unknown"], file=sys.stderr)

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
                   d["global_pos"] or "orbit"), file=sys.stderr)

    # ---- Eternus watch, at the level that actually matters -----------------
    # The board-wide share is the early signal; THIS is the one that expires
    # the metric, because the ordering only breaks once the CEILING PLAYERS
    # themselves are percentile-ranked.
    named = [d for d in out if d.get("rank_name")]
    et = [d for d in named if d["rank_name"] == "Eternus"]
    if named:
        print("\n  [eternus] %d of %d ceiling players carry a readable rank; "
              "%d at Eternus" % (len(named), len(out), len(et)), file=sys.stderr)
        for d in et:
            print("    %-9s %-12s %-16s net %d"
                  % (d["region"], d["hero"], d["ceiling_player"], d["net_wins"]),
                  file=sys.stderr)
        if et and CEILING_METRIC != "board":
            print("  [eternus] net wins tracks rank only while the ladder is "
                  "progression-based. Once a meaningful share of CEILING "
                  "players sit at Eternus, the top of the ordering is "
                  "measuring the wrong thing — replace ceiling_value().",
                  file=sys.stderr)
    else:
        print("\n  [eternus] no ceiling player carries a readable rank — watch "
              "disabled this run (badge_level is blank, and ranked_rank was "
              "empty or unrecognised)", file=sys.stderr)

    # (the old name-keyed duplicate warning is gone: it grouped by DISPLAY
    # NAME, which is the very thing that cannot be trusted here. The
    # [identity] report above groups by resolved account_id and shows the
    # per-hero games that tell the two cases apart.)


if __name__ == "__main__":
    main()