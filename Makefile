VENV = .venv/bin
PYTHON_DIRS = packages/contracts services/blackboard services/agent-runtime services/envd

.PHONY: up down clean-volumes fmt lint typecheck test check schemas

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
	$(VENV)/pytest

check: lint typecheck test

schemas:
	$(VENV)/python -m bbx_contracts.schemas
