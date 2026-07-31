#!/usr/bin/env python3
"""Scrape r/aigamedev full history (posts + comments) via Arctic Shift API.

Usage:
    python3 scrape.py            # posts then comments
    python3 scrape.py posts
    python3 scrape.py comments

Both channels are resumable: a cursor file records the last created_utc seen,
so re-running only fetches what's new.

Posts are fetched with the full field set on purpose — this subreddit is heavy on
showcase posts, and the browser needs `media` / `gallery_data` / `media_metadata`
/ `preview` to embed images and video inline.
"""
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
import datetime as dt

BASE = "https://arctic-shift.photon-reddit.com/api"
SUB = "aigamedev"
LIMIT = 100
SLEEP_BETWEEN = 0.6
MAX_RETRIES = 6
HERE = os.path.dirname(os.path.abspath(__file__))

COMMENT_FIELDS = "author,body,created_utc,id,link_id,parent_id,score"

# r/aigamedev was created 2022-12-11; start a few months earlier to be safe.
START_TS = 1665000000  # 2022-10-05

CHANNELS = {
    "posts": {
        "url": f"{BASE}/posts/search?subreddit={SUB}&limit={LIMIT}&sort=asc",
        "out": os.path.join(HERE, "posts.jsonl"),
        "cursor": os.path.join(HERE, ".cursor_posts"),
    },
    "comments": {
        "url": f"{BASE}/comments/search?subreddit={SUB}&limit={LIMIT}&sort=asc"
               f"&fields={COMMENT_FIELDS}",
        "out": os.path.join(HERE, "comments.jsonl"),
        "cursor": os.path.join(HERE, ".cursor_comments"),
    },
}


def load_cursor(path):
    if os.path.exists(path):
        with open(path) as f:
            return int(f.read().strip())
    return START_TS


def save_cursor(path, ts):
    with open(path, "w") as f:
        f.write(str(ts))


def fetch(url, after):
    req = urllib.request.Request(
        f"{url}&after={after}",
        headers={"User-Agent": "aigamedev-gems-archive/1.0"},
    )
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.load(resp)["data"]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, http.client.IncompleteRead, KeyError) as e:
            wait = (attempt + 1) * 3
            print(f"  [retry {attempt + 1}/{MAX_RETRIES}] {e} - sleeping {wait}s",
                  file=sys.stderr, flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries at after={after}")


def scrape(name):
    ch = CHANNELS[name]
    cursor = load_cursor(ch["cursor"])
    total = 0
    if os.path.exists(ch["out"]):
        with open(ch["out"]) as f:
            total = sum(1 for _ in f)
        print(f"[{name}] resuming: {total} saved, cursor={cursor}", flush=True)
    else:
        print(f"[{name}] starting fresh", flush=True)

    out = open(ch["out"], "a")
    page = 0
    while True:
        page += 1
        items = fetch(ch["url"], cursor)
        if not items:
            print(f"[{name}] done — empty response at cursor={cursor}", flush=True)
            break

        for it in items:
            out.write(json.dumps(it, ensure_ascii=False) + "\n")
        out.flush()

        new_cursor = items[-1]["created_utc"]
        total += len(items)
        save_cursor(ch["cursor"], new_cursor)
        date_str = dt.datetime.fromtimestamp(new_cursor, dt.UTC).strftime("%Y-%m-%d")
        print(f"[{name}] page {page:>5}: +{len(items):>3} (total {total:>7}) [{date_str}]",
              flush=True)

        if len(items) < LIMIT:
            print(f"[{name}] last page ({len(items)} < {LIMIT}). Done.", flush=True)
            break

        # Identical timestamps across a whole page would loop forever; nudge by 1s.
        cursor = new_cursor if new_cursor > cursor else cursor + 1
        time.sleep(SLEEP_BETWEEN)

    out.close()
    print(f"[{name}] total saved: {total}", flush=True)


def main():
    which = sys.argv[1:] or ["posts", "comments"]
    for name in which:
        if name not in CHANNELS:
            sys.exit(f"unknown channel: {name} (expected posts/comments)")
        scrape(name)


if __name__ == "__main__":
    main()
