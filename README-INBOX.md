# Inbox drop: Baba, and new heroes released "in development" (2026-10-06)

    deadlock_pipeline.py   MODIFIED - a Release hero is in, even with in_development set; Baba's release date
    build_calc_data.py     MODIFIED - the same rule for the build calculator

Before editing, I checked both files against live main (bcbbf69): their blob
hashes matched the versions these edits started from.

## What happened

Baba was released in **game build 6757 (2026-10-06, 21:03 UTC)**. In
heroes.vdata his development state went from PreRelease to **Release**, and his
card art shipped in the same build.

In the same build, Valve also flipped his `m_bInDevelopment` flag to
**true**. No hero had ever been both Release and in development: Rat King went
Release four days earlier with that flag false. deadlock-api passes the flag
through as `in_development`, and the pipeline still treated
`in_development` as "can't be picked". So Baba was dropped in every run after
the patch, and the calculator dropped him too.

## The fix

- **The development state decides now.** Release means in; PreRelease and
  DebugOnly mean out; disabled is always out. `in_development` only matters
  for an old payload with no development state, where it keeps its old
  meaning. Nothing needs editing per hero, so the next hero released this way
  is picked up on the first run after it.
- **Baba is dated from his release.** `KNOWN_RELEASES` now holds Baba
  (2026-10-06), and a hero appearing for the first time takes its known date
  when it has one. He's NEW until Oct 20, not counted from whenever this drop
  first runs.
- **The four other vote heroes stay out until they're released:** Deadman
  Danny, Solomon, Violet and Nurse Harrow are still PreRelease.

## What you'll see

On the first run after unpacking, Baba appears in the NEW tier in both
regions, with his card art and builds from the new-hero fallback:
leaderboard sweep, then orbit. The log shows:

```
[new] 2 new hero(es), fallback ON (within 14 days of first seen): Rat King (since 2026-10-02), Baba (since 2026-10-06)
[new] Baba           NAmerica  0 board + N sweep (...) + ...
```

## Verified

- **The game files** (GameTracking-Deadlock heroes.vdata):
  - Build 6753: Baba is PreRelease with in_development false.
  - Build 6757: Release with in_development true, the only hero in that state.
  - `pak01_dir.txt` lists his card, sm and mm art in 6757.
- **deadlock-api's source:** `player_selectable` comes from the state, and
  `in_development` is passed through raw.
- **Unit tests:** 10 flag combinations through the old and new rule. Only
  Baba's changes; everything else gives the same answer as before. The
  calculator's rule matches.
- **Mock run with Baba flagged as he really shipped:**
  - Old code: he's left out.
  - New code: he's in, with `first_seen` 2026-10-06 and builds from the
    sweep, listed as NEW on the site.
  - Established heroes are identical. With Baba still PreRelease, every output
    file is byte-identical to the live code.
- **Inbox:** the drop was unpacked against a fresh clone of main.
