# Plan: Handover-aware dual proxy (EDGE Challenge, ATP 2026)

## Context
The repo is empty (one blank `test.txt`). This is a plan to build a working emulated prototype, run the experiments, and produce the charts for the 10-page PDF and 1–2 min video. The deadline is 11 Oct 2026, 17 days from today (24 Sep).

The claim to prove: **predicting a handover or outage locally, and using that prediction to (a) warm up a standby path and (b) raise the prefetch budget, reduces user-visible stalls more than the existing mechanisms (QUIC migration, Silk-style prefetch, Speculation-Rules-style prefetch), at an acceptable cost in wasted bytes.**

## Changes to the original plan (and why)

1. **Your machine is macOS. Network namespaces and `tc netem` only exist on Linux.** Develop on macOS, but run everything network-related in a Linux VM (Lima or UTM with Ubuntu 24.04, 4 vCPU) or on a teammate's Linux laptop. Pin this down on Day 1. Unit tests run anywhere; the emulation tests run only in the VM.
2. **Keep mitmproxy out of the measurement path.** In the experiments, a headless client replays sessions against a **local origin server** that serves a frozen snapshot of Wikipedia pages over plain HTTP. This makes the runs reproducible and independent of the live internet and TLS. mitmproxy (with its CA) is used only in the live browser demo for the video. This removes one of the four risky parts from the critical path.
3. **Make-before-break means a warm standby QUIC connection, not multipath.** aioquic has no multipath and no convenient client-initiated path probing. We keep one live connection on the current interface. When the handover predictor fires, we open a **second connection on the next interface**, bound to its source IP, using session resumption. Requests move to it before the old link dies. "QUIC migration only" (config 2) is kept as a separate mechanism for comparison.
4. **Handover traces must be real, not only synthetic.** If we generate the RSSI traces and also tune the predictor on them, the result is circular. Use public drive-test traces with RSRP and throughput over time (for example the Raca et al. Irish 4G/5G datasets from UCC, and Lumos5G). Use them both for the predictor input and to drive netem bandwidth, updating it every 100 ms. Use synthetic traces only for the satellite and tunnel scenarios, and say so in the paper.
5. **Clickstream evaluation needs a temporal split and a frame for cold start.** The Wikipedia clickstream is aggregated `(prev, curr, count)` data, so a Markov model trained on it is almost the ground truth. Train on month M and test on month M+1. Expect Markov to win on popular pages. Jev's likely advantage is on **tail and new pages with no click history**, so split the results by page popularity. The hybrid model is where a real win is most likely. This framing also makes the originality claim hold up better.
6. **Jev was released after my knowledge cutoff.** I can't check its SDK or API shape, so the plan puts it behind a `LinkPredictor` interface. The team must read docs.typesafe.ai on Day 1 before any Jev code is written. Nothing in this plan assumes specific method signatures beyond what you wrote.

## Repo structure
```
pyproject.toml            # uv-managed; deps: aioquic, httpx, selectolax, mitmproxy (demo extra),
                          # numpy, pandas, scikit-learn, matplotlib, pyyaml, pytest, pytest-asyncio, ruff
README.md                 # quickstart, VM setup, reproducing every figure
edgeproxy/
  common/
    protocol.py           # tunnel framing: REQUEST, RESPONSE, PUSH, NOT_MODIFIED, HANDOVER_HINT, BUDGET (msgpack/JSON)
    config.py             # dataclass configs loaded from YAML
    logging.py            # structured JSONL event log (ts, event, fields) — single source for all metrics
  tunnel/
    quic_client.py        # sans-IO pump over aioquic QuicConnection; bind-to-source-IP; migrate(); standby connections
    quic_server.py        # aioquic server; stream per request; DATAGRAM frames for real-time traffic
    tcp_transport.py      # config 1 baseline (same framing over TCP+TLS, reconnect on failure)
    transport.py          # Transport ABC so the proxies don't care which one is in use
  server/
    proxy.py              # handles REQUEST: fetch origin, extract links, call predictor, schedule PUSH
    fetcher.py            # httpx; origin cache with ETag/Last-Modified; conditional revalidation
    links.py              # candidate extraction: href, anchor text, DOM position, same-origin, filter state-changing URLs
    prefetch_policy.py    # threshold(budget, handover_hint) -> links to push; byte budget accounting
  client/
    proxy.py              # local HTTP proxy: cache hit -> serve; miss -> tunnel; revalidate via server
    cache.py              # content-addressed cache with validators; LRU by bytes
    signal_monitor.py     # reads live RSSI/RSRP (nmcli/iw/ModemManager) OR replays a trace file
    handover_predictor.py # moving-average slope + threshold -> HandoverHint(eta_s, confidence, next_iface)
    path_manager.py       # interface selection, make-before-break standby, migration trigger
    mitm_addon.py         # demo only: mitmproxy addon forwarding to client/proxy.py
  predictors/
    base.py               # LinkPredictor ABC: predict(PageState) -> dict[url, prob]
    uniform.py, position.py   # trivial baselines (top-of-page links)
    markov.py             # first-order Markov from clickstream
    logistic.py           # sklearn LR over link features (+ optional Jev prob feature -> hybrid)
    jev.py                # Jev Choice question; response cache on disk keyed by (url, model version)
    hybrid.py
emulation/
  netns_setup.sh          # client ns with 2–3 veth ifaces (wifi, cell, sat) -> router ns -> server ns -> origin ns
  policy_routing.sh       # ip rule "from <src> table N" so sockets bound to an iface IP actually egress there
  profiles/*.yaml         # wifi, 5g, lte, leo, geo: delay/jitter/loss/rate
  shaper.py               # trace-driven netem: replays bandwidth/RSRP traces, applies `tc qdisc change` every 100 ms
  scenarios/*.yaml        # e.g. wifi_to_5g_walk, car_tunnel_45s, leo_gap_15s, geo_fallback
data/
  fetch_clickstream.py    # download 2 consecutive months (e.g. en, 2026-07 / 2026-08)
  build_snapshot.py       # sample ~1000 pages stratified by popularity, pull HTML via Wikimedia REST (UA + rate limit),
                          # rewrite links to the local origin, freeze to data/snapshot/
  origin_server.py        # serves snapshot with ETag/Last-Modified; can mutate X% of assets between runs
  sessions.py             # generate browsing sessions: random walks weighted by month-M+1 clickstream, dwell times
experiments/
  predictor_eval.py       # offline: precision@k, calibration (reliability + ECE), bytes-at-threshold, Jev latency
  runner.py               # (config × scenario × seed) matrix inside netns; writes results/<run_id>/*.jsonl
  client_driver.py        # headless session replayer (httpx through client proxy); "hover oracle" for config 4
  voip_probe.py           # 50 pps UDP-over-QUIC-datagram stream; measures gap/loss/jitter across handover
  analyze.py              # JSONL -> pandas -> tables + figures in figures/
  matrix.yaml             # the 5 configs × scenarios × seeds
tests/
  unit/ ...               # no network needed (run on macOS)
  integration/ ...        # loopback, no netns
  emulation/ ...          # marked @pytest.mark.netns, need root in the Linux VM
paper/                    # LaTeX, figures symlinked from figures/
Makefile                  # make vm-setup | test | netns-up | exp-predictor | exp-system | figures | paper
```

## The five system configs (same code, flags in `matrix.yaml`)
| # | Transport | Standby | Prefetch |
|---|---|---|---|
| 1 | TCP+TLS, reconnect | – | none |
| 2 | QUIC, migrate on iface change | – | none |
| 3 | QUIC | – | always-on, fixed top-k per page (Silk-style) |
| 4 | QUIC | – | hover oracle: the clicked link is known ~200 ms before the click (Speculation Rules "moderate") |
| 5 | QUIC | predictive warm standby | predictor-driven; threshold drops or budget rises on HANDOVER_HINT |

Also run **5a (only the standby path)** and **5b (only the adaptive prefetch)** as ablations, so each part of the claim gets its own evidence. Reviewers will ask for this.

## Milestones

**Days 1–4 (24–27 Sep): skeleton, tunnel, emulation**
- D1: Set up the VM. Write `netns_setup.sh` and `policy_routing.sh`, and ping across each interface. **Spike aioquic migration** (see Risks). One person reads the Jev docs, requests access, and records limits and pricing in `docs/jev_notes.md`.
- D2: `protocol.py`, `transport.py`, QUIC client and server with request/response over streams, TCP transport. Build the snapshot and origin server.
- D3: client and server proxies end to end (no prefetch). Netem profiles, and `shaper.py` with a fixed profile.
- D4: scripted handover (bring an iface down, or set 100% loss). Migration works (config 2) and TCP reconnect works (config 1). Start the JSONL event log. **Gate:** one page loads before, during and after a handover in the VM.

**Days 5–9 (28 Sep–2 Oct): revalidation and predictors**
- D5: ETag and If-Modified-Since in `fetcher.py`. NOT_MODIFIED frame, so the client keeps its copy. Measure bytes saved with the origin mutating X% of assets.
- D6: download the clickstream. `sessions.py`, `links.py`, and the Markov, position and uniform baselines. `predictor_eval.py` skeleton.
- D7: logistic model. `jev.py` with a disk cache and a concurrency limit. Call Jev over the ~1000 snapshot pages once and cache the results.
- D8: hybrid model. Full predictor eval: precision@1/3/5, reliability diagrams, bytes vs hit rate, all split by popularity. **Gate:** choose the predictor for config 5 based on the data.
- D9: `prefetch_policy.py`, PUSH frames, client cache ingest. Configs 3 and 4 working.

**Days 10–13 (3–6 Oct): handover prediction and the full matrix**
- D10: `signal_monitor.py` trace replay, `handover_predictor.py`. Offline evaluation on the real traces: lead time vs false alarms (ROC over the threshold).
- D11: `path_manager.py` standby, and the HANDOVER_HINT → budget logic. Config 5 and the ablations.
- D12: `runner.py` full matrix: 7 configs × ~5 scenarios × 5 seeds. Plus `voip_probe.py`.
- D13: rerun anything that failed. Measure the no-handover proxy-hop overhead. Freeze the results.

**Days 14–17 (7–10 Oct): write-up**
- Figures (`make figures`), PDF, 90 s video (browser demo through mitmproxy + side-by-side stall timeline). Submit on the 10th, keeping a 1-day buffer.

**Suggested split for 3–4 people:** (A) tunnel and netns, (B) data and predictors, (C) client, cache, prefetch and harness, (D, if you have one) paper, figures and video from Day 8. Otherwise A picks this up.

## Test strategy
- **Unit (pytest, runs on macOS):** protocol round-trip, cache LRU and validators, link extraction on fixture HTML (including the logout and add-to-cart filter), prefetch policy thresholds and budget, handover predictor on hand-built slopes, Markov and LR against tiny fixtures, Jev adapter against recorded responses (no live calls in CI).
- **Integration (loopback):** client proxy ↔ QUIC ↔ server proxy ↔ origin server. Forced `migrate()` mid-transfer, where the stream must complete. 304 path. PUSH then cache hit.
- **Emulation (`-m netns`, VM only):** a smoke scenario per config, asserting that the event log contains a handover and that the page completes. Runs before every matrix run.
- `ruff` + `pytest` in a pre-commit hook. GitHub Actions runs unit and integration tests only.

## Experiment harness → charts
- Every component emits JSONL events (`req_start`, `req_done{bytes,from_cache}`, `push{prob,bytes}`, `handover_hint`, `iface_down/up`, `stream_stall`, `voip_pkt`). Runs are keyed by `run_id = config/scenario/seed` and record the git SHA.
- `analyze.py` computes: page load time (CDF), **stall time per handover**, time to recover, prefetch hit rate, wasted bytes (pushed but never used), and no-handover overhead against a direct fetch. For VoIP: longest gap, loss and jitter.
- Figures: (1) predictor precision@k by popularity bucket; (2) reliability diagram; (3) hit rate vs wasted bytes, one curve per predictor; (4) stall time per handover, box plot, 7 configs × scenarios; (5) timeline of one car-tunnel run (signal, hint, standby up, requests served from cache); (6) VoIP sequence gap across a handover; (7) handover predictor lead time vs false-alarm rate. Report medians with 95% bootstrap CIs over 5 seeds.

## Risks (tackle these first)
1. **aioquic client migration (highest risk).** Its asyncio `connect()` wraps one socket. Plan: drive `QuicConnection` directly with our own UDP sockets (`quic_client.py`). To migrate, bind a new socket to the new iface IP, call `change_connection_id()`, and send from there. The server validates the path automatically. **Day 1 spike, 1 day time box.** Fallback: fast reconnect with 0-RTT resumption, and describe it honestly as "resumption, not migration".
2. **Netns and source routing.** A socket bound to an iface IP won't egress there without `ip rule`. Loss-based handovers can also look like congestion rather than an outage. Script both kinds (iface down, 100% loss). Needs root in the VM.
3. **Jev access and limits.** It is waitlisted, rate limits are unknown, and there is a 255-option cap. Mitigations: apply today, and in parallel use a gateway (OpenRouter, Vercel, Cloudflare) if docs.typesafe.ai lists one as official. Cache every response. Pre-filter candidates to ≤255. The predictor experiment is offline, so live latency only matters for config 5. There, use cached predictions plus a measured latency distribution. If access never arrives, the system still runs on LR, and Jev becomes future work. This is a real hit to originality, so escalate early.
4. **mitmproxy.** Demo only (see change 2). Install the CA in a separate browser profile.
5. **Prefetch benefit may be small.** It only shows up in long outages where the next click is predictable. Include the 45 s tunnel and LEO-gap scenarios, and dwell times from the sessions. If the gain is still small, report that honestly. The make-before-break result stands on its own.
6. **Wikimedia REST scraping.** Set a descriptive User-Agent, stay at or below the rate limit, fetch once and freeze. Record the snapshot date for reproducibility.

## Verification (end to end)
- `make test` passes on macOS (unit and integration).
- In the VM: `make netns-up && pytest -m netns` passes. `python -m experiments.runner --config 5 --scenario car_tunnel_45s --seed 0` produces a run directory whose event log shows `handover_hint` before `iface_down`, and page loads during the outage served from the cache.
- `make exp-predictor` produces the predictor tables and figures 1–3. `make exp-system && make figures` regenerates figures 4–7 from scratch in one command. This is what goes in the README for judges.
- Sanity checks: with no handover, configs 1–5 differ only by the proxy-hop overhead. Config 2 stall < config 1 stall. Wasted bytes for config 3 > config 5.
