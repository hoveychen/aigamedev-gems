#!/usr/bin/env python3
"""Build the browser dataset: data.json (post index) + threads/<id>.json (full threads).

Inputs:  posts.jsonl, comments.jsonl, candidates.jsonl, classifications.jsonl
Outputs: data.json          — candidate/classified posts with preview + media, newest first
         filtered.json      — prefilter-rejected posts, light fields but WITH media
                              (the UI lazy-loads this only when the filtered chip is on)
         threads/<id>.json  — full post body + nested comment tree, one file per
                              prefilter-passed post (fetched on demand by the UI)

Post status: "filtered"  failed prefilter (still listed, de-emphasized)
             "pending"   passed prefilter, awaiting local LLM classification
             "gem"/"ok"/"hype"  LLM verdicts from classifications.jsonl

Media matters here in a way it doesn't in the sibling ai-trading-gems: half of
r/aigamedev is video / image / gallery / YouTube posts (5,410 of 10,800; another 860
are external links), so every record carries the
fields the reader needs to embed the artifact inline (hls+mp4 for v.redd.it, the
full image list for galleries, the YouTube id, a thumbnail fallback). Filtered posts
keep their media too — a low-engagement demo is still worth *looking* at even when
it never earns an LLM verdict.
"""
import json
import os
import re
import shutil
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.jsonl")
COMMENTS = os.path.join(HERE, "comments.jsonl")
CANDIDATES = os.path.join(HERE, "candidates.jsonl")
CLASSIFICATIONS = os.path.join(HERE, "classifications.jsonl")
DATA_OUT = os.path.join(HERE, "data.json")
FILTERED_OUT = os.path.join(HERE, "filtered.json")
THREADS_DIR = os.path.join(HERE, "threads")
SUB = "aigamedev"

PREVIEW_LEN = 400
PREVIEW_LEN_FILTERED = 160   # filtered posts are the bulk of the index; keep them light
EMPTY = {"", "[removed]", "[deleted]"}
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL_RE = re.compile(r"https?://\S+")
BAD_THUMBS = {"default", "self", "nsfw", "spoiler", "image", ""}


def preview_text(body):
    """Plain-text preview: markdown links -> text, raw URLs dropped, md noise out."""
    t = MD_LINK_RE.sub(r"\1", body)
    t = URL_RE.sub("", t)
    t = re.sub(r"[#*`>|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_jsonl_by_id(path):
    d = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    d[obj["id"]] = obj
                except (json.JSONDecodeError, KeyError):
                    continue
    return d


def unescape(u):
    return (u or "").replace("&amp;", "&")


def media_kind(post):
    url = post.get("url") or ""
    if re.match(r"https?://i\.redd\.it/", url) or \
            re.search(r"\.(?:png|jpe?g|gif|webp)(?:\?|$)", url, re.I):
        return "image"
    if "v.redd.it" in url:
        return "reddit_video"
    if "reddit.com/gallery" in url:
        return "gallery"
    if re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)", url):
        return "youtube"
    if re.match(r"https?://", url) and f"/r/{SUB}" not in url:
        return "link"
    return "self"


def extract_yt_id(url):
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{6,})", url or "")
    return m.group(1) if m else None


def extract_reddit_video(post):
    rv = ((post.get("media") or {}).get("reddit_video")
          or (post.get("secure_media") or {}).get("reddit_video") or {})
    if not rv:
        return None
    out = {}
    if rv.get("is_gif"):
        out["is_gif"] = True
    if rv.get("hls_url"):
        out["hls"] = unescape(rv["hls_url"])
    if rv.get("fallback_url"):
        out["mp4"] = unescape(rv["fallback_url"])
    return out if (out.get("hls") or out.get("mp4")) else None


def extract_gallery(post):
    """Ordered image URLs for a gallery post.

    Only 543 of 941 gallery posts kept `gallery_data`/`media_metadata` in the
    archive, so fall back through every field that can still yield an image.
    """
    gdata = post.get("gallery_data") or {}
    mm = post.get("media_metadata") or {}
    items = gdata.get("items") or []
    order = [it.get("media_id") for it in items] if items else list(mm.keys())
    imgs = []
    for mid in order:
        meta = mm.get(mid)
        if not isinstance(meta, dict):
            continue
        s = meta.get("s") or {}
        url = s.get("gif") or s.get("mp4") if meta.get("e") == "AnimatedImage" \
            else s.get("u")
        if not url:
            ps = meta.get("p") or []
            if ps:
                url = ps[-1].get("u")
        if url:
            imgs.append(unescape(url))
    return imgs or None


def extract_thumb(post):
    """Best available still image: reddit thumbnail, else a mid-size preview."""
    th = post.get("thumbnail") or ""
    if th not in BAD_THUMBS and th.startswith("http"):
        return unescape(th)
    images = (post.get("preview") or {}).get("images") or []
    if images:
        res = images[0].get("resolutions") or []
        if res:
            # mid resolution keeps filtered.json from ballooning on huge sources
            pick = res[min(2, len(res) - 1)]
            if pick.get("url"):
                return unescape(pick["url"])
        src = (images[0].get("source") or {}).get("url")
        if src:
            return unescape(src)
    return ""


def media_fields(p, kind):
    """The media payload the reader needs to embed this post inline."""
    out = {}
    thumb = extract_thumb(p)
    if thumb:
        out["thumbnail"] = thumb
    if kind == "youtube":
        yid = extract_yt_id(p.get("url") or "")
        if yid:
            out["yt_id"] = yid
    elif kind == "reddit_video":
        vid = extract_reddit_video(p)
        if vid:
            out["video"] = vid
    elif kind == "gallery":
        imgs = extract_gallery(p)
        if imgs:
            out["gallery"] = imgs
    return out


def build_comment_tree(comments, post_author):
    """comments (flat, ascending) -> list of nested root comments."""
    nodes = {}
    for c in comments:
        nodes[c["id"]] = {
            "id": c["id"],
            "author": c.get("author") or "[deleted]",
            "body": c.get("body") or "",
            "score": c.get("score") or 0,
            "created_utc": c.get("created_utc") or 0,
            "is_op": (c.get("author") or "") == post_author and post_author != "",
            "replies": [],
        }
    roots = []
    for c in comments:
        node = nodes[c["id"]]
        parent = (c.get("parent_id") or "")
        if parent.startswith("t1_") and parent[3:] in nodes:
            nodes[parent[3:]]["replies"].append(node)
        else:
            roots.append(node)
    return roots


def main():
    posts = load_jsonl_by_id(POSTS)              # dedup by id, last wins
    candidates = load_jsonl_by_id(CANDIDATES)
    classifications = load_jsonl_by_id(CLASSIFICATIONS)

    comments_by_post = defaultdict(list)
    if os.path.exists(COMMENTS):
        seen = set()
        with open(COMMENTS) as f:
            for line in f:
                c = json.loads(line)
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                pid = (c.get("link_id") or "")[3:]
                if pid in posts:
                    comments_by_post[pid].append(c)

    if os.path.isdir(THREADS_DIR):
        shutil.rmtree(THREADS_DIR)
    os.makedirs(THREADS_DIR)

    index = []
    filtered = []
    n_threads = 0
    status_counts = defaultdict(int)
    kind_counts = defaultdict(int)
    for pid, p in posts.items():
        cand = candidates.get(pid, {})
        cls = classifications.get(pid)
        is_candidate = bool(cand.get("candidate"))

        if cls:
            status = cls["verdict"]
        elif is_candidate:
            status = "pending"
        else:
            status = "filtered"
        status_counts[status] += 1

        kind = media_kind(p)
        kind_counts[kind] += 1

        body = (p.get("selftext") or "").strip()
        if body in EMPTY:
            body = ""
        body = preview_text(body)
        signals = cand.get("signals", {})
        url = "" if (p.get("is_self") or not p.get("url")) else p["url"]

        if not is_candidate and not cls:
            entry = {
                "id": pid,
                "title": p.get("title") or "",
                "author": p.get("author") or "[deleted]",
                "created_utc": p.get("created_utc") or 0,
                "score": p.get("score") or 0,
                "num_comments": p.get("num_comments") or 0,
                "preview": body[:PREVIEW_LEN_FILTERED],
                "status": "filtered",
                "flair": p.get("link_flair_text") or "",
                "kind": kind,
                "url": url,
                "author_followups": 0,
            }
            entry.update(media_fields(p, kind))
            filtered.append(entry)
            continue

        entry = {
            "id": pid,
            "title": p.get("title") or "",
            "author": p.get("author") or "[deleted]",
            "created_utc": p.get("created_utc") or 0,
            "score": p.get("score") or 0,
            "num_comments": p.get("num_comments") or 0,
            "preview": body[:PREVIEW_LEN],
            "body_len": len(body),
            "status": status,
            "flair": p.get("link_flair_text") or "",
            "kind": kind,
            "url": url,
            "author_followups": signals.get("author_followup_count", 0),
        }
        entry.update(media_fields(p, kind))
        if cls:
            entry["tags"] = cls.get("tags", [])
            entry["summary_zh"] = cls.get("summary_zh", "")
            entry["confidence"] = cls.get("confidence", 0.5)
        index.append(entry)

        if is_candidate:
            comments = sorted(comments_by_post.get(pid, []),
                              key=lambda c: c.get("created_utc") or 0)
            thread = {
                "id": pid,
                "title": p.get("title") or "",
                "author": p.get("author") or "[deleted]",
                "created_utc": p.get("created_utc") or 0,
                "score": p.get("score") or 0,
                "selftext": (p.get("selftext") or ""),
                "url": p.get("url") or "",
                "comments": build_comment_tree(comments, p.get("author") or ""),
            }
            with open(os.path.join(THREADS_DIR, f"{pid}.json"), "w") as f:
                json.dump(thread, f, ensure_ascii=False, separators=(",", ":"))
            n_threads += 1

    index.sort(key=lambda e: -e["created_utc"])
    filtered.sort(key=lambda e: -e["created_utc"])
    import datetime as dt
    payload = {
        "subreddit": SUB,
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": dict(status_counts),
        "kinds": dict(kind_counts),
        "posts": index,
    }
    with open(DATA_OUT, "w") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    with open(FILTERED_OUT, "w") as f:
        json.dump(filtered, f, ensure_ascii=False, separators=(",", ":"))

    print(f"data.json: {len(index)} posts, {os.path.getsize(DATA_OUT) / 1e6:.1f} MB")
    print(f"filtered.json: {len(filtered)} posts, "
          f"{os.path.getsize(FILTERED_OUT) / 1e6:.1f} MB")
    print(f"threads/: {n_threads} files")
    print("status:", dict(status_counts))
    print("kinds:", dict(kind_counts))


if __name__ == "__main__":
    main()
