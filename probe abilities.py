#!/usr/bin/env python3
"""
probe_abilities.py — how are ability upgrade picks recorded?

The pipeline already reads `items.*` off match_player for shop builds and
throws away the ~95% of rows that are hero abilities (PROBES.md finding 6).
Those discarded rows are the ability-point data. This probe answers the one
question that decides what can be displayed:

    Q1  Do ability ids REPEAT within a single build (one row per point spent),
        or appear once with a level carried in a subcolumn?
    Q2  What does `items.upgrade_id` hold on an ability row? Is it the 1/2/5
        tier, a distinct upgrade entity id, or always zero?
    Q3  Is `items.game_time_s` populated on ability rows, i.e. can the ORDER
        points were spent in be recovered?
    Q4  Do the ids resolve against /v1/assets/items ability records, and do
        they line up with the hero's signature1-4?

Cost: ONE /v1/sql call (2/min, 20/hr unkeyed — see PROBES.md). Everything else
is assets. Reads output/ceiling.csv for real match ids if present, otherwise
takes a small recent ranked sample.

    python3 probe_abilities.py

Writes probe_out/abilities.json and prints a summary. Stdlib only.
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"
N_BUILDS = int(os.environ.get("ABIL_BUILDS") or 24)
LOOKBACK_DAYS = int(os.environ.get("PROBE_LOOKBACK_DAYS") or 3)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def sql(q):
    url = BASE + "/v1/sql?format=json&query=" + urllib.parse.quote(q)
    print("  [sql] %d chars (encoded %d)" % (len(q), len(url)), file=sys.stderr)
    try:
        rows = get(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:600]
        raise SystemExit("SQL failed (%s):\n%s" % (e.code, body))
    if isinstance(rows, dict):
        rows = rows.get("data", rows.get("rows", []))
    print("  [sql] %d rows" % len(rows), file=sys.stderr)
    return rows


# ---------------------------------------------------------------------------

# ARRAY JOIN keeps the subcolumns element-wise consistent (PROBES.md 3c: never
# walk these arrays positionally). upgrade_id and game_time_s are named
# explicitly because SELECT * omits ALIAS/MATERIALIZED columns.
Q = """
SELECT
    match_id,
    account_id,
    hero_id,
    iid  AS item_id,
    uid  AS upgrade_id,
    gts  AS game_time_s,
    sold AS sold_time_s
FROM match_player
ARRAY JOIN
    items.item_id     AS iid,
    items.upgrade_id  AS uid,
    items.game_time_s AS gts,
    items.sold_time_s AS sold
WHERE match_id IN ({mids})
ORDER BY match_id, account_id, gts
"""

Q_SAMPLE = """
SELECT match_id
FROM match_player
WHERE match_mode = 'Ranked'
  AND game_mode = 'Normal'
  AND start_time >= now() - INTERVAL {days} DAY
GROUP BY match_id
ORDER BY match_id DESC
LIMIT {n}
"""


def match_ids():
    """Prefer real ceiling matches; fall back to a recent ranked sample."""
    path = os.path.join("output", "ceiling.csv")
    ids = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                m = (r.get("last_match_id") or r.get("match_id") or "").strip()
                if m.isdigit():
                    ids.append(int(m))
    if ids:
        print("  [ids] %d from ceiling.csv" % len(ids), file=sys.stderr)
        return sorted(set(ids))[:N_BUILDS], "ceiling.csv"
    print("  [ids] no ceiling.csv - sampling recent ranked", file=sys.stderr)
    rows = sql(Q_SAMPLE.format(days=LOOKBACK_DAYS, n=N_BUILDS))
    return [int(r["match_id"]) for r in rows], "recent ranked sample"


def load_abilities():
    """ability id -> name, and class_name -> id, from /v1/assets/items."""
    raw = get(BASE + "/v1/assets/items")
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("items", []))
    by_id, by_class, upgrades = {}, {}, {}
    for it in raw:
        if it.get("type") != "ability":
            continue
        iid = it.get("id")
        if iid is None:
            continue
        by_id[int(iid)] = it.get("name") or it.get("class_name")
        if it.get("class_name"):
            by_class[it["class_name"]] = int(iid)
        upgrades[int(iid)] = len(it.get("upgrades") or [])
    return by_id, by_class, upgrades


def load_signatures():
    """hero id -> [signature ability class names], in slot order."""
    raw = get(BASE + "/v1/assets/heroes")
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("heroes", []))
    out = {}
    for h in raw:
        items = h.get("items") or {}
        sigs = [items.get("signature%d" % k) for k in (1, 2, 3, 4)]
        out[int(h["id"])] = [s for s in sigs if s]
    return out


# ---------------------------------------------------------------------------


def main():
    os.makedirs(OUT, exist_ok=True)
    abil_name, abil_by_class, abil_upgrades = load_abilities()
    sigs = load_signatures()
    print("  [assets] %d ability records, %d heroes with signatures"
          % (len(abil_name), len(sigs)), file=sys.stderr)

    mids, src = match_ids()
    if not mids:
        raise SystemExit("no match ids to probe")
    rows = sql(Q.format(mids=",".join(str(m) for m in mids)))
    if not rows:
        raise SystemExit("no item rows returned for those matches")

    builds = defaultdict(list)
    for r in rows:
        builds[(int(r["match_id"]), int(r["account_id"]))].append(r)

    report = {"source": src, "matches": len(mids), "builds": len(builds), "rows": len(rows)}

    # ---- Q1: do ability ids repeat within one build? -------------------
    reps, per_build_pts, ability_rows, shop_rows = Counter(), [], 0, 0
    max_rep = 0
    for key, rs in builds.items():
        seen = Counter()
        for r in rs:
            iid = int(r["item_id"])
            if iid in abil_name:
                ability_rows += 1
                seen[iid] += 1
            else:
                shop_rows += 1
        if seen:
            per_build_pts.append(sum(seen.values()))
            for iid, n in seen.items():
                reps[n] += 1
                max_rep = max(max_rep, n)

    report["ability_rows"] = ability_rows
    report["shop_rows"] = shop_rows
    report["repeats_per_ability_per_build"] = dict(sorted(reps.items()))
    report["max_repeat"] = max_rep
    report["points_per_build"] = {
        "min": min(per_build_pts) if per_build_pts else 0,
        "max": max(per_build_pts) if per_build_pts else 0,
        "mean": round(sum(per_build_pts) / len(per_build_pts), 1) if per_build_pts else 0,
    }

    # ---- Q2: what is upgrade_id on an ability row? ---------------------
    uid_vals, uid_by_rep = Counter(), defaultdict(Counter)
    for key, rs in builds.items():
        seen = Counter()
        for r in rs:
            iid = int(r["item_id"])
            if iid not in abil_name:
                continue
            seen[iid] += 1
            uid = r.get("upgrade_id")
            uid_vals[str(uid)] += 1
            uid_by_rep[seen[iid]][str(uid)] += 1
    report["upgrade_id_values"] = uid_vals.most_common(12)
    report["upgrade_id_by_occurrence"] = {k: dict(v) for k, v in sorted(uid_by_rep.items())}

    # ---- Q3: is game_time_s usable for ordering? -----------------------
    gts = [r.get("game_time_s") for r in rows if int(r["item_id"]) in abil_name]
    nums = [float(g) for g in gts if g not in (None, "")]
    report["game_time_s"] = {
        "populated": len(nums), "of": len(gts),
        "min": min(nums) if nums else None, "max": max(nums) if nums else None,
        "distinct": len(set(nums)),
    }

    # ---- Q4: do picks line up with signature1-4? -----------------------
    off_sig, in_sig, unknown = 0, 0, 0
    example = None
    for (mid, aid), rs in builds.items():
        hid = int(rs[0]["hero_id"])
        want = {abil_by_class.get(c) for c in sigs.get(hid, [])}
        order = []
        for r in rs:
            iid = int(r["item_id"])
            if iid not in abil_name:
                continue
            (in_sig, off_sig) = (in_sig + 1, off_sig) if iid in want else (in_sig, off_sig + 1)
            order.append({"ability": abil_name[iid], "t": r.get("game_time_s"),
                          "upgrade_id": r.get("upgrade_id"),
                          "signature": iid in want})
        if example is None and order:
            example = {"match_id": mid, "account_id": aid, "hero_id": hid,
                       "signatures": sigs.get(hid, []), "picks": order[:20]}
    report["picks_in_signature_set"] = in_sig
    report["picks_outside_signature_set"] = off_sig
    report["example_build"] = example

    json.dump(report, open(os.path.join(OUT, "abilities.json"), "w"), indent=1)

    # ---- summary -------------------------------------------------------
    r = report
    print("\n=== SOURCE ===")
    print("  %s | %d matches, %d builds, %d item rows" % (src, r["matches"], r["builds"], r["rows"]))
    print("  ability rows %d / shop rows %d" % (r["ability_rows"], r["shop_rows"]))

    print("\n=== Q1  do ability ids repeat within a build? ===")
    print("  occurrences of the same ability id:", r["repeats_per_ability_per_build"])
    print("  max repeat: %d   points per build: %s" % (r["max_repeat"], r["points_per_build"]))
    print("  -> repeats mean ONE ROW PER POINT; no repeats mean the level lives in a column")

    print("\n=== Q2  upgrade_id on ability rows ===")
    print("  values:", r["upgrade_id_values"])
    print("  by which occurrence it was:", r["upgrade_id_by_occurrence"])
    print("  -> if occurrence 1/2/3 map to distinct values, that IS the tier")

    print("\n=== Q3  game_time_s ===")
    print(" ", r["game_time_s"])
    print("  -> populated and distinct means pick ORDER is recoverable")

    print("\n=== Q4  signature match ===")
    print("  in signature1-4: %d | outside: %d" % (r["picks_in_signature_set"],
                                                   r["picks_outside_signature_set"]))
    if example:
        print("  example build %s (hero %s):" % (example["match_id"], example["hero_id"]))
        for p in example["picks"][:12]:
            print("    %-28s t=%-8s upgrade_id=%-6s sig=%s"
                  % (p["ability"], p["t"], p["upgrade_id"], p["signature"]))

    print("\nwrote %s/abilities.json" % OUT)


if __name__ == "__main__":
    main()
