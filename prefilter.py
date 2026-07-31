#!/usr/bin/env python3
"""Heuristic prefilter: drop obviously-empty posts so the LLM only reads candidates.

Reads posts.jsonl + comments.jsonl, writes candidates.jsonl with one line per post:
    {"id": ..., "candidate": true/false, "reasons": [...], "signals": {...}}

Philosophy: this layer only kills posts that clearly carry no recoverable substance.
Judging "real substance vs hype" is the LLM's job in classify.py — when in doubt,
pass it through.

Two things differ from the sibling ai-trading-gems prefilter, both driven by what
r/aigamedev actually looks like (10,800 posts measured 2026-07-31):

1. MEDIA IS CONTENT. 58% of posts are link/media posts (2,706 v.redd.it videos,
   1,147 images, 941 galleries, 616 YouTube, 860 external links) and 43% have no
   body text at all.
   A showcase post with an empty body but a playable video and a live comment
   thread is exactly what this reader exists to surface, so media presence is a
   pass signal — gated on some discussion so pure drive-by dumps still fall out.

2. FLAIR IS A STRONG PRIOR. This sub enforces flair, and 2,218 posts are flaired
   `Commercial Self Promotion` / `Self Promotion` (19%). Those are demoted unless
   they carry real substance (long body / real discussion), which keeps the LLM
   budget off the ad wall.
"""
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.jsonl")
COMMENTS = os.path.join(HERE, "comments.jsonl")
OUT = os.path.join(HERE, "candidates.jsonl")

EMPTY_BODIES = {"", "[removed]", "[deleted]"}
PROMO_RE = re.compile(
    r"(discord\.gg|t\.me/|telegram|dm me|join my (free )?(group|channel|server)"
    r"|link in bio|whatsapp|wishlist it|steam page|use code|coupon"
    r"|sign ?up (now|free)|free credits|limited time)", re.I)
URL_RE = re.compile(r"https?://\S+|\[([^\]]*)\]\([^)]*\)")

PROMO_FLAIRS = {"commercial self promotion", "self promotion", "promotion",
                "advertisement"}

# Thresholds — swept against the full 10,800-post archive rather than guessed. This
# sub is denser than the ai-trading sibling (40% of posts carry a 400+ char body,
# 37% draw 3+ comments), so even an aggressive sweep only reaches ~33%: the archive
# genuinely is mostly practitioners posting work. Rather than crank the knobs until
# the count looks small, the pass bar stays where "clearly nothing to recover" ends,
# and classify.py absorbs the volume — it walks candidates most-voted-first and is
# resumable, so `--limit N` lets a classify budget be spent top-down over several
# runs while everything else sits visible as `pending`.
MIN_BODY = 500             # 3,902 posts have >=400 chars; 2,050 land in 400-999
MIN_AUTHOR_FOLLOWUP = 250  # chars the author wrote in their own comment section
MIN_SCORE = 15             # score<=1 is 49% of the archive; 20+ is only 9%
MIN_COMMENTS = 10          # 45% of posts get zero comments
# A media post needs *some* sign a human engaged with it, not just an upload.
MEDIA_MIN_SCORE = 12
MEDIA_MIN_COMMENTS = 6
# Promo-flaired posts have to clear a much higher bar to count as substance.
PROMO_MIN_BODY = 1200
PROMO_MIN_COMMENTS = 25


def substantive_len(body):
    """Body length with URLs stripped — link-stuffed template spam scores ~0."""
    return len(re.sub(r"\s+", " ", URL_RE.sub(r"\1", body)).strip())


def media_kind(p):
    """Coarse media class from the post URL (mirrors build_data.py)."""
    url = p.get("url") or ""
    if re.match(r"https?://i\.redd\.it/", url) or \
            re.search(r"\.(?:png|jpe?g|gif|webp)(?:\?|$)", url, re.I):
        return "image"
    if "v.redd.it" in url:
        return "reddit_video"
    if "reddit.com/gallery" in url:
        return "gallery"
    if re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)", url):
        return "youtube"
    if re.match(r"https?://", url) and "/r/aigamedev" not in url:
        return "link"
    return "self"


def load_author_followups():
    """post id -> (chars, count) of comments the post author wrote under their post,
    plus total archived comment count per post."""
    post_author = {}
    with open(POSTS) as f:
        for line in f:
            p = json.loads(line)
            post_author[p["id"]] = p.get("author") or ""

    followup = defaultdict(int)
    followup_count = defaultdict(int)
    archived = defaultdict(int)
    if os.path.exists(COMMENTS):
        with open(COMMENTS) as f:
            for line in f:
                c = json.loads(line)
                pid = (c.get("link_id") or "")[3:]  # strip t3_
                author = c.get("author") or ""
                if not pid:
                    continue
                body = c.get("body") or ""
                if body not in EMPTY_BODIES:
                    archived[pid] += 1
                if author in ("[deleted]", "AutoModerator"):
                    continue
                if post_author.get(pid) == author and body not in EMPTY_BODIES:
                    followup[pid] += len(body)
                    followup_count[pid] += 1
    return followup, followup_count, archived


def evaluate(p, followup_chars, followup_count, archived_comments):
    body = (p.get("selftext") or "").strip()
    body_len = 0 if body in EMPTY_BODIES else substantive_len(body)
    score = p.get("score") or 0
    n_comments = max(p.get("num_comments") or 0, archived_comments)
    title = p.get("title") or ""
    flair = (p.get("link_flair_text") or "").strip().lower()
    kind = media_kind(p)
    has_media = kind in ("image", "reddit_video", "gallery", "youtube")

    signals = {
        "body_len": body_len,
        "author_followup_chars": followup_chars,
        "author_followup_count": followup_count,
        "score": score,
        "num_comments": n_comments,
        "kind": kind,
        "flair": p.get("link_flair_text") or "",
    }

    # Promo-shaped *text* with nothing behind it ("join our discord! play-test
    # soon!") is dead weight — kill it.
    if PROMO_RE.search(title + " " + body[:2000]) and body_len < MIN_BODY \
            and followup_chars < MIN_AUTHOR_FOLLOWUP:
        return False, ["promo_text_no_substance"], signals

    # Promo *flair* is only a weak signal here, NOT grounds to kill. This sub
    # requires `Commercial Self Promotion` on any post about your own project, so
    # 19% of the archive wears it — including solo devs writing up exactly the
    # AI-assisted process this reader is built to surface (a sample audit found a
    # 250-Claude-iteration post-mortem and an open-source voxel-game writeup among
    # the posts an earlier flair-kills-it rule threw away). All it costs a post is
    # the media shortcut below, so silent video ads still fall out.
    is_promo_flair = flair in PROMO_FLAIRS

    # Nothing readable and nothing watchable and nobody talked about it.
    if body_len < 50 and not has_media and followup_chars == 0 and n_comments == 0:
        return False, ["nothing_to_read"], signals

    reasons = []
    if body_len >= MIN_BODY:
        reasons.append(f"body>={MIN_BODY}")
    if followup_chars >= MIN_AUTHOR_FOLLOWUP:
        reasons.append(f"author_followup>={MIN_AUTHOR_FOLLOWUP}")
    if score >= MIN_SCORE:
        reasons.append(f"score>={MIN_SCORE}")
    if n_comments >= MIN_COMMENTS:
        reasons.append(f"comments>={MIN_COMMENTS}")
    # Showcase lane: the media *is* the artifact, so it needs less text but does
    # need a sign that someone engaged. Promo-flaired posts don't get this lane —
    # otherwise every captionless trailer walks in on a handful of upvotes.
    if has_media and not is_promo_flair \
            and (score >= MEDIA_MIN_SCORE or n_comments >= MEDIA_MIN_COMMENTS):
        reasons.append(f"media({kind})+engagement")

    return bool(reasons), reasons or ["no_substance_signal"], signals


def main():
    if not os.path.exists(POSTS):
        sys.exit("posts.jsonl not found — run scrape.py first")
    followup, followup_count, archived = load_author_followups()

    total = kept = 0
    reason_tally = defaultdict(int)
    kind_tally = defaultdict(lambda: [0, 0])   # kind -> [kept, total]
    with open(POSTS) as f, open(OUT, "w") as out:
        for line in f:
            p = json.loads(line)
            pid = p["id"]
            candidate, reasons, signals = evaluate(
                p, followup.get(pid, 0), followup_count.get(pid, 0),
                archived.get(pid, 0))
            total += 1
            kept += candidate
            for r in reasons:
                reason_tally[r] += 1
            k = kind_tally[signals["kind"]]
            k[1] += 1
            k[0] += candidate
            out.write(json.dumps({
                "id": pid, "candidate": candidate,
                "reasons": reasons, "signals": signals,
            }, ensure_ascii=False) + "\n")

    print(f"{kept}/{total} posts pass prefilter ({kept * 100 // max(total, 1)}%)")
    print("\nreasons:")
    for r, c in sorted(reason_tally.items(), key=lambda x: -x[1]):
        print(f"  {c:>6}  {r}")
    print("\nper media kind (kept/total):")
    for k, (a, b) in sorted(kind_tally.items(), key=lambda x: -x[1][1]):
        print(f"  {k:<14} {a:>5}/{b:<6} {a * 100 // max(b, 1):>3}%")


if __name__ == "__main__":
    main()
