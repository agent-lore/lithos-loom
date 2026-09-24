# The change under review

`lithos-loom`'s github-watcher ingests external review activity on a
delivered PR that is still awaiting its human merge (PRD pr-reconciliation
S2, `src/lithos_loom/subscriptions/external_reviews.py`). New reviews,
inline review comments and Conversation-tab comments are surfaced as a
one-shot `[ExternalReview]` finding on the blocked story, de-duped by
per-stream high-water marks written on the `pr` gate.

Acceptance:

- Every stream's new rows are reported exactly once; skipped material still
  advances the marks so it is never re-fetched or re-considered.
- The marker is scoped to the PR url: a replacement PR re-evaluates from
  scratch.
- A failed finding / marker write is retryable — the whole batch retries on
  the next sweep rather than being lost.
