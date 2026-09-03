#!/usr/bin/env python3
"""
Top-3 most-played heroes, last N days, for every player on the general board.

Standalone. Does not touch the pipeline's CSVs and does not share its SQL
budget with a normal run — schedule it separately.

    python3 top_heroes.py

Reads  nothing
Writes ./output/top_heroes.csv        one row per resolved account
       ./output/top_heroes_probe.json what each source returned, for PROBES.md

--------------------------------------------------------------------------
THREE SOURCES, CHEAPEST FIRST. Later sources run only for what earlier ones
could not answer.

  A. top_hero_ids on the general-board entry.  ZERO extra calls — it arrives
     in the leaderboard payload. Populated on ~561 of 1,001 entries. Window is
     undocumented (probably career) and the list may not be ordered, so this
     is treated as a CROSS-CHECK, never as the answer, unless
     TRUST_TOP_HERO_IDS=1.

  B. /v1/players/hero-stats.  100 req/s, a SEPARATE bucket from /v1/sql, so
     effectively free. Returns one row per (account, hero) with
     matches_played, which is exactly the shape wanted.
     UNVERIFIED: whether it accepts a time window. The pipeline has only ever
     sent match_mode. probe_hero_stats_window() settles it with a control —
     an ignored parameter returns a byte-identical body, which is how
     match_mode=Ranked was caught being ignored on the leaderboard endpoint.

  C. /v1/sql.  Always works, costs real budget: ~380 ids per 9,000-char URL at
     the measured 22.7 encoded chars per id, so ~5 calls per region against a
     20/HOUR cap. Only runs if B has no usable window.

--------------------------------------------------------------------------
IDENTITY — read before trusting any row.

Valve publishes NO account ids. The board gives a display name plus
deadlock-api's own fuzzy `possible_account_ids`, so ~1,000 board entries
become ~1,900 CANDIDATE ids per region, and a candidate is not a player.
Name-only matching put the wrong player in 110 of 371 slots.

Two defences, both on by default:

  - MAX_IDS_PER_ENTRY=2. Measured over 1,721 resolutions: 92% resolved from
    slot 0, 8% from slot 1, 0% from slot 2+. Truncating to 2 cut 87,351
    candidate ids to 4,061 at no measured accuracy cost. The list is
    best-match-first and is NEVER re-sorted.
  - MIN_ACCOUNT_GAMES. An account claiming a top-1,000 regional position with
    eight games in the window is not that player. Run 46 split cleanly: bad
    ids had a median of 8 games, good ones a median of 2,374.

Where two candidate ids for one board entry BOTH clear the floor, the entry is
marked ambiguous and the higher-game id is used. That is a guess, and the
column says so.
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
OUT_DIR = os.environ.get("OUT_DIR") or "output"


def _env(name, default):
    v = os.environ.get(name)
    return type(default)(v) if v not in (None, "") else default


REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
DAYS = _env("DAYS", 14)
TOP_N = _env("TOP_N", 3)

# "" = no match_mode filter: standard play, which is what the board is built
# from. Matches the pipeline default.
MATCH_MODE = os.environ.get("MATCH_MODE") or ""

MAX_IDS_PER_ENTRY = _env("MAX_IDS_PER_ENTRY", 2)
MIN_ACCOUNT_GAMES = _env("MIN_ACCOUNT_GAMES", 20)  # over the DAYS window, not career

# URL ceiling ~9,000 confirmed in live runs at 7,200-8,400 with no 414.
MAX_URL = _env("MAX_URL", 9000)
CHARS_PER_ID = float(os.environ.get("CHARS_PER_ID") or 22.7)

# The binding limit is 20/HOUR, not 2/min. Two runs died at exactly chunk 21.
# Budget whole runs; this refuses to start rather than dying two thirds in.
SQL_BUDGET = _env("SQL_BUDGET", 20)
SQL_PAUSE_S = _env("SQL_PAUSE_S", 31)
ALLOW_SQL = os.environ.get("ALLOW_SQL", "1") == "1"
TRUST_TOP_HERO_IDS = os.environ.get("TRUST_TOP_HERO_IDS", "0") == "1"

HTTP_TRIES = _env("HTTP_TRIES", 4)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _get(url, raw=False):
    """GET with retries. A reset connection never reaches an HTTP status, so
    URLError/OSError must be retried alongside HTTPError or a dropped TLS
    handshake kills the run outright (it killed one at hero 52 of 38)."""
    last = None
    for attempt in range(HTTP_TRIES):
        req = urllib.request.Request(url, headers={"User-Agent": "deadlock-topheroes/1.0"})
        if API_KEY:
            req.add_header("X-API-Key", API_KEY)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
            return body if raw else json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                # honour next_request_in; exponential backoff from 1s is far
                # too short against the 50/min GLOBAL ceiling
                wait = 5
                try:
                    wait = float(json.loads(e.read().decode("utf-8"))
                                 .get("next_request_in") or wait)
                except Exception:
                    pass
                time.sleep(min(wait + 1, 90))
                continue
            if 500 <= e.code < 600:
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
            time.sleep(2 ** attempt)
    raise last


# --------------------------------------------------------------------------
# SOURCE A — the general board, and top_hero_ids that rides along free
# --------------------------------------------------------------------------


def fetch_general_board(region):
    """[{pos, name, ids, top_hero_ids}] — position is the LIST INDEX.

    `rank` is not unique: 1,001 NA entries carried only 634 distinct values and
    rank 1 was shared by 14 players. The list is monotonic in rank, so ranking
    by it produced ten heroes tied at position 1. Use the index.

    1,001 entries IS the whole board, not a read cap — limit=1000, 2000 and
    5000 all return 1,001.
    """
    payload = _get("%s/v1/leaderboard/%s" % (BASE, urllib.parse.quote(region)))
    entries = payload.get("entries", []) if isinstance(payload, dict) else payload
    out = []
    for pos, e in enumerate(entries or [], 1):
        nm = e.get("account_name")
        if not nm:
            continue
        ids = []
        for a in (e.get("possible_account_ids") or []):
            a = int(a)
            if a and a not in ids:
                ids.append(a)          # best-match-first: never re-sort
        out.append({"pos": pos, "name": nm,
                    "ids": ids[:MAX_IDS_PER_ENTRY],
                    "ids_truncated": len(ids) > MAX_IDS_PER_ENTRY,
                    "top_hero_ids": [int(h) for h in (e.get("top_hero_ids") or [])]})
    return out


# --------------------------------------------------------------------------
# SOURCE B — hero-stats, if it will take a time window
# --------------------------------------------------------------------------

WINDOW_PARAM_SETS = [
    ("min_unix_timestamp", "max_unix_timestamp"),
    ("min_timestamp", "max_timestamp"),
    ("start_time", "end_time"),
]


def _hs_url(ids, extra=None):
    params = list(extra or [])
    if MATCH_MODE:
        params.append("match_mode=%s" % urllib.parse.quote(MATCH_MODE))
    params.extend("account_ids=%d" % a for a in ids)
    return "%s/v1/players/hero-stats?%s" % (BASE, "&".join(params))


def probe_hero_stats_window(sample_ids):
    """Which time-window parameter, if any, hero-stats actually honours.

    THE CONTROL IS THE POINT. deadlock-api accepts and silently IGNORES
    parameters it does not implement — match_mode=Ranked returned a
    byte-identical body to the unparameterised leaderboard call, and
    match_mode on /v1/players/scoreboard was accepted while returning an
    account with 7,651 matches inside an 8-day window. So a parameter is only
    believed when the response CHANGES.

    A 400 is also informative: it means the parameter name was rejected
    outright, which at least tells you the spelling is wrong rather than the
    feature being absent.
    """
    result = {"windowed": False, "params": None, "notes": []}
    if not sample_ids:
        return result
    sample = sample_ids[:20]
    try:
        baseline = _get(_hs_url(sample), raw=True)
    except Exception as e:
        result["notes"].append("baseline call failed: %s" % e)
        return result
    since = int(time.time()) - DAYS * 86400
    now = int(time.time())
    for lo, hi in WINDOW_PARAM_SETS:
        extra = ["%s=%d" % (lo, since), "%s=%d" % (hi, now)]
        try:
            body = _get(_hs_url(sample, extra), raw=True)
        except urllib.error.HTTPError as e:
            result["notes"].append("%s -> HTTP %s (name rejected)" % (lo, e.code))
            continue
        except Exception as e:
            result["notes"].append("%s -> %s" % (lo, e))
            continue
        if body == baseline:
            result["notes"].append("%s accepted but IGNORED (identical body)" % lo)
            continue
        result["windowed"] = True
        result["params"] = [lo, hi]
        result["notes"].append("%s CHANGES the response — honoured" % lo)
        return result
    return result


def fetch_hero_stats(ids, window_params):
    """(account_id, hero_id) -> games, over the window if one is honoured."""
    per_hero = {}
    extra = []
    if window_params:
        since = int(time.time()) - DAYS * 86400
        extra = ["%s=%d" % (window_params[0], since),
                 "%s=%d" % (window_params[1], int(time.time()))]
    fixed = len(_hs_url([], extra))
    chunk = max(20, int((MAX_URL - fixed - 40) // 24))
    calls = 0
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            rows = _get(_hs_url(part, extra))
        except Exception as e:
            print("  [hs] chunk %d-%d failed: %s" % (i + 1, i + len(part), e),
                  file=sys.stderr)
            continue
        calls += 1
        for r in rows or []:
            a, h = r.get("account_id"), r.get("hero_id")
            if a is None or h is None:
                continue
            # `matches` is a LIST OF MATCH IDS on this endpoint; the count is
            # `matches_played`. Taking `matches` first hands a list to int().
            g = r.get("matches_played")
            if g is None:
                m = r.get("matches")
                g = len(m) if isinstance(m, list) else (m or 0)
            per_hero[(int(a), int(h))] = int(g or 0)
    print("  [hs] %d calls (free bucket), %d (account,hero) rows"
          % (calls, len(per_hero)), file=sys.stderr)
    return per_hero


# --------------------------------------------------------------------------
# SOURCE C — SQL
# --------------------------------------------------------------------------

Q_TOP = """
SELECT account_id, hero_id, count() AS games
FROM match_player
WHERE account_id IN ({ids})
  AND {mode}game_mode = 'Normal'
  AND start_time >= now() - INTERVAL {days} DAY
GROUP BY account_id, hero_id
"""
# No ORDER BY and no LIMIT n BY on purpose. Sorting a large aggregate
# server-side to read a few rows produced a 524 Cloudflare timeout on the
# item_cohort tables; the top-N is trivial to take in Python instead.


def sql_url(q):
    return BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)


def plan_sql_chunks(ids):
    """Chunk sizing that measures the FULL url, prefix included.

    query_items() once measured quote(q) while sql() measured the whole url, so
    a chunk cleared its own check at 8,997 chars and died inside sql() at
    9,007. And overflow must resize proportionally, not halve: a 360-pair chunk
    missing by ten characters dropped to 180 and turned 4 calls into 8 against
    an HOURLY cap.
    """
    mode_sql = "match_mode = '%s' AND " % MATCH_MODE if MATCH_MODE else ""
    fixed = len(sql_url(Q_TOP.format(ids="", mode=mode_sql, days=DAYS)))
    per = CHARS_PER_ID
    size = max(1, int((MAX_URL - fixed) // per))
    chunks = [ids[i:i + size] for i in range(0, len(ids), size)]
    return chunks, mode_sql, size


def fetch_sql(ids):
    per_hero = {}
    chunks, mode_sql, size = plan_sql_chunks(ids)
    print("  [sql] %d ids -> %d calls at chunk %d" % (len(ids), len(chunks), size),
          file=sys.stderr)
    if len(chunks) > SQL_BUDGET:
        raise SystemExit(
            "PROJECTED %d SQL calls against a budget of %d (the real limit is "
            "20/HOUR). Refusing to start rather than dying two thirds in.\n"
            "Options: run one region at a time (REGIONS=NAmerica), set "
            "MAX_IDS_PER_ENTRY=1 to halve the id list at a measured 8%% "
            "accuracy cost, or raise SQL_BUDGET if you have a key."
            % (len(chunks), SQL_BUDGET))
    for n, part in enumerate(chunks, 1):
        q = Q_TOP.format(ids=",".join(str(a) for a in part),
                         mode=mode_sql, days=DAYS)
        url = sql_url(q)
        if len(url) > MAX_URL:
            print("  [sql] chunk %d over ceiling at %d chars, trimming"
                  % (n, len(url)), file=sys.stderr)
            part = part[:int(len(part) * MAX_URL / len(url)) - 1]
            q = Q_TOP.format(ids=",".join(str(a) for a in part),
                             mode=mode_sql, days=DAYS)
            url = sql_url(q)
        if n > 1:
            time.sleep(SQL_PAUSE_S)
        rows = _get(url)
        if isinstance(rows, dict):
            rows = rows.get("data", rows.get("rows", []))
        for r in rows or []:
            per_hero[(int(r["account_id"]), int(r["hero_id"]))] = int(r["games"])
        print("  [sql] call %d/%d -> %d rows" % (n, len(chunks), len(rows or [])),
              file=sys.stderr)
    return per_hero


# --------------------------------------------------------------------------
# ASSEMBLY
# --------------------------------------------------------------------------


def load_heroes():
    try:
        return {int(h.get("id", h.get("hero_id"))):
                (h.get("name") or h.get("display_name") or "")
                for h in _get(BASE + "/v1/assets/heroes")
                if h.get("id", h.get("hero_id")) is not None}
    except Exception as e:
        print("  [assets] hero names unavailable (%s) — ids only" % e, file=sys.stderr)
        return {}


def resolve(entry, per_hero):
    """Pick which candidate id is this board entry, and its top heroes.

    Returns (account_id, totals, ranked_heroes, verdict). verdict is one of
    resolved / ambiguous / implausible / no_data.
    """
    scored = []
    for aid in entry["ids"]:
        heroes = sorted(((g, h) for (a, h), g in per_hero.items() if a == aid),
                        reverse=True)
        total = sum(g for g, _ in heroes)
        scored.append((total, aid, heroes))
    if not scored:
        return None, 0, [], "no_data"
    scored.sort(reverse=True)
    clearing = [s for s in scored if s[0] >= MIN_ACCOUNT_GAMES]
    if not clearing:
        # every candidate is near-empty: a top-1,000 board entry whose ids all
        # have almost no play is a resolution failure, not a quiet player
        return scored[0][1], scored[0][0], [], "implausible"
    total, aid, heroes = clearing[0]
    verdict = "ambiguous" if len(clearing) > 1 else "resolved"
    return aid, total, heroes, verdict


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    heroes = load_heroes()
    probe_log = {"days": DAYS, "match_mode": MATCH_MODE or "(unfiltered)",
                 "regions": REGIONS, "sources": {}}

    boards = {}
    all_ids = []
    for rg in REGIONS:
        b = fetch_general_board(rg)
        boards[rg] = b
        ids = [a for e in b for a in e["ids"]]
        all_ids.extend(ids)
        with_top = sum(1 for e in b if e["top_hero_ids"])
        print("[A] %-9s %d entries, %d candidate ids, top_hero_ids on %d (%.0f%%)"
              % (rg, len(b), len(ids), with_top, 100.0 * with_top / max(len(b), 1)),
              file=sys.stderr)
        probe_log["sources"].setdefault("top_hero_ids", {})[rg] = {
            "entries": len(b), "with_top_hero_ids": with_top,
            "candidate_ids": len(ids),
            "max_list_len": max([len(e["top_hero_ids"]) for e in b] or [0])}

    all_ids = sorted(set(all_ids))
    print("\n[B] probing hero-stats for a time window (%d ids total)" % len(all_ids),
          file=sys.stderr)
    probe = probe_hero_stats_window(all_ids)
    probe_log["sources"]["hero_stats_window"] = probe
    for n in probe["notes"]:
        print("  [probe] %s" % n, file=sys.stderr)

    per_hero, source = {}, None
    if probe["windowed"]:
        print("  [B] window honoured via %s — using hero-stats, no SQL budget spent"
              % probe["params"][0], file=sys.stderr)
        per_hero = fetch_hero_stats(all_ids, probe["params"])
        source = "hero_stats_windowed"
    elif ALLOW_SQL:
        print("  [B] no honoured window. Falling through to SQL.\n"
              "\n[C] SQL, %d day window" % DAYS, file=sys.stderr)
        per_hero = fetch_sql(all_ids)
        source = "sql"
    else:
        print("  [B] no honoured window and ALLOW_SQL=0. Falling back to "
              "UNWINDOWED hero-stats — these are career counts, NOT the last "
              "%d days. Marked as such in the output." % DAYS, file=sys.stderr)
        per_hero = fetch_hero_stats(all_ids, None)
        source = "hero_stats_career"
    probe_log["source_used"] = source

    rows, verdicts = [], defaultdict(int)
    for rg in REGIONS:
        for e in boards[rg]:
            aid, total, ranked, verdict = resolve(e, per_hero)
            verdicts[verdict] += 1
            row = {"region": rg, "board_pos": e["pos"], "account_name": e["name"],
                   "account_id": aid if aid is not None else "",
                   "resolution": verdict,
                   "ids_offered": len(e["ids"]),
                   "ids_truncated": "YES" if e["ids_truncated"] else "",
                   "window_days": DAYS, "source": source,
                   "games_in_window": total,
                   # Valve's own view, free with the board. Cross-check only:
                   # undocumented window, possibly unordered.
                   "valve_top_hero_ids": " ".join(str(h) for h in e["top_hero_ids"]),
                   "valve_agrees": ""}
            for i in range(TOP_N):
                g, h = ranked[i] if i < len(ranked) else ("", "")
                row["hero%d" % (i + 1)] = heroes.get(h, h if h == "" else "hero_%s" % h)
                row["hero%d_games" % (i + 1)] = g
            if ranked and e["top_hero_ids"]:
                row["valve_agrees"] = "YES" if ranked[0][1] in e["top_hero_ids"] else "NO"
            rows.append(row)

    cols = (["region", "board_pos", "account_name", "account_id", "resolution",
             "games_in_window", "window_days", "source"]
            + [c for i in range(TOP_N)
               for c in ("hero%d" % (i + 1), "hero%d_games" % (i + 1))]
            + ["valve_top_hero_ids", "valve_agrees", "ids_offered", "ids_truncated"])
    path = os.path.join(OUT_DIR, "top_heroes.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    print("\n  -> %s (%d rows)" % (path, len(rows)), file=sys.stderr)

    print("  resolution: %s" % dict(verdicts), file=sys.stderr)
    if verdicts["implausible"]:
        print("  [warn] %d board entries had no candidate id clearing %d games in "
              "the window. Those are resolution failures, not quiet players — a "
              "top-1,000 entry with no play does not exist."
              % (verdicts["implausible"], MIN_ACCOUNT_GAMES), file=sys.stderr)
    agree = [r for r in rows if r["valve_agrees"]]
    if agree:
        yes = sum(1 for r in agree if r["valve_agrees"] == "YES")
        print("  [check] our #1 hero is in Valve's top_hero_ids for %d of %d "
              "entries that carry both (%.0f%%)"
              % (yes, len(agree), 100.0 * yes / len(agree)), file=sys.stderr)

    ppath = os.path.join(OUT_DIR, "top_heroes_probe.json")
    json.dump(probe_log, open(ppath, "w"), indent=2)
    print("  -> %s" % ppath, file=sys.stderr)


if __name__ == "__main__":
    main()
