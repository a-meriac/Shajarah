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

- Experiment harness: 30 browsing sessions that follow real August clicks
  (`data/sessions.json`), Jev answers fetched ahead of time (`experiments/warm_jev.py`, the
  emulation has no internet), the per-run client program and the runner (`make experiments`),
  and `experiments/analyze.py`. The origin serves stand-ins for linked articles outside the
  snapshot, so pushes cost what real pages would.

Tests: `make test` -> 118 passed (unit + loopback, any OS). `make test-netns` -> 2 passed (Linux).

## First system results (batch `main`, 24 Sep, 70 runs: 7 configs × 2 scenarios × 5 readers)

Car tunnel (45 s with no network), total time each reader spent waiting for pages, in seconds:

| config | reader 0 | 1 | 2 | 3 | 4 | mean |
|---|---|---|---|---|---|---|
| 1 TCP+TLS | 44.8 | 21.6 | 5.2 | 23.7 | 52.8 | 29.6 |
| 2 QUIC migration | 32.7 | 9.7 | 1.1 | 10.6 | 41.5 | 19.1 |
| 3 fixed prefetch | 2.8 | 9.7 | 1.0 | 10.1 | 41.4 | 13.0 |
| 4 hover oracle | 32.6 | 9.8 | 0.3 | 10.3 | 41.1 | 18.8 |
| 5 full system | 2.8 | 2.0 | 1.1 | 10.2 | 41.0 | 11.4 |

- Each layer helps; the full system has the lowest mean. The hint-driven extra pushes saved
  reader 1 (2.0 s vs 9.7 s with fixed prefetch).
- Prediction is hit or miss per reader: readers 3 and 4 clicked pages that weren't pushed and
  waited as long as with plain QUIC.
- 5b (hints only) equals 5 and 5a (switch early only) equals 2: in a tunnel there is no other
  network, so all the gain comes from hint-driven prefetch.
- Data: ~0.6 MB pushed per run with config 5, ~97% never read.
- Wi-Fi walk: every config waited under 2 s in total. Without prediction the switch comes ~1 s
  after Wi-Fi dies and few clicks land in that second; page loads don't show the benefit of
  switching early (a continuous stream would).

Caveat: 5 readers with ~1 outage click each is too few for stable numbers.

Reproduce: `python -m experiments.warm_jev` (needs the key), `make experiments`, then
`python -m experiments.analyze main`.

## Second system batch (`tunnel20`, 25 Sep: car tunnel, 20 readers, configs 1, 2, 3, 5)

Settings: outage push cutoff 0.01 (from the offline sweep: next page already pushed 57% vs 39%
at 0.03), two clicks deep on. Total waiting per reader:

| config | mean (95% CI) | median | outage clicks from cache | pushed per run |
|---|---|---|---|---|
| 1 TCP+TLS | 31.7 s (22–41) | 28.5 s | 3 of 20 | 0 |
| 2 QUIC migration | 21.6 s (14–29) | 16.8 s | 3 of 22 | 0 |
| 3 fixed prefetch | 20.2 s (12–28) | 12.6 s | 5 of 23 | 0.24 MB |
| 5 full (before the fix below) | 19.7 s (11–29) | 7.4 s | 15 of 27 | 6.6 MB, 0.6% read |

**Finding: pushing into the outage backfires.** Config 5 served far more outage clicks from cache
and halved the median, but 5 of 20 readers waited *longer* than with plain QUIC. The server
didn't know when the link actually died and kept pushing (29 pushes during the outage in one
run); the unacknowledged backlog stalled QUIC's loss recovery, and the first ping after the link
returned was answered at 114 s instead of 85 s, so the reader's page queued behind it. Fixed
(`213b8bc`): pushing stops at the warned dropout time (eta_s minus `push_stop_margin_s`, 1 s) and
stays off until the warning is cleared. Worth a paragraph in the paper: prefetch must respect
the predicted outage start, not just the budget. The old config-5 runs are kept in
`results/tunnel20-before-push-stop/`.

**Second fix needed** (`5763ed9`): with only the eta-based stop (re-run 26 Sep, kept in
`results/tunnel20-eta-stop-only/`), config 5 improved (mean 18.0 s, median 6.9 s, 3.5 MB pushed)
but readers 15 and 16 still stalled: the warned dropout came 2.5 s early (predicted ~40.5 s, link
died at 38 s) and 26 pushes went into the dead link in the last 1.6 s. The server now also keeps
at most `prefetch.max_push_backlog_bytes` (256 KB) of push data unacknowledged per client, so a
link that dies early strands little whatever the timing estimate says. Config 5 needs one more
re-run with this.

Two runs had the PC asleep mid-run (config 2 and 5, session 7); `analyze.py` now detects and
skips such runs, and both were redone.

## Voice-call probe (26 Sep, Wi-Fi walk, 5 runs per mode)

50 packets/s through the tunnel as QUIC datagrams, echoed by the server:

| mode | switch at | packets lost | longest silence |
|---|---|---|---|
| react after failure (configs 1-4) | 29.4-29.5 s, 1 s after Wi-Fi dies | 1.8-2.0% | 0.93-1.08 s |
| switch early (config 5/5a) | 22.7 s, 5.8 s before | 0.3-0.5% | 0.05-0.07 s |

Switching early costs a call nothing beyond the 5G link's normal loss (0.2% each way); reacting
means a one-second dropout. This is the evidence for the "switch early" half of the claim, which
page loads in the walk couldn't show. `python -m experiments.voip_probe summary`.

**Third fix** (`d7d88e6`): with the backlog cap (re-run 26 Sep, kept in
`results/tunnel20-before-pto-reset/`), config 5's median dropped to 3.1 s, but 5 readers still
waited longer than with QUIC. Only ~265 KB went into the dead link now, yet the server's replies
left ~30 s after it received the reader's request: QUIC doubles its probe timeout after every
unanswered probe, and after a 45 s outage the next probe was ~30 s away, with nothing new allowed
out before it. Both tunnel ends now reset that backoff when a packet arrives after >1 s of
silence. It changes every QUIC config, so configs 2, 3 and 5 are being re-run together.

## Final car-tunnel results (`tunnel20`, 26 Sep, all fixes, 20 readers per config)

| config | mean wait (95% CI) | median | outage clicks from cache | pushed per run |
|---|---|---|---|---|
| 1 TCP+TLS | 31.7 s (22–41) | 28.5 s | 3 of 20 | 0 |
| 2 QUIC migration | 21.6 s (14–29) | 17.0 s | 3 of 22 | 0 |
| 3 fixed prefetch | 20.1 s (12–28) | 12.6 s | 5 of 23 | 0.24 MB |
| 5 full system | 14.0 s (7.5–21) | 3.4 s | 14 of 26 | 2.9 MB, <1% read |

Per reader, config 5 vs QUIC: better for 8 of 20, worse for none, mean saving 7.6 s (the rest
clicked a page that wasn't predicted and waited for the link like QUIC). Vs fixed prefetch:
better for 7, worse for 2, mean saving 6.1 s. The cost to report: ~2.9 MB pushed per tunnel.
Superseded runs, for the paper's "what went wrong" paragraph: `results/tunnel20-before-push-stop/`,
`-eta-stop-only/`, `-before-pto-reset/`.

## Next

1. **Image-only links** reach Jev with no text (found by `experiments/general_web.py` on a shop):
   use the image's alt text or the link's title. Changes Jev's questions for such links, so
   re-warm (`python -m experiments.warm_jev`) afterwards.
2. **Replay page** (`site/replay.html`): `make replay-export`, commit and push; rerun a few runs
   for the demo video.
3. **Figures**, redone from `tunnel20` and the voice probe when the paper layout is known.
4. More scenarios (LEO gap, GEO fallback); smaller items: Jev's overconfidence before its
   probabilities set budgets; origin modifying X% of pages between runs (revalidation savings);
   forums link mostly off-site, which `links.same_origin_only` drops.
5. Optional: rerun the voice probe on the final code (the probe-backoff reset only matters
   after long silences, so the walk results should be unchanged).

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
