# Next steps

Where the project stands and what comes next. The full plan (architecture, milestones, risks) is
in `docs/PLAN.md`.

## Status (24 Sep 2026)

Done:
- QUIC tunnel with connection migration, and a TCP+TLS baseline that reconnects instead (config 1).
- Client proxy (cache; serves pushed pages, revalidates, falls back to stale copies during an
  outage) and server proxy (fetches from the origin).
- Local origin server with ETag / If-Modified-Since, so revalidation works end to end.
- Emulated network (network namespaces + netem), handover predictor, prefetch policy, Jev client.
- **Day-4 gate passed** on the Linux PC: the tunnel survives a real Wi-Fi cut (below).
- Wikipedia snapshot: 1,942 pages (334 seeds each from popular / medium / obscure articles, plus
  their most-clicked next pages), 791 MB, with the July and August clickstream rows for them.
  Rebuild with `python -m data.clickstream fetch 2026-07 2026-08 && python -m data.build_snapshot`
  (the page list is in `data/snapshot_manifest.json`; the pages themselves aren't in git).
- `settings.yaml`: every tunable value in one commented file.
- Link prediction is wired end to end: the server proxy extracts links, asks the predictor
  (`--predictor none|position|jev`), picks pages within the byte budget and pushes them; a
  HANDOVER_HINT from the client lowers the threshold and raises the budget.
- Site-agnostic link choice for Jev (`jev.link_order`, default `content_first`: skip
  nav/header/footer/aside, which ordinary sites put before their content).
- Jev is the only predictor the system uses (decided 24 Sep: no click logs or blending). The
  offline evaluation (`python -m experiments.predictor_eval`) scores it on July clicks for the
  1,002 seed pages; the two baselines are there only as reference points:

  | predictor | hit@1 | hit@3 | hit@5 | hit@10 | calibration error (top 10) |
  |---|---|---|---|---|---|
  | **Jev** (40 links, content first) | 6.2% | 16.1% | 23.3% | 36.2% | 0.069 |
  | position (any site, no data) | 2.7% | 9.4% | 16.7% | 32.6% | 0.024 |
  | past clicks, June (needs site logs) | 19.2% | 38.1% | 48.7% | 63.6% | 0.003 |

  hit@k = share of real clicks that went to the predictor's top k links. Jev beats position
  about 2x on its top picks, is overconfident (favourite often given 30–70%), and 100 links or
  extra page context didn't help on a 60-page test. Full run: 1,002 calls, $0.074, cached in
  `data/cache/jev`.
- Tunnel compression: page bodies deflated on the wire (HTML 5.7x smaller); the prefetch budget
  counts wire bytes.
- Path manager (`client/path_manager.py`): switches interface early on a predicted fade, or after
  1 s of unanswered pings; sends HANDOVER_HINT when no other interface is healthy. On the
  scenario files it switches 5.8 s before Wi-Fi dies and warns the server 5.2 s before the tunnel.

Measured on the snapshot (July clicks, median per seed page): the first 40 links get 65–69% of
real clicks, the first 100 get 79–92%, the best possible 40 would get 87–96%. The link count
matters much more than the selection rule on Wikipedia. This caps what Jev can reach.

Tests: `make test` -> 84 passed (unit + loopback, any OS). `make test-netns` -> 2 passed (Linux).

## Next, in plan order

1. **Experiment harness:** `data/sessions.py` (browsing sessions from the August clickstream),
   a headless client driver, and `experiments/runner.py` that starts origin, server proxy,
   client proxy + path manager and the netem shaper in the namespaces for each
   (config × scenario × seed). Configs: 1 TCP, 2 QUIC, 3 fixed prefetch, 4 hover oracle,
   5 full, 5a switch-early only, 5b hints only.
2. **Correct Jev's overconfidence** before its probabilities set the prefetch budget (fit a
   simple mapping on half the pages, check on the other half).
3. **Ordinary websites:** run link extraction + Jev on a handful of non-Wikipedia pages as a
   sanity check and demo.
4. **Origin changes between runs** (X% of pages modified) to measure what revalidation saves.
5. More scenarios (LEO gap, GEO fallback), the VoIP probe, `analyze.py` and the figures.

## Still open outside the code

- [ ] Fill in the unknowns at the bottom of `docs/jev_notes.md` (rate limits, data retention)
- [ ] Find public drive-test signal traces (e.g. the Raca et al. 4G/5G datasets, Lumos5G) to
      replace the hand-made signal curves in `emulation/scenarios/`

## Setting up a Linux machine

```sh
git clone https://github.com/a-meriac/Shajarah.git && cd Shajarah
# needs python3 (3.11–3.14), iproute2, iptables (the nft-backed one is fine)
make venv                                  # creates .venv-linux
make test
sudo modprobe sch_netem && echo netem ok   # the kernel must be able to emulate links
make test-netns                            # the Day-4 gate
```

If `modprobe` fails: on Ubuntu install `linux-modules-extra-$(uname -r)`; on Arch/CachyOS, reboot
after a kernel update (the running kernel's modules are gone until you do).

## The Day-4 gate

`make test-netns` builds the emulated network (client, router, server proxy and origin, each in its
own network namespace; the client has `wifi0`, `cell0` and `sat0` links) and runs two tests:

- **with migration:** the client sends a request every 50 ms over Wi-Fi. After 3 s, Wi-Fi is cut
  (100% loss) and the tunnel moves to `cell0`. Passes if requests keep completing and the longest
  gap stays under 1 s.
- **without migration:** same, but the tunnel stays on dead Wi-Fi. Passes if nothing completes
  after the cut, which shows the first test isn't passing by accident.

To see the numbers:

```sh
make netns-up
sudo ip netns exec ep-srv .venv-linux/bin/python -m experiments.tunnel_probe server &
sudo ip netns exec ep-cli .venv-linux/bin/python -m experiments.tunnel_probe client
sudo ip netns exec ep-cli .venv-linux/bin/python -m experiments.tunnel_probe client --mode none
sudo pkill -f "experiments.tunnel_probe server"; make netns-down   # `kill %1` fails in fish
```

Result on the Linux PC (24 Sep), with migration: 157 requests, longest gap 52 ms, 98 completed
after the cut. The gap equals the 50 ms request interval, so migration itself adds no measurable
stall. Caveat for the paper: the probe migrates the instant it cuts Wi-Fi (zero detection time), so
this is the mechanism's best case, not the stall of a real handover.

| Symptom | Likely cause |
|---|---|
| `iptables: command not found` / nft errors in `netns_setup.sh` | install `iptables` |
| `Error: Specified qdisc kind is unknown` | netem module missing, see above |
| "with migration" gets no replies after the switch | source routing: `sudo ip -n ep-cli rule` should show `from 10.2.0.2 lookup 102` |
| Both tests hang for 30 s | the server didn't start: run the server command above by hand and read the error |
| Leftover namespaces after a crash | `make netns-down` |
