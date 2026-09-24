# Next steps (on the Linux PC)

Left on 24 Sep 2026. The code so far was written and tested on macOS. Everything below is the
part that needs Linux. The full project plan (architecture, milestones, risks) is in `docs/PLAN.md`.

## 1. Get the repo and install

```sh
git clone https://github.com/a-meriac/Shajarah.git && cd Shajarah
sudo apt install python3-venv iproute2 iptables    # Ubuntu/Debian names
python3 --version                                   # must be 3.11–3.13 (aioquic has no 3.14 build)
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
sudo kill %1; make netns-down
```

### If it fails

| Symptom | Likely cause |
|---|---|
| `iptables: command not found` / nft errors in `netns_setup.sh` | install `iptables` (the nft-backed one is fine) |
| `Error: Specified qdisc kind is unknown` | netem module missing, see step 2 |
| "with migration" gets no replies after the switch | source routing: check `sudo ip -n ep-cli rule` shows `from 10.2.0.2 lookup 102` |
| Both tests hang for 30 s | the server didn't start: run the server command above by hand and read the error |
| Leftover namespaces after a crash | `make netns-down` |

## 4. After the gate passes (next coding, in plan order)

1. Client proxy and server proxy end to end over the tunnel, TCP baseline transport (config 1),
   and an origin server that serves a frozen Wikipedia snapshot.
2. ETag / If-Modified-Since revalidation (the NOT_MODIFIED frame already exists).
3. Download a month of the Wikipedia clickstream. It's the answer key for scoring the link
   predictor: for each page, did Jev's top guesses match the links people actually clicked most?
4. Plug the Jev predictor (`edgeproxy/predictors/jev.py`, already working) into the server proxy.
   It needs `OPENROUTER_API_KEY` in `.env` (copy `.env.example`).

## Still open outside the code

- [ ] Fill in the unknowns at the bottom of `docs/jev_notes.md` (rate limits, data retention)
- [ ] Find public drive-test signal traces (e.g. the Raca et al. 4G/5G datasets, Lumos5G) to
      replace the hand-made signal curves in `emulation/scenarios/`
