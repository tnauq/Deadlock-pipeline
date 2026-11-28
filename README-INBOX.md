# master-image-drop

A second download from the daily run: `deadlock-master-builds`, one PNG with
every hero's 12-slot elite build, EU beside NA, in the site's grid style,
sized to post on 4chan (PNG, under 4 MiB, under 10000 px a side).

- `make_master.py` (new). Renders `output/images/master_builds.png` from
  `docs/data.json`, `docs/calc/items.json` and `docs/icons/`. Zero API
  calls, zero SQL, about 5 seconds.
  - Fits the image to `MASTER_MAX_BYTES` (4 MiB less 16 KiB) and
    `MASTER_MAX_PX` (10000): it starts at 1.7x the original prototype's size,
    which is the 10000 px ceiling for 44 heroes, and steps down 0.05 at a
    time until the file fits. Each try is logged as `[master]`.
  - 256-colour indexed PNG, plus light edge-preserving smoothing of the
    paper grain in the item art only (text, borders and tier badges are
    untouched). Together they hold 1.7x at ~3.76 MiB; without opencv the
    same data lands at 1.5x.
  - Fails with a plain message, not a broken file, if nothing fits.
- `master_builds.js` (new). Runs `loadEliteBuild()` and `relatedIds()`,
  lifted at run time from `docs/index.html`, over every hero and region, so
  each grid is exactly what the site's calculator loads. If those functions
  are renamed it stops with the name it could not find.
- `fonts/` (new). Inter Bold, ExtraBold and Medium under the SIL Open Font
  License (`fonts/OFL.txt`); the runner has no Inter, and the DejaVu
  fallback looks noticeably different.

`daily.yml` needs one hand edit (the inbox has no PAT for workflow files):
paste the two steps in `daily-yml-step.yml` at the END of the job, after
"Upload aggregate CSVs", so the image can never delay the data commit. The
build step is `continue-on-error`: a failed image shows as a red annotation
on a green run and never blocks the refresh.

Not in this drop: no change to the site, the pipeline or the committed data.
