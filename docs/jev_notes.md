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

Sanity check: for the Albert Einstein page, with a reader coming from Physics → Theory of relativity, it gave
special relativity 0.80, Nobel Prize 0.17, photoelectric effect 0.02, and Ulm/Princeton (infobox) 0.

## Gotchas

- **Probabilities are rounded to 2 decimals.** With hundreds of options many tie at 0, so `JevPredictor` sends only
  the first 40 links on the page (`max_options`). This also affects the calibration
  analysis; mention it in the paper.
- Context limit is 32k tokens per request (page state + all options).

## Still unknown

- [ ] Rate limits (requests per minute, concurrency)
- [ ] Maximum options per Choice question (TypeSafe docs suggested 255)
- [ ] Data retention for the page content we send (check the data policy on the
      [model page](https://openrouter.ai/typesafe/jev-1.13)); needed for the privacy section
- [ ] Latency from the server proxy's real location (AWS region)
