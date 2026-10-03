# Inbox drop: new-hero support and the calculator item fix (2026-10-03)

    deadlock_pipeline.py       MODIFIED - new heroes reach the tier list; writes output/roster.csv
    build_site_data.py         MODIFIED - NEW row for heroes the ladder can't place yet
    fetch_icons.py             MODIFIED - fetches art for heroes with no data yet
    docs/index.html            MODIFIED - NEW row styling, and a note on the hero panel
    build_calc_data.py         MODIFIED - leaves unreleased heroes out of the calculator
    ref/shop_items_wiki.json   MODIFIED - two items renamed in the 2026-09-29 patch

Before editing, I checked all six against live main (f5aeb42): their blob hashes
matched the versions these edits started from. The only commit since the 7ca5aba
dump touched docs/data.json.

## Why Rat King never showed up

Every hero reaches tierlist.csv through Valve's per-hero leaderboard. A hero
released hours ago has an empty leaderboard. The orbit fill is meant to top up
short heroes, but it only looped over heroes that already had a board player.
Rat King had zero in both regions, so he never got a pool, never got a tier-list
row, and never had his icon fetched.

deadlock-api was fine. It published the Rat King build (assets-6737) 23
minutes after release, and docs/calc/heroes.json already carries his stats.

## What changes on the site

- A hero the ladder can't place yet goes in a **NEW** row under D. This covers
  three cases: no ceiling row, a ceiling that comes from the orbit only, or no
  pool at all yet. The hero shows its art and whatever builds exist.
- NEW heroes are never ranked, and they don't shift any other hero's tier.
- A hero moves into S–D on its own once a leaderboard player is confirmed for
  it in that region.

## Next Daily refresh log

```
[assets] 5 hero(es) not yet released, skipped until Valve flips them: Baba, Deadman Danny, Nurse Harrow, Solomon, Violet
[lb] 1 hero(es) have NO board entries in any region: Rat King (84) - ...
[NAmerica] 1 listed as NEW (no board-backed ceiling yet): Rat King (N builds)
[calc] items 156 (...)        <- was 154: Spirit Shredder + Armor Piercer are back
```

This adds no SQL calls. The orbit query already runs every run, because Mirage
and Lady Geist are short.

## The other five

Deadman Danny, Solomon, Violet, Nurse Harrow and Baba are still PreRelease in
build 6745. Their card art isn't in the game files yet. Rat King's card art
shipped in the same build that released him. When Valve releases each of the
five, it shows up as NEW on the next run, art included, with no code change
needed.

## Not in this drop

- `daily.yml`, an optional one-line change that uploads `output/roster.csv`
  with the other aggregates. Workflow files need a PAT secret in this repo, so I
  kept it out.
- `batch-fix.zip` at the repo root is a Dl_toolkit drop that was uploaded here
  on 2026-08-12. This repo's inbox leaves it alone because it has no
  README-INBOX.md. It's safe to delete.
