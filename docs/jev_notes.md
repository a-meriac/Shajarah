# Jev (TypeSafe "System One") — integration notes

Code: `edgeproxy/predictors/jev.py`. Tested 24 Sep 2026. TypeSafe's own API isn't accepting new users, so we use Jev through OpenRouter.

## Access

- Endpoint: `POST https://openrouter.ai/api/alpha/decisions` (labelled alpha, may change)
- Auth: `Authorization: Bearer $OPENROUTER_API_KEY`. The key lives in `.env` (git-ignored); see `.env.example`
- The official `typesafe-sdk` also works: set its base URL to `https://openrouter.ai/api/v1/systemone`
- Model: request `typesafe/jev-1.13`. The response reports the exact version as
  `typesafe/jev-1.13-20260917`; cite that one in the paper. Don't use the `~typesafe/jev-latest` alias
  for experiments.
- Billing: prepaid OpenRouter credits (the account has $5). Calls fail with HTTP 402 when the credits run out.

## Request format (Choice question)

```json
{
  "model": "typesafe/jev-1.13",
  "state": {"current_page": "...", "previous_pages": ["...", "..."]},
  "questions": {
    "next_click": {
      "type": "choice",
      "instructions": "Which link on this page will the reader most likely click next?",
      "criteria": {"option_key": "description of the option", "...": "..."}
    }
  }
}
```

Response: `answers.next_click.probabilities` (one entry per option key), `choice`, `confidence`, plus
`usage.input_tokens` and `usage.cost` (USD).

## Measured

| | |
|---|---|
| Latency (from macOS dev machine, 3 calls) | 471–886 ms round trip |
| Cost, 5 options, 464 input tokens | $0.0000195 per call |
| Estimated cost for a 40-link request | ~$0.0001 per call, ~$0.10 for the 1000-page experiment |
| Actual, 40 links (Linux PC, 25 Sep) | ~2,000 input tokens, $0.00008 per call; 1,002-page eval $0.074 |
| Latency (Linux PC, cached replays of real calls) | median ~0.7 s |
| Load | ~5,400 calls at 4 in parallel (eval + warm-ups), no 429s or other errors |

Sanity check: for the Albert Einstein page, with a reader coming from Physics → Theory of relativity, it gave
special relativity 0.80, Nobel Prize 0.17, photoelectric effect 0.02, and Ulm/Princeton (infobox) 0.

## Gotchas

- **Probabilities are rounded to 2 decimals.** With hundreds of options many tie at 0, so `JevPredictor` sends only
  the first 40 links on the page (`max_options`). This also affects the calibration
  analysis; mention it in the paper.
- Context limit is 32k tokens per request (page state + all options).

## Checked 25 Sep (public docs)

- **Pricing:** $0.042 per million input tokens, output tokens free, 32,000-token context
  ([Jev docs](https://openrouter.ai/docs/guides/community/jev)). Matches what we pay.
- **Endpoint:** still labelled alpha, same URL.
- **Rate limits:** OpenRouter publishes no per-minute limit for paid requests; free models are
  capped, Cloudflare blocks abuse, and an "in-flight spending budget" limits concurrent requests
  by cost ([limits](https://openrouter.ai/docs/api-reference/limits)). Our 4-way concurrency
  never hit a limit.
- **Maximum options per Choice question:** not documented. We have used up to 100.
- **Probability rounding** (2 decimals): not documented; still what we observe.
- **Privacy:** OpenRouter lets an account refuse providers that train on prompts and filter
  providers by data policy per request
  ([privacy and logging](https://openrouter.ai/docs/features/privacy-and-logging)). What we send
  is only page titles, link texts and link targets (no page content unless
  `jev.include_context` is on, and never anything from the user's device beyond the pages'
  URLs and titles).

## Privacy position (for the paper)

We don't rely on TypeSafe's retention policy. The prototype calls Jev through a hosted API because
that was available; in a perfect world the model would run locally on the proxy server (a local
decision model such as Laya), so page titles and link texts never leave infrastructure we control.
Say this in the paper's privacy section and list the hosted API as a prototype limitation.

## Still unknown

- [ ] Latency from the server proxy's real location (AWS region)
