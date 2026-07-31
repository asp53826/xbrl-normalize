.PHONY: test bench run clean

test:
	uv run pytest -q

bench:
	uv run python bench/coverage.py

clean:
	rm -rf .pytest_cache **/__pycache__ ~/.cache/xbrl-normalize
