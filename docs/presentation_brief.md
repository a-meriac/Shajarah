# Shajarah (شجرة): brief for planning the presentation and video

Context for planning the challenge presentation and a 1–2 minute video script. Everything below
is from the project's code, test logs and results as of 27 Sep 2026. Numbers are exact unless
marked approximate.

## The challenge

- **EDGE Challenge, ATP 2026.** Topic: keeping connectivity seamless when a device moves between
  Wi-Fi, 5G and satellite, including outright dropouts.
- **Deliverables:** a 10-page PDF and a 1–2 minute video. **Deadline: 11 October 2026.**
- The official brief (judging criteria, required sections) is not in the project files; check it
  before finalising the structure.

## One-line pitch

Shajarah keeps your connection alive when your phone switches networks: no new handshake, and the
switch happens before the signal drops. As a bonus, when there's nowhere to switch to (a tunnel),
an AI model picks the pages you're likely to open next and fetches them in advance.

**Emphasis (the team's decision):** the main appeal is not having to redo a TCP handshake when the
network changes (QUIC connection migration) plus switching early. The AI prefetching is an
additional feature, not the headline.

## The problem

Phones ignore a fading signal until the connection breaks. Only then do they look for another
network, reconnect (a new TCP handshake), and retry whatever was loading: frozen pages, dropped
calls, spinners in tunnels, lifts, trains. The usual remedies each solve half:
- QUIC can carry a connection across networks, but only after the switch, and it can't load
  anything while there's no signal at all.
- Prefetching can store pages in advance, but done all the time it wastes data.

## How it works

```
browser → client proxy (on the phone) ══ QUIC tunnel over Wi-Fi / 5G / satellite ══ server proxy (cloud) → websites
```

1. **Watch the signal.** The phone reads signal strength on every network 10 times a second and
   fits a trend line over the last 5 s. If the current network will fall below usable strength
   within ~8 s (−80 dBm Wi-Fi, −110 dBm cellular), it raises a dropout warning.
2. **Switch before the drop (main feature).** If another network is healthy, the tunnel moves there
   immediately using QUIC connection migration: same connection, new network, no new handshake,
   nothing in flight lost. Fallback without prediction: if pings through the tunnel go unanswered
   for 1 s, switch then.
3. **Prefetch when there's nowhere to go (bonus).** In a tunnel every network dies together. The
   phone tells the server "dropout in N seconds". The server asks **Jev** (TypeSafe's decision
   model, via OpenRouter) how likely each link on the current page is to be clicked, then pushes
   the likely pages, and the likely pages after those ("two clicks deep").
4. **Read without signal.** Pushed pages open instantly from the phone's cache; pages read before
   come back as saved copies.

Details worth knowing:
- **Jev input:** page title, last 5 pages read, up to 40 links (menus/headers/footers skipped, image
  and meta links filtered). Only titles and link text are sent, never page content. ~$0.00008 and
  ~0.7 s per call. Normal mode pushes only links Jev rates ≥ 25%; with a dropout warning ≥ 1%.
- **Pushing stops just before the predicted dropout** and never has more than 256 KB unacknowledged
  (see lessons below).
- **Compression:** pages are deflated in the tunnel, 5.7× smaller for HTML (median).
- **Phone cache:** 200 MB, least-recently-used eviction; unread prefetched pages expire after 15 min.
  Cached pages are revalidated with the server (ETag) before reuse; a saved copy is shown when
  there's no signal.
- **Privacy:** the prototype calls Jev through a hosted API; a production system would run the model
  on our own server so nothing leaves our infrastructure.

## What makes it new

Connection migration and prefetching both exist. Shajarah adds **timing**: one prediction from the
fading signal drives an early, handshake-free network switch, and, when there's nowhere to switch
to, tells the server when to prefetch, how much, and when to stop.

## How it was built and tested

- **Built in Python** (aioquic for QUIC): client proxy, server proxy, QUIC tunnel with migration,
  TCP baseline, dropout predictor, path manager, prefetch policy, Jev client. ~120 automated tests.
  Development ran on a macOS laptop and a Linux PC (the emulation needs Linux). Started 24 Sep 2026.
- **Emulated network on Linux:** the phone, a router, the server and the websites each in their own
  network namespace; the phone's Wi-Fi, 5G and satellite links shaped with realistic delay, loss and
  bandwidth (netem); scripted scenarios fade and cut links on a timeline.
- **Scenarios:** (1) a car entering a **45-second tunnel** with no signal on any network (cellular
  fades first); (2) **walking out of a building's Wi-Fi** with 5G available throughout.
- **Readers:** simulated from Wikipedia's public clickstream (the only large public record of real
  clicks). Each follows links real readers chose in August 2026, with realistic reading times
  (median 20 s), over ~2,150 Wikipedia pages frozen locally. Every setup replays exactly the same
  readers and clicks.
- **Setups compared:** Ordinary (TCP, reconnects after failure: how most apps work); Modern (QUIC
  migration, no prediction); Always-on prefetch; Shajarah. (Also hover-prefetch and ablations in an
  earlier batch.)
- **Separate tests:** Jev scored against real July clicks on 1,002 pages; a live-call probe
  (50 packets/s) on the Wi-Fi walk; the pipeline run on ordinary sites (BBC, GOV.UK, Python docs,
  a shop, Hacker News).

## Results

**Car tunnel, 20 readers per setup** (total time spent looking at a page still loading, per trip):

| Setup | Mean wait (95% CI) | Median wait | Tunnel clicks opened instantly | Extra data |
|---|---|---|---|---|
| Ordinary (TCP) | 31.7 s (22–41) | 28.5 s | 3 of 20 | none |
| Modern (QUIC) | 21.6 s (14–29) | 17.0 s | 3 of 22 | none |
| Always-on prefetch | 20.1 s (12–28) | 12.6 s | 5 of 23 | 0.24 MB |
| **Shajarah** | **14.0 s (7.5–21)** | **3.4 s** | **14 of 26** | **2.9 MB** (<1% read) |

- **56% less waiting than an ordinary connection**; median 3.4 s vs 28.5 s.
- **Reader by reader vs QUIC: faster for 8 of 20, slower for none** (mean saving 7.6 s). The others
  clicked a page Jev hadn't predicted and waited for the signal exactly as with QUIC.
- QUIC alone already beats TCP (21.6 vs 31.7 s mean), which is the no-handshake benefit.

**Live call on the Wi-Fi walk, 5 runs each:**

| | Switch time | Longest silence | Packets lost |
|---|---|---|---|
| React after Wi-Fi dies | 29.4–29.5 s (1 s after Wi-Fi died at 28.5 s) | 0.93–1.08 s | 1.8–2.0% |
| **Shajarah (switch early)** | **22.7 s (5.8 s before)** | **0.05–0.07 s** | **0.3–0.5%** (the network's normal loss) |

**Tunnel migration check:** requests every 50 ms across a Wi-Fi cut: longest gap 52 ms (the probe's
resolution) with migration; nothing gets through without it.

**Click prediction (Jev, 1,002 pages, share of real clicks caught):** top pick 6.2%, top 3 16.1%,
top 10 36.2%, against 2.7 / 9.4 / 32.6% for simply taking the first links on the page, and
19.2 / 38.1 / 63.6% for a site with its own click logs. Honest framing: modest accuracy, about twice
as good as position on its top picks; misses cost nothing extra.

**Push cutoff sweep:** with a dropout warning, pushing links Jev rates ≥ 1% puts the next page on
the phone 57% of the time (vs 39% at ≥ 3%), for ~0.7 MB per warning.

## Five showcase tests (used on the replay page)

Hand-picked: the readers where Shajarah saved the most waiting (disclose this; averages above).

| Test | Ordinary (TCP) | Modern (QUIC) | Shajarah | Story |
|---|---|---|---|---|
| 1 | 60.5 s | 39.0 s | 0.5 s | One click mid-tunnel (0:46). The ordinary phone gives up after a minute without loading the page; Shajarah had already delivered it. |
| 2 | 54.3 s | 42.1 s | 2.7 s | Three pages in a row with no signal: two prefetched (one two clicks ahead), one saved copy. |
| 3 | 44.9 s | 32.8 s | 2.8 s | Two clicks in the tunnel: next page ready instantly, previous one from cache. |
| 4 | 53.2 s | 41.4 s | 16.3 s | Partial win: one of two tunnel clicks predicted. |
| 5 | 21.6 s | 9.4 s | 1.6 s | One tunnel click, prefetched. |

## Problems found and fixed (good material for "what we learned")

1. **Prefetching into a dying link backfired.** The first full version pushed pages until the link
   died; the unacknowledged backlog jammed the connection when coverage returned, and 5 of 20
   readers waited longer than with plain QUIC (one run: 29 pushes during the outage, first reply
   30 s after the signal came back). Fixes, in three rounds:
   - stop pushing at the predicted dropout time (mean 19.7 → 18.0 s, data 6.6 → 3.5 MB);
   - the prediction came ~2.5 s late (predicted ~40.5 s, link died at 38 s), so cap unacknowledged
     push data at 256 KB whatever the prediction says;
   - (then the next problem, below).
2. **QUIC waits too long after a long outage.** QUIC doubles its retry timer with every
   unanswered attempt; after 45 s of silence the next retry was ~30 s away, so the connection stayed
   silent long after the signal returned. Fix: reset that backoff as soon as the other end is heard
   from again. Applied to all QUIC setups for fairness. Result: no reader worse than QUIC.
3. **A predictive switch looks pointless for page loads** (the walk: every setup waited < 2 s,
   because few clicks land in the 1 s reactive gap). The live-call probe showed where it matters:
   1 s of silence vs 60 ms.
4. **Smaller bugs caught by the tests:** pushed pages stored under a different URL spelling than the
   browser requested (accented titles), so they never hit; messages reordered after an outage
   scrambled the reading history sent to Jev; prefetches of pages outside our frozen snapshot were
   silently free, understating the data cost (fixed with realistic stand-in pages); Jev's 40 link
   slots partly wasted on image and editor links; the test PC went to sleep mid-run (runs are now
   checked and redone).

## Limitations (state them)

- Emulated network with hand-made signal curves; real drive-test traces are the next step.
- Measured on Wikipedia, the only public record of real clicks; checked on other sites, but no
  click data there.
- Prefetching costs ~3 MB per dropout, mostly unread (about one phone photo, or a couple of minutes
  of music), spent mainly just before the dropout.
- Jev's click prediction is modest (above).
- Prototype uses a hosted API for Jev; HTTPS on real devices needs a design decision (local
  decryption, a browser extension, or no prefetching for HTTPS).
- Real phones: Android exposes signal strength and dual-network binding (needs a native app); iOS
  has no public signal API.

## Assets

- **Project page (English and Arabic):** https://a-meriac.github.io/Shajarah/ with results and
  explanation; Arabic at `ar.html`.
- **Replay page:** https://a-meriac.github.io/Shajarah/replay.html (Arabic: `replay-ar.html`).
  The five tests above, second by second: coverage along the road, signal with the dropout warning
  marked, ping latency (red ticks = no reply), and each click as a bar whose length is the wait
  (green = opened from the phone, grey = over the network, red = failed), plus prefetch bursts.
  Two setups side by side; play at 1–20×. Good screen-capture material for the video: e.g. Test 1
  at 5×, Shajarah vs Ordinary, around 0:30–1:50.
- **Name:** Shajarah (شجرة) means "tree" in Arabic; the wordmark is set in Beiruti.
- Deadline plan: write-up and video 7–10 Oct, submit by the 10th.
