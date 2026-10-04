# Inbox drop: icon refresh (2026-10-04)

    fetch_icons.py   MODIFIED - downloads every icon each run and replaces site copies whose art changed

Before editing, I checked it against live main (a448478): its blob hash
matched the drop-1 version this edit started from. No workflow file changes.

## Why the site kept the old art

- **The filename never changes.** Icons are saved as
  `docs/icons/<sha1 of the URL>.png`. When Valve redraws an item, the asset
  bucket keeps the same URL (`.../items/weapon/melee_charge.png`), so the file
  keeps the same name.
- **Nothing ever overwrote the old file.** `fetch_icons.py` skipped icons
  already on disk, and the workflow copies with `cp -n`, which never
  overwrites. 196 of the 197 files in `docs/icons` were committed on
  2026-09-02, plus Rat King's card on 2026-10-03, and none has ever been
  updated.
- **The page loads that file first.** It uses `docs/icons` before the bucket,
  so the stale copy always wins. That covers item icons (160) and hero cards
  (39).
- **Ability icons were never affected.** They load straight from the bucket.

## Now

- **Every referenced icon is downloaded every run,** about 200 small files. In
  CI `./icons/` starts empty, so that was already happening; there's no new
  cost.
- **A site copy whose bytes differ is replaced.** Only a complete image can
  replace one: it must have a PNG, WebP or JPEG signature, and a PNG must end
  in IEND. An error page, a failed download or a cut-off transfer leaves the
  old copy in place.
- **New art is published by the existing commit step,** which already adds
  `docs/icons`. The `cp -n` line in the workflow now has nothing left to do.

## In the log (the "Fetch any new icons" step)

```
[icons] 199 fetched, 0 failed, 0 not an image; site copies: 0 new, 41 updated (art changed upstream), 158 unchanged
  [icons] updated: melee_charge.png, close_quarters.png, ...
```

(Numbers illustrative.) If "updated" is 0, the asset bucket hasn't
re-exported the new art yet, and the first run after it does will pick it up.
Browsers can hold an old image for about 10 minutes (GitHub Pages caching), so
a reload after that shows the new art.

## Verified

- **All 197 current icons pass the completeness check,** so nothing that works
  today would be refused.
- **Simulated run:**
  - New art at an unchanged URL replaced the site copy, and git shows the file
    modified, ready to commit.
  - Identical art was left untouched.
  - An HTML error page and a failed download each kept the old copy.
  - A new item's icon was added.
  - A truncated PNG was not added.
- **Inbox:** the drop was unpacked against a fresh clone of main.
