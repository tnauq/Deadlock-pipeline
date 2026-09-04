#!/usr/bin/env python3
"""
Probe /v1/players/scoreboard for use as an IDENTITY VERIFIER.

    python3 probe_scoreboard.py

Writes ./output/probe_scoreboard.json  (paste into PROBES.md)

--------------------------------------------------------------------------
WHY THIS ENDPOINT, AGAIN

It was probed 2026-08-07 and filed as "NOT a ceiling source": sort_by=winrate
and sort_by=wins both 500, match_mode is accepted and IGNORED, region is
ignored. Every one of those failures is about RANKING. None of them touches
the question asked here, which is only:

    does account 105508858 appear among the people who play Bebop?

That matters because scoreboard is the ONLY endpoint that publishes a real
`account_id`. The leaderboards publish display names plus deadlock-api's fuzzy
possible_account_ids, which put the wrong player in 110 of 371 slots when used
alone. A verifier built on scoreboard shares NO failure mode with the thing it
is verifying — that independence is the entire point.

--------------------------------------------------------------------------
THREE QUESTIONS, AND ONLY ONE OF THEM IS INTERESTING

Q1. DOES hero_id ACTUALLY FILTER?  <- the one that decides everything
    PROBES records that the per-hero path TAKES hero_id. It does NOT record
    that hero_id was verified to CHANGE the results. That is exactly the trap
    match_mode set on this same endpoint: accepted, 200, silently ignored,
    caught only by comparing bodies. If hero_id is ignored, every hero returns
    one global career board and the verifier is worthless.

    THE CONTROL: fetch two very different heroes and compare account-id sets.
    Near-identical sets mean the parameter is decorative. A high overlap is
    also possible for real reasons (both boards are dominated by the same
    high-volume accounts), so the test reports Jaccard AND set sizes rather
    than a bare yes/no, and an invalid hero_id is fetched as a second control.

Q2. WHICH RATE-LIMIT BUCKET?
    Decides whether 38 hero calls per run are free (100 req/s bucket, like the
    leaderboards) or catastrophic (20/HOUR SQL bucket). Measured by timing a
    short burst and watching for 429.

Q3. DOES min_matches HIDE THE ACCOUNTS WE CARE ABOUT?
    Default is 20. The accounts most in need of verification are the
    low-volume ones — Snakes had 8 games. If min_matches cannot go below 20,
    the verifier is blind to exactly the cases it was built for, which is a
    limitation to record rather than a failure.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT_DIR = os.environ.get("OUT_DIR") or "output"

HERO_A = int(os.environ.get("HERO_A") or 1)
HERO_B = int(os.environ.get("HERO_B") or 15)
LIMIT = int(os.environ.get("LIMIT") or 500)
BURST = int(os.environ.get("BURST") or 6)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return json.loads(body.decode("utf-8")), round(time.time() - t0, 2)


def try_get(url):
    """(payload, seconds, error) — never raises, so one 400 cannot end the run."""
    try:
        payload, secs = get(url)
        return payload, secs, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return None, None, "HTTP %s %s" % (e.code, detail)
    except Exception as e:
        return None, None, str(e)


def ids_of(payload):
    rows = payload.get("entries", payload) if isinstance(payload, dict) else payload
    out = []
    for r in rows or []:
        a = r.get("account_id") if isinstance(r, dict) else None
        if a is not None:
            out.append(int(a))
    return out


def hero_url(hero_id, limit=None, min_matches=None):
    q = ["limit=%d" % (limit or LIMIT)]
    if min_matches is not None:
        q.append("min_matches=%d" % min_matches)
    return "%s/v1/players/scoreboard/%d?%s" % (BASE, hero_id, "&".join(q))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    log = {"probed": time.strftime("%Y-%m-%d"), "base": BASE,
           "hero_a": HERO_A, "hero_b": HERO_B, "limit": LIMIT, "questions": {}}

    # ---- path discovery ---------------------------------------------------
    # PROBES says "two paths exist; the second takes hero_id" without naming
    # them, so try the plausible shapes rather than assuming one.
    # TEMPLATES, not string substitution. Swapping hero ids with
    # url.replace("1", "15", 1) hits the "1" in "/v1/" — caught in testing,
    # and it would have silently probed a nonexistent path.
    templates = ["%s/v1/players/scoreboard/{hero}?limit=%d" % (BASE, LIMIT),
                 "%s/v1/players/scoreboard?hero_id={hero}&limit=%d" % (BASE, LIMIT),
                 "%s/v1/players/hero-scoreboard/{hero}?limit=%d" % (BASE, LIMIT)]
    working, a_payload = None, None
    for tpl in templates:
        u = tpl.format(hero=HERO_A)
        payload, secs, err = try_get(u)
        print("  [path] %s -> %s" % (u.replace(BASE, ""), err or "OK %ss" % secs),
              file=sys.stderr)
        if payload is not None and ids_of(payload):
            working, a_payload = tpl, payload
            break
    log["working_path"] = working
    if not working:
        log["verdict"] = ("No per-hero scoreboard path returned account ids. "
                          "The verifier cannot be built on this endpoint.")
        _dump(log)
        return

    # ---- Q1: does hero_id filter? ----------------------------------------
    a_ids = ids_of(a_payload)
    b_payload, _s, b_err = try_get(working.format(hero=HERO_B))
    b_ids = ids_of(b_payload) if b_payload else []
    # a hero id that cannot exist: if it returns a full board, the parameter is
    # not being applied at all
    bad_payload, _s, bad_err = try_get(working.format(hero=99999))
    bad_ids = ids_of(bad_payload) if bad_payload else []

    sa, sb = set(a_ids), set(b_ids)
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    jac = round(inter / union, 4)
    identical = a_ids == b_ids

    q1 = {"hero_a_rows": len(a_ids), "hero_b_rows": len(b_ids),
          "distinct_a": len(sa), "distinct_b": len(sb),
          "overlap": inter, "jaccard": jac,
          "byte_identical_order": identical,
          "invalid_hero_rows": len(bad_ids), "invalid_hero_error": bad_err,
          "hero_b_error": b_err}
    if identical or jac > 0.95:
        q1["verdict"] = ("hero_id IS IGNORED — two different heroes return the "
                         "same accounts. The verifier is dead; do not retry.")
    elif bad_ids and len(bad_ids) > 0.5 * len(a_ids):
        q1["verdict"] = ("hero_id is not validated — an impossible hero id "
                         "returned a full board, so filtering cannot be trusted.")
    elif jac < 0.5:
        q1["verdict"] = ("hero_id FILTERS. Distinct account sets per hero, and "
                         "an invalid id returns %d rows. Usable as an "
                         "independent identity channel." % len(bad_ids))
    else:
        q1["verdict"] = ("Ambiguous: overlap %.2f. High-volume accounts may "
                         "legitimately appear on both boards. Repeat with two "
                         "heroes of very different popularity before trusting "
                         "it." % jac)
    log["questions"]["q1_hero_id_filters"] = q1
    print("  [q1] %s" % q1["verdict"], file=sys.stderr)

    # ---- Q2: which rate-limit bucket? ------------------------------------
    # The leaderboards are 100 req/s and effectively free; /v1/sql is 20/HOUR.
    # 38 hero calls per run is nothing against the first and fatal against the
    # second, so this decides whether the verifier can run every time.
    t0, ok, first_429 = time.time(), 0, None
    for i in range(BURST):
        _p, _s, err = try_get(working.format(hero=HERO_A))
        if err and "429" in err:
            first_429 = i + 1
            break
        ok += 1
    elapsed = round(time.time() - t0, 2)
    q2 = {"burst": BURST, "succeeded": ok, "seconds": elapsed,
          "first_429_at_call": first_429,
          "observed_rate_per_s": round(ok / elapsed, 2) if elapsed else None}
    q2["verdict"] = ("Fast bucket: %d calls in %ss with no 429. 38 hero calls "
                     "per run is affordable." % (ok, elapsed)) if not first_429 \
        else ("Rate limited at call %d. Budget hero calls and cache per hero."
              % first_429)
    log["questions"]["q2_rate_limit"] = q2
    print("  [q2] %s" % q2["verdict"], file=sys.stderr)

    # ---- Q3: can min_matches go below its default of 20? -----------------
    # The accounts most needing verification are low-volume ones — the id
    # claiming NA position 15 had EIGHT games. If the floor cannot drop, the
    # verifier is structurally blind to its main use case.
    q3 = {}
    for mm in (0, 1, 5, 20):
        payload, _s, err = try_get(hero_url(HERO_A, limit=LIMIT, min_matches=mm))
        ids = ids_of(payload) if payload else []
        lows = 0
        rows = payload.get("entries", payload) if isinstance(payload, dict) else payload
        for r in (rows or []):
            if isinstance(r, dict) and (r.get("matches") or 0) < 20:
                lows += 1
        q3["min_matches=%d" % mm] = {"rows": len(ids), "under_20_matches": lows,
                                     "error": err}
    baseline = q3.get("min_matches=20", {}).get("under_20_matches", 0)
    loose = q3.get("min_matches=0", {}).get("under_20_matches", 0)
    q3["verdict"] = ("min_matches LOWERS the floor (%d sub-20 accounts at 0 vs "
                     "%d at 20) — low-volume accounts are verifiable."
                     % (loose, baseline)) if loose > baseline else \
        ("min_matches does NOT expose sub-20 accounts. The verifier cannot "
         "confirm or deny low-volume ids, which are the ones most in doubt. "
         "Record as a limitation: absence from this board is then not evidence.")
    log["questions"]["q3_min_matches"] = q3
    print("  [q3] %s" % q3["verdict"], file=sys.stderr)

    log["verdict"] = q1["verdict"]
    _dump(log)


def _dump(log):
    path = os.path.join(OUT_DIR, "probe_scoreboard.json")
    json.dump(log, open(path, "w"), indent=2)
    print("\n  -> %s" % path, file=sys.stderr)
    print(json.dumps(log, indent=2)[:1400], file=sys.stderr)


if __name__ == "__main__":
    main()
