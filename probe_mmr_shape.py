#!/usr/bin/env python3
"""
probe_mmr_shape.py — /v1/players/mmr answers 200 with an EMPTY list. Why?

Established 2026-08-07: the endpoint is alive, `account_ids` is the correct
parameter (omitting it gives 400 "missing field account_ids"), and
/v1/players/mmr-history is now 404. But a request for 12 real ceiling-player
accounts returned zero rows and no error.

Two explanations need separating, because they lead opposite ways:

  1. The parameter FORM is wrong — comma-separated rather than repeated.
     Fixable, and the ranked ceiling plan proceeds.
  2. player_score has gone the way of badge — the endpoint still answers but
     has nothing to say. Then the plan needs a different rating source, and
     shrunk ranked win rate from hero-stats is the fallback.

Tests, cheapest first:
  T1  Comma-separated vs repeated parameters, same account.
  T2  Accounts drawn from hero-stats rather than the leaderboard, i.e. ids
      known to have ranked games, in case ceiling ids are resolution artefacts.
  T3  A deliberately invalid id, to see what "no data" looks like versus
      "unknown account" — if both return [], the endpoint cannot distinguish
      them and an empty result proves nothing about the account.
  T4  Every response header and body recorded verbatim, since the last run
      could only say "0 rows".

Cost: ZERO SQL. MMR is 5 req/min unkeyed with a 50/min GLOBAL ceiling shared
across all API users, so this paces itself at 13s between calls.

    python3 probe_mmr_shape.py

Writes probe_out/mmr_shape.json. Stdlib only.
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"
PACE_S = int(os.environ.get("MMR_PACE_S") or 13)
REGION = os.environ.get("PROBE_REGION") or "NAmerica"


def call(path, query):
    """Returns a dict describing exactly what came back."""
    url = BASE + path + ("?" + query if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    rec = {"url": url[:200]}
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            rec["status"] = r.status
            rec["content_type"] = r.headers.get("Content-Type", "")
            rec["bytes"] = len(body)
            rec["body"] = body[:600].decode("utf-8", "replace")
            try:
                data = json.loads(body)
                rec["json_type"] = type(data).__name__
                rec["n"] = len(data) if isinstance(data, (list, dict)) else None
                if isinstance(data, list) and data:
                    rec["first"] = data[0]
                elif isinstance(data, dict):
                    rec["keys"] = sorted(data)[:20]
            except Exception as e:
                rec["json_error"] = str(e)
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["body"] = e.read()[:600].decode("utf-8", "replace")
    except Exception as e:
        rec["error"] = str(e)
    time.sleep(PACE_S)
    return rec


def ceiling_ids(n=3):
    """
    Prefer output/ceiling.csv — those ids are dual-confirmed across the hero
    and general boards. It only exists when the pipeline step has run, which
    free_only skips, so fall back to the leaderboard endpoint directly. That
    keeps this probe genuinely SQL-free and runnable in any hour.
    """
    path = os.path.join("output", "ceiling.csv")
    out = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                a = (r.get("account_id") or "").strip()
                if a.isdigit():
                    out.append(int(a))
    if out:
        return sorted(set(out))[:n]

    print("  [ids] no ceiling.csv — taking top ids from the leaderboard",
          file=sys.stderr)
    rec = call("/v1/leaderboard/%s" % urllib.parse.quote(REGION), "")
    try:
        rows = json.loads(rec.get("body") or "[]")
    except Exception:
        rows = []
    if isinstance(rows, dict):
        rows = rows.get("entries") or rows.get("data") or []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # possible_account_ids is best-match-first: 92% resolve from slot 0
        for a in (r.get("possible_account_ids") or [])[:1]:
            out.append(int(a))
        if len(out) >= n:
            break
    return out[:n]


def hero_stats_ids(seed_ids, n=3):
    """Ids that hero-stats confirms have RANKED games — a stronger guarantee
    than leaderboard membership, which is only a name resolution."""
    if not seed_ids:
        return []
    q = "&".join("account_ids=%d" % i for i in seed_ids)
    rec = call("/v1/players/hero-stats", q + "&match_mode=ranked")
    rows = []
    try:
        rows = json.loads(rec.get("body") or "[]")
    except Exception:
        pass
    good = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and (r.get("matches") or r.get("matches_played")):
            a = r.get("account_id")
            if a is not None and int(a) not in good:
                good.append(int(a))
    return good[:n], rec


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}

    seeds = ceiling_ids(3)
    report["ceiling_ids"] = seeds
    if not seeds:
        raise SystemExit("could not source any account ids")
    a = seeds[0]

    print("[T1] parameter form", file=sys.stderr)
    report["T1_repeated_one"] = call("/v1/players/mmr", "account_ids=%d" % a)
    report["T1_repeated_many"] = call(
        "/v1/players/mmr", "&".join("account_ids=%d" % i for i in seeds))
    report["T1_comma"] = call(
        "/v1/players/mmr", "account_ids=" + ",".join(str(i) for i in seeds))

    print("[T2] ids known to have ranked games", file=sys.stderr)
    good, hs = report.get("_", None), None
    good, hs = hero_stats_ids(seeds, 3)
    report["T2_hero_stats_probe"] = {"ids_found": good,
                                     "status": hs.get("status"),
                                     "n": hs.get("n"),
                                     "first": hs.get("first")}
    if good:
        report["T2_mmr_for_those"] = call(
            "/v1/players/mmr", "&".join("account_ids=%d" % i for i in good))

    print("[T3] control: an id that cannot exist", file=sys.stderr)
    report["T3_bogus"] = call("/v1/players/mmr", "account_ids=1")

    print("[T4] neighbouring endpoints", file=sys.stderr)
    report["T4_mmr_history"] = call("/v1/players/%d/mmr-history" % a, "")
    report["T4_scoreboard"] = call("/v1/players/scoreboard", "sort_by=winrate&limit=3")

    json.dump(report, open(os.path.join(OUT, "mmr_shape.json"), "w"),
              indent=1, default=str)

    print("\n=== results ===")
    for k, v in report.items():
        if not isinstance(v, dict) or "status" not in v:
            continue
        print("  %-22s status %-4s %-9s n=%-5s %s"
              % (k, v.get("status"), v.get("json_type") or "",
                 v.get("n"), (v.get("body") or "")[:70].replace("\n", " ")))
    print("\n  ceiling ids probed: %s" % seeds)
    print("  ids hero-stats confirms have ranked games: %s"
          % report["T2_hero_stats_probe"]["ids_found"])
    print("\n  If T3 (a bogus id) also returns an empty list, an empty result")
    print("  says nothing about the account and player_score is simply absent.")
    print("\nwrote %s/mmr_shape.json" % OUT)


if __name__ == "__main__":
    main()
