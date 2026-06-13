# story-implement Claude prompt

This file is the prompt template fed to Claude when `story-implement` runs
inside a per-task worktree.

## Substitution tokens

- `<<PRD_BODY>>` — the parent PRD's Markdown body (read from Lithos)
- `<<STORY_BRIEF>>` — the story brief (`note_type: task_record` doc body)
- `<<PROJECT_AGENTS_MD>>` — the project repo's `AGENTS.md` / `CLAUDE.md`
- `<<INTEGRATION_BRANCH>>` — the per-PRD integration branch name (`loom/<prd-slug>`)

## Prompt body

(Superseded by `story-develop`; this stub is slated for removal — see US-2 in
`docs/prd/orchestration.md`.)
