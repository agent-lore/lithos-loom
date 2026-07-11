# ADR 0008 — story-develop's PR access runs through the typed GitHubClient, gh CLI kept only for local-checkout conveniences

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** Dave Snowdon

> Tracking task: **ARCH-7c** (architecture review 2026-07-06; split from ARCH-7,
> whose two low-risk dedups shipped in #239). Builds on [ADR 0002](0002-story-develop-session-mechanism.md)
> (develop-cycle) and [ADR 0004](0004-review-only-mode.md) (review-only mode).

## Context

Loom talks to one external service — GitHub — through **two adapters chosen by
subsystem**, and they overlapped specifically on pull requests:

- The **watcher family** (`github_issue_watcher`, `_github_issue_sync`,
  `_github_issue_push`, `_develop_pr_merge`) uses the typed HTTP adapter
  `github_client.GitHubClient`: bearer token from `gh auth token`, a typed error
  hierarchy (`GitHubError` / `GitHubAuthError` / `GitHubRepoNotFoundError` /
  `GitHubIssueNotFoundError`), rate-limit retry, and pagination.
- **story-develop** (`pr_delivery`, `review_resolve`) instead shelled the `gh`
  CLI for the *same PRs* — `gh api .../reviews`, `.../comments`,
  `.../requested_reviewers`, `gh pr comment`, `gh pr view`. `_develop_pr_merge`
  inspected a PR's merge state via `GitHubClient.get_pull_request` (HTTP) while
  `pr_delivery` created/commented/inspected the same PR via subprocess — two
  disjoint code paths and two error models (typed exceptions vs
  `CompletedProcess.returncode`) for one service.

That is a **shallow split**: the "how do I reach GitHub" knowledge was duplicated,
and a behavioural change (auth, a new PR field, rate limits) had two homes.

The confounding constraint is that **story-develop's plugin core is synchronous**
(`develop()` is a plain `def`; the turn loop runs subprocess agents, not asyncio),
while `GitHubClient` is async. This is the same impedance `lithos_io` / `daemon_io`
already resolve for the async `LithosClient`: bridge via `asyncio.run` and keep the
core sync.

Not every `gh` call is REST-shaped, though. `gh pr create`, `git push`, and
`gh repo view --json nameWithOwner` resolve the **local working tree** (its branches,
its origin remote) — something the REST API cannot do, because you must already know
`owner/repo` to call it. Those are genuine CLI conveniences, not REST calls wearing a
subprocess.

## Decision

**Consolidate PR access behind one seam — the `GitHubClient` interface — with two
adapters at that seam.** (Chosen from the review's "Option 1".)

1. **REST-shaped PR ops become typed `GitHubClient` methods** (added alongside the
   watcher's existing `get_pull_request` / `list_issues_since` / `update_issue_*`):
   `list_pull_request_reviews`, `list_pull_request_review_comments`,
   `request_reviewers`, `add_assignees`, `create_review_comment_reply`,
   `create_issue_comment`, plus `get_pull_request` extended to carry the PR's
   `head_sha` / `base_ref` / `head_ref` / `title` / `body`. `GitHubClient` stays
   **pure HTTP** — no subprocess inside it — so story-develop and the watcher family
   share one error hierarchy, one rate-limit policy, one pagination.
2. **The sync plugin core reaches those methods through `github_access.github_call`**,
   an `asyncio.run` bridge modelled on `lithos_io` / `daemon_io`. story-develop's
   existing wrapper functions (`pr_delivery.request_copilot`,
   `fetch_copilot_comments`, `review_resolve._gh_pr_view`, …) remain the seam the
   orchestration programs against and the tests monkeypatch; only their bodies swap
   from `gh` subprocess to `github_call(...)`, translating typed exceptions back to
   each caller's existing contract (raise / `False` / `None` / `[]`).
3. **The gh CLI is kept only for local-checkout conveniences** — `create_pr`,
   `push_branch` (`pr_delivery`), and the shared `github_access.repo_name_with_owner`
   — because the REST equivalent can't resolve a working tree's remote/branches.
   "Two adapters (typed HTTP + gh CLI) at one seam is fine; two seams is not": the
   caller depends on the wrapper, not on whether a given op is HTTP or subprocess
   underneath.

We considered a `PrGateway` Protocol injected through `deliver_guarded` → `main`. It
was rejected as churn without proportional gain: the module-function wrappers already
*are* the test surface the suite uses, `github_client` already concentrates the REST
grammar, and threading an injected gateway through the delivery-critical path (the
lifecycle that shipped #191/#192/#194/#196/#198) added risk a soak-alone change should
not take. The facade would have been a thin pass-through (deletion test).

**#207 becomes moot rather than worked-around.** `review_resolve` never requested the
PR's base-ref OID (which `gh pr view` rejected); it derives the diff base as the
merge-base of the base branch and the head — the true diff base GitHub shows, which
also avoids spurious deletions when the base advanced after the PR was cut. That
derivation stays local git; the typed `get_pull_request` supplies only `base_ref`
(the branch name to merge-base against), so there is nothing to reintroduce.

## Consequences

- One home for GitHub PR behaviour (locality): auth, rate limits, a new PR field, or a
  changed endpoint touch `github_client` once, for both story-develop and the watchers.
- Error handling unifies on the typed hierarchy; the self-author-422 fallback in
  `request_operator_review` now branches on a typed `GitHubError`'s message instead of
  a subprocess `stderr` string.
- Each bridged call resolves `gh auth token` and builds a short-lived `httpx` client —
  one subprocess per REST op, comparable to the `gh api` subprocess it replaced. A
  token/client cache is a possible future optimisation, not a correctness concern for
  delivery's handful of calls.
- `GitHubClient`'s pure-HTTP nature, the watcher family's existing usage,
  `git push` staying git, and the ADR 0002/0004 develop-cycle decisions are unchanged.
