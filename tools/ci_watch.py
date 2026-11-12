#!/usr/bin/env python3
"""
Watch the GitHub Actions build for ppliver/cmcc-s4602-rv1109 and (optionally)
download the zboot.img artifact.

Usage:
  python3 tools/ci_watch.py                  # watch the latest run
  python3 tools/ci_watch.py --run <id>      # watch a specific run
  python3 tools/ci_watch.py --download DIR  # also download + extract artifact

Notes:
  - Token is read from D:/projects/bebop/ghtoken.txt (or --token-file).
  - GitHub artifact endpoints return a ZIP that must be extracted.
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request
import zipfile

REPO = "ppliver/cmcc-s4602-rv1109"
DEFAULT_TOKEN_FILE = r"D:/projects/bebop/ghtoken.txt"


def api(url, token, timeout=30):
    req = urllib.request.Request(url, headers={
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "s4602-ci-watch",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def latest_run(token):
    d = api("https://api.github.com/repos/%s/actions/runs?per_page=1" % REPO, token)
    runs = d.get("workflow_runs", [])
    if not runs:
        raise SystemExit("ERROR: no workflow runs found")
    return str(runs[0]["id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="specific run id (default: latest)")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--download", metavar="DIR", help="download + extract artifact into DIR")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--max-min", type=int, default=40)
    args = ap.parse_args()

    token = open(args.token_file, encoding="utf-8").read().strip()
    run = args.run or latest_run(token)
    url = "https://api.github.com/repos/%s/actions/runs/%s" % (REPO, run)
    print("[ci_watch] run %s  https://github.com/%s/actions/runs/%s" % (run, REPO, run), flush=True)

    rounds = int(args.max_min * 60 / args.interval)
    conclusion = None
    for i in range(rounds):
        try:
            d = api(url, token)
        except Exception as e:
            print("[ci_watch] poll error: %s" % e, flush=True)
            time.sleep(args.interval)
            continue
        st, concl = d.get("status"), d.get("conclusion")
        print("[ci_watch] t=%ds status=%s conclusion=%s" % (i * args.interval, st, concl), flush=True)
        if st == "completed":
            conclusion = concl
            break
        time.sleep(args.interval)

    if conclusion is None:
        print("[ci_watch] TIMEOUT (%d min) - run still not completed" % args.max_min, flush=True)
        return 2

    # Report jobs + failed steps so a failure is diagnosable from the console
    try:
        jobs = api(url + "/jobs", token)
        for j in jobs.get("jobs", []):
            print("[ci_watch]   job %-24s %s / %s" % (j["name"], j["status"], j.get("conclusion")), flush=True)
            for s in j.get("steps", []):
                if s.get("conclusion") not in (None, "success", "skipped"):
                    print("[ci_watch]     FAILED STEP: %s -> %s" % (s["name"], s.get("conclusion")), flush=True)
    except Exception as e:
        print("[ci_watch] jobs query failed: %s" % e, flush=True)

    arts = api(url + "/artifacts", token).get("artifacts", [])
    for a in arts:
        print("[ci_watch]   artifact %-32s id=%s size=%s expired=%s" % (
            a["name"], a["id"], a.get("size_in_bytes"), a.get("expired")), flush=True)

    if conclusion != "success":
        print("[ci_watch] BUILD FAILED (conclusion=%s)" % conclusion, flush=True)
        return 1

    if not args.download:
        print("[ci_watch] BUILD OK", flush=True)
        return 0

    if not arts:
        print("[ci_watch] no artifacts to download", flush=True)
        return 1

    os.makedirs(args.download, exist_ok=True)
    a = arts[0]
    dl = "https://api.github.com/repos/%s/actions/artifacts/%s/zip" % (REPO, a["id"])
    req = urllib.request.Request(dl, headers={
        "Authorization": "token " + token,
        "User-Agent": "s4602-ci-watch",
    })
    print("[ci_watch] downloading artifact %s (%s bytes) ..." % (a["name"], a.get("size_in_bytes")), flush=True)
    with urllib.request.urlopen(req, timeout=300) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        print("[ci_watch] zip entries: %s" % z.namelist(), flush=True)
        z.extractall(args.download)
    print("[ci_watch] extracted into %s" % args.download, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
