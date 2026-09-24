# Shajarah

Our entry for the EDGE Challenge (ATP 2026): keep browsing smooth when your connection hops between Wi-Fi, 5G and satellite.

The idea: your device can usually tell a dropout is coming because the signal fades first. When it does, we switch the connection to the next network early and download the pages you're likely to open next, so there's something to read while you're offline.

```
browser → client proxy ══ QUIC tunnel (Wi-Fi / 5G / satellite) ══ server proxy → websites
```

## Running it

```sh
make venv && make test      # any OS, Python 3.11–3.14
make test-netns             # Linux only: emulated networks, needs sudo
```

## Where things are

- `edgeproxy/`: the proxies, the tunnel, and the prediction code
- `emulation/`: fake Wi-Fi/5G/satellite links and dropout scenarios
- `experiments/`: measurement scripts
- `site/`: the project page (GitHub Pages)
- `docs/PLAN.md`: the full plan

Work in progress. See [NEXT_STEPS.md](NEXT_STEPS.md) for where we left off.
