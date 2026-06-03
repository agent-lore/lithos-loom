# ADR 0001 — Storing per-project GitHub-watch config on the project-context doc

- **Status:** Accepted (interim) — superseding move tracked by [agent-lore/lithos#305](https://github.com/agent-lore/lithos/issues/305)
- **Date:** 2026-06-03
- **Deciders:** Dave Snowdon

## Context

The GitHub issue watcher needs per-project configuration attached to each
watched project: which GitHub repo the project maps to, whether watching is
currently enabled, and optional exclude filters (labels / authors). This
config belongs to the project, so the natural home is the canonical
project-context document Loom already maintains in Lithos.

Lithos exposes two persistence surfaces for a document via the MCP write
API (`lithos_write`):

- a `tags: list[str]` field (free-form, operator-settable), and
- a fixed set of typed fields (`confidence`, `source_url`, `note_type`,
  `status`, `summaries`, …).

It does **not** expose a writable key-value `metadata`/`extra` field for
documents. Tasks have a first-class `metadata: dict[str, Any]`
(`lithos_task_create` / `lithos_task_update`); documents do not. The
`KnowledgeMetadata.extra: dict` field exists in the Lithos data model and
round-trips through YAML frontmatter, but it is not reachable from the
write API.

The original GitHub-watcher PRD (decision D45) specified the name/value
form: `note.metadata.github_repo = "owner/name"` +
`github_issues_enabled = true`.

## Decision

Store the watcher's per-project config as **structured tags** on the
canonical project-context doc, because tags are the only free-form,
operator-writable, server-queryable field available on the document write
surface today:

| Concern | Tag encoding |
|---|---|
| Repo mapping | `github-repo:<owner>/<name>` (exactly one; app-enforced) |
| Watching on/off | `github-watch` present / absent |
| Exclude label | `github-exclude-label:<name>` (zero or more) |
| Exclude author | `github-exclude-author:<login>` (zero or more) |

Mutations go through a shared read-mutate-write CAS loop
(`src/lithos_loom/cli/_github_metadata.py`) using `expected_version`
optimistic locking, exposed via `project set-github-repo` /
`enable-github` / `disable-github`.

A second, material benefit beyond "only free-form field": tags give
**free server-side filtering**. The watcher discovers its entire work
list with one call —
`note_list(path_prefix="projects/", tags=["github-watch"])`. No
metadata-filtered list exists for documents (`lithos_list` filters by
`path_prefix, tags, author, since, title_contains, content_query`).

## Consequences

### Negative (accepted as interim cost)

- **Semantic pollution** — config strings appear in `lithos_tags` global
  counts and any tag-based browsing; categorization and configuration
  share one list field.
- **Encoding limits** — values must be tag-safe. GitHub labels containing
  spaces cannot be expressed and therefore cannot be filtered (documented
  limitation in `_github_metadata.py`).
- **No typing** — booleans are presence/absence; all values are strings.
- **Hand-rolled invariants** — "exactly one repo per doc" is enforced in
  application code (strip `github-repo:*`, re-append) rather than by the
  storage model.

### Positive

- Ships today with no upstream Lithos dependency.
- Server-side discovery query is a single filtered list call.
- CAS / versioning reused unchanged.

## Better approach and what it requires

The name/value design the PRD originally wanted requires a **Lithos
change**, because documents have no writable metadata field. The storage
substrate already exists (`KnowledgeMetadata.extra` round-trips through
frontmatter), so the gap is purely the API surface. Filed as
[agent-lore/lithos#305](https://github.com/agent-lore/lithos/issues/305):

- **Part A (enabling):** add a `metadata` param to `lithos_write` →
  `extra`, with per-key merge semantics matching tasks; surface metadata
  on `lithos_read` / `lithos_list`. Reuse `expected_version`.
- **Part B (filtering):** add a metadata-match filter to `lithos_list`.
  Worth doing symmetrically for `lithos_task_list` — tasks already have
  writable `metadata` but **also** cannot be queried by it today, so the
  metadata-query gap exists on both surfaces and a single consistent
  design covers both.

Iterating all `projects/` docs and filtering client-side is an acceptable
interim if Part B lags Part A — the watch list is small and refreshed on
`note.*` events, not on a hot path.

### Follow-up on the Loom side once #305 Part A lands

1. Replace tag encoding in `_github_metadata.py` with metadata keys:
   `github_repo`, `github_watch_enabled`, `github_exclude_labels`,
   `github_exclude_authors` (keep the existing CAS wrapper).
2. Carry `metadata` through `note_write` / `note_read` in
   `lithos_client.py` (the `Note` model needs the field).
3. Repoint the watcher's discovery query to the metadata filter (Part B)
   or a client-side filter in the interim.
4. One-shot migration off the existing tags on watched project-context
   docs.
