#!/usr/bin/env python3
"""
Download every hero and item icon referenced by the pipeline CSVs into ./icons/,
skipping any already on disk. Run once (and again only when a patch adds heroes
or items). make_images.py then reads from ./icons/ and never touches the network.

    python3 fetch_icons.py

Reads  ./output/tierlist.csv, ./output/item_frequency.csv
Writes ./icons/<sha1>.png  plus  ./icons/index.json  (url -> filename)
"""

import csv
import hashlib
import json
import os
import sys
import urllib.request

ICONS = "icons"
INDEX = os.path.join(ICONS, "index.json")


def urls_from_csvs():
    urls = set()
    for name, col in (("tierlist.csv", "icon_url"), ("item_frequency.csv", "icon_url")):
        path = os.path.join("output", name)
        if not os.path.exists(path):
            continue
        for r in csv.DictReader(open(path)):
            u = (r.get(col) or "").strip()
            if u:
                urls.add(u)
    return urls


def main():
    os.makedirs(ICONS, exist_ok=True)
    index = {}
    if os.path.exists(INDEX):
        index = json.load(open(INDEX))

    urls = urls_from_csvs()
    fetched, skipped, failed = 0, 0, 0
    for u in sorted(urls):
        fn = hashlib.sha1(u.encode()).hexdigest() + ".png"
        dest = os.path.join(ICONS, fn)
        index[u] = fn
        if os.path.exists(dest):
            skipped += 1
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "deadlock-icons/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            fetched += 1
        except Exception as e:
            print("  [icons] FAIL %s (%s)" % (u.split("/")[-1], e), file=sys.stderr)
            failed += 1

    json.dump(index, open(INDEX, "w"), indent=0)
    print("[icons] %d fetched, %d already present, %d failed, %d total"
          % (fetched, skipped, failed, len(index)), file=sys.stderr)


if __name__ == "__main__":
    main()
