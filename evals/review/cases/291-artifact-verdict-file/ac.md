# #283 slice 2 — the reviewer panel evaluates rendered pages

Both reviewer prompts (round-one and re-review) must enumerate the gate-collected
rendered-page artifacts (screenshots) with in-container paths so personas open and
evaluate the images alongside the diff.

Because candidate-stage checks run AFTER each round's panel, approval must be
HELD whenever the sealing round's candidate run collected artifacts no reviewer
has seen: one panel-only artifact-review pass runs first — each reviewer is
instructed to open the images and write a visual verdict — and **that pass's
verdict controls the outcome**: an LGTM seals approval, visual findings feed the
ordinary fix loop. A green candidate must never deliver screenshots no reviewer
looked at, and the pass's verdict must never be ignored in favour of an earlier
assessment made before the screenshots existed.
