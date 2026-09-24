import pytest

from edgeproxy.predictors.base import Link
from edgeproxy.predictors.select import choose_links


def link(name, pos, occurrences=1, boilerplate=False):
    return Link(f"https://s/{name}", name, pos, pos / 5, target=name,
                occurrences=occurrences, boilerplate=boilerplate)  # fmt: skip


# A typical site: menu links come first in the HTML, the article's links after.
LINKS = [
    link("home", 0, occurrences=2, boilerplate=True),
    link("about", 1, boilerplate=True),
    link("story", 2),
    link("related", 3, occurrences=3),
    link("contact", 4, boilerplate=True),
]


def names(links):
    return [x.target for x in links]


def test_page_order():
    assert names(choose_links(LINKS, 2, "page")) == ["home", "about"]


def test_content_first_skips_the_menu():
    assert names(choose_links(LINKS, 3, "content_first")) == ["story", "related", "home"]


def test_repeats_first():
    assert names(choose_links(LINKS, 3, "repeats_first")) == ["related", "story", "home"]


def test_fewer_links_than_asked():
    assert len(choose_links(LINKS, 40, "content_first")) == 5


def test_unknown_order():
    with pytest.raises(ValueError, match="unknown link order"):
        choose_links(LINKS, 3, "random")
