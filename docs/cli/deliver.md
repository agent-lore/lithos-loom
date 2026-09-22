# `lithos-loom develop deliver` — reference

Turn a **stopped** story-develop run's branch into a delivered, monitored PR:
push it, open (or adopt) its PR, raise the `pr` gate, and complete the
needs-human gate the stop raised. Zero agent tokens — nothing is implemented or
reviewed here; the work already exists on the branch.

This is the operator's **third choice** on a stopped run. The other two are the
needs-human gate's own pair: complete the gate → loom re-develops the story
*from scratch* on a fresh branch (discarding the rounds that landed), or cancel
the story → abandon it. `deliver` keeps what the run produced.

## TL;DR

```bash
# See what it would do — no push, no gh call, no Lithos write
lithos-loom develop deliver de459d10 --dry-run

# Deliver it: push, open the PR, swap the needs-human gate for a pr gate
lithos-loom develop deliver de459d10

# By story id instead of run id (its newest run), recording the result
lithos-loom develop deliver ac1380c1 --json /tmp/deliver.json

# The work dir is gone (retain_failed_workdirs = false): name the branch
lithos-loom develop deliver --branch loom/story-ac1380c1-4f2a --story ac1380c1

# A PR only — no gate, no merge tracking (an UNMONITORED hand-off)
lithos-loom develop deliver de459d10 --no-gate
```

## What it does

Five steps, in this order, each idempotent — a second invocation after a
partial first pass finishes the job and changes nothing else.

1. **Push, append-only.** The branch is classified against `origin` — absent
   → create; already equal → nothing; an ancestor of the local branch → a
   fast-forward; **diverged → refused** (exit 1) naming both shas. `deliver`
   never force-pushes: the divergence may be a collaborator's commit, and
   re-developing the story is the operator's other lever. The classification
   happens **under the delivery claim, immediately before the push**, and the
   push sends that exact **commit** (`<sha>:refs/heads/<branch>`), not the
   symbolic ref — a local process that advances or rewrites the branch in
   between can never make the command classify one commit and deliver another,
   or report a sha it did not send. A push that reports failure is checked
   against the remote before it is believed: a ref that now holds the pushed
   commit *did* land (the response was lost, not the update), so the delivery
   carries on rather than claiming nothing was written. The local branch's
   upstream is then set to `origin/<branch>` — a pinned refspec cannot carry
   `push -u`'s meaning, so the tracking config is written directly. That last
   write is genuinely best-effort *and reported*: a read-only or locked
   `.git/config` never unwinds the pushed state (the branch IS on `origin`)
   and never passes silently either — it becomes one `[Friction]` line naming
   the `git branch --set-upstream-to` that finishes it.
2. **Open or adopt the PR** — but only *this branch's own* PR. `gh pr list
   --head` matches on the head **branch name** alone, so a PR opened from a
   fork whose branch carries the same name looks identical; adopting one would
   point the `pr` gate, merge tracking, review ingestion and the story's
   eventual completion at a third party's work while retiring the story's
   escalation. A candidate is adopted only when it is **same-repository**, its
   **head is the sha just pushed**, and its **base is the base this delivery
   targets** — `--base` when given, else the repository's default, resolved
   *before* the decision so the comparison is never skipped; anything else is
   refused (exit 1) naming what was found. Otherwise
   a PR is opened through the same `pr_delivery` seam story-develop uses on
   approval, onto `--base` or the repo's default branch. The body is the
   generated one — what / acceptance criteria / review / `Closes #N` for an
   issue-linked story — plus a **`## Provenance`** section naming the run, its
   stop *classification*, the branch, and the coder's final handoff summary.
   `[story_develop] operator_github_login`, when set, is asked for review
   exactly as on a daemon delivery.

   A `gh pr create` that **reports** failure is re-asked before it is
   believed, exactly as the push is: the call can commit and lose its
   response, and an open PR that the command reported as never opened would be
   left ungated for ever. The re-ask applies the same adoption rule, so a PR
   recovered that way is one this delivery could have adopted; if GitHub
   answers and there is no such PR, the original failure stands and the push
   is reported as unfinished (exit 2). If the re-ask itself **cannot be made**
   — the create failed *and* the list failed — a PR may exist, and its absence
   is the one thing this run never established: the command reports `PR
   UNCERTAIN` and exits 2, and no line in that state says `NO PR` or "no PR
   was opened", because an operator told a PR is absent opens a second one by
   hand. The push has the same rule: a push that reports failure over a remote
   that then cannot be read is `PUSH UNCERTAIN`, not a refusal — and the
   headline never claims `PUSHED` over an invocation that pushed nothing.

   Every `gh` call is **pinned** to `--repo <owner/name>` resolved from the
   checkout's `origin` — the remote step 1 pushed to. Letting `gh` infer the
   target would, for a fork checkout, resolve to the *parent* repository: the
   PR, the review request and the gate's `repo` metadata would land somewhere
   the branch was never pushed.
3. **Raise the `pr` gate** on the story and record it: `pr_gate_id`, and — on
   the same write — a per-key delete of the stop's failed-attempt marker and
   its `needs_human_gate_id` provenance. This is literally the write the
   daemon's own delivering exit makes (`record_delivery_on_story`), so a
   hand-delivered story is indistinguishable from a daemon-delivered one to
   every later sweep. The story write is made whenever the **live** story does
   not already say it, so a first pass that created the gate but lost the
   metadata write is *repaired* by the next run rather than skipped.

   A gate is adopted only when it watches **this** PR. An open `pr` gate
   pointing at a *different* PR — **even beside one that does match** — means
   the story is already behind another delivery: the command refuses (exit 2),
   leaves every gate alone and leaves the needs-human gate open, rather than
   letting the story's merge semantics stay hostage to a PR that does not
   contain this branch. The gate decision is taken on a **fresh** read (the
   initial one predates the push), and the whole delivery runs under a
   `deliver` claim on the story whose lifetime exceeds the command's entire
   external budget and which is **renewed immediately before the gate work**,
   so two concurrent invocations cannot both decide "no gate yet" and raise
   one each.
4. **Complete the stop's loom `human` gate(s)** — found from the story's
   incoming `waits_on_gate` **edges**, never from the `needs_human_gate_id`
   key (provenance only: it can be stale, and a story may carry several
   gates). This happens **after** step 3 so the story is never momentarily on
   the ready frontier: a story behind a `pr` gate is absent from
   `lithos_task_ready`, so the runner's readiness check defers it and the
   completion cannot trigger a duplicate run. If no `pr` gate could be raised,
   the human gate is deliberately **left open** — it is then the only thing
   standing between the story and a re-dispatch.

   **Only THIS run's own escalation is retired.** Two keys, both required:

   - **the gate's `route` is one this host configures** (`[[routes]]`) — an
     *allowlist*, not a list of the subsystem routes loom happens to ship
     today. Completing an `external-remediation` gate is not a formality but a
     **decision**: it re-arms the S5b budget and lets autonomous `converge
     --from-github` spend money and push to the delivered PR again. That
     consent is the operator's alone, and a denylist would admit the next
     subsystem to raise a gate;
   - **the gate names the run being delivered** (`metadata.run_id`). A story
     can legitimately match two dispatch routes, each with its own stopped run
     and its own open escalation; retiring route B's gate because route A's
     branch was delivered discards a decision nobody made. When no candidate
     names any run (an older gate, or one raised without a run id) a *single*
     candidate is unambiguous and is retired; two are not, and neither goes —
     name the run to retire one. The run-dir-less `--branch`/`--story` form
     takes the same single-candidate rule, since it knows no run id at all.

   Everything else is left **open**, printed, and named in the finding with
   the reason it was kept. Leaving a gate open costs nothing: the `pr` gate
   this delivery raised holds the story either way. Each retired gate also
   authorises clearing *its own* route's failed-attempt marker — never another
   route's — and the story's `needs_human_gate_id` survives while it points at
   a gate this delivery kept.
5. **Post `[ManualDelivery]`** on the story: the run, the PR (opened or
   adopted), the delivered sha (always — an audit that cannot be checked later
   is no audit), the `pr` gate that now holds it, the gates retired, and the
   gates deliberately kept open. The record says what was **done**, never what
   was attempted: a gate phase that failed reads "the pr gate was NOT raised",
   which is not the same sentence as the deliberate `--no-gate` hand-off. Any
   degradation rides along as `[Friction]` text in the same finding. The
   **One rule decides whether it posts:** the `metadata.manual_delivery`
   marker on the story. Post unless the story already records a *complete*
   delivery of this `(run, PR)`. The marker is written **after** the post
   (finding-then-mark, as the subscriptions do) and **only for a delivery that
   finished** — so:

   - everything works → one finding, marked, and every later run is silent
     whatever changed in between (a repair pass never duplicates a record the
     story already carries);
   - a partial pass → its finding says what is owed and leaves no marker, so
     the run that *completes* the delivery posts the corrected record once and
     marks it;
   - a crash between the post and the marker → at most one duplicate, the same
     at-least-once trade every `post_finding_then_mark` caller in loom makes,
     and the safe direction (the alternative loses the audit entirely).

   The marker lives on the story, not the gate, so `--no-gate` gets the same
   guarantee and a gate the merge sweep completes cannot take the record with
   it. It records the state the delivery LEAVES BEHIND, on two axes: whether
   the PR ended up **gated** (a `pr` gate the story already carries for this
   PR counts, not only one this invocation raised) and whether the gate
   **swap** finished — meaning *no open needs-human gate that could be this
   run's escalation is left*, measured against the story's gates rather than
   against this invocation's authority to retire them. The difference
   matters: that authority moves with the host's configured routes and with
   how many unattributed gates are open, so calling an empty entitlement a
   finished swap would let one run's record silence the later one that
   actually retires the gate. Both are floors: a run that actually gates a PR delivered
   `--no-gate`, or that completes the human gate an earlier pass could not,
   posts the corrected record — step 5 promises the finding names the gates
   retired, so the run that retires them is the one that speaks — while a pass
   that achieves less than the record already carries (a later `--no-gate`
   over a gated delivery, a pass with nothing left to retire) corrects nothing
   and stays silent. A `--no-gate` pass over a PR that *is* already gated
   names the gate that holds it, never "UNMONITORED" — the story must not
   contradict its own open gate. **Known boundary:** if the process dies before the finding *and* the PR
   is merged before any re-run, the story is terminal and its PR is closed —
   `deliver` will not find it, and the merge's own `[GateResolved]` finding is
   the record that survives.

From there the PR is a first-class PR-maintenance object (PRD
[`pr-reconciliation.md`](../prd/pr-reconciliation.md)): landability
(`[PRConflicted]`), external-review ingestion and `converge --from-github`
remediation, the base-move re-gate, the conflict resolver, merge → story
completed + dependents nudged, and it counts against the project's S6
admission cap.

**A live dispatch owns the story.** A run writes its terminal `state.json` at
the end of its dialogue, but the daemon applies the result — and raises the
needs-human gate — only after that. Delivering inside that window would gate a
story whose escalation does not exist yet, complete nothing, and leave the
runner to raise the gate afterwards: both gates standing and a finding claiming
the swap was made. Two guards close it, before anything is written:

- the story's **claims** (read with it): a route holding one is a dispatch in
  flight, and `deliver`'s own claim is a different aspect, so nothing else
  would stop it;
- the run's **escalation handoff**. An absent claim proves nothing on its own —
  the route-runner's renew loop swallows every renewal failure, so a Lithos
  outage longer than the claim TTL leaves the claim expired *and invisible*
  while the plugin writes its terminal state and the runner waits to apply the
  result. So the command also requires the handoff to be durably visible on
  the story — and *still holding it*, or the same interleaving arrives from
  the other end: an **open** needs-human gate naming this run, an open gate
  this delivery would retire, or a failed-attempt marker naming the run **and
  naming no gate** (the marker-only `[BlockerFailed]` fallback, the one marker
  shape that suppresses dispatch by itself). A marker that names a gate is
  deliberately not enough: there the gate decides, and once the operator
  completes it the marker stays behind as history while the story goes back on
  the ready frontier — reading it as a handoff would deliver the old run in
  exactly the window the route walks from *ready* to *claimed*, and the run it
  dispatches would raise its own gate over a story this command had just
  reported as delivered. Three things let a delivery past it, each meaning
  there is no producer to race: the
  story already carries a delivery of its own (an idempotent re-run behind its
  own open `pr` gate), **no loom daemon is running on this work dir** (the
  pidfile `drain` uses — the salvage this command exists for), or the
  run-dir-less `--branch`/`--story` form, which is the operator asserting the
  lifecycle is over.

Both of those are reads, and a read is point-in-time: a daemon can boot the
moment after them, bootstrap the story, and pass its readiness check while the
delivery is still pushing — and the runner's own gap between *ready* and
*claimed* spans several awaits. So the delivery also takes a **dispatch
hold**: it claims every configured route on the story before it pushes and
holds it until the `pr` gate exists. That is the same primitive the
route-runner uses to win a dispatch race, so the server decides which of the
two got there first — a route that claimed first stops the delivery dead
(exit 1, nothing written), and a route that arrives later finds the aspect
held, logs the lost race and defers. The hold is taken under an identity of
`deliver`'s own (`<agent>-deliver`), because a claim only excludes another
*agent*: under the daemon's own id it would exclude nothing. Releasing it is
itself the `task.released` that re-triggers the runner's readiness check —
which now defers the story behind the gate this delivery raised.

If a dispatch claims the story *while* the delivery runs anyway (a route
configured on another host, say), the `pr` gate is still raised — that half is
always safe — but no human gate is completed, and the `[Friction]` says to
re-run once the dispatch has finished.

**The repo, not the worktree.** `state.json` names the branch, and the branch
ref lives in the project's own checkout whether or not the run's worktree
survived a salvage. So the run dir is used only to *find* the branch, the
story, and the run's provenance (rounds from `state.json`, cost + test-gate
verdict from its `result.json` `escalation.brief`, the final coder handoff
from `handoff/`); every write is `git -C <repo>` + `gh` + Lithos. The checkout
is the story's project: `metadata.project` → `[projects.<slug>].repo`.

**What the PR body carries, and how.** It names **why** the run stopped — the
status *and* its reason — but the reason is **redacted and bounded** first: it
is not a curated label (for the reason-bearing statuses it is the first line
of the agent CLI's error text, the subprocess stderr, or the tail of unparsed
agent stdout), and a PR body is world-readable on a public repo. So urls,
absolute / home paths, IPv4 literals and bare `host:port` pairs, and
credential-shaped runs become `(url redacted)` / `(host redacted)` /
`(path redacted)` / `(redacted)` — written as prose, because `url`, `host`,
`path` and `redacted` are valid HTML tag names and an angle-bracketed
placeholder is dropped by GitHub's sanitizer, leaving a redaction no reader
can see. The text is capped, and the story keeps the untouched original
(its `[NeedsHuman]` finding, the gate brief, and `--dry-run`, which are all
host-side or operator-only).

The coder's final handoff goes through **the same redaction** and travels as a
**fenced block**, never as inline prose: it is written by the coder agent into
a read-write mount, and a PR description is live markup — GitHub honours
closing keywords (`#12`, `GH-12`, `owner/repo#12`) anywhere in it and notifies
every `@name`. Both are rewritten so they read the same but bind nothing, and
neither defence leans on code spans or on a lone backtick's parity. Bidi
overrides and zero-width formatters are stripped with the C0/C1 control bytes,
so a published line cannot render in an order it was not written in. The same reader refuses to follow a symlink out of the handoff
directory and opens only regular files (`O_NOFOLLOW` + an `fstat` check), so a
planted link cannot choose what a host-privileged process reads and a planted
FIFO cannot hang the command.

Both readers are bounded in **size as well as shape**: the handoff read stops
at 1 MiB, and the redaction pass caps what it ever scans (several of its
patterns cost O(n²) on a long dotted run, and the file feeding them is written
by the coder agent — an unbounded input would hang the command, `--dry-run`
included).

Provenance the run never recorded (a reaped run's rounds or cost) renders as
`unknown`, never as a confident zero — and so does provenance that cannot be
true (a negative round count, a negative / `NaN` / infinite cost).

## Flags

| Flag | Meaning |
|------|---------|
| `RUN` | The stopped run: a run id, or a task id (its newest run) — the keys `develop list` / `attach` / `dump` take. Omit only with `--branch` **and** `--story`. |
| `--branch NAME` | Deliver this branch instead of the one `state.json` names. With `--story`, it needs no run dir at all — the fallback for a host with `retain_failed_workdirs = false` (the PR body then carries no run provenance). |
| `--story TASK_ID` | The story this branch implements (default: the run dir's task id). Read live from Lithos: title, description, `acceptance_criteria`, `project`, `github_issue_url`. |
| `--base REF` | Base branch for the PR (default: the repo's default branch, via `gh repo view`). It constrains **adoption** as well as opening: a candidate PR whose base is not this one is not this delivery's, so it is refused rather than adopted (an adopted PR merges somewhere `deliver` would never have opened onto, with the `pr` gate tracking that merge). |
| `--no-gate` | Open the PR only. No `pr` gate is raised and the needs-human gate is left open, so the PR is **UNMONITORED** — nothing tracks its merge, ingests reviews on it, or re-gates it when the base moves. The finding says so. |
| `--dry-run` | Print the five steps with every fact resolved (remote state, the would-be title, the gates that would be completed and the ones that would be kept) and write nothing: no push, no `gh` call, no Lithos write. Text loom did not author — the stop reason, the story title — is stripped of terminal control bytes before it is echoed: this is the screen the decision is made on. |
| `--json PATH` | Write the structured record. |
| `--config` | Host config path. |

## Output

- **Text**: the PR url, the push outcome, opened-vs-adopted, the `pr` gate, the
  human gates completed, and one `[Friction]` line per degradation.
- **JSON** (`--json`): `run_id`, `story_id`, `branch`, `pushed`, `pushed_sha`,
  `pr_url`, `pr_number`, `adopted`, `pr_gate_id`, `human_gates_completed[]`,
  `human_gates_retained[]` (the gates left open, each with its route, its run
  and why it was kept), `push_uncertain`, `pr_uncertain`, `gate_complete`,
  `complete`, `changed`, `notes[]`. `complete` is what the
  exit code follows: false whenever anything is owed, including a record that
  could not be written (the delivery still stands — `pr_url` says so).

## Exit codes

| Exit | Meaning |
|------|---------|
| `0` | Delivered (or adopted with nothing left to do; or a `--dry-run` plan printed). |
| `1` | **Refused, and this run wrote nothing**: a diverged remote branch, an unknown run, a story a live route dispatch still holds a claim on — including one that claims it between the preflight reads and the delivery's own dispatch hold — or a stopped run whose escalation has not appeared on the story while a loom daemon is running here (a pidfile that exists but cannot be read counts as running: a daemon rewrites it under its lock while booting) (its result may still be being applied — wait for the `[NeedsHuman]` gate, `drain` the daemon, or assert it with the run-dir-less `--branch`/`--story` form), a run that has recorded **no outcome** (no terminal `state.json` — the plugin writes it only at run end, so the run may be mid-round; `--branch` cannot stand in for it, and the run-dir-less `--branch --story` form is the explicit assertion for a reaped run), an **approved** run whose automated delivery has neither completed nor failed (the daemon may be opening its PR right now — watch it with `develop attach`; a recorded delivery failure or an expired delivery budget *is* deliverable), a branch absent from the checkout, a run that already delivered its PR, a story that is not open (without `--no-gate`), a project with no `[projects.<slug>]` mapping, an `origin` that is not a GitHub repository, another `deliver` holding the story's claim, an unreachable Lithos — or a `gh` failure / unadoptable PR **when the branch was already on `origin`**, so nothing of this run's is outside the host. |
| `2` | **Partial — something is committed and something is owed.** The branch was pushed but no PR could be opened or adopted; or an external write may or may not have landed and the read that would settle it failed too (`PUSH UNCERTAIN` / `PR UNCERTAIN` — never reported as "nothing written", and never as an absence this run did not establish); or the PR is open but the gate half did not complete (no `pr` gate, a gate watching another PR, a needs-human gate that would not close, a lost story write, a `[ManualDelivery]` that would not post); or the `--json` record the operator asked for could not be written. Whatever landed is printed and the `[Friction]` says what is owed; re-running finishes it. The classification follows what has been **committed**, not which step raised — once anything is outside the host, this command never claims it wrote nothing. |

## Requirements

A checkout of the story's project whose `origin` is the PR's repository, `gh`
authenticated, and a reachable Lithos. No Docker, no agent CLIs — `deliver`
spends nothing.

## What it is not

- **Not a resume.** It delivers what the run committed; it does not restart the
  coder/reviewer loop. Re-reviewing the delivered PR (under revised acceptance
  criteria, say) is `lithos-loom develop converge <pr> --story <id> --ac-file …`
  — see [`converge.md`](converge.md).
- **Not a rescue for a run with no commits.** A branch with nothing on it
  delivers an empty PR; check `develop dump <run>` first.
- **Never destructive.** It does not force-push, rewrite a branch, cancel a
  gate, or delete a run dir.
