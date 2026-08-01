#!/usr/bin/env python3
"""
Standalone probe of /v1/players/{account_id}/mmr-history.

The batched probe hit a GLOBAL 429 (50 req/min, shared across every user of the
API — not our quota) before reaching this endpoint. This runs it alone, with
retry that honours the API's own next_request_in hint.

    python3 probe_mmr_history.py

Reads account ids from output/candidates.csv (run the pipeline first).

Rate limits: the MMR endpoints share ONE bucket — 5 req/min per IP unkeyed,
25 with a key — on top of a 50/min global ceiling. This makes at most
PROBE_ACCOUNTS calls (default 3) with a 15s gap.

What it answers:
  * Does history reach back to season start (2026-07-30 17:00 UTC)? The archive
    only began 2026-08-01, so this covers the ~2 missed days.
  * Is it per-MATCH with timestamps? If so a daily rating curve can be
    reconstructed retroactively for the whole season, rather than depending on
    daily snapshots going forward.
  * Does player_score actually vary per account, or is it flat?
"""

import csv
import datetime
import json
import os
import sys
import time
import urllib.request

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
N_ACCOUNTS = int(os.environ.get("PROBE_ACCOUNTS") or 3)
GAP = float(os.environ.get("MMR_PAUSE") or (5.0 if API_KEY else 15.0))
SEASON_START = datetime.datetime(2026, 7, 30, 17, 0, tzinfo=datetime.timezone.utc)
ARCHIVE_START = datetime.date(2026, 8, 1)


def get(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < tries - 1:
                wait = 20
                try:
                    j = json.loads(body)
                    err = j.get("error", {})
                    wait = int(err.get("next_request_in") or 0) + 5
                    scope = err.get("type", "?")
                    quota = err.get("quota", {})
                    print("    429 (%s limit %s/%ss) — waiting %ds"
                          % (scope, quota.get("limit"), quota.get("period"), wait),
                          file=sys.stderr)
                except Exception:
                    print("    429 — waiting %ds" % wait, file=sys.stderr)
                time.sleep(max(wait, 20))
                continue
            return None, "HTTP %s: %s" % (e.code, body)
        except Exception as e:
            return None, str(e)
    return None, "gave up after %d tries" % tries


def account_ids(n):
    p = os.path.join("output", "candidates.csv")
    if not os.path.exists(p):
        raise SystemExit("no output/candidates.csv — run the pipeline first")
    ids, seen = [], set()
    for r in csv.DictReader(open(p, encoding="utf-8-sig")):
        a = r.get("account_id")
        if a and a not in seen:
            seen.add(a)
            ids.append((int(a), r.get("account_name", "?"), r.get("hero", "?")))
        if len(ids) >= n:
            break
    return ids


def examine(aid, name, hero):
    data, err = get("%s/v1/players/%d/mmr-history" % (BASE, aid))
    if err:
        print("  %-14s %s" % (name[:14], err), file=sys.stderr)
        return None
    rows = data if isinstance(data, list) else data.get("data", [])
    print("  %-14s (%s) -> %d rows" % (name[:14], hero[:12], len(rows)), file=sys.stderr)
    if not rows:
        return None

    print("    fields: %s" % sorted(rows[0].keys()), file=sys.stderr)
    print("    first:  %s" % json.dumps(rows[0]), file=sys.stderr)
    print("    last:   %s" % json.dumps(rows[-1]), file=sys.stderr)

    ts = sorted(r["start_time"] for r in rows if r.get("start_time"))
    if not ts:
        print("    no start_time — cannot build a dated series", file=sys.stderr)
        return None
    first = datetime.datetime.fromtimestamp(ts[0], datetime.timezone.utc)
    last = datetime.datetime.fromtimestamp(ts[-1], datetime.timezone.utc)
    days = {datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date() for t in ts}
    print("    spans %s .. %s  (%d matches over %d distinct days)"
          % (first.date(), last.date(), len(ts), len(days)), file=sys.stderr)

    in_season = [t for t in ts
                 if datetime.datetime.fromtimestamp(t, datetime.timezone.utc) >= SEASON_START]
    pre_archive = [t for t in in_season
                   if datetime.datetime.fromtimestamp(t, datetime.timezone.utc).date()
                   < ARCHIVE_START]
    print("    %d matches since season start, %d of them before the archive began"
          % (len(in_season), len(pre_archive)), file=sys.stderr)

    scores = [r.get("player_score") for r in rows if r.get("player_score") is not None]
    if scores:
        print("    player_score: %.1f .. %.1f  (%d distinct)"
              % (min(scores), max(scores), len(set(scores))), file=sys.stderr)
    return {"days": days, "in_season": len(in_season), "pre_archive": len(pre_archive),
            "n": len(rows), "first": first.date()}


def main():
    ids = account_ids(N_ACCOUNTS)
    print("probing mmr-history for %d accounts (key: %s, %.0fs gap)"
          % (len(ids), "yes" if API_KEY else "no", GAP), file=sys.stderr)
    print("season start %s | archive began %s\n"
          % (SEASON_START.date(), ARCHIVE_START), file=sys.stderr)

    results = []
    for i, (aid, name, hero) in enumerate(ids):
        if i:
            print("  ... waiting %.0fs (MMR bucket %s/min)"
                  % (GAP, "25" if API_KEY else "5"), file=sys.stderr)
            time.sleep(GAP)
        r = examine(aid, name, hero)
        if r:
            results.append(r)
        print("", file=sys.stderr)

    print("=" * 60, file=sys.stderr)
    if not results:
        print("no usable history returned", file=sys.stderr)
        return
    pre = sum(r["pre_archive"] for r in results)
    earliest = min(r["first"] for r in results)
    print("VERDICT", file=sys.stderr)
    print("  earliest match seen: %s" % earliest, file=sys.stderr)
    print("  matches in the archive gap (season start .. %s): %d across %d accounts"
          % (ARCHIVE_START, pre, len(results)), file=sys.stderr)
    if pre:
        print("  >>> BACKFILL POSSIBLE for the missed days", file=sys.stderr)
    else:
        print("  >>> no pre-archive matches — nothing to backfill", file=sys.stderr)
    if all(len(r["days"]) > 1 for r in results):
        print("  >>> PER-MATCH DATED SERIES: a daily rating curve can be", file=sys.stderr)
        print("      reconstructed retroactively; no need to poll daily", file=sys.stderr)
    print("\n  cost: 1 call per account, %d used. At 5/min unkeyed, a full"
          % len(ids), file=sys.stderr)
    print("  100-player cohort would take ~20 min per region.", file=sys.stderr)


if __name__ == "__main__":
    main()
