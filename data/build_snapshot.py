"""Freeze a sample of Wikipedia pages for the local origin, plus the clickstream rows about them.

1. Rank articles by outgoing link clicks in the answer-key month and sample seed pages from three
   popularity buckets (head / torso / tail), so results can be split by popularity.
2. Add each seed's most-clicked targets in the session month, so browsing sessions have a next
   page to go to.
3. Fetch each page's HTML from the Wikimedia REST API (Parsoid HTML), drop editor metadata
   (data-mw etc., ~25% of the bytes), rewrite article links to /wiki/<Title>, and save it as
   data/snapshot/wiki/<Title>.html. Images keep their upload.wikimedia.org URLs; the experiments
   fetch HTML only.
4. Write the clickstream rows leaving every snapshot page, per month, to
   data/snapshot/clickstream-<month>.tsv, and the page list with revision ids to
   data/snapshot_manifest.json (committed, so the snapshot can be rebuilt exactly).

  python -m data.build_snapshot --key-month 2026-07 --session-month 2026-08
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import random
import re
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from data.clickstream import USER_AGENT, dump_path, outgoing_totals, transitions

HERE = Path(__file__).parent
SNAPSHOT_DIR = HERE / "snapshot"
MANIFEST = HERE / "snapshot_manifest.json"
REST = "https://en.wikipedia.org/api/rest_v1/page/html/{title}"
# Rank ranges (by outgoing link clicks, 0 = most) for the popularity buckets.
BUCKETS = {"head": (0, 1_000), "torso": (1_000, 50_000), "tail": (50_000, 300_000)}
EXCLUDE = {"Main_Page"}
STRIP_ATTRS = ("data-mw", "data-parsoid")
REVISION = re.compile(r"/revision/(\d+)")


def sample_seeds(totals, per_bucket: int, rng: random.Random) -> list[dict]:
    ranked = [t for t, _ in totals.most_common() if t not in EXCLUDE]
    seeds = []
    for bucket, (lo, hi) in BUCKETS.items():
        for rank in sorted(rng.sample(range(lo, min(hi, len(ranked))), per_bucket)):
            title = ranked[rank]
            seeds.append(
                {"title": title, "role": "seed", "bucket": bucket, "rank": rank,
                 "out_clicks": totals[title]}
            )  # fmt: skip
    return seeds


def clean_html(html: str) -> tuple[str, int | None]:
    """Strip Parsoid metadata and point article links at the local origin."""
    tree = HTMLParser(html)
    root = tree.css_first("html")
    about = root.attributes.get("about") if root is not None else None
    match = REVISION.search(about or "")
    for node in tree.css("base, link[rel~=stylesheet]"):
        node.decompose()
    for node in tree.css("[data-mw], [data-parsoid]"):
        for attr in STRIP_ATTRS:
            if attr in node.attrs:
                del node.attrs[attr]
    for node in tree.css("a[href^='./']"):
        node.attrs["href"] = "/wiki/" + node.attributes["href"][2:]
    return tree.html or "", int(match.group(1)) if match else None


def page_path(title: str) -> Path:
    return SNAPSHOT_DIR / "wiki" / f"{title}.html"


async def fetch_page(client: httpx.AsyncClient, page: dict, sem: asyncio.Semaphore) -> None:
    url = REST.format(title=quote(page["title"], safe=""))
    async with sem:
        for attempt in range(5):
            try:
                r = await client.get(url)
            except httpx.TransportError:
                r = None
            if r is not None and r.status_code == 200:
                break
            if r is not None and r.status_code not in (429, 500, 502, 503, 504):
                page["error"] = r.status_code
                return
            retry_after = r.headers.get("retry-after", "") if r is not None else ""
            await asyncio.sleep(float(retry_after) if retry_after.isdigit() else 2**attempt)
        else:
            page["error"] = "retries exhausted"
            return
    html, revision = clean_html(r.text)
    path = page_path(page["title"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    page["revision"] = revision
    page["bytes"] = len(html.encode())


async def fetch_all(pages: list[dict], concurrency: int) -> None:
    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, timeout=60, follow_redirects=True) as client:
        todo = [p for p in pages if not page_path(p["title"]).exists()]
        done = 0

        async def one(page):
            nonlocal done
            await fetch_page(client, page, sem)
            done += 1
            if done % 100 == 0 or done == len(todo):
                print(f"  fetched {done}/{len(todo)}", file=sys.stderr, flush=True)

        await asyncio.gather(*(one(p) for p in todo))
    for page in pages:  # pages fetched by an earlier, interrupted run
        path = page_path(page["title"])
        if "bytes" not in page and "error" not in page and path.exists():
            html = path.read_text(encoding="utf-8")
            page["bytes"] = len(html.encode())
            match = REVISION.search(html[:4000])
            page["revision"] = int(match.group(1)) if match else None


def write_transitions(month: str, sources: set[str]) -> None:
    rows = transitions(dump_path(month), sources)
    path = SNAPSHOT_DIR / f"clickstream-{month}.tsv"
    with path.open("w", encoding="utf-8") as fh:
        for prev in sorted(rows):
            for curr, n in rows[prev].most_common():
                fh.write(f"{prev}\t{curr}\t{n}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-month", default="2026-07", help="answer key for the predictor eval")
    ap.add_argument("--session-month", default="2026-08", help="drives the browsing sessions")
    ap.add_argument("--per-bucket", type=int, default=334)
    ap.add_argument("--targets-per-seed", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    print(f"ranking articles by {args.key_month} clicks", file=sys.stderr)
    seeds = sample_seeds(outgoing_totals(dump_path(args.key_month)), args.per_bucket,
                         random.Random(args.seed))  # fmt: skip
    seed_titles = {p["title"] for p in seeds}

    print(f"picking targets from {args.session_month}", file=sys.stderr)
    session = transitions(dump_path(args.session_month), seed_titles)
    pages = list(seeds)
    known = set(seed_titles)
    for seed in seeds:
        for target, _ in session[seed["title"]].most_common(args.targets_per_seed):
            if target not in known:
                known.add(target)
                pages.append({"title": target, "role": "target", "from": seed["title"]})

    print(f"fetching {len(pages)} pages", file=sys.stderr)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.run(fetch_all(pages, args.concurrency))

    ok = {p["title"] for p in pages if "error" not in p}
    for month in (args.key_month, args.session_month):
        print(f"writing {month} clickstream rows", file=sys.stderr)
        write_transitions(month, ok)

    manifest = {
        "created": datetime.datetime.now(datetime.UTC).date().isoformat(),
        "source": "https://en.wikipedia.org/api/rest_v1/page/html/",
        "key_month": args.key_month,
        "session_month": args.session_month,
        "buckets": BUCKETS,
        "args": vars(args),
        "pages": pages,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    failed = [p["title"] for p in pages if "error" in p]
    total = sum(p.get("bytes", 0) for p in pages)
    print(f"{len(ok)} pages, {total / 1e6:.0f} MB, {len(failed)} failed", file=sys.stderr)


if __name__ == "__main__":
    main()
