# Inbox drop: tier race on the site (2026-11-06)

    build_race_data.py    NEW      - builds docs/archive/race.json from archive/*.gz
    archive_snapshot.py   MODIFIED - calls build_race_data at the end of every run
    docs/index.html       MODIFIED - "Tier race" button + bar chart race, hidden until opened
    docs/archive/race.json NEW     - first build (98 days, Aug 1 - Nov 6), so the race works before the next refresh

Built against main 83bd662.

## What it adds

- A **Tier race** button next to the NA/EU switch. The race panel stays hidden,
  and race.json (~175 KB, gzipped in transit) is only fetched, the first time
  it is opened. Pressing the button again hides it and stops playback.
- Measures, in button order: **Win rate** (default), **Net wins**, **Rank score**,
  **Points**. Top 15 or All, play/pause, ½x-4x, a day slider, tooltips.
- Follows the page's region switch. Bars are coloured by that day's site tier.
- Notes under the date flag the method changes: the ceiling ordered by ranked
  net wins Aug 8 - Sep 2, and the ranked-only player sample through Sep 2.

## How it stays current

archive_snapshot.py already runs every refresh and docs/archive is already in
the commit step, so no workflow edit is needed. The race rebuild is wrapped so a
failure only logs `[race] not rebuilt: ...` and never fails the refresh.

Tiers are rebuilt with build_site_data.py's own rules; the 2026-11-06 rebuild
matches the published data.json in both regions.
