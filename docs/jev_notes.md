# Jev (TypeSafe "System One") — integration notes

Fill this in from the official docs (docs.typesafe.ai) before writing `edgeproxy/predictors/jev.py`.
Do not use unofficial endpoints.

- [ ] Access route: official API (waitlist status: ___ ) / gateway listed as official in the docs: ___
- [ ] SDK: `typesafe-sdk` version ___, `TypeSafeClient.system_one` call shape for a Choice question
- [ ] Model alias pinned for experiments (record the resolved version, not just `jev-latest`): ___
- [ ] Max options per Choice question (expected 255) and max option/label length: ___
- [ ] Rate limits (req/min, concurrency) and pricing per call: ___
- [ ] Measured latency p50/p95 from the server proxy location: ___
- [ ] Data retention / privacy terms for the page content we send (paper: privacy section): ___
- [ ] Is the returned probability calibrated over the listed options only, or is there an implicit "none"? ___

Budget: ~1000 snapshot pages × 1 call each for the offline predictor experiment, all cached to
`data/cache/jev/` keyed by (page key, model version, candidate list hash).
