# #283 slice 1 — gate artifact collection

The per-check tree export is deleted right after each gate check runs, so
anything a check renders there (e.g. the e2e screenshots a repo-parity
`make e2e` writes) is destroyed before any reviewer could see it.

Add a resolved setting `develop_artifacts_path` (project-context metadata,
per-task override): a repo-relative directory that the check runner copies out
of each check's isolated tree export into the run's handoff area at
`artifacts/round_NN/<check>/` **before** the export is deleted, unconditionally
on the check's verdict, best-effort (a collection failure is logged, never
fatal, and never blocks the tree cleanup).

The tree export's contents are repo/check-controlled (untrusted), and agent
containers are deliberately hardened (cap-drop ALL, no-new-privileges,
read-only reviewer worktrees) — collection must not weaken the existing
container-isolation model. The path is validated repo-relative (absolute paths
and `..` rejected) so it cannot point outside the export.
