S2 detection (external-review ingestion): a delivered PR sits behind a `pr`
gate awaiting a human merge, and reviews left on it in the meantime —
Copilot, other bots, humans — are invisible to loom (the reconcile sweep
polls only merge state). On each sweep of a still-open `pr` gate, read the
PR's reviews and inline review comments and surface anything new as a
one-shot `[ExternalReview]` finding on the blocked story (author, state,
path:line, excerpt, thread url), with a de-dup marker on the gate so the
same material never posts twice.

Per-state policy: CHANGES_REQUESTED always posts; COMMENTED only with
content; APPROVED/DISMISSED advance the marker silently. Thread replies ride
on their root comment.

The finding is an operator action item: it must report review activity that
actually needs attention on the delivered PR.
