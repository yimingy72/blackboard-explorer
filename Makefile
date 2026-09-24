VENV = .venv/bin
PYTHON_DIRS = packages/contracts services/blackboard services/agent-runtime services/envd

.PHONY: up down clean-volumes fmt lint typecheck test test-integration check check-all schemas

up:
	docker compose up -d

down:
	docker compose down

clean-volumes:
	docker compose down -v

fmt:
	$(VENV)/ruff format $(PYTHON_DIRS)
	$(VENV)/ruff check --fix $(PYTHON_DIRS)

lint:
	$(VENV)/ruff format --check $(PYTHON_DIRS)
	$(VENV)/ruff check $(PYTHON_DIRS)

typecheck:
	$(VENV)/pyright

test:
	$(VENV)/pytest -m "not integration"

test-integration:
	$(VENV)/pytest -m integration

check: lint typecheck test

check-all: check test-integration

schemas:
	$(VENV)/python -m bbx_contracts.schemas

.PHONY: eval-targets eval-verify

eval-targets:
	sh eval/targets/build.sh

eval-verify:
	$(VENV)/python eval/answers/order-service/verify.py
	uv run --no-project --python 3.12 --with-requirements eval/targets/mini-shop/src-main/requirements.txt python eval/answers/mini-shop/verify/run.py
