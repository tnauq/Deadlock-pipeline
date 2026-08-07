#!/usr/bin/env python3
"""
probe_leaderboard_ranked.py — is there a RANKED leaderboard, and what is on it?

The ceiling metric cross-references a hero board against the general board, and
only ~42% of hero-board entries locate on the general board (1,207 of 2,881 on
the 2026-08-07 run). That gap decides whether the metric is sound, so this
probe establishes what the boards actually contain.

deadlock-api computes its own MMR across ALL matches including Unranked
(PROBES.md, 2026-08-01), which is why the pipeline selects on its own shrunk
RANKED win rate instead. If the API now exposes ranked-filtered boards, the
cross-reference could be ranked-on-ranked and the workaround retired.

Questions:
  Q1  Does /v1/leaderboard accept a ranked filter? Read the OpenAPI spec
      rather than guessing at parameter names.
  Q2  What FIELDS does an entry carry, and are ranked_rank / ranked_subrank /
      badge_level populated yet? They were all empty at the badge collapse.
  Q3  How deep is the general board really — is ~1,000 a read cap or the whole
      board? Ask for more and see what comes back.
  Q4  If a ranked variant exists, how much does it change the picture: board
      size, and how many hero-board players locate on it versus the default.

Cost: ZERO SQL. The leaderboard endpoint is 100 req/s on a separate bucket and
assets are free, so this is safe in any hour.

    python3 probe_leaderboard_ranked.py

Writes probe_out/leaderboard_ranked.json and prints a summary. Stdlib only.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"
REGION = os.environ.get("PROBE_REGION") or "NAmerica"
# a few heroes spanning board sizes: Bebop is deep, Grey Talon is thin
HEROES = [int(h) for h in (os.environ.get("PROBE_HEROES") or "15,17,2,64").split(",")]

# Guesses only where the spec gives nothing. Each is tried and reported, so a
# 400 is a finding rather than a failure.
CANDIDATE_PARAMS = [
    {},
    {"match_mode": "Ranked"},
    {"match_mode": "ranked"},
    {"ranked": "true"},
    {"only_ranked": "true"},
    {"game_mode": "Normal"},
]


def get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def try_get(path, params=None):
    """Returns (payload, error). A 4xx is information, not a crash."""
    try:
        return get(path, params), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
        return None, "HTTP %s %s" % (e.code, body)
    except Exception as e:
        return None, str(e)


def entries(payload):
    if isinstance(payload, dict):
        for k in ("entries", "leaderboard", "data", "players"):
            if isinstance(payload.get(k), list):
                return payload[k]
        return []
    return payload if isinstance(payload, list) else []


def ids_of(rows):
    """account ids an entry claims, capped the way the pipeline caps them."""
    out = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("account_id") is not None:
            out.add(int(r["account_id"]))
        for a in (r.get("possible_account_ids") or [])[:2]:
            out.add(int(a))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"region": REGION, "heroes": HEROES}

    # ---- Q1: what does the spec say? -----------------------------------
    print("[1/4] OpenAPI spec", file=sys.stderr)
    spec, err = try_get("/openapi.json")
    lb_paths = {}
    if spec and isinstance(spec.get("paths"), dict):
        for path, ops in spec["paths"].items():
            if "leaderboard" not in path:
                continue
            for method, op in (ops or {}).items():
                if not isinstance(op, dict):
                    continue
                params = [{"name": p.get("name"), "in": p.get("in"),
                           "schema": (p.get("schema") or {}).get("type"),
                           "enum": (p.get("schema") or {}).get("enum")}
                          for p in (op.get("parameters") or [])]
                lb_paths[path] = {"method": method, "params": params,
                                  "summary": op.get("summary")}
    else:
        lb_paths = {"_error": err or "no paths in spec"}
    report["spec_leaderboard_paths"] = lb_paths

    # ---- Q2/Q3: the general board -------------------------------------
    print("[2/4] general board", file=sys.stderr)
    base_rows, err = try_get("/v1/leaderboard/%s" % urllib.parse.quote(REGION))
    base_rows = entries(base_rows) if base_rows else []
    report["general_default"] = {"n": len(base_rows), "error": err}

    fields = Counter()
    populated = Counter()
    for r in base_rows:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            fields[k] += 1
            if v not in (None, "", 0, [], {}):
                populated[k] += 1
    report["entry_fields"] = {k: {"present": fields[k], "populated": populated[k]}
                              for k in sorted(fields)}
    if base_rows:
        report["entry_sample"] = base_rows[0]

    # is the depth a cap? ask for far more than the default returned
    deeper = {}
    for n in (1000, 2000, 5000):
        got, e = try_get("/v1/leaderboard/%s" % urllib.parse.quote(REGION),
                         {"limit": n})
        deeper[str(n)] = {"n": len(entries(got)) if got else 0, "error": e}
    report["depth_probe"] = deeper

    # ---- Q4: does a ranked variant exist and does it change anything? ---
    print("[3/4] ranked variants", file=sys.stderr)
    variants = {}
    base_ids = ids_of(base_rows)
    for params in CANDIDATE_PARAMS:
        label = urllib.parse.urlencode(params) or "(none)"
        got, e = try_get("/v1/leaderboard/%s" % urllib.parse.quote(REGION), params)
        rows = entries(got) if got else []
        same = rows and len(rows) == len(base_rows) and \
            (rows[0].get("account_name") == base_rows[0].get("account_name")
             if base_rows and isinstance(rows[0], dict) else False)
        variants[label] = {"n": len(rows), "error": e,
                           "identical_to_default": bool(same)}
    report["general_variants"] = variants

    # ---- hero boards, and how much of each locates ---------------------
    print("[4/4] hero boards", file=sys.stderr)
    per_hero = {}
    for hid in HEROES:
        rows, e = try_get("/v1/leaderboard/%s/%d" % (urllib.parse.quote(REGION), hid))
        rows = entries(rows) if rows else []
        hid_ids = ids_of(rows)
        per_hero[str(hid)] = {
            "n": len(rows), "error": e,
            "distinct_ids": len(hid_ids),
            "located_on_general": len(hid_ids & base_ids),
            "located_pct": round(100.0 * len(hid_ids & base_ids) / max(len(hid_ids), 1), 1),
        }
    report["hero_boards"] = per_hero

    json.dump(report, open(os.path.join(OUT, "leaderboard_ranked.json"), "w"), indent=1)

    # ---- summary -------------------------------------------------------
    print("\n=== Q1  spec: leaderboard paths and parameters ===")
    for path, info in lb_paths.items():
        if path == "_error":
            print("  spec unavailable: %s" % info)
            continue
        print("  %s" % path)
        for p in info.get("params", []):
            print("      %-22s in=%-6s %s%s" % (p["name"], p["in"], p["schema"],
                  "  enum=%s" % (p["enum"],) if p["enum"] else ""))
        if not info.get("params"):
            print("      (no parameters)")

    print("\n=== Q2  entry fields on the general board (%d entries) ==="
          % report["general_default"]["n"])
    for k, v in report["entry_fields"].items():
        print("  %-24s populated %d/%d" % (k, v["populated"], v["present"]))

    print("\n=== Q3  depth ===")
    print("  default returned %d" % report["general_default"]["n"])
    for k, v in deeper.items():
        print("  limit=%-5s -> %-5d %s" % (k, v["n"], v["error"] or ""))
    print("  -> same count at every limit means the board IS that size, not capped")

    print("\n=== Q4  ranked variants ===")
    for label, v in variants.items():
        print("  %-24s n=%-5d identical=%-5s %s"
              % (label, v["n"], v["identical_to_default"], v["error"] or ""))

    print("\n=== hero boards vs general ===")
    for hid, v in per_hero.items():
        print("  hero %-4s n=%-4d ids=%-5d located %d (%.0f%%) %s"
              % (hid, v["n"], v["distinct_ids"], v["located_on_general"],
                 v["located_pct"], v["error"] or ""))

    print("\nwrote %s/leaderboard_ranked.json" % OUT)


if __name__ == "__main__":
    main()
