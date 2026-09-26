# Shajarah

Our entry for the EDGE Challenge (ATP 2026): keep browsing smooth when your connection hops between Wi-Fi, 5G and satellite.

The idea: your device can usually tell a dropout is coming because the signal fades first. When it does, we download the pages you're likely to open next, so there's something to read while you're offline, and move the connection to another network early if one is available.

**Project page:** https://a-meriac.github.io/Shajarah/

```
browser → client proxy ══ QUIC tunnel (Wi-Fi / 5G / satellite) ══ server proxy → websites
```

## Running it

```sh
make venv && make test      # any OS, Python 3.11–3.14
make test-netns             # Linux only: emulated networks, needs sudo
```

Reproducing the experiments (Linux, needs the downloaded data and an OpenRouter key in `.env`):

```sh
python -m data.clickstream fetch 2026-06 2026-07 2026-08   # ~1.5 GB of Wikipedia click data
python -m data.build_snapshot                              # ~2,000 pages, ~800 MB
python -m experiments.predictor_eval --predictors jev position history
python -m experiments.warm_jev                             # Jev answers for the sessions
make experiments                                           # ~2 h, asks for sudo
python -m experiments.analyze main
```

## Where things are

- `settings.yaml`: every tunable value (Jev, prefetch budgets, dropout prediction, timeouts, snapshot sample) in one place
- `edgeproxy/`: the proxies, the tunnel, and the prediction code
- `emulation/`: fake Wi-Fi/5G/satellite links and dropout scenarios
- `data/`: the local origin server, the Wikipedia snapshot and clickstream tools, the browsing sessions
- `experiments/`: measurement scripts
- `site/`: the project page and the run replay (`replay.html`), published on GitHub Pages
