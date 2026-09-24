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

Measured on the snapshot (July clicks, median per seed page): the first 40 links, which is what
Jev sees, get 65–69% of real clicks; the best possible 40 would get 87–96%. Removing navigation
boxes and references doesn't change the first 40. This caps what any predictor can reach at 40.

Tests: `make test` -> 56 passed (unit + loopback, any OS). `make test-netns` -> 2 passed (Linux).

## Next, in plan order

1. **Prefetch:** in `ServerProxy.handle`, extract links (`server/links.py`), ask Jev
   (`predictors/jev.py`), push with `PrefetchPolicy`. The client proxy already stores pushes and
   serves them. Jev needs `OPENROUTER_API_KEY` in `.env` (copy `.env.example`; this PC has none).
2. **Predictor evaluation** (`experiments/predictor_eval.py`): Jev's top guesses vs the July
   clicks, split by popularity bucket.
3. **Link choice (offline, free):** measure click coverage for 60/80/100 links and for simple
   selection rules (repeated links, lead/infobox first) before settling `jev.max_options`.
4. **Compress pushed pages** in the tunnel: ~5× smaller HTML means ~5× more pages per outage budget.
5. **Origin changes between runs:** let the origin modify X% of pages, to measure the bytes
   revalidation saves.
6. `data/sessions.py` (browsing sessions from the August clickstream), then the plan's D10–D13:
   handover predictor on real traces, warm standby path (`path_manager.py`, config 5), the
   experiment runner over all configs × scenarios × seeds, VoIP probe, figures.

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
