# Next steps

The full project plan (architecture, milestones, risks) is in `docs/PLAN.md`.

## Status (24 Sep 2026, Linux PC)

- Steps 1–3 below are done on the Linux PC (CachyOS, Python 3.14; aioquic 1.3 has an abi3 wheel,
  so 3.14 works). **The Day-4 gate passed:** `make test-netns` -> 2 passed.
- Done since: client proxy, server proxy, TCP+TLS baseline transport (config 1), local origin
  server (`data/origin_server.py`) and ETag / If-Modified-Since revalidation, with loopback tests
  for both transports (`tests/integration/test_proxy_e2e.py`).
- `tunnel_probe` migrate result: max gap 52 ms (= probe resolution), see step 3.
- On Arch/CachyOS: after a kernel update, reboot before step 2, or `modprobe sch_netem` fails.

## 1. Get the repo and install

```sh
git clone https://github.com/a-meriac/Shajarah.git && cd Shajarah
sudo apt install python3-venv iproute2 iptables    # Ubuntu/Debian names
python3 --version                                   # must be 3.11–3.14
make venv                                           # creates .venv-linux on Linux
make test                                           # expect: 27 passed, 2 deselected
```

## 2. Check the kernel can emulate networks

```sh
sudo modprobe sch_netem && echo netem ok
```

If that fails on Ubuntu: `sudo apt install linux-modules-extra-$(uname -r)` and try again.

## 3. The Day-4 gate: does the tunnel survive a real network switch?

```sh
make test-netns
```

This builds the emulated network (client, router, server proxy and origin, each in its own
network namespace; the client has `wifi0`, `cell0` and `sat0` links) and runs two tests:

- **with migration:** the client sends a request every 50 ms over Wi-Fi. After 3 s, Wi-Fi is cut
  (100% loss) and the tunnel moves to `cell0`. It passes if requests keep completing and the longest
  gap stays under 1 s.
- **without migration:** same, but the tunnel stays on dead Wi-Fi. It passes if nothing completes
  after the cut, which shows the first test isn't passing by accident.

To see the actual numbers, e.g. the stall length for the paper:

```sh
make netns-up
sudo ip netns exec ep-srv .venv-linux/bin/python -m experiments.tunnel_probe server &
sudo ip netns exec ep-cli .venv-linux/bin/python -m experiments.tunnel_probe client
# -> {"mode": "migrate", "requests_ok": ..., "max_gap_s": ..., ...}
sudo ip netns exec ep-cli .venv-linux/bin/python -m experiments.tunnel_probe client --mode none
sudo pkill -f "experiments.tunnel_probe server"; make netns-down   # `kill %1` fails in fish
```

Result on the Linux PC (24 Sep): migrate mode `requests_ok 157, max_gap_s 0.052,
completed_after_switch 98`. The gap equals the 50 ms request interval, so migration itself adds
no measurable stall. Caveat for the paper: the probe migrates the instant it cuts Wi-Fi (zero
detection time), so this is the mechanism's best case, not a real handover's stall.

### If it fails

| Symptom | Likely cause |
|---|---|
| `iptables: command not found` / nft errors in `netns_setup.sh` | install `iptables` (the nft-backed one is fine) |
| `Error: Specified qdisc kind is unknown` | netem module missing, see step 2 |
| "with migration" gets no replies after the switch | source routing: check `sudo ip -n ep-cli rule` shows `from 10.2.0.2 lookup 102` |
| Both tests hang for 30 s | the server didn't start: run the server command above by hand and read the error |
| Leftover namespaces after a crash | `make netns-down` |

## 4. Next coding, in plan order

1. ~~Client proxy and server proxy end to end over the tunnel, TCP baseline transport (config 1),
   and an origin server.~~ Done. Still missing: `data/build_snapshot.py` to freeze ~1000 Wikipedia
   pages into `data/snapshot/wiki/<Title>.html` (the origin already serves that layout).
2. ~~ETag / If-Modified-Since revalidation.~~ Done. Still missing: let the origin mutate X% of
   pages between runs, to measure bytes saved.
3. Download a month of the Wikipedia clickstream. It's the answer key for scoring the link
   predictor: for each page, did Jev's top guesses match the links people actually clicked most?
4. Plug the Jev predictor (`edgeproxy/predictors/jev.py`, already working) into the server proxy
   (`ServerProxy.handle`: extract links, ask Jev, push with `PrefetchPolicy`). The client proxy
   already stores pushes in its cache and serves them. Jev needs `OPENROUTER_API_KEY` in `.env`
   (copy `.env.example`).

## Still open outside the code

- [ ] Fill in the unknowns at the bottom of `docs/jev_notes.md` (rate limits, data retention)
- [ ] Find public drive-test signal traces (e.g. the Raca et al. 4G/5G datasets, Lumos5G) to
      replace the hand-made signal curves in `emulation/scenarios/`
