"""Sanity check beyond Wikipedia: link extraction and Jev on ordinary websites.

There is no click data for these sites, so this is not a score. It shows that the pipeline works
on pages built nothing like Wikipedia: how many links a page has, how many sit in page chrome
(nav/header/footer/aside), which 40 Jev is shown, and what it predicts.

  python -m experiments.general_web                  # the default sites
  python -m experiments.general_web https://example.org/some/page
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx

from data.clickstream import USER_AGENT
from edgeproxy.common.settings import load_settings
from edgeproxy.predictors.select import choose_links
from edgeproxy.server.proxy import build_page_state, make_predictor
from experiments.harness import ROOT

SITES = [
    "https://www.bbc.com/news",  # news front page: heavy navigation before the stories
    "https://docs.python.org/3/library/asyncio.html",  # documentation with a sidebar
    "https://books.toscrape.com/",  # a demo shop built for scraping practice
    "https://news.ycombinator.com/",  # forum: most story links leave the site
    "https://www.gov.uk/browse/driving",  # government service index
]
OUT = ROOT / "results" / "general_web.json"


async def check(url: str, client: httpx.AsyncClient, jev, settings) -> dict:
    r = await client.get(url)
    r.raise_for_status()
    state = build_page_state(
        str(r.url), r.content, [], settings.links.max_candidates, settings.links.same_origin_only
    )
    offered = choose_links(state.candidates, settings.jev.max_options, settings.jev.link_order)
    probs = await jev.predict(state) if state.candidates else {}
    top = sorted(probs, key=probs.get, reverse=True)[:5]
    anchor = {link.url: link.anchor for link in state.candidates}
    return {
        "url": url,
        "title": state.title,
        "bytes": len(r.content),
        "links": len(state.candidates),
        "chrome_links": sum(link.boilerplate for link in state.candidates),
        "offered": len(offered),
        "offered_from_chrome": sum(link.boilerplate for link in offered),
        "first_offered": [link.anchor[:40] for link in offered[:5]],
        "jev_top5": [(anchor[u][:40] or u, round(probs[u], 2)) for u in top],
        "cost_usd": 0.0
        if jev.last_call is None or jev.last_call.cached
        else jev.last_call.cost_usd,
    }


async def main_async(urls: list[str]) -> None:
    settings = load_settings()
    jev = make_predictor("jev", settings)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    results = []
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        for url in urls:
            try:
                results.append(await check(url, client, jev, settings))
            except (httpx.HTTPError, RuntimeError) as e:
                print(f"{url}: {type(e).__name__}: {e}", file=sys.stderr)
    await jev.aclose()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    for r in results:
        print(f"\n{r['title'][:70]}  ({r['url']})")
        print(f"  {r['links']} links on the page, {r['chrome_links']} in nav/header/footer/aside")
        print(
            f"  Jev is shown {r['offered']}, of which {r['offered_from_chrome']} from page chrome"
        )
        print(f"  first shown: {r['first_offered']}")
        print(f"  Jev's top 5: {r['jev_top5']}")
    cost = sum(r["cost_usd"] for r in results)
    print(f"\n{len(results)} pages, ${cost:.4f}; details in {OUT.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*", default=SITES)
    asyncio.run(main_async(ap.parse_args().urls))


if __name__ == "__main__":
    main()
