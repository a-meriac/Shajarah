# Shajarah

Our entry for the EDGE Challenge (ATP 2026): keep browsing smooth when your connection hops between Wi-Fi, 5G and satellite.

The idea: the connection runs over QUIC, so it survives a network change without a new handshake. Your device can usually tell a dropout is coming because the signal fades first. When it is, an AI model picks the pages you're likely to open next and they're downloaded ahead of time, so there's something to read while you're offline.

HTTPS sites are passed through still encrypted: neither proxy can read them, so they aren't prefetched, but they keep their connection when the network changes.

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

## Live demo: browse from an iPhone through the tunnel

```
iPhone (Safari) → Mac: client proxy ══ QUIC tunnel ══ Linux PC: server proxy → websites
```

The iPhone uses the Mac as its Wi-Fi proxy; the Mac and the PC are joined by the QUIC tunnel. HTTPS sites are passed through still encrypted, and the PC logs every site it connects to. All three devices need to be on a network where they can reach each other (office and guest Wi-Fi often keeps devices apart; home Wi-Fi or a hotspot works).

**1. Linux PC (server).** Allow the tunnel through the firewall, then start the server proxy:

```sh
sudo ufw allow 4433/udp                       # if ufw is on
mkdir -p ~/shajarah-certs
.venv-linux/bin/python -m edgeproxy.server.proxy --port 4433 \
    --certdir ~/shajarah-certs --log ~/shajarah-server.jsonl
tail -f ~/shajarah-server.jsonl               # in another terminal: one line per site
```

The first start creates `~/shajarah-certs/cert.pem`. Copy it to the Mac (scp, AirDrop, or paste it into `~/shajarah-certs/cert.pem`); it's the tunnel's public certificate, not a secret. Find the PC's address with `ip -4 -br addr`.

**2. Mac (client proxy).**

```sh
make venv VENV_PY=python3                     # Python 3.11–3.14
.venv/bin/python -m edgeproxy.client.proxy --server <PC address> --port 4433 \
    --certdir ~/shajarah-certs --listen 0.0.0.0 --listen-port 8118
```

It prints `client proxy on 0.0.0.0:8118 -> quic tunnel` once the tunnel is up. `--listen 0.0.0.0` is needed, or only the Mac itself can use the proxy. Allow incoming connections for Python if macOS asks. Check the whole chain from the Mac:

```sh
curl -x http://127.0.0.1:8118 -sS -o /dev/null -w "%{http_code}\n" https://en.wikipedia.org/   # 200 or 301
```

Find the Mac's address in System Settings → Wi-Fi → Details → TCP/IP (or `route -n get default | grep interface`, then `ipconfig getifaddr <that interface>`).

**3. iPhone.** Settings → Wi-Fi → (i) next to the network: turn off **Limit IP Address Tracking**, then **Configure Proxy** → **Manual**, Server = the Mac's address (digits only), Port = `8118`. Browse in Safari; set the proxy back to Off afterwards.

**If it doesn't work:**

| Symptom | Check |
|---|---|
| The Mac's proxy never prints its "client proxy on …" line | The Mac can't reach the PC: `ping <PC address>`, and the PC's firewall must allow UDP 4433. |
| curl works on the Mac, Safari says the server can't be found | `curl http://<Mac address>:8118/` on the Mac should print `only plain HTTP`. If it can't connect, the proxy was started without `--listen 0.0.0.0`. Otherwise check the macOS firewall and the proxy address on the iPhone. |
| Certificate error on the Mac | `cert.pem` doesn't match the PC's; copy it again after the server has started once. |
| Only some sites fail | Pass-through only allows port 443 (`server.connect_ports` in `settings.yaml`). |

## Where things are

- `settings.yaml`: every tunable value (Jev, prefetch budgets, dropout prediction, timeouts, snapshot sample) in one place
- `edgeproxy/`: the proxies, the tunnel, and the prediction code
- `emulation/`: fake Wi-Fi/5G/satellite links and dropout scenarios
- `data/`: the local origin server, the Wikipedia snapshot and clickstream tools, the browsing sessions
- `experiments/`: measurement scripts
- `site/`: the project page and the run replay (`replay.html`), published on GitHub Pages
