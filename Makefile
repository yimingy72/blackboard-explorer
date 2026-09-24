VENV = .venv/bin
PYTHON_DIRS = packages/contracts packages/objects services/blackboard services/agent-runtime services/envd

.PHONY: up down clean-volumes fmt lint typecheck test test-integration check check-all schemas image-exec-env image-egress-proxy

# Package mirrors for image builds; set to empty to use upstream sources.
APT_MIRROR ?= http://mirrors.aliyun.com/ubuntu
PIP_INDEX_URL ?= https://mirrors.aliyun.com/pypi/simple
APK_MIRROR ?= mirrors.aliyun.com
# Optional Docker build flags, for example a build-only proxy and host mapping.
DOCKER_BUILD_ARGS ?=
DEBIAN_MIRROR ?= http://mirrors.aliyun.com/debian

image-exec-env:
	docker build $(DOCKER_BUILD_ARGS) -f services/envd/Dockerfile --build-arg APT_MIRROR=$(APT_MIRROR) \
		--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL) -t bbx-exec-env:latest .

image-egress-proxy:
	docker build $(DOCKER_BUILD_ARGS) -f services/egress-proxy/Dockerfile --build-arg APK_MIRROR=$(APK_MIRROR) \
		-t bbx-egress-proxy:latest .

.PHONY: image-blackboard openapi

image-blackboard:
	docker build $(DOCKER_BUILD_ARGS) -f services/blackboard/Dockerfile \
		--build-arg DEBIAN_MIRROR=$(DEBIAN_MIRROR) --build-arg PIP_INDEX_URL=$(PIP_INDEX_URL) \
		-t bbx-blackboard:latest .

openapi:
	$(VENV)/python -m bbx_blackboard.openapi

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

.PHONY: eval-targets eval-verify

eval-targets:
	sh eval/targets/build.sh

eval-verify:
	$(VENV)/python eval/answers/order-service/verify.py
	uv run --no-project --python 3.12 --with-requirements eval/targets/mini-shop/src-main/requirements.txt python eval/answers/mini-shop/verify/run.py
