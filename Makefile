.PHONY: install fmt lint typecheck test check diagrams gate-exec metrics-history metrics-diff

install:
	uv sync

fmt:
	uv run ruff format src/ tests/ scripts/

lint:
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test

# Regenerate the architecture docs, metrics, and per-component pages under
# docs/generated/. Run after changing code/models and commit the result; CI
# fails if the committed artifacts drift from the code (.github/workflows/ci.yml).
diagrams:
	uv run pytest tests/guardrail/ -q

# Execute every Python check-catalog command for real, in the sandbox image, on a
# clean and a defective fixture (R5b). Host only — needs docker, the image
# (LOOM_GATE_EXEC_IMAGE, default ralph-sandbox:latest) and the network. NOT part of
# `check` or CI: run it by hand after any change to the gate definitions (AGENTS.md).
gate-exec:
	LOOM_GATE_EXEC=1 uv run pytest tests/test_check_catalog_exec.py -v

# Print the architecture-metrics trend mined from the git history of
# docs/generated/metrics.json. FORMAT=csv|mermaid (default csv).
metrics-history:
	uv run python scripts/metrics_history.py --format $(or $(FORMAT),csv)

# Show the metrics delta between BASE (default origin/main) and the working tree.
# `set -e` + `trap` so the recipe exits with metrics_diff.py's status, not rm's.
metrics-diff:
	@set -e; tmp=$$(mktemp); trap 'rm -f $$tmp' EXIT; \
	git show $(or $(BASE),origin/main):docs/generated/metrics.json > $$tmp 2>/dev/null || echo '{}' > $$tmp; \
	uv run python scripts/metrics_diff.py $$tmp docs/generated/metrics.json
