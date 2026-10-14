#!/usr/bin/env python3
"""
lake.py: SQL against deadlock-api's public data lake, run on the runner with
DuckDB. Shared by deadlock_pipeline.py and ceiling_rank.py.

WHY (2026-10-14)
----------------
deadlock-api moved /v1/sql off ClickHouse onto DuckDB over its public parquet
lake (their commit 9773572, 11:03 UTC). Through /v1/sql every query now times
out at 45 s with a 504, returns at most 100,000 rows, keeps the 2/min and
20/hr IP quotas, has to be DuckDB SQL instead of ClickHouse, and the endpoint
is marked for removal in favour of the lake itself. The 21:08 UTC run died on
its first query: the orbit's outer scan had no start_time bound, so it read
every match_player file (hundreds of GB) until the 45 s cut.

The lake is plain public HTTPS (https://data.deadlock-api.com: no key, no
rate limit, files served from R2), so the scripts now read it directly: one
DuckDB connection per script, attached read-only to the DuckLake catalog the
manifest names. It is the same data /v1/sql reads (hourly parquet snapshots),
without the timeout, row cap, URL-length limit or hourly SQL budget.

WHAT THE LAKE DOES DIFFERENTLY from the ClickHouse tables the queries were
written for:
  * Only match_id and start_time skip files. Every match_player query must
    bound start_time or name its match_ids, or it reads the whole table.
    since() writes the bound as a constant of the column's own type, which
    pushes down into the scan. A now()-based bound casts a naive TIMESTAMP
    column instead and loses the pruning (checked on DuckDB 1.5.6).
  * A row can appear twice: an hourly delta can carry a newer copy of a row
    already in a base file, and the newest created_at wins. Queries that count
    games or read a player's arrays keep one row per (match_id, account_id).
  * MATERIALIZED columns are not published, so items.net_worth_at_buy is gone.
    net_worth_at_buy() rebuilds it from the stats timeline the way
    deadlock-api does.
  * Nested columns are flat lists with dotted names: "items.item_id".
  * Enum columns arrive as their names or as their integer codes, depending on
    the exporter's settings. enum_is() matches either.
  * Never SELECT a timestamp column raw: the Python client needs pytz to hand
    back TIMESTAMPTZ values. Cast it, or compare it in SQL.

DuckDB is installed on first use (pip, at least the version the lake's catalog
was written with), so the workflow needs no change. LAKE_DB=<path> points the
scripts at a local DuckDB database instead (tests, offline mocks).
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from importlib import metadata

LAKE_URL = (os.environ.get("LAKE_URL") or "https://data.deadlock-api.com").rstrip("/")
MANIFEST_URL = os.environ.get("LAKE_MANIFEST") or LAKE_URL + "/v1/manifest.json"
# A local DuckDB database with the lake's tables (match_player, steam_profiles)
# in place of the real lake. Tests and offline mocks only.
LAKE_DB = os.environ.get("LAKE_DB") or ""
# Pin the client. Blank = whatever the manifest says the catalog was written
# with (readers need at least that version).
DUCKDB_VERSION = (os.environ.get("DUCKDB_VERSION") or "").lstrip("v")
# Remote parquet reads wait on R2, not the CPU: more threads, more requests in
# flight. deadlock-api runs its own lake queries with the same 16.
THREADS = int(os.environ.get("LAKE_THREADS") or 16)
MEMORY = os.environ.get("LAKE_MEMORY") or "8GB"
# A query that fails on a network error is retried on a freshly attached
# catalog: superseded lake files are swept within the hour, so a run that
# outlives its catalog gets a 404 rather than stale data.
TRIES = int(os.environ.get("LAKE_TRIES") or 3)

# The ClickHouse enums, from deadlock-api's migrations. Used only when the lake
# carries the integer code instead of the name.
ENUMS = {
    "game_mode": {"Invalid": 0, "Normal": 1, "OneVsOneTest": 2, "Sandbox": 3,
                  "StreetBrawl": 4, "ExploreNYC": 5, "Internal": 6},
    "match_mode": {"Invalid": 0, "Unranked": 1, "PrivateLobby": 2, "CoopBot": 3,
                   "Ranked": 4, "ServerTest": 5, "Tutorial": 6, "HeroLabs": 7,
                   "Calibration": 8},
}

_state = {"con": None, "types": {}, "calls": 0, "now": None, "tmp": None}


def log(msg):
    print("  [lake] " + msg, file=sys.stderr)


def first_line(e):
    s = str(e).strip()
    return s.splitlines()[0][:300] if s else type(e).__name__


def _sql_str(s):
    return "'%s'" % str(s).replace("'", "''")


def ident(name):
    """A column name as DuckDB needs it: "items.item_id"."""
    return '"%s"' % name.replace('"', '""')


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------


def _http(url, tries=4, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-pipeline-lake/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 2 ** attempt + 1
            log("%s on %s, retrying in %ds" % (first_line(e), url, wait))
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _vtuple(v):
    """"v1.5.6" -> (1, 5, 6); "1.5.7-dev12" -> (1, 5, 7)."""
    out = []
    for part in (v or "").strip().lstrip("v").split(".")[:3]:
        m = re.match(r"\d+", part)
        out.append(int(m.group()) if m else 0)
    return tuple(out)


def _installed():
    try:
        return metadata.version("duckdb")
    except metadata.PackageNotFoundError:
        return None


def _ensure_duckdb(want):
    """Import duckdb, installing it first if it is missing or older than `want`.

    Checked through package metadata, never by importing: a native module that
    is already loaded cannot be swapped for a newer one in the same process.
    """
    want = ".".join(str(n) for n in _vtuple(want)) if want else ""
    have = _installed()
    if have and (not want or _vtuple(have) >= _vtuple(want)):
        import duckdb
        return duckdb
    # the catalog writer's exact release first, then anything newer, then the
    # latest as a last resort (a pre-release writer has no wheel of its own)
    specs = (["duckdb==%s" % want, "duckdb>=%s" % want, "duckdb"] if want else ["duckdb"])
    log("installing %s (found %s)" % (specs[0], have or "none"))
    for spec in specs:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                            "--disable-pip-version-check", spec],
                           capture_output=True, text=True)
        if r.returncode == 0:
            break
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        log("pip install %s failed: %s" % (spec, tail[-1] if tail else r.returncode))
    else:
        raise RuntimeError("could not install duckdb (%s)" % ", ".join(specs))
    import duckdb
    return duckdb


def _setup(con, remote):
    stmts = []
    if remote:
        stmts += ["INSTALL httpfs", "LOAD httpfs", "INSTALL ducklake", "LOAD ducklake"]
    for s in stmts:
        con.execute(s)
    # icu supplies the TimeZone setting and TIMESTAMPTZ arithmetic. It is
    # built into the Python wheels; the install is a fallback.
    try:
        con.execute("LOAD icu")
    except Exception:
        try:
            con.execute("INSTALL icu")
            con.execute("LOAD icu")
        except Exception as e:
            log("icu unavailable (%s); time zones fall back to UTC" % first_line(e))
    for s in ("SET TimeZone = 'UTC'",
              "SET threads = %d" % THREADS,
              "SET memory_limit = '%s'" % MEMORY):
        try:
            con.execute(s)
        except Exception as e:
            log("%s: %s" % (s, first_line(e)))
    if remote:
        for s in ("SET http_retries = 5", "SET http_retry_wait_ms = 500",
                  "SET http_retry_backoff = 2", "SET parquet_metadata_cache = true"):
            try:
                con.execute(s)
            except Exception as e:
                log("%s: %s" % (s, first_line(e)))


def _detect_types():
    """{(table, column): DuckDB type} for the lake tables the scripts query."""
    con = _state["con"]
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = 'lake' "
            "AND table_name IN ('match_player', 'steam_profiles')").fetchall()
    except Exception as e:
        log("could not read column types (%s); using type-neutral filters" % first_line(e))
        rows = []
    _state["types"] = {(t, c): str(d).upper() for t, c, d in rows}
    if rows:
        want = ("start_time", "game_mode", "match_mode", "created_at")
        log("match_player: %s" % ", ".join(
            "%s %s" % (c, _state["types"].get(("match_player", c), "missing"))
            for c in want))


def connect():
    """The shared connection, attached as `lake` and set as the default
    catalog, so match_player and friends resolve unqualified."""
    if _state["con"] is not None:
        return _state["con"]
    t0 = time.time()
    if _state["now"] is None:
        # one clock for the whole script: every window in a run ends here
        _state["now"] = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    if LAKE_DB:
        duckdb = _ensure_duckdb(DUCKDB_VERSION)
        con = duckdb.connect()
        _setup(con, remote=False)
        con.execute("ATTACH %s AS lake (READ_ONLY)" % _sql_str(LAKE_DB))
        what = "local database %s" % LAKE_DB
    else:
        manifest = json.loads(_http(MANIFEST_URL).decode("utf-8"))
        key = manifest.get("catalog")
        if not key:
            raise RuntimeError("the lake manifest at %s names no DuckLake catalog"
                               % MANIFEST_URL)
        base = (manifest.get("public_url") or LAKE_URL).rstrip("/")
        writer = (manifest.get("duckdb_version") or "").lstrip("v")
        duckdb = _ensure_duckdb(DUCKDB_VERSION or writer)
        # The versioned catalog, downloaded once: the run reads ONE snapshot
        # even if the lake publishes a newer one meanwhile. Data files are
        # registered in it by their absolute public URL, so httpfs is all the
        # reader needs. Same as deadlock-api's own /v1/sql and MCP server.
        blob = _http(base + "/" + key.lstrip("/"))
        tmp = tempfile.mkdtemp(prefix="deadlock-lake-")
        path = os.path.join(tmp, "catalog.ducklake")
        with open(path, "wb") as f:
            f.write(blob)
        _state["tmp"] = tmp
        con = duckdb.connect()
        _setup(con, remote=True)
        con.execute("ATTACH %s AS lake (READ_ONLY)" % _sql_str("ducklake:" + path))
        what = ("manifest v%s, generated %s, catalog %s (%.1f MB, written by DuckDB %s)"
                % (manifest.get("version", "?"), manifest.get("generated_at", "?"),
                   key, len(blob) / 1e6, writer or "?"))
    con.execute("USE lake")
    _state["con"] = con
    log("%s; DuckDB %s; ready in %.0fs" % (what, _installed() or "?", time.time() - t0))
    _detect_types()
    return con


def close():
    con, _state["con"] = _state["con"], None
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
    tmp, _state["tmp"] = _state["tmp"], None
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


def calls():
    return _state["calls"]


def query(sql, label=""):
    """Run one statement and return its rows as dicts.

    A network error (a dropped connection, or a file the lake swept after a
    newer catalog replaced the one attached) is retried on a fresh attach.
    Anything else (a syntax, binder or conversion error) raises at once:
    those are bugs or schema changes, and retrying cannot fix them.
    """
    con = connect()
    duckdb = sys.modules["duckdb"]
    transient = tuple(c for c in (getattr(duckdb, "IOException", None),
                                  getattr(duckdb, "HTTPException", None)) if c) or (OSError,)
    _state["calls"] += 1
    log("#%d %s" % (_state["calls"], label))
    for attempt in range(TRIES):
        t0 = time.time()
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            log("-> %d rows in %.1fs" % (len(rows), time.time() - t0))
            return rows
        except transient as e:
            if attempt == TRIES - 1:
                raise
            wait = 15 * (attempt + 1)
            log("%s; reattaching and retrying in %ds (%d/%d)"
                % (first_line(e), wait, attempt + 1, TRIES - 1))
            time.sleep(wait)
            close()
            con = connect()
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------


def _type(column, table="match_player"):
    connect()
    return _state["types"].get((table, column), "")


def now():
    connect()
    return _state["now"]


def since(days, column="start_time", table="match_player"):
    """`column >= now - days`, written so the scan can skip files.

    The cutoff is a constant of the column's own type: no cast lands on the
    column, so the filter pushes down and the catalog's per-file statistics
    prune everything older.
    """
    cut = now() - datetime.timedelta(days=days)
    t = _type(column, table)
    if t and "INT" in t and "INTERVAL" not in t:
        return "%s >= %d" % (column, int(cut.timestamp()))
    lit = "TIMESTAMP '%s'" % cut.strftime("%Y-%m-%d %H:%M:%S")
    if t.startswith("TIMESTAMP"):
        return "%s >= CAST(%s AS %s)" % (column, lit, t)
    return "%s >= %s" % (column, lit)


def enum_is(column, name, table="match_player"):
    """`column = name` for an enum column, whichever way the lake stores it."""
    t = _type(column, table)
    code = ENUMS.get(column, {}).get(name)
    if t and "INT" in t and code is not None:
        return "%s = %d" % (column, code)
    if t and ("VARCHAR" in t or "ENUM" in t):
        return "%s = %s" % (column, _sql_str(name))
    alts = [_sql_str(name)] + ([_sql_str(code)] if code is not None else [])
    return "CAST(%s AS VARCHAR) IN (%s)" % (column, ", ".join(alts))


def age_days(column="start_time", table="match_player"):
    """Whole calendar days (UTC) from `column` to the run's clock: ClickHouse's
    dateDiff('day', column, now()), which the decay weights were written for."""
    t = _type(column, table)
    col = ("to_timestamp(%s)" % column) if (t and "INT" in t) else column
    return "date_diff('day', CAST(%s AS DATE), DATE '%s')" % (col, now().strftime("%Y-%m-%d"))


def to_epoch(expr, column="start_time", table="match_player"):
    """Seconds since 1970 for `expr`, a value of `column`'s type."""
    t = _type(column, table)
    if t and "INT" in t and "INTERVAL" not in t:
        return "CAST(%s AS BIGINT)" % expr
    return "CAST(epoch(%s) AS BIGINT)" % expr


def has_column(column, table="match_player"):
    return bool(_type(column, table))


def missing_columns(columns, table="match_player"):
    """The listed columns the lake's table lacks. [] when the column types
    could not be read, so the query itself reports the problem."""
    connect()
    if not _state["types"]:
        return []
    return [c for c in columns if (table, c) not in _state["types"]]


# --------------------------------------------------------------------------
# derived columns
# --------------------------------------------------------------------------


def net_worth_at_buy(bought, stat_ts, stat_nw, final):
    """Net worth when an item was bought, as deadlock-api computes it.

    The lake does not publish the MATERIALIZED items.net_worth_at_buy, so this
    is deadlock-api's own rebuild of it (api/src/routes/v1/analytics/
    item_flow_stats.rs, upgrades_source):

        coalesce(arrayElementOrNull(stats.net_worth,
                     arrayFirstIndex(ts -> ts >= bought, stats.time_stamp_s) - 1),
                 net_worth)

    i.e. the last stats sample BEFORE the purchase. A purchase after the last
    sample takes the last sample (index -1 counts from the end); one at or
    before the first sample, or a player with no timeline, takes the player's
    final net worth. That fallback is why SCHEMA.md quirk 3c saw arrays that
    "open at a late-game value, drop, and climb".
    """
    ts = stat_ts or []
    nw = stat_nw or []
    idx = 0                                   # ClickHouse 1-based, 0 = none
    for i, t in enumerate(ts):
        if t is not None and t >= bought:
            idx = i + 1
            break
    k = idx - 1
    if k == 0:
        v = None
    elif k > 0:
        v = nw[k - 1] if k <= len(nw) else None
    else:                                     # k == -1: the last element
        v = nw[-1] if nw else None
    return final if v is None else v
