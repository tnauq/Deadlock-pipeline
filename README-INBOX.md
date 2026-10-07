# Inbox drop: new-hero criteria tighten with age (2026-10-06)

    deadlock_pipeline.py   MODIFIED - age-based games bar, experienced players first, hand-off to the hero's own leaderboard
    docs/index.html        MODIFIED - the NEW note and the footer describe the new order

Before editing, I checked both files against live main (02c8d2e): their blob
hashes matched the versions these edits started from.

## What changes (new heroes only; established heroes are untouched)

1. **The games bar rises with the hero's age.**
   - It starts at 1 game on release day and adds one every other day, up to
     the 5 games every established hero's orbit fill already uses. It gets
     there on day 8.
   - Rat King needs 3 today (day 5), 4 from Oct 8 and 5 from Oct 10.
   - Baba needs 1 today and 2 from Oct 8, so he gets builds within hours of a
     release instead of waiting for players to reach 3.
   - Ending at 5 keeps a slow-to-catch-on hero from being held to a stricter
     bar than an established one.
2. **Experienced players go first.** Anyone with at least twice the bar is
   taken before anyone at the bar alone: the leaderboard sweep, then ring 1.
   For Rat King that's 6+ games today and 10+ from day 8, so a top player
   who has tried him 3 times no longer takes a slot from someone with 6+.
   Inside each pass the order is unchanged: standing, then seeds met. Ring 2
   stays the last resort and applies the same rule within itself.
3. **The fallback hands off to the hero's own leaderboard, not the calendar.**
   - The fallback only ever fills what the hero's own board leaves short, per
     region, so it steps aside on its own, region by region, as the board
     fills.
   - The 14-day cutoff is gone; 30 days is now only a hard stop.
   - Rat King's own board was empty in both regions every day from Oct 4 to
     Oct 7. Under the old rule, his whole pool would have switched on day 15
     from top-ladder players to orbit players at the 5-game bar.

Also:
- **No recency rule.** It would thin pools, especially for an unpopular hero.
- **The ring-2 measurement-only query** still runs for a hero's first 14
  days only, so the longer window doesn't double its cost.

## In the log

```
[new] Rat King       NAmerica  0 board + 20 sweep (...) + 0 orbit + 0 ring 2 = 20/20   [day 5: bar 3 games, 14 of the fill had 6+; eligible ...]
```

"N of the fill had 6+" is how many slots went to experienced players.

## Verified

- **The bar by age:** days 0-1 need 1, 2-3 need 2, 4-5 need 3, 6-7 need 4,
  and day 8 onward needs 5.
- **Mock run, day 4, 3 per region:** the old rule's third pick was the #6
  player with 4 games. The new rule takes the #16 player with 6 games.
- **The rest of the age range:**
  - Day 0 (bar 1): players with 2 games become eligible, 30 of 30 filled.
  - Days 10 and 20: the bar stays at 5.
  - Day 20: no measurement query.
  - Day 31: the fallback is off.
- **Established heroes:** identical to the live code, including with Baba
  released.
- **The page:** the Rat King note renders, the script passes a syntax check,
  and there were no page errors.
- **Inbox:** the drop was unpacked against a fresh clone of main.
