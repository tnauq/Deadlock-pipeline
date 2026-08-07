#!/usr/bin/env python3
"""
probe_scoreboard.py — is /v1/players/scoreboard a usable ranked ceiling source?

Where this came from. /v1/players/mmr and mmr-history now answer 200 with an
EMPTY list for every account, including an id that cannot exist (probed
2026-08-07) — so player_score is gone the way of badge and neither can rank
anyone. But /v1/players/scoreboard returned real rows in the same run:

    {"rank":0,"account_id":1775347295,"value":1.0,"matches":29}

Rank, a real account id, a value and a match count, with sort_by as a
parameter. If it can be filtered to ranked play and to a region, it is the
ceiling ordering we wanted, computed upstream rather than by us.

Questions:
  Q1  What does the OpenAPI spec allow — every parameter, and the full enum of
      sort_by values. Read it rather than guessing.
  Q2  Does it accept a ranked filter (match_mode / game_mode) and a region?
      Compare row sets: a parameter that changes nothing is being ignored.
  Q3  What does `value` mean per sort_by, and is there a minimum-matches floor?
      A sort by win rate with value 1.0 at 29 matches suggests no shrinkage,
      which would make the top of the board small-sample noise.
  Q4  Depth: how many rows can be pulled, and do our board accounts appear?

Cost: ZERO SQL. Reads output/ceiling.csv when present to check overlap, but
does not require it.

    python3 probe_scoreboard.py

Writes probe_out/scoreboard.json. Stdlib only.
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
PACE_S = int(os.environ.get("PROBE_PACE_S") or 2)
PATH = "/v1/players/scoreboard"


def fetch(path, query=""):
    """Returns (rows_or_payload, record) — full body for data, summary for the
    report. Keeping these separate matters: a truncated body parsed as JSON is
    how the previous probe silently found nothing."""
    url = BASE + path + ("?" + query if query else "")
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    rec = {"query": query or "(none)"}
    payload = None
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            rec["status"] = r.status
            rec["bytes"] = len(body)
            payload = json.loads(body)
            rec["n"] = len(payload) if isinstance(payload, (list, dict)) else None
            if isinstance(payload, list) and payload:
                rec["first"] = payload[0]
                rec["last"] = payload[-1]
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["body"] = e.read()[:300].decode("utf-8", "replace").replace("\n", " ")
    except Exception as e:
        rec["error"] = str(e)
    time.sleep(PACE_S)
    return payload, rec


def ids_of(rows):
    return [int(r["account_id"]) for r in (rows or [])
            if isinstance(r, dict) and r.get("account_id") is not None]


def ceiling_ids():
    path = os.path.join("output", "ceiling.csv")
    out = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                a = (r.get("account_id") or "").strip()
                if a.isdigit():
                    out.add(int(a))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}

    # ---- Q1: the spec ---------------------------------------------------
    print("[1/4] spec", file=sys.stderr)
    spec, _ = fetch("/openapi.json")
    params, enums = [], {}
    if spec and isinstance(spec.get("paths"), dict):
        for p, ops in spec["paths"].items():
            if "scoreboard" not in p:
                continue
            for method, op in (ops or {}).items():
                if not isinstance(op, dict):
                    continue
                for prm in (op.get("parameters") or []):
                    sch = prm.get("schema") or {}
                    params.append({"path": p, "name": prm.get("name"),
                                   "in": prm.get("in"), "required": prm.get("required"),
                                   "type": sch.get("type"),
                                   "enum": sch.get("enum"),
                                   "default": sch.get("default")})
                    if sch.get("enum"):
                        enums[prm["name"]] = sch["enum"]
    report["spec_params"] = params
    report["spec_enums"] = enums

    # ---- Q2/Q3: sort keys and filters ----------------------------------
    print("[2/4] sort keys", file=sys.stderr)
    sorts = enums.get("sort_by") or ["winrate", "matches", "wins", "kills"]
    by_sort = {}
    for s in sorts[:12]:
        rows, rec = fetch(PATH, "sort_by=%s&limit=5" % urllib.parse.quote(str(s)))
        rec["ids"] = ids_of(rows)[:5]
        by_sort[str(s)] = rec
    report["sort_by"] = by_sort

    print("[3/4] filters", file=sys.stderr)
    base_rows, base_rec = fetch(PATH, "sort_by=%s&limit=20" % sorts[0])
    base_ids = ids_of(base_rows)
    report["baseline"] = base_rec
    filters = {}
    for q in ("match_mode=Ranked", "match_mode=ranked", "game_mode=Normal",
              "region=NAmerica", "min_matches=50", "hero_id=15"):
        rows, rec = fetch(PATH, "sort_by=%s&limit=20&%s" % (sorts[0], q))
        got = ids_of(rows)
        rec["identical_to_baseline"] = (got == base_ids)
        rec["ids_head"] = got[:4]
        filters[q] = rec
    report["filters"] = filters

    # ---- Q4: depth and overlap with our boards -------------------------
    print("[4/4] depth and overlap", file=sys.stderr)
    depth = {}
    for n in (100, 1000, 5000):
        rows, rec = fetch(PATH, "sort_by=%s&limit=%d" % (sorts[0], n))
        rec["distinct_ids"] = len(set(ids_of(rows)))
        depth[str(n)] = rec
    report["depth"] = depth

    rows, _ = fetch(PATH, "sort_by=%s&limit=1000" % sorts[0])
    board = ceiling_ids()
    sb = set(ids_of(rows))
    report["overlap"] = {
        "scoreboard_ids": len(sb),
        "ceiling_ids": len(board),
        "ceiling_present_on_scoreboard": len(board & sb) if board else None,
    }

    json.dump(report, open(os.path.join(OUT, "scoreboard.json"), "w"),
              indent=1, default=str)

    print("\n=== Q1  spec parameters ===")
    for p in params:
        print("  %-14s in=%-6s req=%-5s %-8s default=%s%s"
              % (p["name"], p["in"], p["required"], p["type"], p["default"],
                 "  enum=%s" % (p["enum"],) if p["enum"] else ""))
    if not params:
        print("  (no scoreboard path found in the spec)")

    print("\n=== Q2/Q3  sort_by ===")
    for s, rec in by_sort.items():
        f = rec.get("first") or {}
        print("  %-16s status %-4s n=%-4s first=%s"
              % (s, rec.get("status"), rec.get("n"), json.dumps(f)[:90]))

    print("\n=== filters (identical=True means the parameter is IGNORED) ===")
    for q, rec in filters.items():
        print("  %-22s status %-4s n=%-4s identical=%-5s %s"
              % (q, rec.get("status"), rec.get("n"),
                 rec.get("identical_to_baseline"), rec.get("body", "")[:50]))

    print("\n=== depth ===")
    for n, rec in depth.items():
        print("  limit=%-5s -> n=%-6s distinct=%-6s" % (n, rec.get("n"), rec.get("distinct_ids")))

    print("\n=== overlap with our ceiling accounts ===")
    print("  %s" % report["overlap"])
    print("\nwrote %s/scoreboard.json" % OUT)


if __name__ == "__main__":
    main()
