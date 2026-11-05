.PHONY: check test-integration

check:
	uv run --frozen ruff format --check src tests
	uv run --frozen ruff check src tests
	uv run --frozen pyright
	uv run --frozen pytest -q -m 'not integration'

test-integration:
	docker build --network none -t bbx-archv2-sandbox-seam:20261106 .
	docker build --network none -f tests/Dockerfile -t bbx-archv2-sandbox-seam-test:20261106 .
	uv run --frozen pytest -q -m integration
