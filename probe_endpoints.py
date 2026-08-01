#!/usr/bin/env python3
"""
Probe the endpoints we aren't using yet, to find out what's actually available
for the top-100 cohort. Read-only, isolated per probe, conservative pacing.

    python3 probe_endpoints.py

Run AFTER deadlock_pipeline.py in the same job — it reads real account_ids from
output/candidates.csv (or output/board.json as a fallback).

Rate limits: none of these are /v1/sql, so none of this touches the 20/hr SQL
budget that forced the region split. hero-stats documents 100 req/s; the mmr
endpoints don't state a limit in the spec, so they get a deliberate pause.

Each probe is wrapped: one failing endpoint reports and the rest continue.

Questions this answers:
  1. ranked-seasons  -> does the game itself confirm the Oct 7 season end?
  2. players/mmr     -> EXPECTED TO BE BROKEN. deadlock-api computes MMR with
                        the same badge formula and 50-match EMA the pipeline
                        uses, from the same average_badge columns that have
                        read 0 across ranked since the reset. Probing to
                        confirm the fault is upstream, not ours.
  3. mmr-history     -> how far back does it reach? if it predates the archive,
                        the missed days can be backfilled instead of lost
  4. hero-stats      -> what per-player-per-hero detail is available in batch
  5. sql/tables      -> SCHEMA.md says schema discovery is impossible
                        (DESCRIBE blocked, system tables denied). Still true?
"""

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
PAUSE = float(os.environ.get("PROBE_PAUSE") or 2.0)
# MMR endpoints share ONE bucket: 5 req/min per IP (25 with a key). Every call
# to /v1/players/mmr AND /v1/players/{id}/mmr-history draws on it. 15s between
# them keeps this probe's 3 calls comfortably inside 5/min.
MMR_PAUSE = float(os.environ.get("MMR_PAUSE") or (5.0 if API_KEY else 15.0))
_mmr_calls = 0


def mmr_get(url, label):
    """Throttled getter for the shared MMR bucket."""
    global _mmr_calls
    if _mmr_calls:
        print("  ... waiting %.0fs (MMR bucket is %s/min)"
              % (MMR_PAUSE, "25" if API_KEY else "5"), file=sys.stderr)
        time.sleep(MMR_PAUSE)
    _mmr_calls += 1
    return get(url, label)
OUT = "output"


def get(url, label):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
    except Exception as e:
        return None, str(e)


def probe(name, fn):
    print("\n" + "=" * 62, file=sys.stderr)
    print("PROBE: %s" % name, file=sys.stderr)
    print("=" * 62, file=sys.stderr)
    try:
        fn()
    except Exception as e:
        print("  [FAILED] %s: %s" % (type(e).__name__, e), file=sys.stderr)
    time.sleep(PAUSE)


def account_ids(limit=60):
    """Real account ids the pipeline already resolved."""
    p = os.path.join(OUT, "candidates.csv")
    if os.path.exists(p):
        ids, seen = [], set()
        for r in csv.DictReader(open(p, encoding="utf-8-sig")):
            a = r.get("account_id")
            if a and a not in seen:
                seen.add(a)
                ids.append(int(a))
            if len(ids) >= limit:
                break
        if ids:
            print("  [ids] %d from candidates.csv" % len(ids), file=sys.stderr)
            return ids
    p = os.path.join(OUT, "board.json")
    if os.path.exists(p):
        board = json.load(open(p, encoding="utf-8"))
        ids = []
        for rg in board:
            for e in board[rg]["entries"]:
                if len(e.get("ids") or []) == 1:
                    ids.append(e["ids"][0])
        ids = ids[:limit]
        print("  [ids] %d from board.json (single-candidate entries)" % len(ids),
              file=sys.stderr)
        return ids
    print("  [ids] none available — run the pipeline first", file=sys.stderr)
    return []


IDS = []


def p_seasons():
    data, err = get(BASE + "/v1/assets/ranked-seasons", "seasons")
    if err:
        return print("  " + err, file=sys.stderr)
    seasons = data if isinstance(data, list) else data.get("seasons", data)
    print("  raw (first 600 chars): %s" % json.dumps(seasons)[:600], file=sys.stderr)
    import datetime
    if isinstance(seasons, list):
        for s in seasons[:6]:
            if not isinstance(s, dict):
                continue
            times = {k: v for k, v in s.items()
                     if isinstance(v, int) and 1600000000 < v < 2000000000}
            human = {k: datetime.datetime.utcfromtimestamp(v).date().isoformat()
                     for k, v in times.items()}
            print("  season: %s  %s"
                  % ({k: v for k, v in s.items() if not isinstance(v, (dict, list))
                      and k not in times}, human), file=sys.stderr)


def p_batch_mmr():
    if not IDS:
        return print("  skipped: no account ids", file=sys.stderr)
    # ONE call only — the bucket is 5/min shared with mmr-history. Batch big
    # rather than testing two sizes; the endpoint takes an account_ids array.
    for n in (40,):
        sample = IDS[:n]
        q = "&".join("account_ids=%d" % a for a in sample)
        data, err = mmr_get(BASE + "/v1/players/mmr?" + q, "mmr")
        if err:
            print("  batch of %d -> %s" % (n, err), file=sys.stderr)
            break
        rows = data if isinstance(data, list) else data.get("data", [])
        print("  batch of %d -> %d rows" % (n, len(rows)), file=sys.stderr)
        if rows:
            print("  sample row: %s" % json.dumps(rows[0]), file=sys.stderr)
            scores = [r.get("player_score") for r in rows if r.get("player_score") is not None]
            ranks = [r.get("rank") for r in rows if r.get("rank") is not None]
            print("  player_score populated on %d/%d rows" % (len(scores), len(rows)),
                  file=sys.stderr)
            # deadlock-api computes MMR as (intDiv(badge,10)-1)*6 + badge%10,
            # EMA over 50 matches — the SAME formula the pipeline uses, from
            # the same average_badge columns. Those have been 0 across ranked
            # since the reset, and badge=0 yields exactly -6. If that is what
            # comes back, this endpoint is broken upstream too and is NOT an
            # independent rating.
            if scores and all(abs(v + 6.0) < 0.01 for v in scores):
                print("  >>> ALL -6.0 — same badge-derived breakage as our own "
                      "mmr column. Not usable as a chart axis.", file=sys.stderr)
            elif scores:
                print("  >>> values vary — worth a closer look", file=sys.stderr)
            if scores:
                print("  player_score range: %.1f .. %.1f" % (min(scores), max(scores)),
                      file=sys.stderr)
            if ranks:
                print("  rank range: %s .. %s | distinct %d"
                      % (min(ranks), max(ranks), len(set(ranks))), file=sys.stderr)
        time.sleep(PAUSE)


def p_mmr_history():
    """The backfill question: does this reach back before the archive started?"""
    if not IDS:
        return print("  skipped: no account ids", file=sys.stderr)
    import datetime
    data, err = mmr_get(BASE + "/v1/players/%d/mmr-history" % IDS[0], "mmr-history")
    if err:
        return print("  " + err, file=sys.stderr)
    rows = data if isinstance(data, list) else data.get("data", [])
    print("  %d history rows for account %d" % (len(rows), IDS[0]), file=sys.stderr)
    if not rows:
        return
    print("  sample: %s" % json.dumps(rows[0]), file=sys.stderr)
    ts = sorted(r["start_time"] for r in rows if r.get("start_time"))
    if ts:
        first = datetime.datetime.utcfromtimestamp(ts[0])
        last = datetime.datetime.utcfromtimestamp(ts[-1])
        print("  spans %s .. %s (%d days)"
              % (first.date(), last.date(), (last - first).days), file=sys.stderr)
        print("  >>> BACKFILL: %s"
              % ("reaches before 2026-08-01, missed days recoverable"
                 if first.date() < datetime.date(2026, 8, 1)
                 else "starts after the archive did, no backfill available"),
              file=sys.stderr)


def p_hero_stats():
    if not IDS:
        return print("  skipped: no account ids", file=sys.stderr)
    q = "&".join("account_ids=%d" % a for a in IDS[:5])
    data, err = get(BASE + "/v1/players/hero-stats?" + q, "hero-stats")
    if err:
        return print("  " + err, file=sys.stderr)
    rows = data if isinstance(data, list) else data.get("data", [])
    print("  %d rows for 5 accounts" % len(rows), file=sys.stderr)
    if rows:
        r = dict(rows[0])
        r.pop("matches", None)          # can be a long array
        print("  sample (matches[] omitted): %s" % json.dumps(r)[:700], file=sys.stderr)
        print("  fields: %s" % sorted(rows[0].keys()), file=sys.stderr)


def p_board_ranks():
    """Are ranked_rank / ranked_subrank alive on leaderboard entries?

    This is the open question for the chart axis. MMR is badge-derived and
    badge is dead across ranked, so player_score is unusable. But the
    leaderboard is a ranked-system artifact, and its entries carry
    ranked_rank / ranked_subrank / badge_level. If the first two are populated
    post-reset they are a genuine rating — unlike badge_level, which the
    2026-07-31 diagnostic found empty on 71/71 entries.

    The /v1/leaderboard endpoint is 100 req/s and NOT in the MMR bucket, so
    this is cheap. Two calls, one per region.
    """
    import collections
    for region in ("NAmerica", "Europe"):
        data, err = get(BASE + "/v1/leaderboard/%s" % region, "leaderboard")
        if err:
            print("  %s -> %s" % (region, err), file=sys.stderr)
            continue
        entries = data.get("entries") if isinstance(data, dict) else data
        if not entries:
            print("  %s -> no entries" % region, file=sys.stderr)
            continue
        top = entries[:100]
        print("  %s: %d entries (top 100 examined)" % (region, len(entries)),
              file=sys.stderr)
        print("    sample entry: %s" % json.dumps(top[0])[:260], file=sys.stderr)

        for field in ("ranked_rank", "ranked_subrank", "badge_level"):
            vals = [e.get(field) for e in top]
            live = [v for v in vals if v not in (None, "")]
            line = "    %-15s populated %3d/100" % (field, len(live))
            if live:
                nums = [v for v in live if isinstance(v, (int, float))]
                if nums:
                    line += "  range %s..%s  distinct %d" % (
                        min(nums), max(nums), len(set(nums)))
            print(line, file=sys.stderr)

        # a usable axis needs to actually separate players near the top
        rr = [e.get("ranked_rank") for e in top[:30]
              if isinstance(e.get("ranked_rank"), (int, float))]
        rs = [e.get("ranked_subrank") for e in top[:30]
              if isinstance(e.get("ranked_subrank"), (int, float))]
        if rr and len(set(rr)) > 1:
            combined = [10 * a + b for a, b in zip(rr, rs)] if len(rs) == len(rr) else rr
            print("    >>> ranked_rank VARIES in the top 30 (distinct %d) — "
                  "usable as a chart axis. combined rank*10+subrank distinct: %d"
                  % (len(set(rr)), len(set(combined))), file=sys.stderr)
        elif rr:
            print("    >>> ranked_rank is CONSTANT (%s) in the top 30 — "
                  "no separation, not usable alone" % rr[0], file=sys.stderr)
        else:
            print("    >>> ranked_rank absent/non-numeric — not usable",
                  file=sys.stderr)
        time.sleep(PAUSE)


def p_sql_tables():
    data, err = get(BASE + "/v1/sql/tables", "sql/tables")
    if err:
        return print("  " + err, file=sys.stderr)
    print("  tables: %s" % json.dumps(data)[:500], file=sys.stderr)
    names = data if isinstance(data, list) else data.get("tables", [])
    target = None
    for t in names:
        n = t if isinstance(t, str) else t.get("name")
        if n == "match_player":
            target = n
            break
    if target:
        time.sleep(PAUSE)
        d2, e2 = get(BASE + "/v1/sql/tables/%s/schema" % target, "schema")
        if e2:
            return print("  schema -> " + e2, file=sys.stderr)
        print("  match_player schema (first 600 chars): %s"
              % json.dumps(d2)[:600], file=sys.stderr)
        print("  >>> SCHEMA.md says discovery is impossible — update it if this worked",
              file=sys.stderr)


def main():
    global IDS
    print("probing %s (key: %s)" % (BASE, "yes" if API_KEY else "no"), file=sys.stderr)
    IDS = account_ids()
    probe("ranked-seasons (confirm season end date)", p_seasons)
    probe("players/mmr (batch — real rating for chart height?)", p_batch_mmr)
    probe("players/{id}/mmr-history (can we backfill?)", p_mmr_history)
    probe("players/hero-stats (batch per-hero performance)", p_hero_stats)
    probe("leaderboard ranked_rank/subrank (a working chart axis?)", p_board_ranks)
    probe("sql/tables (is schema discovery possible now?)", p_sql_tables)
    print("\ndone — no /v1/sql queries were used, pipeline budget untouched",
          file=sys.stderr)


if __name__ == "__main__":
    main()
