#!/usr/bin/env python3
"""
probe_leaderboard_raw.py — what does /raw return that the processed board drops?

The processed general board now carries only four fields — account_name,
possible_account_ids, rank, top_hero_ids (probed 2026-08-07). There is no
account_id, and badge_level / ranked_rank / ranked_subrank are gone from the
response entirely rather than merely empty. Identity resolution therefore
rests wholly on possible_account_ids, which PROBES.md already records as
unreliable: 17.5% of ids are claimed by more than one entry, and matching on
it alone put the wrong player in 110 of 371 slots.

The OpenAPI spec exposes two endpoints nobody here has read:

    /v1/leaderboard/{region}/raw
    /v1/leaderboard/{region}/{hero_id}/raw

If those return the unprocessed Valve payload they may still carry a real
account_id and the badge fields, which would fix identity resolution outright
and give a second read on whether badge has recovered.

Questions:
  Q1  Does /raw respond at all, and in what format? It may be protobuf or
      base64 rather than JSON, so the body is sniffed before parsing.
  Q2  What fields does a raw entry carry that the processed one does not?
  Q3  Is there a real account_id, and does it agree with the processed
      board's possible_account_ids[0]?
  Q4  Are badge_level / ranked_rank / ranked_subrank present and populated?

Cost: ZERO SQL. Leaderboard is 100 req/s on its own bucket.

    python3 probe_leaderboard_raw.py

Writes probe_out/leaderboard_raw.json (and the raw body if it is not JSON).
Stdlib only.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

BASE = "https://api.deadlock-api.com"
API_KEY = os.environ.get("DEADLOCK_API_KEY")
OUT = "probe_out"
REGION = os.environ.get("PROBE_REGION") or "NAmerica"
HERO = int(os.environ.get("PROBE_HERO") or 15)      # Bebop: deep board


def fetch(path):
    """Returns (status, content_type, body_bytes, error)."""
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": "deadlock-probe/1.0"})
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(), None
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()[:400], "HTTP %d" % e.code
    except Exception as e:
        return 0, "", b"", str(e)


def sniff(body):
    """JSON? protobuf? base64? Say which rather than assuming."""
    if not body:
        return "empty", None
    head = body[:1].decode("latin-1")
    if head in "[{":
        try:
            return "json", json.loads(body.decode("utf-8"))
        except Exception as e:
            return "json-invalid:%s" % e, None
    # a base64 blob of protobuf is the other plausible shape
    txt = body[:200].decode("latin-1", "replace")
    if all(c.isalnum() or c in "+/=\n\r" for c in txt.strip()) and len(body) > 32:
        try:
            base64.b64decode(body, validate=True)
            return "base64", None
        except Exception:
            pass
    return "binary", None


def entries(payload):
    if isinstance(payload, dict):
        for k in ("entries", "leaderboard", "data", "players"):
            if isinstance(payload.get(k), list):
                return payload[k]
        return []
    return payload if isinstance(payload, list) else []


def field_census(rows):
    present, populated, sample = Counter(), Counter(), {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            present[k] += 1
            if v not in (None, "", 0, [], {}):
                populated[k] += 1
                sample.setdefault(k, v)
    return {k: {"present": present[k], "populated": populated[k],
                "sample": str(sample.get(k))[:80]}
            for k in sorted(present)}


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {"region": REGION, "hero": HERO}
    rq = urllib.parse.quote(REGION)

    targets = [
        ("general_processed", "/v1/leaderboard/%s" % rq),
        ("general_raw", "/v1/leaderboard/%s/raw" % rq),
        ("hero_processed", "/v1/leaderboard/%s/%d" % (rq, HERO)),
        ("hero_raw", "/v1/leaderboard/%s/%d/raw" % (rq, HERO)),
    ]

    parsed = {}
    for name, path in targets:
        print("[.] %s" % path, file=sys.stderr)
        status, ctype, body, err = fetch(path)
        kind, payload = sniff(body)
        rows = entries(payload) if payload is not None else []
        parsed[name] = rows
        report[name] = {
            "path": path, "status": status, "content_type": ctype,
            "bytes": len(body), "format": kind, "error": err,
            "n_entries": len(rows),
            "fields": field_census(rows) if rows else {},
        }
        if rows:
            report[name]["sample_entry"] = rows[0]
        elif kind != "json" and body:
            # keep the head of a non-JSON body so the shape can be identified
            fn = os.path.join(OUT, "leaderboard_%s.bin" % name)
            open(fn, "wb").write(body[:4096])
            report[name]["body_head_file"] = fn
            report[name]["body_head_hex"] = body[:48].hex()

    # ---- Q3: does raw carry an account_id, and does it agree? -----------
    agree = {}
    proc, raw = parsed.get("general_processed") or [], parsed.get("general_raw") or []
    if proc and raw:
        by_name = {}
        for r in raw:
            if isinstance(r, dict) and r.get("account_name"):
                by_name[r["account_name"]] = r
        checked = match = missing = 0
        for p in proc[:200]:
            if not isinstance(p, dict):
                continue
            r = by_name.get(p.get("account_name"))
            if not r:
                missing += 1
                continue
            aid = r.get("account_id")
            if aid is None:
                continue
            checked += 1
            cand = [int(x) for x in (p.get("possible_account_ids") or [])[:2]]
            if int(aid) in cand:
                match += 1
        agree = {"checked": checked, "raw_id_in_candidates": match,
                 "names_missing_from_raw": missing,
                 "agreement_pct": round(100.0 * match / checked, 1) if checked else None}
    report["id_agreement"] = agree

    json.dump(report, open(os.path.join(OUT, "leaderboard_raw.json"), "w"),
              indent=1, default=str)

    # ---- summary -------------------------------------------------------
    print("\n=== endpoints ===")
    for name, _ in targets:
        b = report[name]
        print("  %-18s %-3s %-28s %-9s %6d bytes  %4d entries %s"
              % (name, b["status"], b["content_type"][:28], b["format"],
                 b["bytes"], b["n_entries"], b["error"] or ""))

    for name in ("general_processed", "general_raw", "hero_raw"):
        b = report.get(name) or {}
        if not b.get("fields"):
            continue
        print("\n=== %s fields ===" % name)
        for k, v in b["fields"].items():
            print("  %-24s %5d/%-5d  e.g. %s"
                  % (k, v["populated"], v["present"], v["sample"]))

    pf = set((report.get("general_processed") or {}).get("fields") or {})
    rf = set((report.get("general_raw") or {}).get("fields") or {})
    if rf:
        print("\n=== what raw adds ===")
        print("  only in raw:      %s" % (sorted(rf - pf) or "nothing"))
        print("  only in processed:%s" % (sorted(pf - rf) or "nothing"))

    if agree:
        print("\n=== identity check ===")
        print("  raw account_id inside processed possible_account_ids[:2]: "
              "%s of %s (%s%%)" % (agree["raw_id_in_candidates"], agree["checked"],
                                   agree["agreement_pct"]))
        print("  names on the processed board missing from raw: %d"
              % agree["names_missing_from_raw"])

    print("\nwrote %s/leaderboard_raw.json" % OUT)


if __name__ == "__main__":
    main()
