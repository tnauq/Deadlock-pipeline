# Inbox drop: new-hero build fallback (2026-10-03, second drop)

    deadlock_pipeline.py   MODIFIED - new-hero sweep, a relaxed games bar, first-seen tracking
    build_site_data.py     MODIFIED - stores first_seen, flags new heroes, single builds
    docs/index.html        MODIFIED - single-build items, and notes that say where builds came from
    orbit_audit.py         MODIFIED - counts sweep builds separately from board builds

Before editing, I checked all four against live main (1da4724): their blob
hashes matched the versions these edits started from.

## The goal is unchanged

Each region's top 20 players **on the hero**, one build each: their most
recent game on it. For a new hero, Valve's per-hero leaderboard can't supply
that yet, so this fills the gap with the closest thing available. Established
heroes are untouched.

## What a NEW hero's pool is built from now, in order

1. **Its own leaderboard players.** The selection is the same as for every
   other hero.
2. **The sweep.** These are players the run already identified from any hero's
   leaderboard, or from the region's top-1000 general ladder, who have 3 or more
   games on the new hero. They're ordered by ladder position. Their per-hero
   stats arrive with every run anyway, so this adds no SQL calls. The general
   ladder costs a few extra per-hero stats calls, which aren't SQL.
3. **The orbit, at the same 3-game bar.** These are players who shared matches
   with top players.

One build per player. A player is never used for the same hero in both regions.

## How "new" is decided

A hero is new for **14 days** from the first time it appears. That date is
stored as `first_seen` in `docs/data.json`. The commit step already commits that
file, so nothing new is committed. On the first run, Rat King gets his real
release date, 2026-10-02, so he stays new until Oct 16. A hero released later
is marked automatically, because it won't be in the published file yet. The
check fails closed: if `data.json` is missing, or implausibly many heroes read
as new, the fallback is off for that run.

## On the site

- A new hero with 1–2 builds in a region now shows its items. The 2-builds
  rule had been hiding NA's only Rat King build.
- The NEW note says the builds are the most recent games of the
  highest-ranked players who have played the hero.
- Once a new hero is ranked but its leaderboard still can't supply 20, a note
  says how many builds came from the fallback.
- Ranking is unchanged. A new hero stays in NEW until its own leaderboard
  confirms a player.

## Next Daily refresh log

```
[new] 1 new hero(es), fallback ON (within 14 days of first seen): Rat King (since 2026-10-02)
[new] Rat King       NAmerica  0 board + N sweep (M on the general board) + K orbit = x/20   [bar 3 games; ...]
[new] Rat King       Europe    ...
```

## Cost

- **SQL:** while a hero is new, at most one or two extra calls per run, for
  the extra builds' items. The orbit query already runs every run.
- **Per-hero stats:** about 15 extra calls, which don't count toward the SQL
  limit.

## Not in this drop

- **The second orbit ring.** The 2026-08-08 measurement put it at the
  population mean: 0.504 median win rate, against 0.530 for orbit-1 and 0.568
  for the seeds. Adding it would fill the 20 with average players. It would
  also need extra SQL, because the orbit-1 list is too long for one query URL.
  It's held until a run shows the sweep can't fill a new hero.
