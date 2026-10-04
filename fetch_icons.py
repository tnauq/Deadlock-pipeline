#!/usr/bin/env python3
"""
Download every hero and item icon referenced by the pipeline CSVs into ./icons/
and keep the site's self-hosted copies in ./docs/icons/ current.
make_images.py then reads from ./icons/ and never touches the network.

    python3 fetch_icons.py

Reads  ./output/tierlist.csv, ./output/item_frequency.csv, ./output/roster.csv
Writes ./icons/<sha1>.png  plus  ./icons/index.json  (url -> filename)
       ./docs/icons/<sha1>.png  (new icons, and icons whose art changed)

WHY EVERY RUN, AND WHY OVERWRITE (2026-10-04). A file is named by the sha1 of
its URL, and the asset bucket keeps the URL when Valve redraws an item
(.../items/weapon/melee_charge.png is the same path before and after). The old
flow skipped any icon already on disk and the workflow copied with `cp -n`,
which never overwrites, so docs/icons kept its first copy forever: 196 of its
197 icons were committed on 2026-09-02 (Rat King's card on 2026-10-03) and not
one had ever been updated. The page tries docs/icons before the bucket, so
the stale copy always won.

Now every referenced icon is downloaded each run (about 200 small files; in CI
./icons/ starts empty anyway, so this costs nothing new) and any site copy
whose bytes differ is replaced. A download only replaces a site copy if it is
a complete image (PNG/WebP/JPEG signature, and a PNG must end in IEND), so an
error page or a truncated transfer can never overwrite good art. The commit
step already adds docs/icons, so changed files are published with the run.
The workflow's later `cp -n icons/*.png docs/icons/` finds nothing left to do.
"""

import csv
import hashlib
import json
import os
import sys
import urllib.request

ICONS = "icons"
INDEX = os.path.join(ICONS, "index.json")
SITE_ICONS = os.path.join("docs", "icons")


def urls_from_csvs():
    urls = set()
    # roster.csv carries every RELEASED hero, including one with no pool yet —
    # a new hero's art has to be fetched before its stats exist, or the site
    # lists it as NEW with a blank tile (added 2026-10-03, for Rat King).
    for name, col in (("tierlist.csv", "icon_url"), ("item_frequency.csv", "icon_url"),
                      ("roster.csv", "icon_url")):
        path = os.path.join("output", name)
        if not os.path.exists(path):
            continue
        for r in csv.DictReader(open(path)):
            u = (r.get(col) or "").strip()
            if u:
                urls.add(u)
    return urls


def complete_image(data):
    """True for a whole PNG, WebP or JPEG. Anything else — an HTML error page,
    an empty body, a transfer cut short — must not replace a good icon."""
    if not data or len(data) < 64:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data[-8:] == b"IEND\xaeB`\x82"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return int.from_bytes(data[4:8], "little") + 8 == len(data)
    if data[:3] == b"\xff\xd8\xff":
        return data[-2:] == b"\xff\xd9"
    return False


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-icons/1.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    os.makedirs(ICONS, exist_ok=True)
    os.makedirs(SITE_ICONS, exist_ok=True)
    index = {}
    if os.path.exists(INDEX):
        index = json.load(open(INDEX))

    urls = urls_from_csvs()
    fetched, failed, bad = 0, 0, 0
    added, updated, same = [], [], 0
    for u in sorted(urls):
        fn = hashlib.sha1(u.encode()).hexdigest() + ".png"
        index[u] = fn
        try:
            data = fetch(u)
        except Exception as e:
            print("  [icons] FAIL %s (%s) — keeping any copy already on disk"
                  % (u.split("/")[-1], e), file=sys.stderr)
            failed += 1
            continue
        if not complete_image(data):
            print("  [icons] NOT AN IMAGE %s (%d bytes) — keeping any copy already "
                  "on disk" % (u.split("/")[-1], len(data or b"")), file=sys.stderr)
            bad += 1
            continue
        fetched += 1
        with open(os.path.join(ICONS, fn), "wb") as f:
            f.write(data)
        site = os.path.join(SITE_ICONS, fn)
        old = None
        if os.path.exists(site):
            with open(site, "rb") as f:
                old = f.read()
        if old == data:
            same += 1
            continue
        with open(site, "wb") as f:
            f.write(data)
        (added if old is None else updated).append(u.split("/")[-1])

    json.dump(index, open(INDEX, "w"), indent=0)
    print("[icons] %d fetched, %d failed, %d not an image; site copies: %d new, "
          "%d updated (art changed upstream), %d unchanged"
          % (fetched, failed, bad, len(added), len(updated), same), file=sys.stderr)
    for label, names in (("new", added), ("updated", updated)):
        if names:
            print("  [icons] %s: %s%s" % (label, ", ".join(names[:40]),
                                          " (+%d more)" % (len(names) - 40)
                                          if len(names) > 40 else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
