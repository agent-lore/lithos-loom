# T1-S1: Lithos client graph reads + data model

Add client methods task_ready / task_blocked / task_get / task_children /
task_edge_list (+ protocol + fake extensions). TaskRecord gains task_type and
resolved_at; new BlockerRecord (all four kinds) and EdgeRecord (all four edge
types) normalizers. Foundational for the whole milestone. Acceptance:
normalizers round-trip all four blocker kinds and all four edge types. PRD
slice 1.
