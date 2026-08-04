#!/usr/bin/env python3
"""
Find duo-queue partners by match co-occurrence, then look at what they play.

    python3 probe_duos.py

ZERO SQL for discovery. /v1/players/hero-stats returns `matches` — an array of
match ids per (account, hero) — batched at 100 req/s. Two accounts that keep
turning up in the same matches are queueing together; no party field is needed,
and none exists (match_player has no party column, and the only party_* fields
in the API are for custom lobbies).

METHOD
  1. leaderboard -> candidate accounts (free)
  2. hero-stats?match_mode=ranked -> match id arrays per (account, hero) (free)
  3. invert to match_id -> [accounts], count co-occurrences pairwise
     (linear in ids, not quadratic in accounts: a match holds at most 12 of
     our accounts, so at most 66 pairs)
  4. score pairs by overlap rate, not raw count
  5. for confirmed pairs, tabulate the hero combinations they run

WHY OVERLAP RATE, NOT COUNT. A player with 300 games shares more matches with
everyone by chance than one with 20. The duo signal is what FRACTION of the
smaller player's games are shared: a real duo sits near 1.0, coincidence sits
near 0.

THE LIMITATION, STATED UP FRONT. hero-stats `matches` carries no team field, so
a shared match could be as OPPONENTS. Matchmaking will not repeatedly pair the
same two people against each other, so a high overlap rate still implies a duo
— but this cannot distinguish "queued together" from "matched against each
other 30 times" on the data alone. Confirming requires one SQL query per
shortlisted pair against match_player.team, which is why the shortlist is kept
small. Until that is done, treat pairs as CANDIDATE duos.
"""

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
# the board gives ~1,000 per region and hero-stats is batched at 100 req/s, so
# reading the whole board costs little more than a fraction of it
N_ACCOUNTS = int(os.environ.get("DUO_ACCOUNTS") or 1000)
MIN_SHARED = int(os.environ.get("DUO_MIN_SHARED") or 5)
MIN_RATE = float(os.environ.get("DUO_MIN_RATE") or 0.30)
MAX_URL = int(os.environ.get("MAX_URL") or 9000)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-duos/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
    except Exception as e:
        return None, str(e)


def hero_names():
    data, err = get(BASE + "/v1/assets/heroes")
    out = {}
    for h in (data or []):
        if h.get("id") is not None:
            out[int(h["id"])] = h.get("name") or str(h["id"])
    return out


def candidates(region, n):
    """Unambiguous account ids off the region's board.

    Only entries with exactly ONE candidate id are used: possible_account_ids
    is a candidate list, not an identity, and Valve's ids do not resolve
    reliably. A wrong id here would invent a duo that does not exist.
    """
    data, err = get(BASE + "/v1/leaderboard/%s" % region)
    if err:
        print("  [lb] %s -> %s" % (region, err), file=sys.stderr)
        return {}
    entries = (data.get("entries") if isinstance(data, dict) else data) or []
    out = {}
    for e in entries:
        ids = [int(a) for a in (e.get("possible_account_ids") or []) if a]
        if len(ids) == 1 and e.get("account_name"):
            out[ids[0]] = e["account_name"]
        if len(out) >= n:
            break
    return out


def fetch_matches(ids):
    """(account, hero) -> match id list, ranked only. No SQL."""
    ids = sorted(ids)
    per_acct = defaultdict(set)          # account -> all match ids
    per_pair = {}                        # (account, hero) -> match ids
    chunk = max(50, MAX_URL // 14)
    i = 0
    while i < len(ids):
        part = ids[i:i + chunk]
        q = "&".join("account_ids=%d" % a for a in part)
        data, err = get("%s/v1/players/hero-stats?match_mode=ranked&%s" % (BASE, q))
        if err:
            print("  [hs] chunk %d-%d -> %s" % (i + 1, i + len(part), err),
                  file=sys.stderr)
        for r in (data or []):
            a, h = r.get("account_id"), r.get("hero_id")
            m = r.get("matches") or []
            if a is None or h is None or not m:
                continue
            per_pair[(int(a), int(h))] = [int(x) for x in m]
            per_acct[int(a)].update(int(x) for x in m)
        i += len(part)
        time.sleep(0.2)
    return per_acct, per_pair


def main():
    heroes = hero_names()
    for region in REGIONS:
        print("\n" + "=" * 68, file=sys.stderr)
        print("%s" % region, file=sys.stderr)
        print("=" * 68, file=sys.stderr)
        names = candidates(region, N_ACCOUNTS)
        if not names:
            continue
        print("  %d unambiguous accounts from the board" % len(names), file=sys.stderr)
        per_acct, per_pair = fetch_matches(list(names))
        total_ids = sum(len(v) for v in per_acct.values())
        print("  %d accounts with ranked matches, %d match ids total"
              % (len(per_acct), total_ids), file=sys.stderr)
        if not per_acct:
            continue

        # invert: match -> accounts. linear in ids.
        by_match = defaultdict(list)
        for a, ms in per_acct.items():
            for m in ms:
                by_match[m].append(a)
        shared_in = Counter()
        for m, accts in by_match.items():
            if len(accts) < 2:
                continue
            accts.sort()
            for x in range(len(accts)):
                for y in range(x + 1, len(accts)):
                    shared_in[(accts[x], accts[y])] += 1
        print("  %d matches contain 2+ of these accounts; %d distinct pairs"
              % (sum(1 for v in by_match.values() if len(v) > 1), len(shared_in)),
              file=sys.stderr)

        # CALIBRATE FIRST. These are the top N players in one region, so they
        # meet each other constantly as OPPONENTS — 6,040 co-occurring pairs in
        # NA at 400 accounts. The raw pair count is therefore meaningless and
        # the rate threshold is doing all the work, so print the null
        # distribution before applying any cutoff.
        rates = []
        for (a, b), n in shared_in.items():
            small = min(len(per_acct[a]), len(per_acct[b]))
            if small:
                rates.append(n / small)
        rates.sort()
        def pct(p):
            return rates[min(int(len(rates) * p), len(rates) - 1)] if rates else 0.0
        print("  overlap-rate distribution over all %d pairs:" % len(rates),
              file=sys.stderr)
        print("     median %.3f | 90th %.3f | 99th %.3f | 99.9th %.3f | max %.3f"
              % (pct(0.50), pct(0.90), pct(0.99), pct(0.999),
                 rates[-1] if rates else 0), file=sys.stderr)
        print("     a threshold is only meaningful well above the 99th percentile",
              file=sys.stderr)
        over = sum(1 for r in rates if r >= MIN_RATE)
        print("     %d pairs (%.2f%%) clear the %.0f%% cutoff\n"
              % (over, 100.0 * over / max(len(rates), 1), 100 * MIN_RATE),
              file=sys.stderr)

        # score by overlap RATE against the smaller schedule
        scored = []
        for (a, b), n in shared_in.items():
            small = min(len(per_acct[a]), len(per_acct[b]))
            if small == 0:
                continue
            rate = n / small
            if n >= MIN_SHARED and rate >= MIN_RATE:
                scored.append((rate, n, small, a, b))
        scored.sort(reverse=True)
        print("  %d CANDIDATE duos (>=%d shared, >=%.0f%% overlap)\n"
              % (len(scored), MIN_SHARED, 100 * MIN_RATE), file=sys.stderr)
        if not scored:
            continue

        print("  %-18s %-18s %6s %7s %7s" % ("player A", "player B", "shared",
                                             "of", "rate"), file=sys.stderr)
        for rate, n, small, a, b in scored[:15]:
            print("  %-18s %-18s %6d %7d %6.0f%%"
                  % (str(names.get(a, a))[:18], str(names.get(b, b))[:18], n, small,
                     100 * rate),
                  file=sys.stderr)

        # players appearing in more than one candidate duo. A noise process
        # would not produce this; people having two regular partners would.
        seen = Counter()
        for _r, _n, _s, a, b in scored:
            seen[a] += 1
            seen[b] += 1
        rep = [(k, v) for k, v in seen.items() if v > 1]
        if rep:
            print("\n  players in more than one candidate duo (%d):" % len(rep),
                  file=sys.stderr)
            for k, v in sorted(rep, key=lambda x: -x[1]):
                print("     %-20s %d partners" % (str(names.get(k, k))[:20], v),
                      file=sys.stderr)

        # what do the duos play together?
        combo = Counter()
        for rate, n, small, a, b in scored:
            ha = {h for (acct, h) in per_pair if acct == a}
            hb = {h for (acct, h) in per_pair if acct == b}
            # main = most match ids on that hero
            def main_hero(acct, hs):
                best, bn = None, -1
                for h in hs:
                    k = len(per_pair.get((acct, h), []))
                    if k > bn:
                        best, bn = h, k
                return best
            ma, mb = main_hero(a, ha), main_hero(b, hb)
            if ma is None or mb is None:
                continue
            combo[tuple(sorted((heroes.get(ma, ma), heroes.get(mb, mb))))] += 1
        if combo:
            print("\n  most common hero pairings among candidate duos:", file=sys.stderr)
            for (x, y), k in combo.most_common(10):
                print("     %-14s + %-14s %d" % (x, y, k), file=sys.stderr)
            print("\n  NOTE: this pairs each player's MOST-PLAYED hero, not the heroes"
                  , file=sys.stderr)
            print("  they actually ran in the shared matches — hero-stats gives match"
                  , file=sys.stderr)
            print("  ids per hero, so the true per-match pairing is recoverable but"
                  , file=sys.stderr)
            print("  needs intersecting per-hero id lists, not per-account ones.",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
