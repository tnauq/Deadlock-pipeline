"""
valve_rank.py: Valve's own rank for an account, read through deadlock-api.

WHY (2026-11-10)
----------------
PROBES.md recorded every upstream rating as dead: badge read 0 on ranked rows
from 2026-07-31, /v1/players/mmr went empty, and ceilings were ordered by net
wins as a stand-in for rank points. The rating had moved, not died. Since
ranked relaunched on 2026-07-30 Valve's match metadata carries each player's
rank (CMsgMatchPlayerRankData): the badge they entered the match with and
their ladder-wide "flat progress" before and after it. That progress is what
the leaderboard sorts by. deadlock-api keeps it on match_player
(player_rank_initial_display_rank, player_rank_final_flat_progress,
rank_interval, ...; also on the lake) and serves it per account:

    /v1/players/rank?account_ids=1,2,3     newest ranked match per account,
                                           up to 1,000 ids, 10,000 ids/min
    /v1/players/region?account_ids=1,2,3   server region of their matches
                                           (added upstream 2026-11-10)
    /v1/analytics/scoreboards/players?sort_by=rank&hero_id=N&match_mode=ranked
                                           players on a hero, best rank first
    /v1/assets/ranked-seasons              each season's interval, dates and
                                           leaderboard_id (1001, 1002, ...)

Everything above was read from deadlock-api's source at 110e282 (2026-11-10).
None of it had been called from this repo before; probe_season2.py measures
it on a live run.

A rank only compares within one ranked interval (season): the ladder was
rescaled for Season 2 and every player re-placed. `current` below means "from
a match that counted for the running interval, after placement".

The caller passes its own `get(url)`, so this module makes no HTTP choices.
"""
import os
import re
import sys
import time

# ~11 characters per id in a comma-separated list; 600 keeps a URL near 7 KB,
# under the ~9 KB this repo treats as safe (SCHEMA.md quirk 4).
CHUNK = int(os.environ.get("VALVE_RANK_CHUNK") or 600)
MIN_CHUNK = 40
FLOOR_TRIES = 3
RETRY_WAIT_S = 2
RATE_WAIT_S = 10
SLOW_S = 15
# The rank and region routes sit behind the same batcher as hero-stats, which
# answers 500 when its query runs past 20 s (HERO-STATS-TIMEOUTS, 2026-10-31).
# A failed call is retried once, then split; past this many seconds the rest
# of the ids are given up on rather than holding the run.
BUDGET_S = int(os.environ.get("VALVE_RANK_BUDGET_S") or 150)

# badge = tier * 10 + subrank (1-6). Names as deadlock-api's schema lists them.
TIERS = ["Obscurus", "Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic",
         "Ritualist", "Emissary", "Oracle", "Phantom", "Ascendant", "Eternus"]


def _log(msg):
    print("  [valve] " + msg, file=sys.stderr)


def badge_name(badge):
    """116 -> "Eternus 6"; 0 or unreadable -> ""."""
    try:
        b = int(badge)
    except (TypeError, ValueError):
        return ""
    if b <= 0 or b // 10 >= len(TIERS):
        return ""
    return "%s %d" % (TIERS[b // 10], b % 10) if b % 10 else TIERS[b // 10]


def _rows(body):
    if isinstance(body, dict):
        body = body.get("entries") or body.get("data") or []
    return [r for r in (body or []) if isinstance(r, dict)]


def _status(err):
    """HTTP status of a failed call, or None (a timeout, a dropped connection)."""
    code = getattr(err, "code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"HTTP Error (\d{3})", str(err))
    return int(m.group(1)) if m else None


def _final(code):
    """A status that the same ids, asked again or in smaller lots, would get
    again: a 4xx other than a timeout, a rate limit or a too-long request."""
    return code is not None and 400 <= code < 500 and code not in (408, 413, 414, 429, 431)


def batched(get, url_for, ids, what, chunk=None, budget_s=None):
    """(rows, failed_ids, calls) for `ids`, asked `chunk` at a time.

    Never raises: the caller decides what a gap means.
      * 5xx, a timeout or a dropped connection: one retry if it failed fast
        (a call that took SLOW_S or longer ran into the API's 20 s query
        limit, and the same call will again), then the ids are split in half,
        down to MIN_CHUNK. A size that failed is not tried again. FLOOR_TRIES
        failures at MIN_CHUNK or under end it: the route is down, not
        overloaded.
      * 429: one wait, one retry.
      * any other 4xx (the route is not there, or refuses the request): no
        retry and no split. The same request cannot start working. A 413 or
        414 (request too long) is split like a timeout.
    Ids that were not fetched, for any of these reasons or because the time
    budget ran out, come back in failed_ids.
    """
    ids = sorted({int(a) for a in ids if a})
    chunk = max(1, int(chunk or CHUNK))
    budget_s = BUDGET_S if budget_s is None else budget_s
    todo = [ids[i:i + chunk] for i in range(0, len(ids), chunk)][::-1]
    rows, failed, calls, t0 = [], [], 0, time.time()
    floor_fails, said, stop, too_big = 0, set(), "", None
    while todo:
        part = todo.pop()
        if not stop and time.time() - t0 > budget_s:
            stop = "out of time after %ds" % int(time.time() - t0)
        if stop:
            failed.extend(part)
            continue
        if too_big and len(part) >= too_big and len(part) > MIN_CHUNK:
            mid = len(part) // 2            # a size already seen to fail
            todo.append(part[mid:])
            todo.append(part[:mid])
            continue
        body, err = None, None
        for attempt in (1, 2):
            calls += 1
            t1 = time.time()
            try:
                body = get(url_for(part))
                err = None
                break
            except Exception as e:          # HTTP error, timeout, bad JSON
                err = e
                code = _status(e)
                if _final(code):
                    break
                if code != 429 and time.time() - t1 >= SLOW_S:
                    break
                if attempt == 1:
                    time.sleep(RATE_WAIT_S if code == 429 else RETRY_WAIT_S)
        if err is None:
            rows.extend(_rows(body))
            continue
        code = _status(err)
        failed_now = str(err)[:80]
        if _final(code) or code == 429:
            failed.extend(part)
            stop = ("rate limited" if code == 429
                    else "the route answered %d" % code)
        elif len(part) > MIN_CHUNK:
            too_big = min(too_big or len(part), len(part))
            mid = len(part) // 2
            todo.append(part[mid:])
            todo.append(part[:mid])
            if mid not in said:
                said.add(mid)
                _log("%s: %d ids failed (%s); trying %d at a time"
                     % (what, len(part), failed_now, mid))
        else:
            failed.extend(part)
            floor_fails += 1
            if floor_fails >= FLOOR_TRIES:
                stop = "%d calls of %d ids or fewer failed (%s)" % (
                    floor_fails, MIN_CHUNK, failed_now)
    if failed:
        _log("%s: %d of %d ids not fetched%s"
             % (what, len(failed), len(ids), (": " + stop) if stop else ""))
    return rows, failed, calls


def fetch_ranks(get, base, account_ids):
    """(ranks, info). ranks: account_id -> the account's newest ranked match:

        badge      tier * 10 + subrank after that match; 0 in placement or
                   when no ranked match reports a rank
        progress   flat ladder progress after the match (before it, if the
                   match reports no final value); None when there is none
        interval   ranked interval the match counted for (1 = Beta Season 1)
        start_time unix time of the match

    An account deadlock-api leaves out (protected) or whose call failed is
    absent. info counts what was asked, answered and ranked.
    """
    rows, failed, calls = batched(
        get, lambda part: "%s/v1/players/rank?account_ids=%s"
        % (base, ",".join(str(a) for a in part)), account_ids, "ranks")
    ranks = {}
    for r in rows:
        try:
            aid = int(r["account_id"])
        except (KeyError, TypeError, ValueError):
            continue
        lm = r.get("last_match") or {}
        prog = lm.get("player_rank_final_flat_progress")
        if prog is None:
            prog = lm.get("player_rank_initial_flat_progress")
        ranks[aid] = {
            "badge": int(r.get("badge") or 0),
            "progress": int(prog) if prog is not None else None,
            # matches from before Season 2 carry no interval: they are all 1
            "interval": int(lm.get("rank_interval") or 1) if lm else None,
            "start_time": int(lm.get("start_time") or 0) if lm else None,
        }
    asked = len({int(a) for a in account_ids if a})
    info = {"asked": asked, "answered": len(ranks), "failed": len(failed),
            "ranked": sum(1 for v in ranks.values() if v["badge"] > 0),
            "calls": calls}
    return ranks, info


def is_current(rank, interval):
    """True when `rank` is a placed rank from ranked interval `interval`."""
    return bool(rank and rank["badge"] > 0 and rank["progress"] is not None
                and rank["interval"] == interval)


def spearman(xs, ys):
    """Rank correlation of two equal-length lists, average ranks for ties.
    None under 5 pairs or when either list is constant."""
    n = len(xs)
    if n < 5 or n != len(ys):
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r, i = [0.0] * n, 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sx = sum((a - mx) ** 2 for a in rx) ** 0.5
    sy = sum((b - my) ** 2 for b in ry) ** 0.5
    if not sx or not sy:
        return None
    return round(sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy), 3)


def fetch_intervals(get, base):
    """Every ranked interval Valve's game files list, oldest first:
    {"interval", "start", "end", "leaderboard_id", "season"}. [] on failure."""
    try:
        seasons = get("%s/v1/assets/ranked-seasons" % base)
    except Exception as e:
        _log("ranked seasons unreadable (%s)" % str(e)[:80])
        return []
    out = []
    for s in seasons if isinstance(seasons, list) else []:
        for i in (s.get("intervals") or []) if isinstance(s, dict) else []:
            try:
                out.append({"interval": int(i["interval"]),
                            "start": int(i["start_timestamp"]),
                            "end": int(i["end_timestamp"]),
                            "leaderboard_id": i.get("leaderboard_id"),
                            "season": s.get("name") or s.get("class_name") or ""})
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(out, key=lambda i: i["start"])


def running_interval(intervals, now=None):
    """The interval running at `now`; the later one at a shared boundary."""
    now = time.time() if now is None else now
    live = [i for i in intervals if i["start"] <= now <= i["end"]]
    return max(live, key=lambda i: i["start"]) if live else None


def board_interval(intervals, leaderboard_id):
    """The interval a leaderboard id belongs to, or None."""
    for i in intervals:
        if i["leaderboard_id"] == leaderboard_id:
            return i
    return None


def fetch_regions(get, base, account_ids):
    """(regions, info). regions: account_id -> the server region deadlock-api
    reports for the account's latest month ("eu_west", "us_north_central",
    ...), or None when it knows none. Absent = left out or failed."""
    rows, failed, calls = batched(
        get, lambda part: "%s/v1/players/region?account_ids=%s"
        % (base, ",".join(str(a) for a in part)), account_ids, "regions")
    regions = {}
    for r in rows:
        try:
            regions[int(r["account_id"])] = r.get("region") or None
        except (KeyError, TypeError, ValueError):
            continue
    asked = len({int(a) for a in account_ids if a})
    return regions, {"asked": asked, "answered": len(regions), "failed": len(failed),
                     "known": sum(1 for v in regions.values() if v), "calls": calls}


# Server region -> the leaderboard's region names. Prefixes, first match wins.
# A GUESS from the two examples in deadlock-api's docs ("eu_west",
# "us_north_central") plus the partner hosts it names; probe_season2.py prints
# every string it meets so this can be corrected. REGION_PREFIXES adds or
# overrides: "prefix=Region,prefix=Region".
_REGION_PREFIXES = [
    ("us_", "NAmerica"), ("na_", "NAmerica"), ("usa", "NAmerica"),
    ("eu_", "Europe"), ("dpkt-frankfurt", "Europe"), ("frankfurt", "Europe"),
    ("helsinki", "Europe"), ("stockholm", "Europe"), ("amsterdam", "Europe"),
    ("sa_", "SAmerica"), ("sam", "SAmerica"), ("brazil", "SAmerica"),
    ("asia", "Asia"), ("sea", "Asia"), ("japan", "Asia"), ("korea", "Asia"),
    ("china", "Asia"), ("india", "Asia"), ("hong", "Asia"), ("singapore", "Asia"),
    ("aus", "Oceania"), ("oce", "Oceania"), ("sydney", "Oceania"),
]
for _pair in (os.environ.get("REGION_PREFIXES") or "").split(","):
    if "=" in _pair:
        _k, _v = _pair.split("=", 1)
        _REGION_PREFIXES.insert(0, (_k.strip().lower(), _v.strip()))


def board_region(server_region):
    """"us_north_central" -> "NAmerica"; None when the string is unknown."""
    s = (server_region or "").strip().lower()
    if not s:
        return None
    for prefix, name in _REGION_PREFIXES:
        if s.startswith(prefix):
            return name
    return None


def hero_top(get, base, hero_id, since, min_matches=3, limit=200):
    """Players with at least `min_matches` ranked matches on a hero since
    `since` (unix), best rank first: [{"account_id", "badge", "progress",
    "matches"}]. `progress` is the flat progress after their newest ranked
    match ON THAT HERO in range. Raises on a failed call."""
    url = ("%s/v1/analytics/scoreboards/players?sort_by=rank&hero_id=%d"
           "&match_mode=ranked&min_matches=%d&min_unix_timestamp=%d&limit=%d"
           % (base, int(hero_id), int(min_matches), int(since), int(limit)))
    out = []
    for r in _rows(get(url)):
        try:
            out.append({"account_id": int(r["account_id"]),
                        "badge": int(r.get("badge") or 0),
                        "progress": int(r.get("value") or 0),
                        "matches": int(r.get("matches") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return out
