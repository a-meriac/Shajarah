import gzip
import random
from collections import Counter

from data.build_snapshot import BUCKETS, clean_html, sample_seeds
from data.clickstream import iter_links, outgoing_totals, transitions
from edgeproxy.server.links import extract_links

PARSOID = """<!DOCTYPE html>
<html about="//en.wikipedia.org/wiki/Special:Redirect/revision/1376489467"><head>
<base href="//en.wikipedia.org/wiki/"/><title>Albert Einstein</title>
<link rel="stylesheet" href="/w/load.php?modules=site.styles"/></head>
<body><p data-mw='{"big":"blob"}' data-parsoid='{}'>
<a rel="mw:WikiLink" href="./Physics" title="Physics">physics</a>
<a rel="mw:WikiLink" href="./Ulm?action=edit&amp;redlink=1">Ulm</a>
<a rel="mw:ExtLink" href="https://example.org/x">ext</a></p></body></html>"""


def test_clean_html_rewrites_links_and_drops_metadata():
    html, revision = clean_html(PARSOID)
    assert revision == 1376489467
    assert "data-mw" not in html and "data-parsoid" not in html
    assert "<base" not in html and "load.php" not in html
    links = extract_links(html, "http://10.8.0.2:8080/wiki/Albert_Einstein")
    assert [link.target for link in links] == ["Physics"]  # redlink dropped, external dropped


def test_sample_seeds_is_stratified_and_reproducible():
    totals = Counter({f"P{i}": 1_000_000 - i for i in range(400_000)})
    totals["Main_Page"] = 10**9
    a = sample_seeds(totals, 5, random.Random(0))
    b = sample_seeds(totals, 5, random.Random(0))
    assert a == b and len(a) == 15
    assert all(p["title"] != "Main_Page" for p in a)
    for p in a:
        lo, hi = BUCKETS[p["bucket"]]
        assert lo <= p["rank"] < hi and p["title"] == f"P{p['rank']}"


def test_clickstream_reader(tmp_path):
    path = tmp_path / "cs.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("other-search\tA\texternal\t500\n")
        fh.write("A\tB\tlink\t30\n")
        fh.write("A\tC\tlink\t12\n")
        fh.write("B\tC\tlink\t20\n")
        fh.write("A\tD\tother\t15\n")
        fh.write('A\t"Carl_\\"Alfalfa\\"_Switzer"\tlink\t11\n')
    assert list(iter_links(path)) == [
        ("A", "B", 30),
        ("A", "C", 12),
        ("B", "C", 20),
        ("A", 'Carl_"Alfalfa"_Switzer', 11),
    ]
    assert outgoing_totals(path) == Counter({"A": 53, "B": 20})
    assert transitions(path, {"B"}) == {"B": Counter({"C": 20})}
