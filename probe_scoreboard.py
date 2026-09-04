#!/usr/bin/env python3
"""
Probe /v1/analytics/scoreboards/players for use as an IDENTITY VERIFIER.

    python3 probe_scoreboard.py

Writes ./output/probe_scoreboard.json  (paste into PROBES.md)

--------------------------------------------------------------------------
PATH CORRECTION — 2026-09-04

PROBES records this endpoint as `/v1/players/scoreboard` (probed 2026-08-07).
IT HAS MOVED. The live OpenAPI spec puts it at
`/v1/analytics/scoreboards/players`, under the Analytics tag. A probe run on
2026-09-04 against the old location returned nothing on all three guessed
paths and was nearly recorded as "the verifier cannot be built on this
endpoint" — a false negative caused entirely by a stale path.

LESSON WORTH KEEPING: when a probe fails to find an endpoint at all, read
/openapi.json before concluding the capability is gone. A 404 says where the
endpoint ISN'T.

--------------------------------------------------------------------------
WHAT THE SPEC ALREADY ANSWERS — do not re-probe these

| Fact | Value |
|---|---|
| Rate limit | 200 req/min per IP, 400 with key, 2000/min global — SHARED across ALL analytics endpoints. Fast bucket, NOT the 20/hr SQL bucket. |
| `sort_by` | REQUIRED. Enum includes `winrate`, `wins`, `matches`, `rank`. |
| `hero_id` | Documented query param, int32. |
| `min_matches` | Default 20, MINIMUM 1 — low-volume accounts are reachable. |
| `limit` | Default 100, max 10,000. `start` gives an offset. |
| `account_ids` | Comma separated, MAX 1000 ITEMS. |
| `min_unix_timestamp` | Present, so the window can be scoped like hero-stats. |

Note `sort_by=winrate` is now IN THE ENUM. It returned 500 on 2026-08-07 and
was one of the two reasons this endpoint was rejected as a ceiling source.
Whether it now answers 200 is checked below but is NOT what this probe is for.

--------------------------------------------------------------------------
WHY VERIFY AT ALL

This is the only endpoint that publishes a real `account_id`. The leaderboards
publish display names plus deadlock-api's fuzzy `possible_account_ids`, which
put the wrong player in 110 of 371 slots when used alone. A verifier built
here shares NO failure mode with the thing it verifies — that independence is
the whole point.

--------------------------------------------------------------------------
THE TWO QUESTIONS A SPEC CANNOT ANSWER

Q1. IS `hero_id` APPLIED, or merely accepted?
    `match_mode` is documented on this same endpoint and was MEASURED being
    silently ignored on 2026-08-07 (an account with 7,651 matches came back
    inside an 8-day window). Documentation is not behaviour here. Control:
    fetch two very different heroes and compare account-id sets, plus an
    impossible hero id.

Q2. Does `account_ids` FILTER, and is it the cheaper shape?
    If it works, the verifier inverts: instead of pulling thousands of players
    per hero and intersecting, pass the candidate ids AND hero_id and see
    which come back. One call per hero per 1,000 candidates. This is the
    design that matters, so it gets its own control — a request for ids that
    cannot exist must come back empty.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
PATH = "/v1/analytics/scoreboards/players"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT_DIR = os.environ.get("OUT_DIR") or "output"

HERO_A = int(os.environ.get("HERO_A") or 1)
HERO_B = int(os.environ.get("HERO_B") or 15)
LIMIT = int(os.environ.get("LIMIT") or 500)
SORT_BY = os.environ.get("SORT_BY") or "matches"
DAYS = int(os.environ.get("DAYS") or 14)


def url(**params):
    params.setdefault("sort_by", SORT_BY)      # REQUIRED by the spec
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(v)))
                 for k, v in params.items() if v is not None)
    return BASE + PATH + "?" + q


def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return None, "HTTP %s %s" % (e.code, body)
    except Exception as e:
        return None, str(e)


def ids_of(payload):
    rows = payload.get("entries", payload) if isinstance(payload, dict) else payload
    return [int(r["account_id"]) for r in (rows or [])
            if isinstance(r, dict) and r.get("account_id") is not None]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    log = {"probed": time.strftime("%Y-%m-%d"), "path": PATH,
           "sort_by": SORT_BY, "hero_a": HERO_A, "hero_b": HERO_B,
           "note": "path corrected from /v1/players/scoreboard", "questions": {}}

    # ---- baseline: does the path work at all? -----------------------------
    base_payload, err = get(url(limit=LIMIT))
    if base_payload is None:
        log["verdict"] = "Baseline call failed: %s" % err
        return _dump(log)
    base_ids = ids_of(base_payload)
    log["baseline_rows"] = len(base_ids)
    print("  [base] %d rows, %d with account_id" % (len(base_payload or []),
                                                    len(base_ids)), file=sys.stderr)
    if not base_ids:
        log["verdict"] = ("Endpoint responds but returns no account_id. "
                          "Verifier cannot be built here.")
        return _dump(log)

    # ---- Q1: is hero_id applied? -----------------------------------------
    a_payload, a_err = get(url(hero_id=HERO_A, limit=LIMIT))
    b_payload, b_err = get(url(hero_id=HERO_B, limit=LIMIT))
    bad_payload, bad_err = get(url(hero_id=99999, limit=LIMIT))
    a, b = set(ids_of(a_payload or [])), set(ids_of(b_payload or []))
    bad = ids_of(bad_payload or [])
    jac = round(len(a & b) / (len(a | b) or 1), 4)
    q1 = {"hero_a_ids": len(a), "hero_b_ids": len(b), "overlap": len(a & b),
          "jaccard": jac, "invalid_hero_rows": len(bad),
          "invalid_hero_error": bad_err, "errors": [e for e in (a_err, b_err) if e],
          "differs_from_unfiltered": sorted(a) != sorted(set(base_ids))}
    if not a or not b:
        q1["verdict"] = "hero_id returned nothing — check the hero ids used."
    elif jac > 0.95:
        q1["verdict"] = ("hero_id IS IGNORED — two heroes return the same "
                         "accounts. Verifier dead; record and stop.")
    elif len(bad) > 0.5 * len(a):
        q1["verdict"] = ("hero_id not validated — an impossible hero id "
                         "returned a full board. Filtering untrustworthy.")
    elif jac < 0.5:
        q1["verdict"] = ("hero_id FILTERS (jaccard %.3f, invalid id -> %d rows). "
                         "Usable as an independent identity channel." % (jac, len(bad)))
    else:
        q1["verdict"] = ("Ambiguous at jaccard %.3f — retry with heroes of very "
                         "different popularity." % jac)
    log["questions"]["q1_hero_id_applied"] = q1
    print("  [q1] %s" % q1["verdict"], file=sys.stderr)

    # ---- Q2: does account_ids filter? ------------------------------------
    # The design that matters. If this works, verification is one call per
    # hero per 1,000 candidates instead of pulling whole boards.
    sample = sorted(a)[:5] if a else base_ids[:5]
    got_payload, got_err = get(url(hero_id=HERO_A, account_ids=",".join(
        str(i) for i in sample), limit=LIMIT))
    got = ids_of(got_payload or [])
    # control: ids that cannot exist must come back empty, or the parameter
    # is being ignored the way match_mode is
    ghost_payload, ghost_err = get(url(hero_id=HERO_A, account_ids="1,2,3",
                                       limit=LIMIT))
    ghost = ids_of(ghost_payload or [])
    q2 = {"asked_for": sample, "returned": len(got),
          "returned_only_asked": bool(got) and set(got) <= set(sample),
          "impossible_ids_returned": len(ghost),
          "errors": [e for e in (got_err, ghost_err) if e]}
    if got and set(got) <= set(sample) and not ghost:
        q2["verdict"] = ("account_ids FILTERS and the control is clean. "
                         "Verify with one call per hero per 1,000 candidates.")
    elif ghost:
        q2["verdict"] = ("account_ids IGNORED — impossible ids returned %d rows. "
                         "Fall back to pulling boards and intersecting."
                         % len(ghost))
    else:
        q2["verdict"] = ("Inconclusive: asked for %d ids, got %d back."
                         % (len(sample), len(got)))
    log["questions"]["q2_account_ids_filter"] = q2
    print("  [q2] %s" % q2["verdict"], file=sys.stderr)

    # ---- Q3: has sort_by=winrate been fixed? -----------------------------
    # Not what this probe is for, but it is one call and it was one of the two
    # reasons the endpoint was rejected as a CEILING source on 2026-08-07.
    wr_payload, wr_err = get(url(hero_id=HERO_A, sort_by="winrate", limit=50))
    log["questions"]["q3_sort_by_winrate"] = {
        "error": wr_err, "rows": len(ids_of(wr_payload or [])),
        "verdict": ("still broken: %s" % wr_err) if wr_err else
                   "sort_by=winrate now returns 200 — the 2026-08-07 500 is fixed. "
                   "Revisit as a ceiling source separately."}
    print("  [q3] %s" % log["questions"]["q3_sort_by_winrate"]["verdict"],
          file=sys.stderr)

    log["verdict"] = q1["verdict"]
    _dump(log)


def _dump(log):
    path = os.path.join(OUT_DIR, "probe_scoreboard.json")
    json.dump(log, open(path, "w"), indent=2)
    print("\n  -> %s" % path, file=sys.stderr)
    print(json.dumps(log, indent=2)[:1600], file=sys.stderr)


if __name__ == "__main__":
    main()
