"""``lithos-loom eval triage`` — the S5a triage step measured on known verdicts.

PRD pr-reconciliation S8, triage shape: a known-false external finding must be
REJECTED with cited evidence, a known-true one must PROCEED, an ambiguous one
must PROCEED (default-to-act). Cases are batches, as production triage is —
over-suppression shows in mixed batches, not in isolation.
"""
