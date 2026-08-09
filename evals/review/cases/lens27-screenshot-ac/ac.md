# e2e harness: fake-Lithos app mode + playwright smoke suite

Give lens a browser-level evaluation surface so loom's coder/reviewers/gate can
run and see the UI, not just read diffs.

1. **Fake-Lithos app mode.** A harness that serves the REAL app against the
   in-process protocol fakes (the fake client seam from T1-S1), seeded with a
   small fixture dataset (notes, tasks, edges, a contradicts pair). No live
   Lithos required.

2. **Playwright smoke suite** (`make e2e`): every top-level route renders from
   the seeded fixtures; at least one key click-through interaction is
   exercised; screenshots at 320/768/1024/1440 written to an artifacts dir.

3. **Dependency + environment contract.** Declare playwright as a
   dev-dependency of the e2e suite, compatible with the browser + OS deps
   baked into the ralph-sandbox:python-ui gate image (playwright 1.62.0,
   browser cache at `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`). `make e2e`
   installs the browser when absent — a no-op against the image's baked cache.

4. **Make target, kept OUT of `make check`.** `make e2e` runs the suite
   headless so the fast gate stays fast. Gate integration happens via the
   AGGREGATE parity command (the operator appends `&& make e2e` to
   `develop_parity_command` after this story merges).

**Downstream consumer:** loom's visual-review artifact flow (screenshots into
the review handoff so the panel evaluates rendered pages). This task's
screenshot artifacts dir is the contract point.
