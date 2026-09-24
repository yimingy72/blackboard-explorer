VENV = .venv/bin
PYTHON_DIRS = packages/contracts services/blackboard services/agent-runtime services/envd

.PHONY: up down clean-volumes fmt lint typecheck test test-integration check check-all schemas image-exec-env image-egress-proxy

# Package mirrors for image builds; set to empty to use upstream sources.
APT_MIRROR ?= http://mirrors.aliyun.com/ubuntu
PIP_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple
APK_MIRROR ?= mirrors.aliyun.com
# Optional Docker build flags, for example a build-only proxy and host mapping.
DOCKER_BUILD_ARGS ?=

image-exec-env:
	docker build $(DOCKER_BUILD_ARGS) -f services/envd/Dockerfile --build-arg APT_MIRROR=$(APT_MIRROR) \
		--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL) -t bbx-exec-env:latest .

image-egress-proxy:
	docker build $(DOCKER_BUILD_ARGS) -f services/egress-proxy/Dockerfile --build-arg APK_MIRROR=$(APK_MIRROR) \
		-t bbx-egress-proxy:latest .

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

test-integration: image-exec-env image-egress-proxy
	$(VENV)/pytest -m integration

check: lint typecheck test

check-all: check test-integration

schemas:
	$(VENV)/python -m bbx_contracts.schemas
