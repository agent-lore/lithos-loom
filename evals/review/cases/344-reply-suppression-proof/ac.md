S2 detection (external-review ingestion), backfill-guard slice: on each sweep
of a still-open `pr` gate, read the PR's reviews and inline review comments
and post anything new as a one-shot `[ExternalReview]` finding on the blocked
story, de-duped by high-water marks on the gate.

Backfill requirement: until the inline Copilot round is retired, delivery
remediates root comments, pushes the fix, and replies — all before the gate
exists. A markerless gate's first sweep must not re-report that
already-handled history as fresh findings.

Suppression must be sound: a root comment may be skipped only when it was
genuinely handled — the operator must never lose live, unresolved review
activity to the de-dup. Findings are the operator's only view of external
reviews on a delivered PR.
