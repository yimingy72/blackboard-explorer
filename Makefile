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

.PHONY: image-blackboard image-agent-runtime openapi

image-agent-runtime:
	docker build $(DOCKER_BUILD_ARGS) -f services/agent-runtime/Dockerfile \
		--build-arg PIP_INDEX_URL=$(PIP_INDEX_URL) -t bbx-agent-runtime:latest .

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
	$(VENV)/pytest -m "not integration and not live"

test-integration: image-exec-env image-egress-proxy image-agent-runtime
	$(VENV)/pytest -m "integration and not live"

check: lint typecheck test

check-all: check test-integration

schemas:
	$(VENV)/python -m bbx_contracts.schemas

.PHONY: eval-targets eval-verify

E2E_ENV_FILE ?=
E2E_ARGS ?=
.PHONY: e2e

e2e: eval-targets
	uv run $(if $(E2E_ENV_FILE),--env-file "$(E2E_ENV_FILE)") --no-sync python -m bbx_runtime.e2e $(E2E_ARGS)

eval-targets:
	sh eval/targets/build.sh

eval-verify:
	$(VENV)/python eval/answers/order-service/verify.py
	uv run --no-project --python 3.12 --with-requirements eval/targets/mini-shop/src-main/requirements.txt python eval/answers/mini-shop/verify/run.py

.PHONY: web-install web-dev web-build web-check web-types

PLAYWRIGHT_DOWNLOAD_HOST ?= https://registry.npmmirror.com/-/binary/playwright
PLAYWRIGHT_BROWSERS_PATH ?= $(CURDIR)/.data/playwright
.PHONY: web-e2e web-e2e-install

web-e2e-install:
	PLAYWRIGHT_DOWNLOAD_HOST=$(PLAYWRIGHT_DOWNLOAD_HOST) PLAYWRIGHT_BROWSERS_PATH=$(PLAYWRIGHT_BROWSERS_PATH) pnpm --dir web exec playwright install --only-shell chromium

web-e2e: web-e2e-install
	PLAYWRIGHT_BROWSERS_PATH=$(PLAYWRIGHT_BROWSERS_PATH) pnpm --dir web e2e

web-install:
	pnpm --dir web install --frozen-lockfile

web-dev:
	pnpm --dir web dev

web-build:
	pnpm --dir web build

web-check:
	pnpm --dir web check

web-types:
	pnpm --dir web types

.PHONY: test-live

test-live:
	@test -n "$$DEEPSEEK_API_KEY" && test "$$DEEPSEEK_API_KEY" != "replace-me" || { echo "DEEPSEEK_API_KEY is required for explicit live tests"; exit 1; }
	$(VENV)/pytest -m live
