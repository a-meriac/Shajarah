from edgeproxy.server.links import extract_links, is_safe_to_prefetch, wiki_title

HTML = """
<html><body>
<div id="nav"><a href="/wiki/Special:Random">random</a><a href="/logout?token=abc">Log out</a></div>
<div id="content">
  <a href="/wiki/Albert_Einstein">Einstein</a>
  <a href="/wiki/Physics#History">physics <b>history</b></a>
  <a href="/wiki/Albert_Einstein">again</a>
  <a href="https://other.example/x">external</a>
  <a href="#cite">[1]</a>
  <a href="mailto:a@b">mail</a>
  <a href="/cart/add-to-cart?id=3">Add to cart</a>
</div>
</body></html>
"""


def test_extracts_safe_same_origin_candidates_in_order():
    links = extract_links(HTML, "https://en.wikipedia.org/wiki/Main", content_selector="#content")
    assert [l.url for l in links] == [
        "https://en.wikipedia.org/wiki/Albert_Einstein",
        "https://en.wikipedia.org/wiki/Physics",
    ]
    assert links[0].anchor == "Einstein"
    assert links[1].anchor == "physics history"
    assert links[1].target == "Physics"
    assert [l.position for l in links] == [0, 1]


def test_cross_origin_allowed_when_requested():
    links = extract_links(HTML, "https://en.wikipedia.org/wiki/Main", same_origin_only=False)
    assert any(not l.same_origin for l in links)


def test_cap():
    many = "".join(f'<a href="/wiki/P{i}">p</a>' for i in range(300))
    links = extract_links(f"<body>{many}</body>", "https://w/wiki/X", max_candidates=255)
    assert len(links) == 255 and links[-1].target == "P254"


def test_unsafe_urls():
    for url in ["/logout", "/account/sign-out", "/item?action=delete", "/cart", "/x?token=1"]:
        assert not is_safe_to_prefetch(url), url
    assert is_safe_to_prefetch("/wiki/Cartography")


def test_wiki_title():
    assert wiki_title("https://en.wikipedia.org/wiki/New_York_City") == "New_York_City"
    assert wiki_title("https://en.wikipedia.org/wiki/Caf%C3%A9") == "Café"
    assert wiki_title("https://en.wikipedia.org/wiki/File:X.png") is None
    assert wiki_title("https://en.wikipedia.org/w/index.php") is None


def test_counts_repeats_and_flags_boilerplate():
    html = """<body>
      <header><a href="/home">Home</a><a href="/news">News</a></header>
      <main><a href="/story">Story</a><a href="/news">more news</a><a href="/story">again</a></main>
      <div role="navigation"><a href="/sitemap">Sitemap</a></div>
      <footer><a href="/home">Home</a></footer>
    </body>"""
    links = {link.url.rsplit("/", 1)[1]: link for link in extract_links(html, "https://s/page")}
    assert links["story"].occurrences == 2 and not links["story"].boilerplate
    assert links["home"].occurrences == 2 and links["home"].boilerplate
    assert not links["news"].boilerplate  # also linked from the content
    assert links["sitemap"].boilerplate
