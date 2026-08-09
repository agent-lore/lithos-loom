# e2e harness: fake-Lithos app mode + playwright smoke suite

Give lens a browser-level evaluation surface so loom's coder/reviewers/gate can
run and see the UI, not just read diffs.

1. **Fake-Lithos app mode.** A harness that serves the REAL app against the
   in-process protocol fakes (the fake client seam from T1-S1) — e.g.
   `LITHOS_LENS_FAKE=1 uvicorn ...` or an app factory taking the fake client —
   seeded with a small fixture dataset (notes, tasks, edges, a contradicts
   pair). Hermetic: no live Lithos, no network beyond first-run browser install.

2. **Playwright smoke suite** (`make e2e`): every top-level route renders with
   no console errors; key interactions (HTMX swap on an expandable card, a
   graph-view node click) work; screenshots at 320/768/1024/1440 written to an
   artifacts dir; basic a11y pass (axe or playwright's accessibility snapshot)
   as informational.

3. **Dependency + environment contract.** Add `playwright` (pin the 1.62.x
   line — it must stay compatible with the browser + OS deps baked into the
   ralph-sandbox:python-ui gate image, which pins playwright 1.62.0) as a lens
   dev-dependency; the image does NOT provide the Python package, only the
   browser cache (`PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`), OS deps, and
   CLI. `make e2e` starts with `uv run playwright install chromium` — a no-op
   when the project's version matches the baked revision.

4. **Make target, kept OUT of `make check`.** `make e2e` runs the suite
   headless so the fast gate stays fast. Gate integration happens via the
   AGGREGATE parity command (the operator appends `&& make e2e` to
   `develop_parity_command` after this story merges).

**Downstream consumer:** loom's visual-review artifact flow (screenshots into
the review handoff so the panel evaluates rendered pages). This task's
screenshot artifacts dir is the contract point.
