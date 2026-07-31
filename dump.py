#!/usr/bin/env python3
"""Dump selected posts + full comment threads as markdown for local LLM research.

Writes research/<created>-<id>-<slug>.md (one file per post, full nested thread,
OP replies marked) plus research/INDEX.md. Feed the directory (or single files)
to `claude -p` for deep analysis. LOCAL-ONLY: research/ is gitignored.

Examples:
    python3 dump.py                              # all gems
    python3 dump.py --status gem ok               # gems + ok
    python3 dump.py --tags workflow assets        # only these tags
    python3 dump.py --ids 1abc2d 3ef45g           # specific posts
    python3 dump.py --since 2026-01-01 --min-score 10
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")
THREADS_DIR = os.path.join(HERE, "threads")
MEDIA_DIR = os.path.join(HERE, "media")
OUT_DIR = os.path.join(HERE, "research")
SUB = "aigamedev"


def slugify(s, n=50):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:n] or "untitled"


def render_comment(c, depth=0):
    indent = "  " * depth
    who = f"**{c['author']}**" + (" 🔵OP" if c.get("is_op") else "")
    when = dt.datetime.fromtimestamp(c["created_utc"]).strftime("%Y-%m-%d")
    body = c["body"].replace("\n", f"\n{indent}> ")
    lines = [f"{indent}- {who} ({c['score']} pts, {when}):",
             f"{indent}> {body}"]
    for r in c.get("replies", []):
        lines.append(render_comment(r, depth + 1))
    return "\n".join(lines)


def render_post(meta, thread, still_name=None):
    created = dt.datetime.fromtimestamp(meta["created_utc"]).strftime("%Y-%m-%d")
    lines = [
        f"# {meta['title']}",
        "",
        f"- reddit: https://www.reddit.com/r/{SUB}/comments/{meta['id']}/",
        f"- author: u/{meta['author']}  date: {created}  "
        f"score: {meta['score']}  comments: {meta['num_comments']}",
        f"- verdict: {meta['status']}"
        + (f"  tags: {', '.join(meta.get('tags', []))}" if meta.get("tags") else ""),
    ]
    if meta.get("summary_zh"):
        lines.append(f"- summary: {meta['summary_zh']}")
    # Showcase posts carry their substance in the media, not the text — say so,
    # so the reading LLM doesn't treat an empty body as an empty post.
    if meta.get("kind") and meta["kind"] not in ("self", "link"):
        lines.append(f"- media: {meta['kind']}"
                     + (f" ({len(meta['gallery'])} images)" if meta.get("gallery") else ""))
    if still_name:
        # Sits right next to the .md so `claude -p --add-dir research` can open it.
        lines += ["", f"![contact sheet of the post's media]({still_name})", "",
                  "*(frames/images rendered locally by shots.py — the model can look "
                  "at this instead of guessing what the demo showed)*"]
    lines += ["", "## Post body", ""]
    lines.append(thread.get("selftext") or "*(no body / link or media post)*")
    if meta.get("url"):
        lines.append(f"\nlink: {meta['url']}")
    lines += ["", f"## Comment thread ({meta['num_comments']} comments)", ""]
    comments = thread.get("comments", [])
    lines.append("\n".join(render_comment(c) for c in comments)
                 if comments else "*(no archived comments)*")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", nargs="+", default=["gem"],
                    choices=["gem", "ok", "hype", "pending", "filtered"])
    ap.add_argument("--tags", nargs="+", default=[])
    ap.add_argument("--ids", nargs="+", default=[])
    ap.add_argument("--since", default="")
    ap.add_argument("--min-score", type=int, default=0)
    args = ap.parse_args()

    with open(DATA) as f:
        posts = json.load(f)["posts"]

    since_ts = 0
    if args.since:
        since_ts = dt.datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    selected = []
    for p in posts:
        if args.ids:
            if p["id"] in args.ids:
                selected.append(p)
            continue
        if p["status"] not in args.status:
            continue
        if args.tags and not set(args.tags) & set(p.get("tags", [])):
            continue
        if p["created_utc"] < since_ts or p["score"] < args.min_score:
            continue
        selected.append(p)

    os.makedirs(OUT_DIR, exist_ok=True)
    index_lines = ["# Research dump index", ""]
    written = skipped = stills = 0
    for p in sorted(selected, key=lambda x: -x["score"]):
        tpath = os.path.join(THREADS_DIR, f"{p['id']}.json")
        if not os.path.exists(tpath):
            skipped += 1
            continue
        with open(tpath) as f:
            thread = json.load(f)
        created = dt.datetime.fromtimestamp(p["created_utc"]).strftime("%Y%m%d")
        stem = f"{created}-{p['id']}-{slugify(p['title'])}"
        still_name = None
        src_still = os.path.join(MEDIA_DIR, f"{p['id']}.jpg")
        if os.path.exists(src_still):
            still_name = f"{stem}.jpg"
            shutil.copyfile(src_still, os.path.join(OUT_DIR, still_name))
            stills += 1
        with open(os.path.join(OUT_DIR, f"{stem}.md"), "w") as f:
            f.write(render_post(p, thread, still_name))
        fname = f"{stem}.md"
        index_lines.append(
            f"- [{p['title']}]({fname}) — {p['status']}, {p['score']} pts"
            + (f" — {p['summary_zh']}" if p.get("summary_zh") else ""))
        written += 1

    with open(os.path.join(OUT_DIR, "INDEX.md"), "w") as f:
        f.write("\n".join(index_lines) + "\n")
    print(f"research/: {written} posts dumped, {stills} with a media contact sheet"
          + (f" ({skipped} skipped, no thread file — not prefilter candidates)"
             if skipped else ""))


if __name__ == "__main__":
    main()
