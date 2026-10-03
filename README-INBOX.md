# Inbox drop: ring 2 of the orbit for new heroes (2026-10-03, third drop)

    deadlock_pipeline.py   MODIFIED - ring 2 as the last new-hero tier, and its measurement
    orbit_audit.py         MODIFIED - counts ring-2 builds separately (orbit2_builds)
    docs/index.html        MODIFIED - the NEW note and footer mention lobby-mates

Before editing, I checked all three against live main (fa48c98): their blob
hashes matched the drop-2 versions these edits started from.
`build_site_data.py` is unchanged.

## What ring 2 is

Players who shared a match with ring-1 (orbit) players in the same 3-day
window, starting from the same 12 seeds. The seeds and their order are
unchanged.

## Rules

- **New heroes only, last tier.** The order is: the hero's own board, then the
  sweep, then ring 1, then ring 2. Ring-2 players are taken only where a gap
  is still left. Established heroes are untouched.
- **Filter:** at least 2 ring-1 matches in the window (`ORBIT2_MIN_SHARED`).
- **Order:** by the share of the player's own games in the window that were
  ring-1 lobbies, not the raw count. A raw count rewards volume: 4 of 30 games
  would beat 4 of 4. Two phantom games outside the band (`ORBIT2_SHARE_K`) keep
  2 of 2 (0.50) from beating 18 of 20 (0.82). Ties go to the raw count, then
  the win rate on the hero.
- Same as the other tiers: 3 games on the hero, one build per player, and a
  player is never used for the same hero in both regions.

## The measurement

While a hero is NEW, every run queries ring 2 and prints this, even when
nothing is short (in that case nobody is taken):

```
[orbit2] NAmerica  N ring-2 players of Rat King. On the top-1000 board:
[orbit2]   by ring-1 matches shared:   1: x% of n  |  2: ...  |  3-5: ...  |  6+: ...
[orbit2]   by share of their games:    <1/3: ...  |  1/3-2/3: ...  |  2/3+: ...
[orbit2]   ring 1, for comparison:     y% of m (players of the same hero(es))
```

If being in the leaders' lobbies tracks standing, the percentages climb from
left to right. A bucket close to the ring-1 figure is about as near the top
as the leaders' own lobby-mates. When ring 2 does fill a gap, a further line
lists each pick as ring-1 lobbies / games played.

## Cost

- **SQL:** one call per region per run while a hero is new. With the API key,
  the 20/hr IP cap doesn't apply (deadlock-api drops IP quotas for keyed
  requests); the key's limit is 10/min. The 40 s SQL pause adds about 80 s per
  run.
- **Per-hero stats:** only when there's a gap, and only for the 1,000 closest
  players who pass the filter.
- **Non-fatal:** if the query fails, the run says so and carries on without
  ring 2.

## Switches (env)

- `ORBIT2_FILL=0`: never take ring-2 players.
- `ORBIT2_MEASURE=0`: skip the measurement-only query.
- `ORBIT2_MIN_SHARED` and `ORBIT2_SHARE_K` tune the filter and the order.

## Rat King right now

The 17:07 and 19:03 UTC runs on drop 2 filled him 20/20 in both regions from
the sweep and ring 1. Ring 2 will only measure him. It's there for the next
releases: five heroes landing together would split the same top players five
ways.

## Verified

- **The query ran on a real ClickHouse engine** (24.8, local) over synthetic
  `match_player` data shaped like deadlock-api's table: ReplacingMergeTree,
  duplicate rows, and both match modes. Every row and its order matched a
  reference computation.
  - It passes deadlock-api's own query checks.
  - The URL is about 2 KB.
- **Mock pipeline runs:**
  - A gap fills closest-first: 4/4 and 3/3 come ahead of the grinders at 6/40
    and 3/30.
  - Ring-1 rows that leak into the result are dropped.
  - A failed query doesn't stop the run.
  - Both switches work.
- **Established heroes:** identical to drop 2. When ring 2 isn't needed,
  Rat King's pool is identical too.

## Heads-up, not acted on

deadlock-api now marks `/v1/sql` as **deprecated**: "Direct SQL access will
be removed". It points to an hourly-exported public data lake at
data.deadlock-api.com (DuckDB / DuckLake, or an MCP server at `/v1/mcp`). No
removal date is published. Every SQL call in this pipeline (orbit, items,
pool wins, ring 2) will eventually need to move there.
