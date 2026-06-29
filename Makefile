.PHONY: install fmt lint typecheck test check diagrams

install:
	uv sync

fmt:
	uv run ruff format src/ tests/

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

typecheck:
	uv run pyright

test:
	uv run pytest

check: lint typecheck test

# Regenerate the architecture & domain diagrams under docs/generated/.
# Run after changing code/models and commit the result; CI fails if the
# committed diagrams drift from the code (see .github/workflows/ci.yml).
diagrams:
	uv run pytest tests/guardrail/ -q
