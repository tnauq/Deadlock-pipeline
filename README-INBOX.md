# Inbox drop: calculator ability points (2026-10-06)

    docs/index.html        MODIFIED - ability points for heroes with no builds yet; the level slider updates them
    build_calc_data.py     MODIFIED - the calculator bundle lists every hero's four abilities

Before editing, I checked both files against live main (aeaa000): their blob
hashes matched the versions these edits started from.

## Two bugs, one panel

1. **A hero with no builds got no ability panel.** The calculator read a
   hero's four ability slots only from the site's build data, and a hero with
   no sampled builds (Baba, hours after release) has none. The panel silently
   didn't draw.
   - Now `heroes.json` in the calculator bundle carries every hero's slots
     (`"abilities": {"1": id, ... "4": id}`, from the assets' signature1-4).
   - The page falls back to them when there's no build data.
   - Names and icons fall back to the bundle's ability records.
   - The imbue picker uses the same fallback.
2. **The level slider never updated the ability budget, for any hero.** While
   the slider moves, only the stat panels follow it, and the ability panel
   kept the level it was drawn at. From level 1, one unlock could be bought;
   at level 36, "Unlocks 0/1 · Points 0/0" still showed and 12 of 16 buttons
   stayed disabled.
   - Now the ability panel redraws when the slider settles (release or key
     press).
   - Only its own buttons are rebuilt, so the slider keeps focus and dragging
     stays smooth.

Also removed: a second, identical copy of the imbue helpers (`imbueDefault`,
`applyImbueDefaults`, `drawImbue`). The later copy was the one in effect, and
the fix would otherwise have to be made twice.

## When it shows

- **The slider fix** works as soon as the page is published.
- **Baba's ability panel** needs the next refresh, because the Build
  calculator data step has to write the new `heroes.json`.

## Verified (headless Chromium on the live data.json and calculator bundle)

- **Baba, with the new `heroes.json`:**
  - The panel shows Threadsap, Granny Long Legs, Baba's Brew and Feed The
    Birds; his slots are taken from heroes.vdata, build 6757.
  - At level 1 the budget is 0/1 · 0/0. Sliding to 36 gives 1/4 · 0/32.
  - All four abilities upgrade fully to 4/4 · 32/32.
  - The imbue picker offers his four abilities.
  - The ability detail opens.
- **Abrams:** the slots still come from the region's build data (checked
  equal), and the ability order strip is unchanged.
- **The slider:** after sliding to 36, Abrams goes from 12 disabled buttons
  (live page) to 0.
- **`build_calc_data.py`** on mock assets writes four slots for every
  released hero, all present in `abilities.json`. A hero whose slots don't
  resolve is named in a warning.
- **The page script passes a syntax check,** and no page errors occurred.
- **Inbox:** the drop was unpacked against a fresh clone of main.
