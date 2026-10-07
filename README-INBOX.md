# Inbox drop: a top-player ability order for every hero (2026-10-07)

    build_site_data.py   MODIFIED - every sampled hero gets its ability orders; when the ceiling player wasn't sampled, the top player is the sampled player who stands highest
    docs/index.html      MODIFIED - labels that player "top player"

Before editing, I checked both files against live main (304483f): their blob
hashes matched the versions these edits started from.

## Why orders were missing

- The two orders (top player, most common) were built by walking
  `ceiling.csv`.
  - **A hero with no ceiling row got neither order.** Baba has no ceiling
    row yet (empty leaderboard, and none of his players has the games the
    ceiling's fallback needs). His ability points showed, but his order panel
    was empty in both regions.
  - **A hero whose ceiling player isn't among the sampled builds got "most
    common" only.** On the 01:10 UTC run that was Rat King NA, The Doorman
    NA and Drifter EU.

## What changes

1. **Every hero-region with sampled builds gets its orders.** The ceiling row
   is looked up when there is one.
2. **The top player's build:**
   - It is still the ceiling player's own build whenever that game is in the
     sample. That covers 75 of 80 hero-regions today.
   - Otherwise it is the build of the sampled player who stands highest:
     - **Established hero:** confirmed accounts first by general-board
       position, then other general-board accounts, then hero-board-only
       players by ladder position. This is the order the orbit seeds use, and
       it matches the ceiling's preference for confirmed accounts.
     - **New hero:** general-board position alone, the order the new-hero
       fill picks its players in.
   - Players added from the orbit have no leaderboard position, so they are
     never picked. If nobody in the sample has a position, the hero shows
     "most common" only, as before.
3. **The page** labels it "top player", the same as a ceiling player. The
   second toggle is "most common".
4. **The log** counts the three kinds, and names any hero that has ability
   points but no order.

Expected on the next run: Baba (both regions), Rat King NA, The Doorman NA and
Drifter EU all show a top player.

## After uploading

`docs/data.json` is rebuilt from the run's CSVs, so the fix shows after the
next refresh. Run the workflow by hand, or wait for the next scheduled run.

## Verified (mock pipeline, ceiling and site build, then the page in Chromium)

- **Reproduced:** with the live code, Baba and Rat King got no order. With
  the new code they get one.
- **Unmodified mock:** every established hero's orders are identical to the
  live code's, and nothing else in `data.json` changed.
- **Synthetic cases.** Each build was tagged with its owner, so the exact
  player picked could be checked:

  | Case | Picked |
  |---|---|
  | Established hero, ceiling player not sampled | the confirmed #21 over an unconfirmed #5 |
  | Hero-board-only players | ladder #1 |
  | Nobody with a position | "most common" only |
  | New hero | general-board #1, whether confirmed or not |
  | Sampled ceiling player | kept, for both new and established heroes |

- **Page:** "Ability order · top player", then "most common" on toggle 2, for
  Baba, Rat King and an established fallback, in both regions. No page
  errors.
- **Inbox:** the drop was unpacked against a fresh clone of main.
