#!/usr/bin/env python3
"""
probe_season2.py: what there is to build ceilings and pools from in Season 2.
Measurement only. It changes nothing the site reads.

WHY (2026-11-10)
----------------
Season 2's leaderboards need 50 ranked matches in the season (15 on the hero
for a hero board), so they are nearly empty and the pipeline is pinned to
Season 1's frozen boards (board_season.py). Valve's own per-player rank is in
every ranked match (valve_rank.py), which would let the pipeline find "the
top players on a hero" with no leaderboard at all. Before anything is built on
that (FINDINGS.md, rule zero), this measures it on a live run:

    boards    how full each season's boards are, and which one "no id" gives
    ranks     do board players carry a current rank; does last season's board
              position predict it
    native    the players on each hero by current rank (scoreboard, ranked
              only), and the region their matches are served from
    compare   those lists against this run's own pool and ceilings
    lake      whether the lake carries the same rank columns and regions

Run by board_season.run_probe(): once a day at the end of ceiling_rank.py,
and whenever the board check stops a run. `python3 probe_season2.py` by hand
does the same.

Writes  archive/probes/season2-YYYY-MM-DD.json   counts only: no account ids
                                                 and no names. Committed.
        output/rank_native.json                  the lists, with account ids.
                                                 Stays on the runner.
Every section is independent: one that fails records its error and the rest
still run. The file is rewritten after each section, so a probe that is
stopped early keeps what it had.
"""
import csv
import datetime
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict

import valve_rank

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
REGIONS = [r.strip() for r in
           (os.environ.get("REGIONS") or "NAmerica,Europe").split(",") if r.strip()]
OUT_DIR = "output"
NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
OUT_JSON = os.path.join("archive", "probes", "season2-%s.json" % NOW.strftime("%Y-%m-%d"))
NATIVE_JSON = os.path.join(OUT_DIR, "rank_native.json")

# Players asked for per hero, and the fewest ranked matches on the hero (in
# the running season) to be listed.
NATIVE_LIMIT = int(os.environ.get("PROBE_NATIVE_LIMIT") or 200)
NATIVE_MIN_MATCHES = int(os.environ.get("PROBE_NATIVE_MIN_MATCHES") or 3)
# Seconds the per-hero scoreboard calls may take in total. Heroes not reached
# are counted, not retried.
NATIVE_BUDGET_S = int(os.environ.get("PROBE_NATIVE_BUDGET_S") or 150)
# Analytics endpoints share 200 requests a minute per IP, and ceiling_rank's
# scoreboard cross-check has just used up to ~90 of them.
NATIVE_PAUSE_S = 0.6
# The region route charges per account: 10,000 a minute per IP.
REGION_MAX_IDS = 9000
BOARD_SAMPLE_TOP = 400
POOL = 20

RESULT = {"version": 1, "at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "reason": sys.argv[1] if len(sys.argv) > 1 else "manual"}
BOARDS = {}            # (leaderboard_id or None, region) -> entries; never saved
INTERVALS = []


def say(msg):
    print("  [probe] " + msg, file=sys.stderr)


def get(url, tries=1, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2)
    raise RuntimeError("unreachable")


def save():
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    RESULT["seconds"] = int(time.time() - T0)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(RESULT, f, indent=1, sort_keys=True)
        f.write("\n")


def section(name, fn):
    t = time.time()
    try:
        RESULT[name] = fn()
    except Exception as e:
        RESULT[name] = {"error": "%s: %s" % (type(e).__name__, str(e)[:300])}
        say("%s: FAILED (%s)" % (name, str(e)[:160]))
    if isinstance(RESULT[name], dict):
        RESULT[name]["seconds"] = round(time.time() - t, 1)
    try:
        save()
    except OSError as e:
        say("could not write %s (%s)" % (OUT_JSON, e))


def read_csv(name):
    path = os.path.join(OUT_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def hero_ids():
    """[(hero_id, name)] from the committed site data, else from the assets."""
    try:
        with open(os.path.join("docs", "data.json"), encoding="utf-8") as f:
            heroes = json.load(f)["heroes"]
        out = sorted((int(h["id"]), h.get("name") or s) for s, h in heroes.items()
                     if str(h.get("id") or "").isdigit())
        if out:
            return out
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return sorted((int(h["id"]), h.get("name") or "") for h in get(BASE + "/v1/assets/heroes", 2)
                  if h.get("id") is not None and not h.get("disabled")
                  and not h.get("in_development"))


def entries_of(payload):
    e = payload.get("entries") if isinstance(payload, dict) else payload
    return e or []


def badge_idx(badge):
    """Badge on an even scale: 6 steps per tier (11 -> 1, 16 -> 6, 21 -> 7)."""
    b = int(badge or 0)
    return (b // 10 - 1) * 6 + b % 10 if b > 0 else 0


def idx_badge(idx):
    idx = int(round(idx))
    return ((idx - 1) // 6 + 1) * 10 + (idx - 1) % 6 + 1 if idx > 0 else 0


def med_badge(badges):
    """Median badge of a list, as a name ("Phantom 3"); "" when empty."""
    idx = [badge_idx(b) for b in badges if b]
    return valve_rank.badge_name(idx_badge(statistics.median(idx))) if idx else ""


# --------------------------------------------------------------------------
# boards
# --------------------------------------------------------------------------


def boards():
    global INTERVALS
    INTERVALS = valve_rank.fetch_intervals(get, BASE)
    live = valve_rank.running_interval(INTERVALS, NOW.timestamp())
    lids = sorted({i["leaderboard_id"] for i in INTERVALS if i["leaderboard_id"]}) or [1001, 1002]
    try:
        heroes = hero_ids()
    except Exception:
        heroes = []                       # the general boards still get counted
    sample = sorted({heroes[0][0], heroes[len(heroes) // 2][0], heroes[-1][0]}) if heroes else []
    out = {"intervals": INTERVALS, "running_interval": live["interval"] if live else None,
           "general": {}, "hero_sample": {}, "sample_heroes": sample, "default_is": {}}
    for lid in [None] + lids:
        key = str(lid or "default")
        qs = ("?leaderboard_id=%d" % lid) if lid else ""
        for rg in REGIONS:
            try:
                e = entries_of(get("%s/v1/leaderboard/%s%s" % (BASE, rg, qs), 2))
                BOARDS[(lid, rg)] = e
                out["general"].setdefault(key, {})[rg] = len(e)
            except Exception as ex:
                out["general"].setdefault(key, {})[rg] = "error: %s" % str(ex)[:80]
            sizes = {}
            for hid in sample:
                try:
                    sizes[str(hid)] = len(entries_of(get(
                        "%s/v1/leaderboard/%s/%d%s" % (BASE, rg, hid, qs))))
                except Exception as ex:
                    sizes[str(hid)] = "error: %s" % str(ex)[:60]
                time.sleep(0.05)
            out["hero_sample"].setdefault(key, {})[rg] = sizes
    # which season's board a request with no id gets
    for rg in REGIONS:
        d = [e.get("account_name") for e in BOARDS.get((None, rg), [])[:50]]
        same = [lid for lid in lids if d and d == [
            e.get("account_name") for e in BOARDS.get((lid, rg), [])[:50]]]
        out["default_is"][rg] = same[0] if same else None
    for key in sorted(out["general"]):
        say("board %-7s general %s; hero boards %s"
            % (key, ", ".join("%s %s" % (rg, out["general"][key].get(rg)) for rg in REGIONS),
               "; ".join("%s %s" % (rg, "/".join(str(v) for v in
                                                 out["hero_sample"][key].get(rg, {}).values()))
                         for rg in REGIONS)))
    say("no id gives board %s; running interval %s"
        % (", ".join("%s %s" % (rg, out["default_is"][rg]) for rg in REGIONS),
           out["running_interval"]))
    return out


# --------------------------------------------------------------------------
# ranks of board players
# --------------------------------------------------------------------------


def _fullest_board(rg):
    """(leaderboard_id, entries) of the fullest named board read for a region."""
    best = max(((len(e), lid or 0) for (lid, r), e in BOARDS.items() if r == rg and lid),
               default=None)
    return (best[1], BOARDS[(best[1], rg)]) if best and best[0] else (None, [])


def ranks():
    live = valve_rank.running_interval(INTERVALS, NOW.timestamp())
    out = {}
    for rg in REGIONS:
        lid, entries = _fullest_board(rg)
        # one possible id = an unambiguous name; a fuzzy one only adds noise.
        # With too few of those, the first listed id (the best match, right
        # for ~92% of entries per PROBES.md) stands in.
        top = list(enumerate(entries[:BOARD_SAMPLE_TOP], 1))
        picks = [(pos, int(e["possible_account_ids"][0])) for pos, e in top
                 if len(e.get("possible_account_ids") or []) == 1]
        ids_from = "unambiguous names"
        if len(picks) < 50:
            picks = [(pos, int(e["possible_account_ids"][0])) for pos, e in top
                     if e.get("possible_account_ids")]
            ids_from = "first listed id"
        if not picks:
            out[rg] = {"board": lid, "asked": 0}
            continue
        got, info = valve_rank.fetch_ranks(get, BASE, [a for _p, a in picks])
        cur = live["interval"] if live else max(
            [r["interval"] for r in got.values() if r["interval"]] or [0])
        now = [(pos, got[a]) for pos, a in picks
               if a in got and valve_rank.is_current(got[a], cur)]
        prev = sum(1 for _p, a in picks if a in got and got[a]["badge"] > 0
                   and got[a]["interval"] != cur)
        out[rg] = {
            "board": lid, "ids_from": ids_from,
            "asked": info["asked"], "answered": info["answered"],
            "failed": info["failed"], "current_interval": cur,
            "current": len(now), "ranked_in_an_older_interval": prev,
            "no_rank": info["answered"] - len(now) - prev,
            "current_among_top_50": sum(1 for pos, _r in now if pos <= 50),
            "asked_among_top_50": sum(1 for pos, _a in picks if pos <= 50),
            "tiers": dict(Counter(valve_rank.TIERS[min(r["badge"] // 10, 11)]
                                  for _p, r in now)),
            "median_badge": med_badge([r["badge"] for _p, r in now]),
            # +1 = the old board order and the current rank agree perfectly
            "spearman_board_vs_progress": valve_rank.spearman([-pos for pos, _r in now],
                                                   [r["progress"] for _p, r in now]),
        }
        o = out[rg]
        say("ranks %s: board %s, %d ids (%s) of the top %d -> %d answered, "
            "%d with a current rank (interval %s), %d only an older one, %d none; "
            "median %s; board order vs rank: rho %s"
            % (rg, lid, o["asked"], ids_from, BOARD_SAMPLE_TOP, o["answered"], o["current"], cur,
               prev, o["no_rank"], o["median_badge"] or "-",
               o["spearman_board_vs_progress"]))
    return out


def board_regions():
    """account_id -> region, for accounts this run can place from the boards:
    the sampled pool (candidates.csv, ids already resolved by the pipeline),
    then general-board entries whose name resolves to one or two ids. An id
    seen in two regions is dropped. Used to check the region route, and to
    stand in for it when it does not answer."""
    seen = defaultdict(set)
    for c in read_csv("candidates.csv") or []:
        if (c.get("account_id") or "").isdigit() and c.get("region"):
            seen[int(c["account_id"])].add(c["region"])
    for rg in REGIONS:
        _lid, entries = _fullest_board(rg)
        for e in entries:
            ids = e.get("possible_account_ids") or []
            if 1 <= len(ids) <= 2:
                for a in ids:
                    seen[int(a)].add(rg)
    return {a: next(iter(r)) for a, r in seen.items() if len(r) == 1}


# --------------------------------------------------------------------------
# rank-native lists: the top players on each hero, no leaderboard involved
# --------------------------------------------------------------------------


def native():
    live = valve_rank.running_interval(INTERVALS, NOW.timestamp())
    since = live["start"] if live else int(NOW.timestamp()) - 30 * 86400
    heroes = hero_ids()
    lists, secs, errors, skipped, t0 = {}, [], Counter(), 0, time.time()
    for hid, _name in heroes:
        if time.time() - t0 > NATIVE_BUDGET_S:
            skipped += 1
            continue
        t = time.time()
        try:
            lists[hid] = valve_rank.hero_top(get, BASE, hid, since,
                                             NATIVE_MIN_MATCHES, NATIVE_LIMIT)
            secs.append(time.time() - t)
        except Exception as e:
            errors[str(e)[:60]] += 1
        time.sleep(NATIVE_PAUSE_S)
    out = {"since": since, "min_matches": NATIVE_MIN_MATCHES, "limit": NATIVE_LIMIT,
           "heroes_asked": len(heroes), "heroes_answered": len(lists),
           "heroes_skipped_for_time": skipped, "errors": dict(errors),
           "call_seconds_median": round(statistics.median(secs), 2) if secs else None,
           "call_seconds_max": round(max(secs), 2) if secs else None,
           "players_per_hero_median": (statistics.median(len(v) for v in lists.values())
                                       if lists else 0)}
    # Is match_mode applied? A rank sort lists only players with a ranked match
    # in range, so asking for unranked matches must come back empty.
    if lists:
        hid = next(iter(lists))
        try:
            url = ("%s/v1/analytics/scoreboards/players?sort_by=rank&hero_id=%d"
                   "&match_mode=unranked&min_matches=%d&min_unix_timestamp=%d&limit=20"
                   % (BASE, hid, NATIVE_MIN_MATCHES, since))
            out["unranked_control_rows"] = len(valve_rank._rows(get(url)))
        except Exception as e:
            out["unranked_control_rows"] = "error: %s" % str(e)[:60]

    ids = []
    for rows in lists.values():
        ids.extend(r["account_id"] for r in rows)
    ids = list(dict.fromkeys(ids))
    out["distinct_players"] = len(ids)
    regions, info = valve_rank.fetch_regions(get, BASE, ids[:REGION_MAX_IDS])
    out["regions"] = info
    raw = Counter(regions.get(a) or "(none)" for a in ids[:REGION_MAX_IDS] if a in regions)
    out["server_regions"] = dict(raw.most_common(25))
    out["server_regions_unmapped"] = sorted(
        s for s in raw if s != "(none)" and not valve_rank.board_region(s))
    # The region route is a day old upstream. Check it against the regions the
    # boards give for the same accounts, and fall back on those where it has
    # nothing, so the per-region counts below are not empty without it.
    known = board_regions()
    where, check = {}, Counter()
    for a in ids:
        by_route = valve_rank.board_region(regions.get(a))
        by_board = known.get(a)
        if by_route and by_board:
            check["same" if by_route == by_board else "different"] += 1
        where[a] = (by_route or by_board, "route" if by_route else
                    "board" if by_board else "unknown")
    out["region_source"] = dict(Counter(src for _r, src in where.values()))
    out["region_route_vs_board"] = dict(check)
    out["board_regions"] = dict(Counter(r or "(unknown)" for r, _s in where.values()))

    per = {}
    for hid, rows in lists.items():
        for r in rows:
            r["region"] = where[r["account_id"]][0]
        for rg in REGIONS:
            per[(hid, rg)] = [r for r in rows if r["region"] == rg]
    for rg in REGIONS:
        mine = {hid: v for (hid, r), v in per.items() if r == rg}
        o = {"heroes": len(mine)}
        for k in (3, 5, 10):
            n = [sum(1 for r in v if r["matches"] >= k) for v in mine.values()]
            o["min_%d_matches" % k] = {"heroes_with_20": sum(1 for x in n if x >= POOL),
                                       "heroes_with_10": sum(1 for x in n if x >= 10),
                                       "heroes_with_1": sum(1 for x in n if x >= 1),
                                       "players_median": statistics.median(n) if n else 0}
        o["first_badge_median"] = med_badge([v[0]["badge"] for v in mine.values() if v])
        o["first_badge_lowest"] = valve_rank.badge_name(
            min([v[0]["badge"] for v in mine.values() if v] or [0]))
        o["twentieth_badge_median"] = med_badge(
            [v[POOL - 1]["badge"] for v in mine.values() if len(v) >= POOL])
        out[rg] = o
        m3 = o["min_%d_matches" % 3]
        say("native %s: %d heroes; with 3+ ranked games on the hero: %d have 20 players, "
            "%d have 10, %d have 1; #1 is median %s (lowest %s), #20 is median %s"
            % (rg, o["heroes"], m3["heroes_with_20"], m3["heroes_with_10"],
               m3["heroes_with_1"], o["first_badge_median"] or "-",
               o["first_badge_lowest"] or "-", o["twentieth_badge_median"] or "-"))
    say("native: %d of %d heroes answered (%s s median, %s s max), %d players; "
        "unranked control: %s rows"
        % (len(lists), len(heroes), out["call_seconds_median"], out["call_seconds_max"],
           len(ids), out.get("unranked_control_rows")))
    say("regions: the route knows %d of %d; region taken from %s; route vs board "
        "on the same accounts: %s; server regions: %s; unmapped: %s"
        % (info["known"], info["asked"], out["region_source"],
           out["region_route_vs_board"] or "no overlap",
           ", ".join("%s %d" % kv for kv in raw.most_common(8)) or "none",
           ", ".join(out["server_regions_unmapped"]) or "none"))
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(NATIVE_JSON, "w", encoding="utf-8") as f:
            json.dump({"since": since, "min_matches": NATIVE_MIN_MATCHES,
                       "lists": {str(h): v for h, v in lists.items()}}, f)
    except OSError as e:
        say("could not write %s (%s)" % (NATIVE_JSON, e))
    return out


# --------------------------------------------------------------------------
# compare with this run's pool and ceilings
# --------------------------------------------------------------------------


def compare():
    try:
        with open(NATIVE_JSON, encoding="utf-8") as f:
            lists = {int(h): v for h, v in json.load(f)["lists"].items()}
    except (OSError, ValueError, KeyError):
        return {"skipped": "no rank-native lists this run"}
    out = {}
    by_region = {(hid, rg): [r for r in rows if r.get("region") == rg]
                 for hid, rows in lists.items() for rg in REGIONS}
    server = {r["account_id"]: r.get("region") for rows in lists.values() for r in rows}

    cands = read_csv("candidates.csv")
    if cands is None:
        out["pool"] = {"skipped": "no output/candidates.csv"}
    else:
        pool = defaultdict(set)
        for c in cands:
            if (c.get("account_id") or "").isdigit() and (c.get("hero_id") or "").isdigit():
                pool[(int(c["hero_id"]), c.get("region"))].add(int(c["account_id"]))
        got, info = valve_rank.fetch_ranks(get, BASE, {a for v in pool.values() for a in v})
        live = valve_rank.running_interval(INTERVALS, NOW.timestamp())
        cur = live["interval"] if live else max(
            [r["interval"] for r in got.values() if r["interval"]] or [0])
        agree = Counter()
        for rg in REGIONS:
            over, listed, pool_b, nat_b, cur_share = [], [], [], [], []
            for (hid, r), ids in pool.items():
                if r != rg or hid not in lists:
                    continue
                nat = by_region.get((hid, rg), [])
                top = {x["account_id"] for x in nat[:POOL]}
                everyone = {x["account_id"] for x in lists[hid]}
                over.append(len(ids & top))
                listed.append(len(ids & everyone) / float(len(ids)))
                now = [got[a]["badge"] for a in ids
                       if a in got and valve_rank.is_current(got[a], cur)]
                cur_share.append(len(now) / float(len(ids)))
                if now:
                    pool_b.append(statistics.median(badge_idx(b) for b in now))
                if len(nat) >= POOL:
                    nat_b.append(statistics.median(badge_idx(x["badge"]) for x in nat[:POOL]))
                for a in ids:
                    if a in server:
                        agree["same" if server[a] == rg else
                              ("other" if server[a] else "unknown")] += 1
            out["pool_" + rg] = {
                "hero_regions": len(over),
                "pool_in_native_top20_median": statistics.median(over) if over else None,
                "pool_share_listed_at_all_median": (round(statistics.median(listed), 2)
                                                    if listed else None),
                "pool_share_with_current_rank_median": (round(statistics.median(cur_share), 2)
                                                        if cur_share else None),
                "pool_median_badge": (valve_rank.badge_name(idx_badge(statistics.median(pool_b)))
                                      if pool_b else ""),
                "native_top20_median_badge": (valve_rank.badge_name(
                    idx_badge(statistics.median(nat_b))) if nat_b else ""),
            }
            o = out["pool_" + rg]
            say("pool %s: of each hero's 20 sampled players, median %s are in the "
                "rank-native top 20 and %s%% have a current rank; pool median %s, "
                "rank-native top 20 median %s"
                % (rg, o["pool_in_native_top20_median"],
                   int(100 * (o["pool_share_with_current_rank_median"] or 0)),
                   o["pool_median_badge"] or "-", o["native_top20_median_badge"] or "-"))
        out["pool_region_check"] = dict(agree)
        out["pool_ranks"] = info
        say("region check on pool players also in the lists: %s" % dict(agree))

    ceil = read_csv("ceiling.csv")
    if ceil is None:
        out["ceiling"] = {"skipped": "no output/ceiling.csv"}
    else:
        where = Counter()
        for r in ceil:
            if not ((r.get("account_id") or "").isdigit() and (r.get("hero_id") or "").isdigit()):
                continue
            nat = [x["account_id"] for x in by_region.get((int(r["hero_id"]), r.get("region")), [])]
            aid = int(r["account_id"])
            if int(r["hero_id"]) not in lists:
                where["hero not listed"] += 1
            elif aid not in nat:
                where["absent"] += 1
            else:
                i = nat.index(aid) + 1
                where["first" if i == 1 else "top 5" if i <= 5 else
                      "top 20" if i <= POOL else "below 20"] += 1
        out["ceiling"] = {
            "rows": len(ceil), "place_in_native_list": dict(where),
            "metric": dict(Counter(r.get("ceiling_metric") or "" for r in ceil)),
            "with_current_rank": sum(1 for r in ceil if r.get("valve_current") == "YES"),
            "board_and_rank_agree": dict(Counter(r.get("rank_board_agree") or "n/a"
                                                 for r in ceil)),
        }
        say("ceilings: %d rows by %s; in the rank-native list of their hero and "
            "region: %s" % (len(ceil), out["ceiling"]["metric"], dict(where)))
    return out


# --------------------------------------------------------------------------
# the lake
# --------------------------------------------------------------------------


def lake_section():
    import lake
    out = {}
    lake.connect()
    cols = defaultdict(dict)
    for r in lake.query("SELECT table_name, column_name, data_type FROM duckdb_columns() "
                        "WHERE database_name = 'lake'", "probe: columns"):
        cols[r["table_name"]][r["column_name"]] = str(r["data_type"])
    out["tables"] = {t: len(c) for t, c in cols.items()}
    out["columns"] = {t: sorted(c) for t, c in cols.items() if t != "match_player"}
    mp = cols.get("match_player", {})
    want = ["rank_interval", "player_rank_initial_display_rank",
            "player_rank_initial_flat_progress", "player_rank_final_flat_progress",
            "player_rank_desired_progress_change", "player_match_outcome",
            "average_badge_team0", "average_badge", "ranked_type"]
    out["match_player_rank_columns"] = {c: mp.get(c) for c in want}
    say("lake: tables %s; rank columns missing from match_player: %s"
        % (", ".join(sorted(cols)), ", ".join(c for c in want if c not in mp) or "none"))

    def step(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": str(e)[:300]}
            say("lake %s: FAILED (%s)" % (name, str(e)[:120]))

    def ranked_rows():
        parts = ["CAST(count(*) AS BIGINT) AS ranked_rows",
                 "CAST(count(DISTINCT account_id) AS BIGINT) AS accounts",
                 "CAST(count(DISTINCT match_id) AS BIGINT) AS matches"]
        for c, alias in (("player_rank_final_flat_progress", "with_final_progress"),
                         ("rank_interval", "with_interval"),
                         ("average_badge_team0", "with_team_badge"),
                         ("average_badge", "with_average_badge")):
            if c in mp:
                parts.append("CAST(count(*) FILTER (WHERE %s IS NOT NULL) AS BIGINT) AS %s"
                             % (c, alias))
        if "player_rank_initial_display_rank" in mp:
            parts.append("CAST(count(*) FILTER (WHERE player_rank_initial_display_rank > 0) "
                         "AS BIGINT) AS placed")
            parts.append("CAST(count(*) FILTER (WHERE player_rank_initial_display_rank >= 110) "
                         "AS BIGINT) AS eternus_rows")
        if "rank_interval" in mp:
            parts.append("CAST(max(rank_interval) AS BIGINT) AS newest_interval")
        if "player_rank_final_flat_progress" in mp:
            parts.append("CAST(max(player_rank_final_flat_progress) AS BIGINT) AS highest_progress")
        row = lake.query("SELECT %s FROM match_player WHERE %s AND %s"
                         % (", ".join(parts), lake.since(3),
                            lake.enum_is("match_mode", "Ranked")),
                         "probe: ranked rows, 3 days")[0]
        say("lake ranked rows, 3 days: %s" % row)
        return row

    def clusters():
        rows = lake.query("SELECT region, CAST(count(*) AS BIGINT) AS clusters, "
                          "CAST(sum(matches) AS BIGINT) AS matches FROM server_clusters "
                          "GROUP BY region ORDER BY 3 DESC", "probe: server clusters")
        say("lake server regions: %s" % ", ".join(
            "%s (%s)" % (r["region"] or "(none)", r["matches"]) for r in rows[:14]))
        return rows[:40]

    def board_history():
        cut = (NOW - datetime.timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        rows = lake.query("SELECT CAST(CAST(fetched_at AS DATE) AS VARCHAR) AS day, "
                          "CAST(region AS VARCHAR) AS region, CAST(count(*) AS BIGINT) AS n "
                          "FROM leaderboard WHERE fetched_at >= TIMESTAMP '%s' "
                          "GROUP BY 1, 2 ORDER BY 1, 2" % cut, "probe: board snapshots, 8 days")
        return rows[:80]

    step("ranked_rows_3_days", ranked_rows)
    if "server_clusters" in cols:
        step("server_clusters", clusters)
    if "leaderboard" in cols:
        step("board_snapshots", board_history)
    return out


T0 = time.time()


def main():
    say("Season 2 probe (%s) -> %s" % (RESULT["reason"], OUT_JSON))
    section("boards", boards)
    section("ranks", ranks)
    section("native", native)
    section("compare", compare)
    if (os.environ.get("PROBE_LAKE") or "1") != "0":
        section("lake", lake_section)
    say("done in %ds" % (time.time() - T0))


if __name__ == "__main__":
    main()
