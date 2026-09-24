import gzip

from edgeproxy.predictors.base import Link, PageState
from edgeproxy.predictors.markov import MarkovPredictor
from edgeproxy.predictors.simple import PositionPredictor, UniformPredictor


def state():
    links = [Link(f"http://w/wiki/{t}", t, i, i / 3, target=t) for i, t in enumerate("ABC")]
    return PageState("http://w/wiki/P", "P", links, key="P")


def test_uniform_and_position():
    s = state()
    assert UniformPredictor().predict(s) == {l.url: 1 / 3 for l in s.candidates}
    p = PositionPredictor().predict(s)
    assert p["http://w/wiki/A"] > p["http://w/wiki/B"] > p["http://w/wiki/C"]
    assert abs(sum(p.values()) - 1) < 1e-9


def test_markov_from_clickstream(tmp_path):
    f = tmp_path / "cs.tsv.gz"
    with gzip.open(f, "wt") as fh:
        fh.write("P\tC\tlink\t60\nP\tA\tlink\t20\nP\tZ\tlink\t20\nother-search\tP\texternal\t999\n")
    m = MarkovPredictor(alpha=0.0)
    m.load_clickstream(f)
    probs = m.predict(state())
    # Normalised by all 100 clicks out of P, including Z (not a candidate).
    assert probs["http://w/wiki/C"] == 0.6
    assert probs["http://w/wiki/A"] == 0.2
    assert probs["http://w/wiki/B"] == 0.0
    assert m.known("P") and not m.known("other-search")
