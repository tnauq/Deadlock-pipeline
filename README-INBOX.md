# board-season-drop (against main 928d8b5)

Pins every leaderboard read to ONE ranked season, and switches to the next
season automatically once its boards have filled.

- `board_season.py` (new). `LEADERBOARD_ID` = `auto` (default) or a number to pin.
  - Auto starts at 1001 (Season 1) and probes id + 1. It switches when, in every
    region, that board's general board has >= 900 entries
    (`BOARD_SWITCH_DEPTH`), its hero boards have a median >= 20 entries
    (`BOARD_SWITCH_HERO_MEDIAN`), and it isn't a copy of the current board.
  - The switch is saved to `archive/board_season.json`, which the commit step
    already commits, so it sticks and Season 3 (1003) is detected the same way.
  - Whatever board is chosen, if any region's general board has under 500
    entries (`BOARD_MIN_DEPTH`), the run stops and the site keeps its last
    refresh.
- `deadlock_pipeline.py`, `ceiling_rank.py`: all four leaderboard URLs carry
  `?leaderboard_id=N`. The pipeline decides; ceiling_rank reuses its decision
  from `output/board_choice.json`.

No workflow change is needed.
