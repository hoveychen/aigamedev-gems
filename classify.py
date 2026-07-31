#!/usr/bin/env python3
"""Classify candidate posts with a local `claude -p` call (LOCAL-ONLY step).

Separates reusable substance ("gem") from promotion and content-free posting
("hype"), with "ok" in between. Resumable: results append to classifications.jsonl
keyed by post id; already-classified posts are skipped, so daily incremental runs
are just:

    python3 scrape.py && python3 prefilter.py && python3 classify.py

Two adaptations for r/aigamedev, both because half this sub is media posts (5,410 of
10,800 are video/image/gallery/YouTube, and 43% carry no body text):

- The model CANNOT see the video/image. Every post is labelled with its media kind
  and its flair, and posts whose body is thin get the highest-scoring comments
  attached — for a showcase post the substance lives in the thread ("how did you
  do the animation?" → OP explains the pipeline), not in the (usually empty) body.
- "No body text" must not read as "no substance". The verdict guide below says so
  explicitly, otherwise every Demo post collapses into hype.

Options:
    --limit N      classify at most N posts this run (pilot batches)
    --model M      claude model alias (default: leave to claude CLI default)
    --batch N      posts per claude call (default 12)
    --workers N    concurrent claude calls (default 4)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.jsonl")
COMMENTS = os.path.join(HERE, "comments.jsonl")
CANDIDATES = os.path.join(HERE, "candidates.jsonl")
OUT = os.path.join(HERE, "classifications.jsonl")
ERRLOG = os.path.join(HERE, "classify_errors.log")

VERDICTS = {"gem", "ok", "hype"}
TAGS = {"workflow", "assets", "codegen", "npc-ai", "showcase", "tooling",
        "lesson", "debate", "question", "news", "promo"}

TOP_COMMENTS_PER_POST = 6
THIN_BODY = 400            # below this, pull in the thread to judge the post

PROMPT_HEADER = """\
You are vetting posts from r/aigamedev — a subreddit about using generative AI in \
game development. The reader wants REAL, reusable substance: concrete pipelines and \
workflows, which model/tool/settings were used and how, tooling write-ups, honest \
post-mortems including failures, and genuinely informative discussion or news. They \
want to skip pure promotion, content-free self-congratulation, and engagement bait.

IMPORTANT — most posts here are MEDIA posts (a video, image, gallery or YouTube \
link). You cannot see the media. Each post is labelled with MEDIA: <kind>. For those \
posts, judge from the title, the flair, the body, and the COMMENTS — an empty body \
is NOT evidence of an empty post. A demo whose author explains their pipeline in the \
comments is a gem; a demo with nothing but "looks cool" replies is ok, not hype.

For EACH post below, judge mainly: is there a concrete, reusable practice or a \
verifiable detail (specific models, tools, settings, numbers, code, process), \
anywhere in the post or its thread?

Return ONLY a JSON array, one object per post, no other text:
[{"id": "<id>", "verdict": "gem|ok|hype", "tags": ["workflow|assets|codegen|npc-ai|showcase|tooling|lesson|debate|question|news|promo", ...], "summary_zh": "一句话中文摘要，说清这帖有什么（或为什么没料）", "confidence": 0.0-1.0}]

verdict guide:
  gem  = concrete reusable substance — a pipeline or workflow you could follow, \
tool/model specifics, an honest post-mortem, a genuinely informative analysis.
  ok   = a real artifact or real information, but the method is not transparent \
(typical showcase demo), or the discussion is thin/opinion-only.
  hype = promotion of a product/service/course/Discord, content-free bragging, \
engagement bait, or low-effort output dumps with nothing to learn.

tag guide:
  workflow = the AI-assisted process itself (pipelines, prompt→asset→engine chains)
  assets   = generating art / audio / 3D / animation assets
  codegen  = using LLMs to write game code (incl. "vibe coding" a game)
  npc-ai   = AI *inside* the game (LLM NPCs, agents, procedural behaviour)
  showcase = showing off a game/demo/asset the author made
  tooling  = tools, plugins, engine integrations, infrastructure
  lesson   = post-mortems, failures, what didn't work, cost/time reality checks
  debate   = the AI-in-gamedev controversy (ethics, backlash, store policy, jobs)
  question = asking for help or recommendations
  news     = industry / platform / policy news
  promo    = selling or marketing something

POSTS:
"""


def clip(s, n):
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def load_inputs(limit):
    candidates = {}
    with open(CANDIDATES) as f:
        for line in f:
            c = json.loads(line)
            if c["candidate"]:
                candidates[c["id"]] = c.get("signals", {})

    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    todo_ids = set(candidates) - done

    posts, authors = [], {}
    with open(POSTS) as f:
        for line in f:
            p = json.loads(line)
            if p["id"] in todo_ids:
                p["_signals"] = candidates.get(p["id"], {})
                posts.append(p)
                authors[p["id"]] = p.get("author") or ""

    # Highest-engagement first, so a `--limit N` run spends the budget where the
    # payoff is. Comments are weighted because r/aigamedev routinely leaves a
    # genuinely argued thread sitting at 0 points ("Valve AI banning — how would
    # they EVEN KNOW?": 0 pts, 22 comments), and pure score ordering would bury it
    # behind every mildly-upvoted trailer.
    posts.sort(key=lambda p: -((p.get("score") or 0) + 2 * (p.get("num_comments") or 0)))
    if limit:
        posts = posts[:limit]
        keep = {p["id"] for p in posts}
        authors = {k: v for k, v in authors.items() if k in keep}

    followups = defaultdict(list)
    top_comments = defaultdict(list)
    if os.path.exists(COMMENTS) and posts:
        keep = {p["id"] for p in posts}
        with open(COMMENTS) as f:
            for line in f:
                c = json.loads(line)
                pid = (c.get("link_id") or "")[3:]
                if pid not in keep:
                    continue
                body = (c.get("body") or "").strip()
                if not body or body in ("[removed]", "[deleted]"):
                    continue
                author = c.get("author") or ""
                if author == authors.get(pid):
                    followups[pid].append(body)
                elif author != "AutoModerator":
                    top_comments[pid].append((c.get("score") or 0, author, body))

    for pid in top_comments:
        top_comments[pid].sort(key=lambda t: -t[0])
        del top_comments[pid][TOP_COMMENTS_PER_POST:]

    return posts, followups, top_comments, len(candidates), len(done)


def render_post(p, followups, top_comments):
    sig = p.get("_signals") or {}
    kind = sig.get("kind") or "self"
    flair = p.get("link_flair_text") or "—"
    parts = [f'--- POST id={p["id"]} score={p.get("score", 0)} '
             f'comments={p.get("num_comments", 0)}',
             f'MEDIA: {kind}   FLAIR: {flair}',
             f'TITLE: {clip(p.get("title"), 300)}']
    body = clip(p.get("selftext"), 2500)
    if body and body not in ("[removed]", "[deleted]"):
        parts.append(f"BODY: {body}")
    else:
        parts.append("BODY: (empty — media or link post)")
    fu = followups.get(p["id"])
    if fu:
        parts.append(f"AUTHOR FOLLOW-UP COMMENTS: {clip(chr(10).join(fu), 1800)}")
    # Thin-bodied posts (most media posts) are judged from the thread.
    body_len = sig.get("body_len") or 0
    tc = top_comments.get(p["id"])
    if tc and body_len < THIN_BODY:
        rendered = "\n".join(f"[{s} pts] u/{a}: {clip(b, 400)}" for s, a, b in tc)
        parts.append(f"TOP COMMENTS: {clip(rendered, 1800)}")
    return "\n".join(parts)


def parse_response(text, expected_ids):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in response")
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        # Malformed array (usually an unescaped quote in one summary).
        # Salvage the individual objects that do parse.
        arr = []
        for om in re.finditer(r"\{[^{}]*\}", m.group(0)):
            try:
                arr.append(json.loads(om.group(0)))
            except json.JSONDecodeError:
                continue
        if not arr:
            raise
    results = []
    for obj in arr:
        if not isinstance(obj, dict) or obj.get("id") not in expected_ids:
            continue
        if obj.get("verdict") not in VERDICTS:
            continue
        obj["tags"] = [t for t in obj.get("tags", []) if t in TAGS]
        try:
            obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0.5))))
        except (TypeError, ValueError):
            obj["confidence"] = 0.5
        results.append({k: obj[k] for k in
                        ("id", "verdict", "tags", "summary_zh", "confidence")
                        if k in obj})
    return results


class RateLimited(Exception):
    """Session-level failure (rate limit / quota / auth). The operator swaps
    accounts when this hits, so workers wait 30s and knock again instead of
    aborting (capped — see LIMIT_RETRY_MAX)."""


LIMIT_RETRY_SLEEP = 30        # seconds between retries while rate limited
LIMIT_RETRY_MAX = 120         # give up after ~1h of continuous limiting


RATE_LIMIT_RE = re.compile(
    r"(rate.?limit|usage limit|session limit|hit your .{0,20}limit|limit reached"
    r"|resets \d|too many requests|429|overloaded|quota|credit balance"
    r"|login|authenticat)", re.I)


def run_claude(prompt, model):
    cmd = ["claude", "-p", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=600)
    if proc.returncode != 0:
        blob = (proc.stderr + " " + proc.stdout)[:1000]
        if RATE_LIMIT_RE.search(blob):
            raise RateLimited(blob.strip()[:300])
        raise RuntimeError(f"claude exited {proc.returncode}: {blob[:500]}")
    if RATE_LIMIT_RE.search(proc.stdout[:300]) and "[" not in proc.stdout[:300]:
        # exit 0 but the "response" is an error banner, not JSON
        raise RateLimited(proc.stdout.strip()[:300])
    return proc.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="")
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    posts, followups, top_comments, n_cand, n_done = load_inputs(args.limit)
    print(f"candidates={n_cand} classified={n_done} this_run={len(posts)}", flush=True)
    if not posts:
        return

    batches = [posts[i:i + args.batch] for i in range(0, len(posts), args.batch)]
    out = open(OUT, "a")
    lock = threading.Lock()
    stop = threading.Event()      # set on rate limit / repeated failures
    stop_reason = []
    ok = failed = done_batches = skipped = 0
    consec_failures = 0
    MAX_CONSEC_FAILURES = 3

    def work(batch):
        ids = {p["id"] for p in batch}
        prompt = PROMPT_HEADER + "\n\n".join(
            render_post(p, followups, top_comments) for p in batch)
        attempts = limit_hits = 0
        while not stop.is_set():
            try:
                return ids, parse_response(run_claude(prompt, args.model), ids)
            except RateLimited as e:
                limit_hits += 1
                if limit_hits >= LIMIT_RETRY_MAX:
                    if not stop.is_set():
                        stop.set()
                        stop_reason.append(f"rate limited for ~1h straight: {e}")
                    return None, None
                if limit_hits == 1 or limit_hits % 10 == 0:
                    print(f"  rate limited (x{limit_hits}) — retrying in "
                          f"{LIMIT_RETRY_SLEEP}s: {str(e)[:100]}", flush=True)
                time.sleep(LIMIT_RETRY_SLEEP)
            except Exception as e:
                attempts += 1
                print(f"  batch attempt {attempts} failed: {e}",
                      file=sys.stderr, flush=True)
                if attempts >= 2:
                    return ids, []
        return None, None         # stop was set elsewhere

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, b) for b in batches]
        for fut in as_completed(futures):
            ids, results = fut.result()
            with lock:
                if ids is None:   # skipped after stop
                    skipped += 1
                    continue
                done_batches += 1
                if not results:
                    failed += len(ids)
                    consec_failures += 1
                    if consec_failures >= MAX_CONSEC_FAILURES and not stop.is_set():
                        stop.set()
                        stop_reason.append(
                            f"{consec_failures} consecutive batch failures")
                    with open(ERRLOG, "a") as elog:
                        elog.write(f"batch failed: {sorted(ids)}\n")
                else:
                    consec_failures = 0
                    for r in results:
                        out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    out.flush()
                    ok += len(results)
                    missing = ids - {r["id"] for r in results}
                    if missing:
                        print(f"  warn: missing {sorted(missing)} (retry next run)",
                              flush=True)
                print(f"progress: {done_batches}/{len(batches)} batches, "
                      f"{ok} classified, {failed} failed", flush=True)

    out.close()
    if stop.is_set():
        print(f"ABORTED EARLY: {'; '.join(stop_reason)}", flush=True)
        print(f"  +{ok} classified, {failed} failed, ~{skipped} batches skipped. "
              f"Progress is saved — rerun classify.py later to resume.", flush=True)
        sys.exit(2)
    print(f"done: +{ok} classifications ({failed} failed; rerun to retry)", flush=True)


if __name__ == "__main__":
    main()
