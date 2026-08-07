#!/usr/bin/env python3
"""
probe_mmr_pool.py — can a daily ranked ceiling be built, and can lobby-mates
                    fix thin build pools?

Two questions, one run.

A. BATCH SIZE. /v1/players/mmr returns player_score, which is ranked-aware and
   opponent-derived — a truer ceiling than win rate, which cannot see who you
   beat. It is 5 req/min unkeyed (25 keyed) with a 50/min GLOBAL ceiling shared
   with every other API user. Whether a daily rating pass is viable depends
   entirely on how many ids fit in one call, which nobody here has measured.

B. LOBBY EXPANSION. Valve's boards gate eligibility, and thin boards (Grey
   Talon 13 entries) cannot fill a 20-build sample. A ceiling player's ranked
   lobby holds 11 other players matched against them, who are plausibly of
   similar standing. If that holds, rosters are a source of extra candidates
   that needs no leaderboard at all. If it does not, the idea dies here rather
   than quietly widening the cohort.

   This is a CLAIM TO TEST, not an assumption: the probe scores the lobby-mates
   and reports how their ratings sit against the seed's, and how many clear the
   top-N threshold.

Cost: ~2 SQL calls (rosters) plus MMR calls, which are a separate bucket.
Budget the SQL against the 20/hour cap before running alongside the pipeline.

    python3 probe_mmr_pool.py

Writes probe_out/mmr_pool.json. Stdlib only.
"""

import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"
REGION = os.environ.get("PROBE_REGION") or "NAmerica"
SEEDS = int(os.environ.get("PROBE_SEEDS") or 12)      # ceiling players to expand from
LOOKBACK_DAYS = int(os.environ.get("PROBE_LOOKBACK_DAYS") or 7)
BATCH_STEPS = [1, 10, 20, 50, 100, 200, 500]


def get(path, params=None, pairs=None):
    url = BASE + path
    qs = []
    if params:
        qs.append(urllib.parse.urlencode(params))
    if pairs:
        qs.append("&".join("%s=%s" % (k, v) for k, v in pairs))
    if qs:
        url += "?" + "&".join(qs)
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8")), len(url)


def try_get(path, params=None, pairs=None):
    try:
        payload, n = get(path, params, pairs)
        return payload, n, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:160].replace("\n", " ")
        return None, 0, "HTTP %d %s" % (e.code, body)
    except Exception as e:
        return None, 0, str(e)


def sql(q, label="", tries=4):
    """
    Retries a 429 honouring next_request_in. The pipeline step runs immediately
    before this probe and leaves the 2/min IP bucket empty, so the first call
    here reliably 429s — exponential backoff from 1s is far too short, the body
    says exactly how long to wait.
    """
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)
    print("  [sql] %s (%d char url)" % (label, len(url)), file=sys.stderr)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "probe/1.0"}),
                    timeout=180) as r:
                rows = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < tries - 1:
                wait = 35
                try:
                    wait = int(json.loads(body)["error"]["quota"].get("period", 60))
                    wait = int(json.loads(body)["error"].get("next_request_in", wait)) + 2
                except Exception:
                    pass
                print("  [sql] 429, waiting %ds" % wait, file=sys.stderr)
                time.sleep(wait)
                continue
            raise SystemExit("SQL failed (%s): %s" % (e.code, body[:400]))
    else:
        raise SystemExit("SQL still rate limited after %d tries" % tries)
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] %d rows" % len(rows), file=sys.stderr)
    return rows


# The first attempt returned 200 with ZERO rows for a single id, which means
# the parameter name or the response shape is wrong — not that the batch was
# too large. Try the plausible spellings and report what each gives back
# rather than parsing blind and calling it truncation.
MMR_PATHS = [
    ("/v1/players/mmr", "account_ids"),
    ("/v1/players/mmr", "account_id"),
    ("/v1/players/mmr-history", "account_ids"),
]
_mmr_shape = {"path": None, "param": None, "sample": None}


def _rows_from(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "players", "results", "mmr"):
            if isinstance(payload.get(k), list):
                return payload[k]
        # a bare object keyed by account id is also plausible
        if payload and all(str(k).isdigit() for k in payload):
            return [{"account_id": k, **(v if isinstance(v, dict) else {"value": v})}
                    for k, v in payload.items()]
    return []


def _score_of(r):
    for k in ("player_score", "score", "rank", "mmr"):
        if isinstance(r.get(k), (int, float)):
            return r[k]
    return None


def discover_mmr_shape(one_id):
    """Find the working path+param once, and keep a sample response."""
    for path, param in MMR_PATHS:
        payload, url_len, err = try_get(path, pairs=[(param, str(one_id))])
        rows = _rows_from(payload)
        print("   probe %-26s %-12s -> %s rows %s"
              % (path, param, len(rows), err or ""), file=sys.stderr)
        if rows:
            _mmr_shape.update({"path": path, "param": param, "sample": rows[0]})
            return True
        if payload is not None and not rows:
            _mmr_shape["sample"] = payload if not isinstance(payload, list) else None
        time.sleep(13)
    return False


def mmr_for(ids):
    """One batched call. Returns {account_id: player_score}."""
    if not _mmr_shape["path"]:
        return {}, 0, "mmr shape unknown"
    pairs = [(_mmr_shape["param"], str(i)) for i in ids]
    payload, url_len, err = try_get(_mmr_shape["path"], pairs=pairs)
    out = {}
    for r in _rows_from(payload):
        if isinstance(r, dict) and r.get("account_id") is not None:
            out[int(r["account_id"])] = _score_of(r)
    return out, url_len, err


def seed_ids():
    """Ceiling accounts from output/ceiling.csv, this region only."""
    path = os.path.join("output", "ceiling.csv")
    if not os.path.exists(path):
        return []
    ids = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("region") != REGION:
                continue
            a = (r.get("account_id") or "").strip()
            if a.isdigit():
                ids.append(int(a))
    return sorted(set(ids))


Q_ROSTER = """
SELECT match_id, account_id, hero_id
FROM match_player
WHERE match_id IN (
    SELECT match_id FROM match_player
    WHERE account_id IN ({seeds})
      AND match_mode = 'Ranked' AND game_mode = 'Normal'
      AND start_time >= now() - INTERVAL {days} DAY
)
"""


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"region": REGION}

    seeds = seed_ids()
    if not seeds:
        raise SystemExit("no ceiling.csv for %s — run the pipeline first" % REGION)
    seeds = seeds[:SEEDS]
    report["seeds"] = len(seeds)
    print("[1/3] %d seed accounts" % len(seeds), file=sys.stderr)

    # ---- A. how many ids fit in one /v1/players/mmr call? ---------------
    print("[2/3] mmr batch size", file=sys.stderr)
    if not discover_mmr_shape(seeds[0]):
        print("   NO working mmr path/param found — recording the response and "
              "skipping part A", file=sys.stderr)
    report["mmr_shape"] = _mmr_shape
    # pad the seed list by repeating it, so a large batch can be attempted
    pool = (seeds * 200)[:max(BATCH_STEPS)]
    batch = {}
    best = 0
    for n in (BATCH_STEPS if _mmr_shape["path"] else []):
        ids = pool[:n]
        got, url_len, err = mmr_for(ids)
        distinct = len(set(ids))
        batch[str(n)] = {"requested": n, "distinct_requested": distinct,
                         "returned": len(got), "url_chars": url_len, "error": err}
        print("   %4d ids -> %3d rows, url %d %s"
              % (n, len(got), url_len, err or ""), file=sys.stderr)
        if err:
            break
        if len(got) >= distinct:
            best = n
        else:
            batch[str(n)]["truncated"] = True
            break
        time.sleep(13)          # 5 req/min unkeyed; stay well inside it
    report["batch"] = batch
    report["largest_clean_batch"] = best

    # ---- B. are lobby-mates comparable to the seed? ---------------------
    print("[3/3] lobby expansion", file=sys.stderr)
    time.sleep(35)          # let the 2/min IP bucket refill after the pipeline
    rows = sql(Q_ROSTER.format(seeds=",".join(str(s) for s in seeds),
                               days=LOOKBACK_DAYS), "rosters")
    mates = Counter()
    per_match = {}
    for r in rows:
        aid, mid = int(r["account_id"]), int(r["match_id"])
        per_match.setdefault(mid, set()).add(aid)
        if aid not in seeds:
            mates[aid] += 1
    report["matches_seen"] = len(per_match)
    report["distinct_lobby_mates"] = len(mates)
    report["mates_seen_more_than_once"] = sum(1 for v in mates.values() if v > 1)

    # score the seeds and a sample of mates with the same endpoint
    sample = [a for a, _ in mates.most_common(min(len(mates), max(best, 20) * 2))]
    seed_scores, _, e1 = mmr_for(seeds)
    time.sleep(13)
    mate_scores = {}
    step = max(best, 20)
    for i in range(0, min(len(sample), step * 3), step):
        got, _, err = mmr_for(sample[i:i + step])
        mate_scores.update(got)
        if err:
            break
        time.sleep(13)

    sv = [v for v in seed_scores.values() if isinstance(v, (int, float))]
    mv = [v for v in mate_scores.values() if isinstance(v, (int, float))]
    report["seed_scores"] = {"n": len(sv),
                             "min": min(sv) if sv else None,
                             "median": statistics.median(sv) if sv else None,
                             "max": max(sv) if sv else None}
    report["mate_scores"] = {"n": len(mv),
                             "min": min(mv) if mv else None,
                             "median": statistics.median(mv) if mv else None,
                             "max": max(mv) if mv else None}
    if sv and mv:
        floor = min(sv)
        report["mates_at_or_above_seed_floor"] = sum(1 for v in mv if v >= floor)
        report["mates_within_2_of_seed_median"] = sum(
            1 for v in mv if abs(v - statistics.median(sv)) <= 2.0)
    report["mate_score_missing"] = len(sample) - len(mate_scores)

    json.dump(report, open(os.path.join(OUT, "mmr_pool.json"), "w"), indent=1)

    print("\n=== A  /v1/players/mmr batching ===")
    print("  working shape: path=%s param=%s" % (_mmr_shape["path"], _mmr_shape["param"]))
    print("  sample row: %s" % json.dumps(_mmr_shape["sample"])[:300])
    for k, v in batch.items():
        print("  %4s ids -> %3d rows  url %5d  %s%s"
              % (k, v["returned"], v["url_chars"],
                 "TRUNCATED " if v.get("truncated") else "", v["error"] or ""))
    print("  largest clean batch: %d" % best)
    if best:
        calls = -(-3197 // best)
        print("  -> 3,197 board ids would take %d calls, ~%.0f min at 5/min unkeyed"
              % (calls, calls / 5.0))

    print("\n=== B  lobby expansion ===")
    print("  %d seeds -> %d matches -> %d distinct lobby-mates (%d seen more than once)"
          % (len(seeds), report["matches_seen"], report["distinct_lobby_mates"],
             report["mates_seen_more_than_once"]))
    print("  seed player_score : %s" % report["seed_scores"])
    print("  mate player_score : %s" % report["mate_scores"])
    if sv and mv:
        print("  mates at or above the LOWEST seed score: %d of %d"
              % (report["mates_at_or_above_seed_floor"], len(mv)))
        print("  mates within 2.0 of the seed median:     %d of %d"
              % (report["mates_within_2_of_seed_median"], len(mv)))
    print("  mates with no score returned: %d" % report["mate_score_missing"])
    print("\n  -> tight clustering supports using lobby-mates as extra candidates;")
    print("     a wide spread means the lobby is NOT a comparable population.")
    print("\nwrote %s/mmr_pool.json" % OUT)


if __name__ == "__main__":
    main()
