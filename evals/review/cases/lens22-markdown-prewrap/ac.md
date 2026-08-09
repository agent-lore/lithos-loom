# K1-S1: Server-side markdown rendering + knowledge.py module

Add the markdown-it-py dependency; create the knowledge.py Foundation module
(mirroring tasks.py) registered in BOTH the import-linter contract
(pyproject.toml) and docs/architecture.toml (guardrail tests enforce both);
render /note/{id} body as HTML. Foundational for the note page.

Acceptance: headings/tables render; raw <script> is escaped; a javascript: href
is neutralized. PRD slice 1 (docs/prd/k1-knowledge-note-view.md).
