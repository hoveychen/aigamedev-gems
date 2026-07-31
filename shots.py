#!/usr/bin/env python3
"""Pre-render one still per media post into media/<id>.jpg, so an agent can SEE it.

Why this exists: half of r/aigamedev is video, and an LLM reading a post gets the
title, the body and the thread — but not the artifact. A 2x2 contact sheet of frames
turns "I made a bubble shooter" into something a model can actually judge (measured:
ffmpeg pulls a reddit mp4 and writes a 4-frame tile in ~0.8s).

    python3 shots.py                 # candidates (data.json) missing a still
    python3 shots.py --all           # include prefilter-rejected posts too
    python3 shots.py --ids 1pe8r3x   # specific posts (re-renders even if present)
    python3 shots.py --limit 50 --workers 8

Idempotent: a post whose media/<id>.jpg already exists is skipped, so CI only pays
for what's new. Posts Reddit blocks (a measured ~10% of archived videos 403 on every
url and header combination) are recorded in media/.failed so repeat runs don't retry
them forever — delete that file to force a retry.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")
FILTERED = os.path.join(HERE, "filtered.json")
OUT_DIR = os.path.join(HERE, "media")
FAILED_PATH = os.path.join(OUT_DIR, ".failed")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TILE_W = 300          # per-cell width; a 2x2 sheet lands around 20-50 KB at q=4
SINGLE_W = 900        # a lone image gets real resolution — plenty of posts here are
                      # screenshots of code, prompts or comment threads, and those are
                      # unreadable at tile width (measured: a wide comment screenshot
                      # scaled to 300px came out 3 KB and illegible)
JPEG_Q = 4            # ffmpeg -q:v (2 best … 31 worst)
FRAMES = 4            # cells in the contact sheet
FFMPEG_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 45
MEDIA_KINDS = ("reddit_video", "gallery", "image", "youtube")


def run(cmd, timeout=FFMPEG_TIMEOUT):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def video_sheet(post, out):
    """4 frames spread over the clip, tiled 2x2."""
    v = post.get("video") or {}
    src = v.get("mp4") or v.get("hls")
    if not src:
        return False, "no video url"
    # Probe the duration so the frames span the whole clip instead of the first
    # seconds — a title card 4x over is useless.
    dur = 0.0
    p = run(["ffprobe", "-hide_banner", "-loglevel", "error", "-user_agent", UA,
             "-show_entries", "format=duration", "-of", "csv=p=0", src], timeout=60)
    if p.returncode == 0:
        try:
            dur = float((p.stdout or "0").strip())
        except ValueError:
            dur = 0.0
    step = max(dur / (FRAMES + 1), 0.5) if dur > 1 else 1.0
    vf = (f"fps=1/{step:.3f},scale={TILE_W}:-2,tile=2x2")
    r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-user_agent", UA,
             "-i", src, "-vf", vf, "-frames:v", "1", "-q:v", str(JPEG_Q), "-y", out])
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
        return True, f"video {dur:.0f}s"
    return False, (r.stderr or "ffmpeg failed").strip()[:120]


def images_sheet(urls, out, label):
    """Download up to 4 images; tile them, or scale a lone image up to SINGLE_W."""
    urls = [u for u in urls if u][:FRAMES]
    if not urls:
        return False, "no image urls"
    with tempfile.TemporaryDirectory() as td:
        raw = []
        for i, u in enumerate(urls):
            path = os.path.join(td, f"raw_{i:02d}")
            try:
                fetch(u, path)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
                continue
            if os.path.getsize(path) > 500:
                raw.append(path)
        if not raw:
            return False, "all image downloads failed"

        # Normalise through ffmpeg so webp/png/gif all become jpg at one width.
        width = SINGLE_W if len(raw) == 1 else TILE_W
        seq = 0
        for path in raw:
            norm = os.path.join(td, f"seq_{seq + 1:02d}.jpg")
            r = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
                     "-vf", f"scale={width}:-2", "-frames:v", "1", "-q:v",
                     str(JPEG_Q), "-y", norm])
            if r.returncode == 0 and os.path.exists(norm):
                seq += 1
        if seq == 0:
            return False, "ffmpeg could not decode any download"
        if seq == 1:
            shutil.copyfile(os.path.join(td, "seq_01.jpg"), out)
            return True, f"{label} 1 image @{width}px"
        # tile needs a full grid; 2 images tile 2x1, 3-4 tile 2x2 (last cell blank)
        grid = "2x1" if seq == 2 else "2x2"
        r = run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-i", os.path.join(td, "seq_%02d.jpg"),
                 "-vf", f"tile={grid}", "-frames:v", "1", "-q:v", str(JPEG_Q),
                 "-y", out])
        if r.returncode == 0 and os.path.exists(out):
            return True, f"{label} {seq} images"
        return False, (r.stderr or "tile failed").strip()[:120]


def render(post):
    """-> (id, ok, note). Writes media/<id>.jpg."""
    pid = post["id"]
    out = os.path.join(OUT_DIR, f"{pid}.jpg")
    kind = post.get("kind")
    try:
        if kind == "reddit_video":
            ok, note = video_sheet(post, out)
            if not ok and post.get("thumbnail"):
                # Blocked video: at least keep the still Reddit does serve.
                ok, note = images_sheet([post["thumbnail"]], out, "video-still")
        elif kind == "gallery":
            ok, note = images_sheet(post.get("gallery") or [post.get("thumbnail")],
                                    out, "gallery")
        elif kind == "image":
            # The original first, alone, so it gets SINGLE_W. Only fall back to the
            # 140px reddit thumbnail if the original is gone.
            ok, note = images_sheet([post.get("url")], out, "image")
            if not ok and post.get("thumbnail"):
                ok, note = images_sheet([post["thumbnail"]], out, "image-thumb")
        elif kind == "youtube":
            # YouTube's own poster frames are public; no yt-dlp needed.
            yid = post.get("yt_id")
            urls = ([f"https://i.ytimg.com/vi/{yid}/maxresdefault.jpg",
                     f"https://i.ytimg.com/vi/{yid}/hqdefault.jpg"] if yid else [])
            urls.append(post.get("thumbnail"))
            ok, note = images_sheet(urls[:1] or urls, out, "youtube")
            if not ok:
                ok, note = images_sheet(urls[1:], out, "youtube")
        else:
            return pid, False, f"kind={kind} has no media"
    except subprocess.TimeoutExpired:
        return pid, False, "timeout"
    except Exception as e:                        # keep one bad post from killing the run
        return pid, False, f"{type(e).__name__}: {e}"[:120]
    if not ok and os.path.exists(out):
        os.remove(out)
    return pid, ok, note


def load_posts(include_filtered):
    with open(DATA) as f:
        posts = json.load(f)["posts"]
    if include_filtered and os.path.exists(FILTERED):
        with open(FILTERED) as f:
            posts += json.load(f)
    return posts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="include prefilter-rejected posts (filtered.json)")
    ap.add_argument("--ids", nargs="+", default=[],
                    help="render these post ids even if a still already exists")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--retry-failed", action="store_true",
                    help="also retry posts recorded in media/.failed")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    failed_before = set()
    if os.path.exists(FAILED_PATH) and not args.retry_failed:
        with open(FAILED_PATH) as f:
            failed_before = {l.strip() for l in f if l.strip()}

    posts = load_posts(args.all or bool(args.ids))
    if args.ids:
        want = set(args.ids)
        todo = [p for p in posts if p["id"] in want]
    else:
        todo = [p for p in posts
                if p.get("kind") in MEDIA_KINDS
                and p["id"] not in failed_before
                and not os.path.exists(os.path.join(OUT_DIR, f"{p['id']}.jpg"))]
        # Highest-engagement first, so a --limit run covers what matters most.
        todo.sort(key=lambda p: -((p.get("score") or 0) + 2 * (p.get("num_comments") or 0)))
    if args.limit:
        todo = todo[:args.limit]

    have = len([n for n in os.listdir(OUT_DIR) if n.endswith(".jpg")])
    print(f"stills present={have} skipped_failed={len(failed_before)} "
          f"to_render={len(todo)}", flush=True)
    if not todo:
        return

    ok = bad = 0
    new_failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(render, p): p for p in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            pid, good, note = fut.result()
            if good:
                ok += 1
            else:
                bad += 1
                new_failed.append(pid)
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}  ok={ok} failed={bad}   last: {pid} {note[:60]}",
                      flush=True)

    if new_failed:
        # Rewrite sorted+deduped rather than appending, so daily CI runs don't grow
        # the file with repeats.
        every = failed_before | set(new_failed)
        with open(FAILED_PATH, "w") as f:
            f.write("".join(pid + "\n" for pid in sorted(every)))
    total_bytes = sum(os.path.getsize(os.path.join(OUT_DIR, n))
                      for n in os.listdir(OUT_DIR) if n.endswith(".jpg"))
    print(f"done: +{ok} stills, {bad} failed. media/ now "
          f"{len([n for n in os.listdir(OUT_DIR) if n.endswith('.jpg')])} files, "
          f"{total_bytes / 1e6:.0f} MB", flush=True)


if __name__ == "__main__":
    main()
