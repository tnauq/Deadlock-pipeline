"""
Which ranked season's leaderboards to read. deadlock_pipeline.py and
ceiling_rank.py both call resolve(), so every board in a run comes from one
season.

WHY (2026-11-10)
----------------
Since Ranked Beta Season 2 (Nov 5) every season has its own board, and
deadlock-api selects one with `?leaderboard_id=`: 1001 is Season 1, 1002 is
Season 2. The scripts sent no id and got the API's current default. Sometime
between the Nov 8 03:31 and Nov 10 00:26 UTC refreshes that default became
Season 2, whose boards were almost empty (general depth 1 in NA, 2 in EU).
Getting onto a Season 2 board takes 50 ranked matches this season, and onto a
hero board 15 on that hero, so a new season's boards take weeks to fill. The
site went to 44 of 44 heroes NEW.

HOW IT DECIDES
--------------
LEADERBOARD_ID=<number>  pins that board. No probing and no automatic switch.
LEADERBOARD_ID=auto      (default) reads the current id from STATE_PATH, or
                         1001 if there's no state file, then probes id + 1.
                         The run switches to id + 1 only when, in EVERY
                         region:
                           * its general board has >= SWITCH_DEPTH entries, and
                           * the median of its hero boards has >= SWITCH_HERO_MEDIAN
                             entries, and
                           * its general board is not a copy of the current
                             board's. An unknown id that just returns the
                             default must not count as a new season.
                         The switch is written to STATE_PATH, which the commit
                         step commits (it's under archive/), so the decision
                         sticks and the next season is probed after it.

Whatever board is chosen, every region's general board must have at least
BOARD_MIN_DEPTH entries. If not, the run stops (SystemExit) and the site
keeps its last refresh. Before this check, a 1-entry board published a page
with every hero NEW.

The first script to call resolve() in a run writes its decision to
CHOICE_PATH (output/, which isn't committed). The second script reads it
there, so the two scripts can't end up on different seasons.

THE PROBE (2026-11-10)
----------------------
run_probe() starts probe_season2.py in its own process, with a time limit,
and never fails the caller. ceiling_rank.py calls it at the end of a run
(once per UTC day, until PROBE_SEASON2_UNTIL), and the board check below
calls it before it stops a run, so the log of a stopped run still says what
the rank data could offer. PROBE_SEASON2=0 turns it off, =force runs it
every time.
"""
import datetime
import json
import os
import statistics
import subprocess
import sys
import time

DEFAULT_ID = 1001
STATE_PATH = os.environ.get("BOARD_STATE_PATH") or os.path.join("archive", "board_season.json")
CHOICE_PATH = os.environ.get("BOARD_CHOICE_PATH") or os.path.join("output", "board_choice.json")
CHOICE_MAX_AGE_S = 6 * 3600

SETTING = (os.environ.get("LEADERBOARD_ID") or "auto").strip().lower()
SWITCH_DEPTH = int(os.environ.get("BOARD_SWITCH_DEPTH") or 900)
SWITCH_HERO_MEDIAN = float(os.environ.get("BOARD_SWITCH_HERO_MEDIAN") or 20)
BOARD_MIN_DEPTH = int(os.environ.get("BOARD_MIN_DEPTH") or 500)
# How many leading names two boards must share before one counts as a copy of
# the other.
ALIAS_PREFIX = 200

PROBE = (os.environ.get("PROBE_SEASON2") or "auto").strip().lower()
PROBE_UNTIL = os.environ.get("PROBE_SEASON2_UNTIL") or "2026-11-30"
PROBE_TIMEOUT_S = int(os.environ.get("PROBE_SEASON2_TIMEOUT_S") or 600)

_chosen = None


def _log(msg):
    print("  [season] " + msg, file=sys.stderr)


def suffix(lid):
    """Query string to append to a /v1/leaderboard/... path."""
    return "?leaderboard_id=%d" % lid


def _entries(payload):
    e = payload.get("entries") if isinstance(payload, dict) else payload
    return e or []


def _general(get, base, region, lid):
    return _entries(get("%s/v1/leaderboard/%s%s" % (base, region, suffix(lid))))


def _names(entries):
    return [e.get("account_name") for e in entries[:ALIAS_PREFIX]]


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")


def _ready(get, base, regions, hero_ids, cur, nxt, cur_general):
    """(ready, evidence) for board nxt. Any error means not ready."""
    ev = {}
    for rg in regions:
        try:
            gen = _general(get, base, rg, nxt)
        except Exception as e:
            return False, "%s general board %d unreadable (%s)" % (rg, nxt, e)
        if len(gen) < SWITCH_DEPTH:
            return False, "%s general board %d has %d entries (needs %d)" % (
                rg, nxt, len(gen), SWITCH_DEPTH)
        if _names(gen) == _names(cur_general.get(rg, [])):
            return False, ("%s board %d is identical to board %d: the API is "
                           "probably serving its default for an unknown id"
                           % (rg, nxt, cur))
        sizes = []
        for hid in hero_ids:
            try:
                sizes.append(len(_entries(get("%s/v1/leaderboard/%s/%d%s"
                                              % (base, rg, hid, suffix(nxt))))))
            except Exception:
                sizes.append(0)
            time.sleep(0.05)
        med = statistics.median(sizes) if sizes else 0
        if med < SWITCH_HERO_MEDIAN:
            return False, ("%s hero boards on %d: median %g entries (needs %g), "
                           "%d of %d empty" % (rg, nxt, med, SWITCH_HERO_MEDIAN,
                                               sizes.count(0), len(sizes)))
        ev[rg] = {"general": len(gen), "hero_median": med,
                  "hero_empty": sizes.count(0)}
    return True, ev


def run_probe(reason, force=False):
    """Run probe_season2.py in its own process. Never raises.

    Without `force` it runs once per UTC day (it leaves a dated file under
    archive/probes/, which the commit step commits) and not at all after
    PROBE_UNTIL.
    """
    if PROBE in ("0", "off", "no", "false"):
        return
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if not force and PROBE != "force":
        if today > PROBE_UNTIL:
            return
        if os.path.exists(os.path.join("archive", "probes", "season2-%s.json" % today)):
            return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_season2.py")
    if not os.path.exists(script):
        return
    # The probe's few lake queries fail fast instead of working through
    # lake.py's retries: it is a measurement, and the run is waiting on it.
    env = dict(os.environ)
    for k, v in (("LAKE_THREADS", "8"), ("LAKE_MIN_THREADS", "4"), ("LAKE_TRIES", "1")):
        env.setdefault(k, v)
    try:
        sys.stderr.flush()
        subprocess.run([sys.executable, script, reason], timeout=PROBE_TIMEOUT_S,
                       check=False, env=env)
    except subprocess.TimeoutExpired:
        _log("probe stopped after %ds; what it had is in its file" % PROBE_TIMEOUT_S)
    except Exception as e:
        _log("probe not run (%s)" % e)


def _stop(msg):
    """Stop the run so the site keeps its last refresh, probing first."""
    _log(msg)
    run_probe("board check stopped the run", force=True)
    raise SystemExit(msg)


def _general_or_stop(get, base, region, lid):
    try:
        return _general(get, base, region, lid)
    except Exception as e:
        _stop("leaderboard %d: the %s general board could not be read (%s). "
              "Stopping so the site keeps its last refresh." % (lid, region, e))


def resolve(get, base, regions, hero_ids):
    """The leaderboard id this run reads. Decided once per run, then cached."""
    global _chosen
    if _chosen is not None:
        return _chosen

    lid, how = None, None
    if SETTING not in ("", "auto"):
        try:
            lid, how = int(SETTING), "pinned by LEADERBOARD_ID"
        except ValueError:
            raise SystemExit("LEADERBOARD_ID must be a number or 'auto', got %r"
                             % SETTING)
    else:
        c = _read_json(CHOICE_PATH)
        if (isinstance(c, dict) and isinstance(c.get("leaderboard_id"), int)
                and time.time() - c.get("at", 0) < CHOICE_MAX_AGE_S):
            lid, how = c["leaderboard_id"], "decided earlier this run (%s)" % CHOICE_PATH

    cur_general = {}
    if lid is None:
        st = _read_json(STATE_PATH)
        cur = st.get("leaderboard_id") if isinstance(st, dict) else None
        cur = cur if isinstance(cur, int) else DEFAULT_ID
        nxt = cur + 1
        for rg in regions:
            cur_general[rg] = _general_or_stop(get, base, rg, cur)
        ok, ev = _ready(get, base, regions, hero_ids, cur, nxt, cur_general)
        if ok:
            lid, how = nxt, "AUTO-SWITCHED from %d: board %d has filled" % (cur, nxt)
            _write_json(STATE_PATH, {
                "leaderboard_id": nxt, "previous": cur,
                "switched_at": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "evidence": ev,
                "rule": "general >= %d, hero-board median >= %g, every region"
                        % (SWITCH_DEPTH, SWITCH_HERO_MEDIAN)})
            cur_general = {}
        else:
            lid, how = cur, "auto; staying on %d: %s" % (cur, ev)

    # Never publish from a near-empty board, whichever one was chosen.
    for rg in regions:
        gen = cur_general.get(rg)
        if gen is None:
            gen = _general_or_stop(get, base, rg, lid)
        if len(gen) < BOARD_MIN_DEPTH:
            _stop("leaderboard %d: %s general board has %d entries, under "
                  "BOARD_MIN_DEPTH=%d. Stopping so the site keeps its last refresh. "
                  "Pin another season with LEADERBOARD_ID if this one is empty."
                  % (lid, rg, len(gen), BOARD_MIN_DEPTH))

    _log("leaderboard_id %d (%s)" % (lid, how))
    try:
        _write_json(CHOICE_PATH, {"leaderboard_id": lid, "at": time.time(), "how": how})
    except OSError as e:
        _log("could not cache the choice (%s); the next script decides again" % e)
    _chosen = lid
    return lid
