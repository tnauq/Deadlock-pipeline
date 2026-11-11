# rank-ceiling-drop (against main a3385f9)

While the leaderboards are frozen on Season 1, the ceiling is ordered by each
player's own Valve rank in Season 2. A probe measures whether the pipeline
could find its players from rank alone, with no leaderboard.

Background: since 2026-07-30 every ranked match carries each player's rank
(the badge they entered with and their ladder progress before and after).
deadlock-api stores it on match_player and serves it per account. Read from
deadlock-api's source at 110e282; nothing in this repo used it before.

- `valve_rank.py` (new). `/v1/players/rank` (up to 1,000 ids a call), the
  season list, `/v1/players/region` and the rank-sorted scoreboard. A failed
  call is retried once, then split; it never raises.
- `ceiling_rank.py`. `CEILING_METRIC` gains `rank` and `auto` (the new default).
  - `rank`: among a hero's board players who hold a rank in the running season
    and have 3+ games on the hero in 30 days (`RANK_MIN_RECENT_HERO_GAMES`),
    the one with the most ladder progress. Heroes are ordered the same way.
  - `auto`: `rank` while the board in use belongs to a season that has ended,
    `board` once it is the running season's.
  - It stays on `board` when the ranks cover under 90% of hero-regions
    (`RANK_MIN_COVERAGE`), when the board players' median rank is under Oracle
    (`RANK_MIN_MEDIAN_TIER`, a check that the route means what it is read as),
    or when the season list can't be read. The log says which and why.
  - `CEILING_METRIC=board` gives exactly the old output.
  - ceiling.csv gains `valve_badge`, `valve_rank`, `valve_progress`,
    `valve_interval`, `valve_current` and `rank_board_agree`. They are written
    under every ordering, and reach the public archive with the rest of the file.
- `probe_season2.py` (new). Measurement only: how full each season's boards
  are, the ranks of board players, the top players on each hero by rank with
  their server region, and how those lists compare with this run's pool and
  ceilings. Counts go to `archive/probes/season2-<date>.json` (no account ids
  or names; committed by the existing commit step). The lists themselves stay
  on the runner in `output/rank_native.json`.
- `board_season.py`. `run_probe()`: the probe runs in its own process, with a
  10-minute limit, once a day at the end of ceiling_rank.py until 2026-11-30,
  and whenever the board check stops a run. It cannot fail a run.
  `PROBE_SEASON2=0` turns it off. A board that can't be read now stops the
  run with a plain message instead of a traceback.

No workflow change is needed. To hold the ceiling on board order, set
`CEILING_METRIC: "board"` in daily.yml's env.
