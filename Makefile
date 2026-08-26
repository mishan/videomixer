# `make` on its own prints this help rather than running a target, so an
# argument-less invocation can never surprise you.
.DEFAULT_GOAL := help
.PHONY: help dev lint test up down logs

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-6s\033[0m %s\n",$$1,$$2}'

dev:  ## Install development dependencies (flake8)
	pip install -r requirements-dev.txt

lint:  ## Run flake8 (configured in setup.cfg)
	@python3 -m flake8 --version >/dev/null 2>&1 \
	  || { echo "flake8 is not installed -- run 'make dev' first"; exit 1; }
	python3 -m flake8 *.py

test: up  ## Bring the stack up and run the end-to-end check
	./scripts/test_e2e.sh

up:  ## Build and start the compose stack
	docker compose up --build -d

down:  ## Stop the stack and remove volumes
	docker compose down -v

logs:  ## Follow the container logs
	docker compose logs -f
