.PHONY: lint test up down logs

lint:
	python3 -m flake8 --max-line-length=100 *.py

# End-to-end check against the compose stack; brings it up first.
test: up
	./scripts/test_e2e.sh

up:
	docker compose up --build -d

down:
	docker compose down -v

logs:
	docker compose logs -f
