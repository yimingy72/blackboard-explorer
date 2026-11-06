.PHONY: check test-integration test-assignment-cleanup

check:
	uv run --frozen ruff format --check src tests
	uv run --frozen ruff check src tests
	uv run --frozen pyright
	uv run --frozen pytest -q -m 'not integration'

test-integration:
	docker build --network none -t bbx-archv2-sandbox-seam:20261106 .
	docker build --network none -f tests/Dockerfile -t bbx-archv2-sandbox-seam-test:20261106 .
	uv run --frozen pytest -q -m integration

test-assignment-cleanup:
	docker build --network none -t bbx-archv2-assignment-stop:20261106 .
	docker build --network none -f tests/Dockerfile --build-arg BASE_IMAGE=bbx-archv2-assignment-stop:20261106 -t bbx-archv2-assignment-stop-test:20261106 .
	BBX_EXECUTION_TEST_IMAGE=bbx-archv2-assignment-stop-test:20261106 uv run --frozen pytest -q -m integration tests/test_assignment_cleanup_integration.py tests/test_execution_integration.py::test_identity_files_foreground_and_output
