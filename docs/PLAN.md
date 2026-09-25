# Plan: Handover-aware dual proxy (EDGE Challenge, ATP 2026)

## Context
Written on 24 Sep 2026, when the repo was empty. The goal is a working emulated prototype, the experiments, and the charts for the 10-page PDF and 1–2 min video. The deadline is 11 Oct 2026. Where things stand day to day is in `NEXT_STEPS.md`.

The claim to prove: **predicting a handover or outage locally, and using that prediction to (a) warm up a standby path and (b) raise the prefetch budget, reduces user-visible stalls more than the existing mechanisms (QUIC migration, Silk-style prefetch, Speculation-Rules-style prefetch), at an acceptable cost in wasted bytes.**

## Changes to the original plan (and why)

1. **Network namespaces and `tc netem` only exist on Linux.** Unit and loopback tests run anywhere (macOS too); the emulation runs on the Linux PC.
2. **Keep mitmproxy out of the measurement path.** In the experiments, a headless client replays sessions against a **local origin server** that serves a frozen snapshot of Wikipedia pages over plain HTTP. This makes the runs reproducible and independent of the live internet and TLS. mitmproxy (with its CA) is used only in the live browser demo for the video. This removes one of the four risky parts from the critical path.
3. **Make-before-break means a warm standby QUIC connection, not multipath.** aioquic has no multipath and no convenient client-initiated path probing. We keep one live connection on the current interface. When the handover predictor fires, we open a **second connection on the next interface**, bound to its source IP, using session resumption. Requests move to it before the old link dies. "QUIC migration only" (config 2) is kept as a separate mechanism for comparison.
4. **Handover traces must be real, not only synthetic.** If we generate the RSSI traces and also tune the predictor on them, the result is circular. Use public drive-test traces with RSRP and throughput over time (for example the Raca et al. Irish 4G/5G datasets from UCC, and Lumos5G). Use them both for the predictor input and to drive netem bandwidth, updating it every 100 ms. Use synthetic traces only for the satellite and tunnel scenarios, and say so in the paper.
5. **Link prediction uses Jev only (decided 24 Sep).** The Wikipedia clickstream is the answer key: for each snapshot page, compare Jev's top guesses with the links people actually clicked most. Report the results split by page popularity. Without a trained baseline, the comparison against existing mechanisms comes from the system experiment (configs 1–5), not the predictor experiment.
6. **Jev access is through OpenRouter** (TypeSafe's own waitlist is closed). Details, costs and gotchas: `docs/jev_notes.md`.

## Repo structure
Files marked *(planned)* don't exist yet.
```
pyproject.toml            # pip + venv (make venv); deps: aioquic, httpx, selectolax, numpy, pandas,
                          # matplotlib, pyyaml; extras: demo (mitmproxy), dev (pytest, ruff)
settings.yaml             # every tunable value, commented; loaded by edgeproxy/common/settings.py
Makefile                  # make venv | test | lint | netns-up | netns-down | test-netns
                          # (planned: exp-predictor | exp-system | figures | paper)
edgeproxy/
  common/
    protocol.py           # tunnel framing: REQUEST, RESPONSE, PUSH, NOT_MODIFIED, HANDOVER_HINT, BUDGET
    http.py               # header handling shared by both proxies
    eventlog.py           # structured JSONL event log (ts, event, fields): single source for all metrics
    env.py                # secrets from the environment or .env
    settings.py           # loads settings.yaml; typos and wrong types are errors
  tunnel/
    transport.py          # Transport ABC so the proxies don't care which one is in use
    quic_tunnel.py        # QUIC client + server over aioquic; migrate(); streams, pushes, DATAGRAM frames
    tcp_transport.py      # config 1 baseline: same frames over TCP+TLS, reconnect instead of migrate
    certs.py              # self-signed tunnel certificate
  server/
    proxy.py              # handles REQUEST: fetch origin (planned: extract links, call predictor, PUSH)
    fetcher.py            # httpx; passes conditional headers through, 304 -> NOT_MODIFIED
    links.py              # candidates: href, anchor text, DOM position, same-origin, no state-changing URLs
    prefetch_policy.py    # threshold(budget, handover_hint) -> links to push; byte budget accounting
  client/
    proxy.py              # local HTTP proxy: push hit / revalidate / stale during outage / tunnel
    cache.py              # LRU by bytes, validators, push hit and wasted-byte accounting
    signal_monitor.py     # replays a scenario's signal trace (planned: live nmcli/iw/ModemManager)
    handover_predictor.py # sliding-window slope + threshold -> HandoverHint(eta_s, confidence)
    path_manager.py       # switch interface early on a predicted fade (or after pings fail), and
                          # send HANDOVER_HINT when no other interface is healthy
    mitm_addon.py         # (planned) demo only: mitmproxy in front of the client proxy for HTTPS
  predictors/
    base.py               # Link / PageState: what the predictor sees
    select.py             # which N links a predictor sees (page order, content first, repeats first)
    jev.py                # Jev Choice question via OpenRouter; disk cache
emulation/
  netns_setup.sh          # client ns with wifi0/cell0/sat0 -> router ns -> server ns / origin ns,
                          # plus policy routing so a socket bound to an iface IP egresses there
  profiles.yaml           # wifi, 5g, lte, leo, geo, dead: delay/jitter/loss/rate
  shaper.py               # applies profiles with tc netem following a scenario timeline
                          # (planned: replay bandwidth traces, `tc qdisc change` every 100 ms)
  scenarios/*.yaml        # wifi_to_5g_walk, car_tunnel_45s (planned: leo_gap_15s, geo_fallback)
data/
  origin_server.py        # serves the snapshot with ETag/Last-Modified and 304s, stand-ins for
                          # linked articles outside it (planned: mutate X% of pages between runs)
  clickstream.py          # download and read the monthly clickstream dumps (en, 2026-06..08)
  build_snapshot.py       # ~2,000 pages stratified by popularity via Wikimedia REST, links rewritten
                          # to the local origin, frozen to data/snapshot/ (manifest committed)
  sessions.py             # readers following real August clicks, log-normal dwell -> sessions.json
experiments/
  harness.py              # addresses, the 7 configs, session loading (shared by the scripts below)
  tunnel_probe.py         # Day-4 gate: request every 50 ms across a Wi-Fi cut, reports the longest gap
  predictor_eval.py       # hit@k and calibration on July clicks, by popularity (Jev + 2 references)
  cutoff_sweep.py         # offline: next page pushed vs data, per outage push cutoff
  general_web.py          # link extraction + Jev on a few ordinary websites (sanity check)
  warm_jev.py             # asks Jev about every page view ahead of time (the emulation has no internet)
  client_run.py           # one run in ep-cli: netem timeline, tunnel, proxy, path manager, reader
  runner.py               # (config × scenario × session) in the namespaces -> results/<batch>/
  analyze.py              # event logs -> per-run metrics (runs.csv) + table
  voip_probe.py           # 50 pps over QUIC datagrams across a switch: loss, longest silence
  export_replay.py        # finished runs -> site/replays/ for the replay page
  figures.py              # paper figures (to be redone with the 20-reader batch)
tests/
  unit/                   # no network needed (run anywhere)
  integration/            # loopback: QUIC migration, proxies end to end over QUIC and TCP
  emulation/              # marked netns, need root on Linux (make test-netns)
site/                     # project page and replay page, published by .github/workflows/pages.yml
paper/                    # (planned) LaTeX, figures from figures/
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
- D1: Write `netns_setup.sh` (with policy routing), and ping across each interface. **Spike aioquic migration** (see Risks). Jev access set up via OpenRouter; findings in `docs/jev_notes.md`.
- D2: `protocol.py`, `transport.py`, QUIC client and server with request/response over streams, TCP transport. Build the snapshot and origin server.
- D3: client and server proxies end to end (no prefetch). Netem profiles, and `shaper.py` with a fixed profile.
- D4: scripted handover (bring an iface down, or set 100% loss). Migration works (config 2) and TCP reconnect works (config 1). Start the JSONL event log. **Gate:** one page loads before, during and after a handover in the emulation.

**Days 5–9 (28 Sep–2 Oct): revalidation and predictors**
- D5: ETag and If-Modified-Since in `fetcher.py`. NOT_MODIFIED frame, so the client keeps its copy. Measure bytes saved with the origin mutating X% of assets.
- D6: download the clickstream. `sessions.py`, `links.py`. `predictor_eval.py` skeleton.
- D7: add a concurrency limit to `jev.py`. Call Jev over the ~1000 snapshot pages once and cache the results.
- D8: Full predictor eval: precision@1/3/5, reliability diagrams, bytes vs hit rate, all split by popularity.
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
- **Unit (pytest, runs anywhere):** protocol round-trip, cache LRU and validators, link extraction on fixture HTML (including the logout and add-to-cart filter), prefetch policy thresholds and budget, handover predictor on hand-built slopes, Jev adapter against a fake HTTP server (no live calls in CI).
- **Integration (loopback):** client proxy ↔ QUIC ↔ server proxy ↔ origin server. Forced `migrate()` mid-transfer, where the stream must complete. 304 path. PUSH then cache hit.
- **Emulation (`-m netns`, Linux only):** a smoke scenario per config, asserting that the event log contains a handover and that the page completes. Runs before every matrix run.
- `ruff` + `pytest` (`make lint`, `make test`). Not set up yet: a pre-commit hook, and a GitHub Actions job running unit and integration tests.

## Experiment harness → charts
- Every component emits JSONL events (`req_start`, `req_done{bytes,from_cache}`, `push{prob,bytes}`, `handover_hint`, `iface_down/up`, `stream_stall`, `voip_pkt`). Runs are keyed by `run_id = config/scenario/seed` and record the git SHA.
- `analyze.py` computes: page load time (CDF), **stall time per handover**, time to recover, prefetch hit rate, wasted bytes (pushed but never used), and no-handover overhead against a direct fetch. For VoIP: longest gap, loss and jitter.
- Figures: (1) predictor precision@k by popularity bucket; (2) reliability diagram; (3) hit rate vs wasted bytes as the prefetch threshold varies; (4) stall time per handover, box plot, 7 configs × scenarios; (5) timeline of one car-tunnel run (signal, hint, standby up, requests served from cache); (6) VoIP sequence gap across a handover; (7) handover predictor lead time vs false-alarm rate. Report medians with 95% bootstrap CIs over 5 seeds.

## Risks (tackle these first)
1. **aioquic client migration (highest risk).** Its asyncio `connect()` wraps one socket. **Resolved (24 Sep):** `quic_tunnel.py` binds a new socket on the new interface's IP, hands it to the same protocol object and calls `change_connection_id()`; the server validates the path itself. The Day-4 gate passed in the emulation: across a Wi-Fi cut the longest gap between completed requests was 52 ms (the probe's 50 ms resolution).
2. **Netns and source routing.** Source routing works (the gate relies on it). A socket bound to an iface IP won't egress there without `ip rule`. Loss-based handovers can also look like congestion rather than an outage. Script both kinds (iface down, 100% loss). Needs root.
3. **Jev access and limits.** Resolved: working through OpenRouter (tested 24 Sep). Remaining risks: the endpoint is labelled alpha, rate limits are unknown, and probabilities are rounded to 2 decimals. Mitigation: cache every response, so experiments never depend on the service staying up.
4. **mitmproxy.** Demo only (see change 2). Install the CA in a separate browser profile.
5. **Prefetch benefit may be small.** It only shows up in long outages where the next click is predictable. Include the 45 s tunnel and LEO-gap scenarios, and dwell times from the sessions. If the gain is still small, report that honestly. The make-before-break result stands on its own.
6. **Wikimedia REST scraping.** Set a descriptive User-Agent, stay at or below the rate limit, fetch once and freeze. Record the snapshot date for reproducibility.

## Verification (end to end)
- `make test` passes (unit and integration).
- On the Linux PC: `make test-netns` passes. `python -m experiments.runner --config 5 --scenario car_tunnel_45s --seed 0` produces a run directory whose event log shows `handover_hint` before `iface_down`, and page loads during the outage served from the cache.
- `make exp-predictor` produces the predictor tables and figures 1–3. `make exp-system && make figures` regenerates figures 4–7 from scratch in one command. This is what goes in the README for judges.
- Sanity checks: with no handover, configs 1–5 differ only by the proxy-hop overhead. Config 2 stall < config 1 stall. Wasted bytes for config 3 > config 5.
