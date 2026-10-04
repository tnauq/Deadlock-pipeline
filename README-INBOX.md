# Inbox drop: orbit seeds — active, strongest, twice as many (2026-10-04)

    deadlock_pipeline.py   MODIFIED - seeds: the 24 strongest board players who played in the last 3 days
    ceiling_rank.py        MODIFIED - the same rule for its orbit fallback

Before editing, I checked both files against live main (89c8ffa): their blob
hashes matched the versions these edits started from.

## What the seeds were

The seeds were recomputed every run, but as the **12 lowest Steam account
ids** among that run's board players: `sorted(set(ids))[:12]`. Both scripts
have done this since the orbit landed on 2026-08-07.

- **Lowest id means oldest account.** The oldest accounts on the boards barely
  change, so the same dozen stayed the seeds for weeks, whether or not they
  still played.
- **`ceiling_rank.py` never got its intended order.** It orders its seeds by
  board position ("seed from the strongest board positions"), but the sort
  inside its `fetch_orbit1` threw that order away.
- **The tested "received order" is a different list.** In PROBES.md,
  best-match-first is about each leaderboard entry's `possible_account_ids`.
  That one is still kept exactly as received.

## What they are now

- **The strongest board players in the region.**
  - The pipeline goes by general-board position, id-confirmed accounts first.
  - The ceiling uses its dual-confirmed players by board position, as its
    comment always intended.
- **Only players who played in the last 3 days.** The check runs inside the
  same query: it's sent the top 72 candidates, and the first 24 with at least
  one match become seeds. No extra SQL.
- **24 seeds instead of 12.** `ORBIT_SEEDS` sets the count and
  `ORBIT_SEED_OVERFETCH=3` sets how many candidates are checked.

## Check them in the log

These lines appear in both the Run pipeline step and the Ceiling ranking step:

```
[orbit] NAmerica seeds: 24 active of the first 27 candidates (3 with no match in the last 3 days, skipped); matches per seed: min 2, median 14, max 39
[orbit] 251 matches, 1874 players; seeds met: {1: ..., 2: ..., ...}
[orbit] NAmerica  seeds' general-board positions: 1-12, 14-20, 22-26
```

(Numbers illustrative.) "Skipped" counts strong players with no match in the
window, the kind the old rule kept using.

## What changes on the site

- **Thin established heroes** (hero-regions the boards can't fill to 20) take
  their orbit builds from a bigger, current ring 1. Expect those builds, and
  the pooled win rate shown with them, to shift. Board players and the tier
  order don't change.
- **New heroes:** ring 1 gets bigger, so ring 2 should be needed even less.
- **Ceiling:** only its orbit fallback uses seeds, so a ceiling backed by the
  boards can't move.

## Cost

- **SQL:** still one call per region in each script.
- **Per-hero stats:** about twice as many ring-1 players means about twice the
  free calls for them. The pipeline fetches them once; the ceiling fetches
  them twice, for the career and the 30-day window. Expect roughly a minute
  more per run.

## Verified

- **Unit test, both scripts:**
  - candidates are sent in the given order;
  - inactive candidates are skipped;
  - a match containing only a passed-over candidate is dropped;
  - a passed-over candidate who met seeds is still kept as a member;
  - the case where nobody is active.
- **Mock runs:**
  - Inactive top players are skipped, and the positions are logged.
  - With every seed active, the outputs are byte-identical to drop 3, and so
    is the ceiling. The mock's lobbies are symmetric, so only real data will
    show the new picks.
- **Inbox:** the drop was unpacked against a fresh clone of main.
