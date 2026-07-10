# ADR 0007 — Subscription handlers are hand-wired, not discovered

- **Status:** Accepted
- **Date:** 2026-07-10
- **Deciders:** Dave Snowdon

> Tracking task: **ARCH-6** (architecture review 2026-07-06). Follow-up: **#237**
> (consolidate the github-watcher retry loop against `SubscriptionRunner`).

## Context

Loom's architecture is `sources → bus → subscribers`. The subscription layer shipped
with a Python **entry-point registry**: a `lithos_loom.subscriptions.handlers`
entry-point group plus `discover_handlers()`, which loaded `{name: entry_point.load()}`
so handlers could — in principle — be registered declaratively, even out-of-tree.

By the 2026-07-06 architecture review the registry was vestigial and the seam that
actually carried production was elsewhere:

- The entry-point group registered exactly **one** handler — `noop` — since the
  daemon shipped. Every real handler is hand-wired: the `obsidian-sync` child builds
  a `{action: handler}` map by name and feeds it to `build_runners()`.
- `discover_handlers()` had a single caller: `main.py`'s `validate-config --dry-run`
  predicate builder. It was **not** used by any runtime child.
- That single use was subtly broken: `build_runners()` validates each spec's `action`
  against the handler map, and the dry-run's map was only `{noop}` — so the dry-run
  could not validate any real subscription's action (only `noop`-action configs).

The **deletion test** verdict: `discover_handlers()` + the entry-point group are a
shallow pass-through (deleting them changes only the dry-run). `build_runners()` — the
fan-out that compiles structural matches + `where` predicates and drives
retry-with-friction — is deep and earns its keep. The halfway state (a registry that
exists but is bypassed) fails the test; either direction passes it.

The decisive constraint is **dependency injection**: an entry point yields a *zero-arg*
callable, but every real handler is a **factory needing runtime dependencies** —
`make_obsidian_projection_handler(cfg, sync_state=…)`, `make_note_push_handler(cfg, …)`,
etc. `noop` is the only handler the registry could ever express because it's the only
dep-free one. Making the registry able to express the handlers we actually have would
mean building a dependency-injection layer over entry points — a lot of new machinery
to make an honest version of a seam the deletion test already flags as not earning its
keep. Separately, the github-watcher dispatches **inline** (bypassing the bus) because
`EventBus.publish` is fire-and-forget and drops on a full queue; a bus-routed handler
could silently lose a GH↔Lithos reconcile. Routing everything through a discovery seam
would also have to re-solve that.

## Decision

**Delete the entry-point handler registry. Bless hand-wiring as the seam.**

- Removed `discover_handlers()`, the `_HANDLER_ENTRY_POINT_GROUP` constant, and the
  `[project.entry-points."lithos_loom.subscriptions.handlers"]` group (the `noop`
  registration).
- Kept the deep piece unchanged: `build_runners()` takes a caller-built
  `{action: handler}` map. Each hosting child constructs its handlers by name (with
  their real dependencies) and passes the map. **That map is the registration seam.**
- Added `SUBSCRIPTION_ACTIONS` — a plain in-tree catalog of known action names (the one
  declarative artefact that replaces the registry). `validate-config --dry-run`
  validates config actions against it, so a typo'd action surfaces as a dead
  subscription instead of a silent no-op; the `obsidian-sync` child derives its hosted
  set from it. This is a name vocabulary, **not** a plugin registry — handlers still
  carry dependencies and cannot be resolved from a name alone.
- `_noop.py` stays: it's the stateless smoke/test handler, still used by tests and by
  the dry-run's never-invoked placeholder map.

## Consequences

- The seam that exists is the seam that's used; there is one way to register a handler.
- Adding a subscriber is: add its action to `SUBSCRIPTION_ACTIONS`, and wire its factory
  in the hosting child. If the child names an action it doesn't wire, `build_runners()`
  raises at startup — a loud failure, not a silent drop.
- **Given up:** the (unused) ability to load third-party handler plugins via entry
  points. If out-of-tree handler plugins ever become a real requirement, this is
  rebuildable — but it would need a dependency-injection protocol richer than plain
  entry points, so it should be designed then, not carried speculatively now.
- **A future architecture review should not re-suggest a handler registry / entry-point
  discovery for subscriptions** on the grounds that the handlers are hand-wired. That is
  the deliberate outcome recorded here.

## Alternatives considered

- **Route all children through `discover_handlers()` / `build_runners()` (re-inflate the
  registry).** Rejected: requires a DI layer over entry points to express dep-carrying
  handlers, plus re-solving the github-watcher's queue-full / at-least-once constraint
  (its inline dispatch exists precisely so a full bus queue can't drop a reconcile). Re-
  inflates a shallow pass-through into a real seam on speculative out-of-tree demand.
