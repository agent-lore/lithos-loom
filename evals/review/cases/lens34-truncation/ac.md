# T1-S2: Frontier join + Ready/In-progress/Blocked sections

Add frontier.py (pure functions) joining the master-open list + ready + blocked
frontiers; render the three workable sections with blocker chips;
claimed-but-blocked renders in In progress with a blocked decoration. Delete
claimed_state, _apply_claim_filter, _enrich_open_tasks. Never re-implement the
readiness predicate. Foundational for sections 3–5/7/9–12. Acceptance: an
open-predecessor task renders in Blocked with the predecessor's title chip;
completing the predecessor (fake) moves it to Ready. PRD slice 2.
