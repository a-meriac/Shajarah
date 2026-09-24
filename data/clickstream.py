"""Read the Wikipedia clickstream dumps (https://dumps.wikimedia.org/other/clickstream/).

Each line is `prev <TAB> curr <TAB> type <TAB> n`: n readers went from article `prev` to article
`curr` that month. Only type "link" matters here (a click on a link in `prev`); "external" and
"other" rows have search engines or unknown pages as `prev`. Pairs with 10 or fewer clicks are
left out of the dumps.

  python -m data.clickstream fetch 2026-07 2026-08
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx

RAW_DIR = Path(__file__).parent / "raw"
URL = "https://dumps.wikimedia.org/other/clickstream/{m}/clickstream-enwiki-{m}.tsv.gz"
USER_AGENT = "Shajarah/0.1 (EDGE Challenge research; https://github.com/a-meriac/Shajarah)"


def dump_path(month: str) -> Path:
    return RAW_DIR / f"clickstream-enwiki-{month}.tsv.gz"


def fetch(month: str) -> Path:
    path = dump_path(month)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    headers = {"User-Agent": USER_AGENT}
    with httpx.stream("GET", URL.format(m=month), headers=headers, timeout=60) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    shutil.move(tmp, path)
    return path


def iter_links(path: Path) -> Iterator[tuple[str, str, int]]:
    """Yields (prev, curr, n) for every link click row."""
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 4 and parts[2] == "link":
                yield parts[0], parts[1], int(parts[3])


def outgoing_totals(path: Path) -> Counter[str]:
    """Link clicks leaving each article (its popularity as a place people navigate from)."""
    totals: Counter[str] = Counter()
    for prev, _, n in iter_links(path):
        totals[prev] += n
    return totals


def transitions(path: Path, sources: set[str]) -> dict[str, Counter[str]]:
    """Link clicks from each of `sources` to each target."""
    out: dict[str, Counter[str]] = {s: Counter() for s in sources}
    for prev, curr, n in iter_links(path):
        if prev in out:
            out[prev][curr] += n
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("months", nargs="+", help="e.g. 2026-07")
    args = ap.parse_args()
    for month in args.months:
        print(fetch(month), file=sys.stderr)


if __name__ == "__main__":
    main()
