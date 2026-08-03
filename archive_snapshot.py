#!/usr/bin/env python3
"""
Archive one day's snapshot so change over time can be charted later.

    python3 archive_snapshot.py

Reads  ./output/ceiling.csv, ./output/item_frequency.csv, ./output/tierlist.csv
Writes ./docs/archive/YYYY-MM-DD-<region>.json    (full trimmed snapshot)
       ./docs/archive/index.json                  (rolled up, for charts)
       ./archive/YYYY-MM-DD-<region>.csv.gz       (raw CSVs, OUTSIDE docs/)

Design notes:

* JSON is stored UNCOMPRESSED. GitHub Pages gzips text content types on the
  fly, so a ~62KB snapshot goes over the wire at ~11KB anyway, and a plain
  fetch() can read it with no DecompressionStream dance. At ~46MB/year this
  stays far under the 1GB published-Pages ceiling.

* DAILY, not per-run. The pipeline runs 6x/day alternating regions, but builds
  are a 20-player sample — consecutive runs differ mostly by noise. If today's
  file already exists for this region it is left alone unless ARCHIVE_FORCE=1.

* The raw .csv.gz copy lives OUTSIDE docs/ on purpose: it is not part of the
  published site, so it doesn't count against the Pages size limit. It exists
  because the trimmed JSON drops columns, and you cannot backfill a column you
  didn't keep.

* index.json is what the site charts from — one entry per day per region with
  only the small stuff (ceiling positions, tier letters). Item counts stay in
  the per-day files so the index doesn't balloon.
"""

import csv
import datetime
import gzip
import json
import os
import shutil
import sys
from collections import defaultdict

OUT = "output"
DOCS_ARCHIVE = os.path.join("docs", "archive")
RAW_ARCHIVE = "archive"
INDEX = os.path.join(DOCS_ARCHIVE, "index.json")
MANIFEST = os.path.join(DOCS_ARCHIVE, "items_manifest.json")   # latest catalogue
PATCHES = os.path.join(DOCS_ARCHIVE, "patches.json")           # dated change log

REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
FORCE = (os.environ.get("ARCHIVE_FORCE") or "0") == "1"
KEEP_RAW = (os.environ.get("ARCHIVE_RAW") or "1") == "1"
SNAPSHOTS = ["4.8k", "9.6k", "14.4k", "20.8k", "postgame"]


def read(name):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        raise SystemExit("missing %s — run the pipeline first" % path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def slug(name):
    s = "".join(c if c.isalnum() else "_" for c in name).strip("_").lower()
    while "__" in s:
        s = s.replace("__", "_")
    return s


def track_item_manifest():
    """Detect and log item catalogue changes, so a patch is visible in the data.

    An item patch adds, removes, renames, recosts and recategorises items. The
    dangerous case is a REWORK: same item_id, same name, completely different
    behaviour. A chart spanning the patch would draw one continuous line across
    two different items. Cost and tier changes are the cheapest available proxy
    for "this item was touched", so they are logged even though they don't
    prove a rework on their own.

    Writes the current catalogue and appends any diff to patches.json. Returns
    a summary dict for the day's snapshot, or None if the manifest is missing.
    """
    src = os.path.join(OUT, "items_manifest.json")
    if not os.path.exists(src):
        return None
    try:
        with open(src, encoding="utf-8") as f:
            now = json.load(f)
    except Exception as e:
        print("  [warn] items_manifest.json unreadable (%s)" % e, file=sys.stderr)
        return None

    prev = {}
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = {}

    today = datetime.date.today().isoformat()
    diff = {"added": [], "removed": [], "renamed": [],
            "recategorised": [], "recosted": [], "retiered": []}
    if prev:
        for iid in sorted(set(now) - set(prev)):
            diff["added"].append({"id": iid, "name": now[iid]["name"]})
        for iid in sorted(set(prev) - set(now)):
            diff["removed"].append({"id": iid, "name": prev[iid]["name"]})
        for iid in sorted(set(now) & set(prev)):
            a, b = prev[iid], now[iid]
            if a["name"] != b["name"]:
                diff["renamed"].append({"id": iid, "from": a["name"], "to": b["name"]})
            if a["cat"] != b["cat"]:
                diff["recategorised"].append({"id": iid, "name": b["name"],
                                              "from": a["cat"], "to": b["cat"]})
            if a["cost"] != b["cost"]:
                diff["recosted"].append({"id": iid, "name": b["name"],
                                         "from": a["cost"], "to": b["cost"]})
            if a["tier"] != b["tier"]:
                diff["retiered"].append({"id": iid, "name": b["name"],
                                         "from": a["tier"], "to": b["tier"]})

    changed = sum(len(v) for v in diff.values())
    if changed:
        log = {"patches": []}
        if os.path.exists(PATCHES):
            try:
                with open(PATCHES, encoding="utf-8") as f:
                    log = json.load(f)
            except Exception:
                log = {"patches": []}
        entry = dict(diff)
        entry["date"] = today
        entry["total_changes"] = changed
        entry["item_count"] = len(now)
        log.setdefault("patches", []).append(entry)
        log["patches"].sort(key=lambda d: d["date"])
        with open(PATCHES, "w", encoding="utf-8") as f:
            json.dump(log, f, separators=(",", ":"), ensure_ascii=False)
        print("  [items] CATALOGUE CHANGED: %s"
              % ", ".join("%s %d" % (k, len(v)) for k, v in diff.items() if v),
              file=sys.stderr)

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(now, f, separators=(",", ":"), ensure_ascii=False, sort_keys=True)

    return {"item_count": len(now), "changed_today": changed}


def build_players(region, ceil_rows):
    """Top-N cross-hero board, for charting player movement over time.

    Why players and not just heroes: a hero's ceiling rank jumps 15+ places the
    moment a different person takes custody of it, which makes a hero race
    strobe. A player's own ladder position drifts — measured across real runs,
    median movement between snapshots is 0-1 positions once the reset settled.
    Switching hero keeps them at roughly the same position with a new label.

    IDENTITY IS THE CATCH. Board entries carry possible_account_ids, which is a
    CANDIDATE list (one name can hold 30+), not an identity. An id is only
    trustworthy where it agrees with a pool member the pipeline already
    resolved. Entries are therefore tagged confirmed/unconfirmed rather than
    being silently keyed on a display name, which would merge two people who
    share a name or split one who renames.
    """
    path = os.path.join(OUT, "board.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            board = json.load(f)
    except Exception as e:
        print("  [warn] board.json unreadable (%s)" % e, file=sys.stderr)
        return None
    if region not in board:
        return None

    # names the pipeline resolved to a real account for this region
    resolved = {}
    for r in ceil_rows:
        if r["region"] == region and r.get("ceiling_player"):
            resolved.setdefault(r["ceiling_player"], []).append(r["hero"])

    out = []
    for e in board[region]["entries"]:
        ids = e.get("ids") or []
        confirmed = e["name"] in resolved and len(ids) == 1
        out.append({
            "pos": e["pos"],
            "name": e["name"],
            # single unambiguous candidate AND corroborated by a resolved pool
            # member; anything else is a label, not an identity
            "id": ids[0] if confirmed else None,
            "confirmed": confirmed,
            "top_heroes": e.get("top_heroes") or [],
            "ceiling_for": resolved.get(e["name"], []),
        })
    return {"depth": board[region]["depth"], "entries": out}


def build_snapshot(region, ceil_rows, item_rows, tier_rows):
    """Trimmed, chartable representation of one region on one day."""
    tier = {r["hero"]: r for r in tier_rows}

    heroes = {}
    for r in ceil_rows:
        if r["region"] != region:
            continue
        t = tier.get(r["hero"], {})
        def _num(v, cast=int):
            try:
                return cast(v)
            except (TypeError, ValueError):
                return None
        heroes[slug(r["hero"])] = {
            "name": r["hero"],
            # BOTH orderings are archived on purpose. `rank` is the ladder
            # ceiling — the hero's best player's position on Valve's board,
            # which is NOT ranked-gated and so is partly a pre-season artifact.
            # `ranked_rank` orders the same players by shrunk ranked win rate
            # instead. Keeping both means the season series stays continuous
            # across the 2026-08-03 switch and the two can be compared directly
            # rather than argued about.
            "rank": int(r["ceiling_rank"]),
            "ranked_rank": _num(r.get("ranked_rank")),
            "ranked_rating": _num(r.get("ranked_rating"), float),
            "pos": int(r["global_pos"]),
            "depth": int(r["region_depth"]),
            # carried for reference; the site ranks by ceiling, not win rate
            "winrate": float(t["elite_winrate"]) if t.get("elite_winrate") else None,
            "players": int(t["players"]) if t.get("players") else None,
        }

    builds = defaultdict(lambda: defaultdict(dict))
    of_builds = {}
    for r in item_rows:
        if r.get("region") != region or r["snapshot"] not in SNAPSHOTS:
            continue
        s = slug(r["hero"])
        builds[s][r["snapshot"]][r["item_id"]] = int(r["count"])
        of_builds[s] = int(r["of_builds"])

    # item id -> name, so a snapshot is readable without the live data.json
    items = {}
    for r in item_rows:
        if r["item_id"] not in items:
            items[r["item_id"]] = {"name": r["item"], "cat": r["category"] or "?"}

    players = build_players(region, ceil_rows)

    return {
        "date": datetime.date.today().isoformat(),
        "region": region,
        "players": players,
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "heroes": heroes,
        "of_builds": of_builds,
        "items": items,
        "builds": {k: dict(v) for k, v in builds.items()},
    }


def update_index(date, region, snap):
    """Small rolled-up series the site can chart without fetching every day."""
    index = {"days": []}
    if os.path.exists(INDEX):
        try:
            with open(INDEX, encoding="utf-8") as f:
                index = json.load(f)
        except Exception as e:
            print("  [warn] index.json unreadable (%s) — rebuilding" % e, file=sys.stderr)
            index = {"days": []}

    entry = {
        "date": date,
        "region": region,
        "file": "%s-%s.json" % (date, region),
        # just enough to plot movement without opening the day file
        "ranks": {s: h["rank"] for s, h in snap["heroes"].items()},
        "ranked_ranks": {s: h["ranked_rank"] for s, h in snap["heroes"].items()
                         if h.get("ranked_rank") is not None},
        "pos": {s: h["pos"] for s, h in snap["heroes"].items()},
    }
    days = [d for d in index.get("days", [])
            if not (d.get("date") == date and d.get("region") == region)]
    days.append(entry)
    days.sort(key=lambda d: (d["date"], d["region"]))
    index["days"] = days
    index["updated_at"] = snap["generated_at"]
    index["regions"] = sorted({d["region"] for d in days})

    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"), ensure_ascii=False)
    return len(days)


def main():
    os.makedirs(DOCS_ARCHIVE, exist_ok=True)
    ceil_rows = read("ceiling.csv")
    item_rows = read("item_frequency.csv")
    tier_rows = read("tierlist.csv")

    if "region" not in (item_rows[0] if item_rows else {}):
        raise SystemExit("item_frequency.csv has no region column — pipeline is out of date")

    present = sorted({r["region"] for r in ceil_rows})
    date = datetime.date.today().isoformat()
    wrote = 0

    # catalogue tracking is region-independent — do it once per run
    catalogue = track_item_manifest()

    for region in present:
        if region not in REGIONS:
            continue
        path = os.path.join(DOCS_ARCHIVE, "%s-%s.json" % (date, region))
        if os.path.exists(path) and not FORCE:
            print("  [skip] %s already archived today (ARCHIVE_FORCE=1 to overwrite)"
                  % region, file=sys.stderr)
            continue

        snap = build_snapshot(region, ceil_rows, item_rows, tier_rows)
        if catalogue:
            snap["catalogue"] = catalogue
        if not snap["heroes"]:
            print("  [warn] %s: no ceiling rows, nothing archived" % region, file=sys.stderr)
            continue

        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"), ensure_ascii=False)
        n_days = update_index(date, region, snap)
        wrote += 1
        p = snap.get("players")
        conf = sum(1 for e in p["entries"] if e["confirmed"]) if p else 0
        print("  -> %s (%.0f KB, %d heroes, %d board players / %d id-confirmed) "
              "| index now %d entries"
              % (path, os.path.getsize(path) / 1024, len(snap["heroes"]),
                 len(p["entries"]) if p else 0, conf, n_days), file=sys.stderr)

        # raw CSVs, outside docs/ so they don't count against the Pages limit
        if KEEP_RAW:
            os.makedirs(RAW_ARCHIVE, exist_ok=True)
            for name in ("ceiling.csv", "item_frequency.csv", "tierlist.csv"):
                dest = os.path.join(RAW_ARCHIVE,
                                    "%s-%s-%s.gz" % (date, region, name[:-4]))
                if os.path.exists(dest) and not FORCE:
                    continue
                with open(os.path.join(OUT, name), "rb") as src, \
                        gzip.open(dest, "wb", compresslevel=9) as dst:
                    shutil.copyfileobj(src, dst)

    if not wrote:
        print("  [archive] nothing new written", file=sys.stderr)


if __name__ == "__main__":
    main()
