.DEFAULT_GOAL := help
PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip

.PHONY: install
install: $(BIN)/python ## Install runtime + dev dependencies
	$(BIN)/pip install -r requirements-dev.txt
	$(BIN)/pip install -e .

.PHONY: install-voice
install-voice: install ## Also install the real-time voice stack (pipecat)
	$(BIN)/pip install -e ".[voice]"

.PHONY: env
env: ## Create .env from the template
	@test -f .env || (cp .env.example .env && echo "created .env — fill in your keys")

.PHONY: run
run: ## Run the API with reload
	$(BIN)/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

.PHONY: tunnel
tunnel: ## Expose the local API to Twilio (requires ngrok)
	ngrok http 8000

.PHONY: db-init
db-init: ## Create the tables (SQLite dev bootstrap)
	$(BIN)/python -m app.cli db init

.PHONY: migrate
migrate: ## Apply Alembic migrations
	$(BIN)/alembic upgrade head

.PHONY: migration
migration: ## Autogenerate a migration: make migration m="add x"
	$(BIN)/alembic revision --autogenerate -m "$(m)"

.PHONY: seed
seed: db-init ## Load the sample South Delhi lead list
	$(BIN)/python -m app.cli leads import data/sample_leads.csv

.PHONY: test
test: ## Run the test suite
	$(BIN)/pytest

.PHONY: cov
cov: ## Run the tests with coverage
	$(BIN)/pytest --cov=app --cov=eval --cov-report=term-missing

.PHONY: lint
lint: ## Lint + format check
	$(BIN)/ruff check app eval tests
	$(BIN)/ruff format --check app eval tests

.PHONY: fmt
fmt: ## Auto-format and fix
	$(BIN)/ruff format app eval tests
	$(BIN)/ruff check --fix app eval tests

.PHONY: eval
eval: ## Run the baseline eval suite
	$(BIN)/python -m eval.run_eval --label baseline --quiet

.PHONY: eval-regression
eval-regression: ## Prove the harness catches a prompt regression
	$(BIN)/python -m eval.run_eval --label loose-prompt --variant v1-loose --quiet \
		--compare eval/reports/baseline.json

.PHONY: doctor
doctor: ## Show which integrations are configured
	$(BIN)/python -m app.cli doctor

.PHONY: costs
costs: ## Weekly cost / outcome report
	$(BIN)/python -m app.cli costs report

.PHONY: docker
docker: ## Build the production image
	docker build -t vox-orchestrator:local .

.PHONY: clean
clean: ## Remove caches and local databases
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f ./*.sqlite3
