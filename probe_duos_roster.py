#!/usr/bin/env python3
"""
Find duo partners from match ROSTERS — team-confirmed, no identity guessing.

    python3 probe_duos_roster.py

WHY THIS REPLACES THE CO-OCCURRENCE APPROACH

probe_duos.py inferred duos from /v1/players/hero-stats `matches` arrays. Two
problems, both fatal:

  * `matches` has no team field, so a "shared match" might be as OPPONENTS.
  * It matched players via the leaderboard's possible_account_ids, which does
    not resolve identity reliably. Only ~60% of board entries even carry a
    single candidate id.

Calibration killed it: the 99th percentile of overlap rate came back at 0.250
on live data, so the 30% threshold sat barely above chance and only ~6 duos per
region survived a defensible cutoff.

This works the way a human would read a match history instead: take a KNOWN
account, pull its recent matches, and look at who keeps appearing ON ITS TEAM.
match_player carries account_id and team per row, so a teammate is a fact, not
an inference. No possible_account_ids anywhere in the pipeline.

TWO SIGNALS, and the second is the strong one:

  rate    — share of the seed's matches a teammate appears in. In a top-N pool
            players meet constantly, so this is noisy on its own.
  streak  — longest run of CONSECUTIVE matches together. Random matchmaking
            essentially never produces a long same-team streak, so this
            separates duos from familiar faces far better than rate does.

COST: match ids come free from hero-stats. Rosters need SQL — about 4 calls for
60 seeds at 20 matches each. Do not run in the same hour as the pipeline.
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict, Counter

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
# ceiling players are always seeded; this is how many CONTROLS to add
CONTROLS = int(os.environ.get("DUO_CONTROLS") or 60)
CONTROL_FROM = int(os.environ.get("DUO_CONTROL_FROM") or 200)
RECENT = int(os.environ.get("DUO_RECENT") or 20)
MIN_STREAK = int(os.environ.get("DUO_MIN_STREAK") or 4)
MIN_TOGETHER = int(os.environ.get("DUO_MIN_TOGETHER") or 4)
MAX_URL = int(os.environ.get("MAX_URL") or 9000)
SQL_PAUSE = float(os.environ.get("SQL_PAUSE_S") or 35)
_sql_calls = 0


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-duos/2.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:
        return None, str(e)


def sql(query, label):
    global _sql_calls
    if _sql_calls:
        print("    ... waiting %.0fs (SQL 2/min)" % SQL_PAUSE, file=sys.stderr)
        time.sleep(SQL_PAUSE)
    _sql_calls += 1
    rows, err = get(BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(query))
    if err:
        print("    [%s] %s" % (label, err), file=sys.stderr)
        return []
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    return rows


def seed_accounts(n_controls):
    """Ceiling players, plus optional controls from further down the board.

    The ceiling players are the point of interest: one per hero per region, the
    strongest player each hero reaches. They come from output/ceiling.csv with a
    confirmed account_id (the id present on BOTH the hero board and the general
    board), so no name resolution happens here.

    CONTROLS matter for interpretation. "N ceiling players duo" means nothing
    without a base rate — if everyone at this level duos, it is not a property
    of ceiling players. Controls are drawn from further down the same general
    board, so any difference in duo rate is about ladder position rather than
    about how the two groups were selected.
    """
    seeds, seen = [], set()
    p = os.path.join("output", "ceiling.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            a = r.get("account_id")
            if not a or a in seen:
                continue                      # one account can hold several ceilings
            seen.add(a)
            seeds.append((int(a), r.get("ceiling_player", ""), "ceiling", r["region"]))
        print("  [seeds] %d distinct ceiling players from ceiling.csv" % len(seeds),
              file=sys.stderr)
    else:
        print("  [seeds] no output/ceiling.csv - run ceiling_rank.py first",
              file=sys.stderr)

    if n_controls > 0:
        for region in REGIONS:
            data, err = get(BASE + "/v1/leaderboard/%s" % region)
            if err:
                continue
            entries = (data.get("entries") if isinstance(data, dict) else data) or []
            # skip the top of the board so controls are genuinely a different
            # stratum, and take only unambiguous single-id entries
            got = 0
            for i, e in enumerate(entries):
                if i < CONTROL_FROM:
                    continue
                ids = [int(x) for x in (e.get("possible_account_ids") or []) if x]
                if len(ids) != 1 or str(ids[0]) in seen:
                    continue
                seen.add(str(ids[0]))
                seeds.append((ids[0], e.get("account_name", ""), "control", region))
                got += 1
                if got >= n_controls // max(len(REGIONS), 1):
                    break
            print("  [seeds] %d controls from %s below board position %d"
                  % (got, region, CONTROL_FROM), file=sys.stderr)
    return seeds


def recent_matches(ids):
    """Most recent ranked match ids per account, from hero-stats. No SQL."""
    out = defaultdict(set)
    chunk = max(50, MAX_URL // 14)
    i = 0
    while i < len(ids):
        part = ids[i:i + chunk]
        q = "&".join("account_ids=%d" % a for a in part)
        data, err = get("%s/v1/players/hero-stats?match_mode=ranked&%s" % (BASE, q))
        if err:
            print("  [hs] %s" % err, file=sys.stderr)
        for r in (data or []):
            a = r.get("account_id")
            if a is None:
                continue
            out[int(a)].update(int(m) for m in (r.get("matches") or []))
        i += len(part)
        time.sleep(0.2)
    # match ids increase monotonically, so the largest are the most recent
    return {a: sorted(ms)[-RECENT:] for a, ms in out.items() if ms}


def main():
    seeds = seed_accounts(CONTROLS)
    if not seeds:
        raise SystemExit("no seed accounts")
    name = {a: nm for a, nm, _grp, _rg in seeds}
    group = {a: grp for a, _nm, grp, _rg in seeds}
    sched = recent_matches([a for a, _nm, _g, _rg in seeds])
    print("  [hs] %d seeds with ranked matches" % len(sched), file=sys.stderr)

    wanted = sorted({m for ms in sched.values() for m in ms})
    print("  [sql] %d distinct matches to fetch rosters for" % len(wanted),
          file=sys.stderr)

    # roster: match -> {account: team}
    roster = defaultdict(dict)
    per = max(50, MAX_URL // 25)
    for i in range(0, len(wanted), per):
        part = wanted[i:i + per]
        q = ("SELECT match_id, account_id, team, hero_id FROM match_player "
             "WHERE match_id IN (%s)" % ",".join(str(m) for m in part))
        for r in sql(q, "roster %d-%d" % (i + 1, i + len(part))):
            roster[int(r["match_id"])][int(r["account_id"])] = (r["team"],
                                                                int(r["hero_id"]))
    print("  [sql] rosters for %d matches, %d SQL calls\n" % (len(roster), _sql_calls),
          file=sys.stderr)
    if not roster:
        return

    # CALIBRATE FIRST. In a top-N pool players meet constantly, so collect
    # every seed/teammate relationship before applying any cutoff and print the
    # null distribution. The rate-based predecessor was killed by exactly this
    # check: its 99th percentile came back at 0.250, so a 30% threshold sat
    # barely above chance.
    allrel = []
    for a, ms in sorted(sched.items()):
        ms = [m for m in ms if m in roster]
        if not ms:
            continue
        tog, stk, run = Counter(), Counter(), Counter()
        for m in ms:
            mine = roster[m].get(a)
            if not mine:
                run.clear()
                continue
            here = {b for b, (t, _h) in roster[m].items() if b != a and t == mine[0]}
            for b in here:
                tog[b] += 1
                run[b] += 1
                stk[b] = max(stk[b], run[b])
            for b in list(run):
                if b not in here:
                    run[b] = 0
        for b, n in tog.items():
            allrel.append((stk[b], n))
    if allrel:
        st = sorted(x[0] for x in allrel)
        def pc(p):
            return st[min(int(len(st) * p), len(st) - 1)]
        print("  streak distribution over %d seed/teammate relationships:" % len(st),
              file=sys.stderr)
        print("     median %d | 90th %d | 99th %d | 99.9th %d | max %d"
              % (pc(0.50), pc(0.90), pc(0.99), pc(0.999), st[-1]), file=sys.stderr)
        over = sum(1 for x in st if x >= MIN_STREAK)
        print("     %d (%.2f%%) reach a streak of %d\n"
              % (over, 100.0 * over / len(st), MIN_STREAK), file=sys.stderr)

    print("=" * 74, file=sys.stderr)
    print("TEAM-CONFIRMED partners (>=%d together, >=%d consecutive)"
          % (MIN_TOGETHER, MIN_STREAK), file=sys.stderr)
    print("=" * 74, file=sys.stderr)
    print("  %-20s %-20s %7s %7s %7s %s"
          % ("seed", "partner", "together", "of", "streak", "heroes"), file=sys.stderr)

    found = 0
    for a, ms in sorted(sched.items()):
        ms = [m for m in ms if m in roster]
        if len(ms) < MIN_TOGETHER:
            continue
        together = Counter()
        streak = Counter()
        run = Counter()
        pair_heroes = defaultdict(Counter)
        for m in ms:                      # ascending = chronological
            mine = roster[m].get(a)
            if not mine:
                run.clear()
                continue
            here = set()
            for b, (team, hid) in roster[m].items():
                if b != a and team == mine[0]:
                    here.add(b)
                    together[b] += 1
                    pair_heroes[b][(mine[1], hid)] += 1
            for b in here:
                run[b] += 1
                streak[b] = max(streak[b], run[b])
            for b in list(run):
                if b not in here:
                    run[b] = 0
        for b, n in together.most_common():
            if n >= MIN_TOGETHER and streak[b] >= MIN_STREAK:
                found += 1
                hs = pair_heroes[b].most_common(1)[0]
                print("  %-20s %-20s %8d %7d %7d %s"
                      % (str(name.get(a, a))[:20], str(name.get(b, b))[:20],
                         n, len(ms), streak[b], "%d+%d x%d" % (hs[0][0], hs[0][1], hs[1])),
                      file=sys.stderr)
    # ceiling vs control duo rate - the comparison the controls exist for
    have = defaultdict(set)
    for a, ms in sched.items():
        ms = [m for m in ms if m in roster]
        if not ms:
            continue
        tog, stk, run = Counter(), Counter(), Counter()
        for m in ms:
            mine = roster[m].get(a)
            if not mine:
                run.clear()
                continue
            here = {b for b, (t, _h) in roster[m].items() if b != a and t == mine[0]}
            for b in here:
                tog[b] += 1
                run[b] += 1
                stk[b] = max(stk[b], run[b])
            for b in list(run):
                if b not in here:
                    run[b] = 0
        for b in tog:
            if tog[b] >= MIN_TOGETHER and stk[b] >= MIN_STREAK:
                have[a].add(b)
    print("\n  duo rate by group:", file=sys.stderr)
    for grp in ("ceiling", "control"):
        pool = [a for a in sched if group.get(a) == grp]
        if not pool:
            continue
        withduo = sum(1 for a in pool if have.get(a))
        print("     %-8s %3d seeds | %3d with a partner (%.0f%%)"
              % (grp, len(pool), withduo, 100.0 * withduo / len(pool)), file=sys.stderr)
    print("\n  A large gap means duoing is characteristic of the top of the ladder.",
          file=sys.stderr)
    print("  A small one means it is just how everyone plays, and the ceiling",
          file=sys.stderr)
    print("  players are not special in this respect.", file=sys.stderr)

    print("\n  %d partner relationships found across %d seeds" % (found, len(sched)),
          file=sys.stderr)
    print("  A partner is on the seed's TEAM - this is a fact from match_player,",
          file=sys.stderr)
    print("  not an inference. Streak is the discriminator: matchmaking does not",
          file=sys.stderr)
    print("  put the same two people on the same team many times consecutively.",
          file=sys.stderr)
    print("\n  %d SQL calls used." % _sql_calls, file=sys.stderr)


if __name__ == "__main__":
    main()
