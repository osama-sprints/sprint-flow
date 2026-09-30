# SprintFlow — common operations.
.DEFAULT_GOAL := help
COMPOSE := docker compose

# Recipes that pass POSTGRES_* to psql or alembic read them from .env, the same
# file docker compose substitutes from, so the shell and compose never disagree.
LOAD_ENV := set -a; . ./.env; set +a;

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: prepare
prepare: ## Create bind-mount dirs with the uids the containers need
	./scripts/prepare_volumes.sh

.PHONY: up
up: prepare ## Build and start the whole stack (ai-core applies migrations on start)
	$(COMPOSE) up -d --build

.PHONY: bootstrap
bootstrap: ## Create the admin, team, bot, token and outgoing webhook
	./scripts/bootstrap_mattermost.sh

.PHONY: smoke
smoke: ## End-to-end test: post a message and wait for the bot's reply
	./scripts/smoke_test.sh

# --- Database ------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply Alembic migrations to head inside the ai-core container
	$(COMPOSE) exec -T ai-core /app/.venv/bin/alembic upgrade head

.PHONY: migrate-downgrade
migrate-downgrade: ## Roll back the most recent Alembic revision (downgrade -1)
	$(COMPOSE) exec -T ai-core /app/.venv/bin/alembic downgrade -1

.PHONY: migrate-history
migrate-history: ## Show the migration history and the current revision
	$(COMPOSE) exec -T ai-core /app/.venv/bin/alembic history --verbose
	$(COMPOSE) exec -T ai-core /app/.venv/bin/alembic current

.PHONY: seed
seed: ## Re-seed roles and ceremony types (idempotent; also runs at every ai-core start)
	$(COMPOSE) exec -T ai-core /app/.venv/bin/python /app/scripts/seed_reference_data.py

.PHONY: db-reset
db-reset: ## DROP the SprintFlow domain tables + alembic_version (checkpointer tables kept), then migrate
	@echo "This drops every SprintFlow domain table in the live database, plus any table left by"
	@echo "the pre-consolidation branch migrations, and re-applies the migration from scratch."
	@echo "LangGraph checkpointer tables (checkpoints, checkpoint_*) are NOT touched."
	@echo "All cohorts, memberships, sprints, ceremonies, tickets and onboarding rows are LOST."
	@read -r -p "Type 'reset' to continue: " answer; [ "$$answer" = "reset" ] || { echo "aborted"; exit 1; }
	@$(LOAD_ENV) $(COMPOSE) exec -T postgres psql -U $${POSTGRES_USER:-sprintflow} -d $${POSTGRES_DB:-sprintflow} \
	  -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS \
	  onboarding_steps, escalation_tickets, daily_standups, ceremony_amendments, ceremonies, sprints, \
	  cohort_memberships, cohorts, ceremony_types, roles, users, \
	  escalation, dailyprogress, ceremony, sprint, cohortmembership, cohort, ceremonytype, role, person, \
	  onboarding_state, \"session\", thread, \"user\", alembic_version CASCADE;"
	$(MAKE) migrate

.PHONY: psql
psql: ## psql shell on the ai-core database (domain tables + checkpointer)
	@$(LOAD_ENV) $(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-sprintflow} -d $${POSTGRES_DB:-sprintflow}

.PHONY: psql-mm
psql-mm: ## psql shell on the Mattermost database
	@$(LOAD_ENV) $(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-sprintflow} -d $${MATTERMOST_DB:-mattermost}

# --- Code quality (inside the container, so the versions match the image) --

.PHONY: test
test: ## Unit tests (pytest) inside the ai-core container
	$(COMPOSE) exec -T ai-core /app/.venv/bin/python -m pytest -q tests/

.PHONY: lint
lint: ## ruff check + ruff format --check inside the ai-core container
	$(COMPOSE) exec -T ai-core /app/.venv/bin/ruff check app/ alembic/ scripts/ tests/
	$(COMPOSE) exec -T ai-core /app/.venv/bin/ruff format --check app/ alembic/ scripts/ tests/

.PHONY: format
format: ## ruff format (rewrites files) inside the ai-core container
	$(COMPOSE) exec -T ai-core /app/.venv/bin/ruff format app/ alembic/ scripts/ tests/

.PHONY: typecheck
typecheck: ## pyright inside the ai-core container (must report 0 errors)
	$(COMPOSE) exec -T ai-core /app/.venv/bin/python -m pyright

# --- Verification --------------------------------------------------------

.PHONY: verify-fast
verify-fast: lint typecheck test ## lint + typecheck + unit tests (no Mattermost, no LLM)

.PHONY: verify
verify: ## Run every verification suite in order (stack up + bootstrapped; takes several minutes)
	@$(LOAD_ENV) \
	./scripts/smoke_test.sh && \
	python3 scripts/verify_schema.py && \
	python3 scripts/verify_authorisation.py && \
	python3 scripts/verify_escalation.py && \
	python3 scripts/verify_orchestration.py && \
	python3 scripts/verify_scheduling.py && \
	python3 scripts/verify_onboarding_journey.py && \
	python3 scripts/verify_routing.py && \
	python3 scripts/verify_threading.py && \
	python3 scripts/verify_isolation.py && \
	python3 scripts/verify_onboarding.py && \
	python3 scripts/verify_admin_agent.py && \
	python3 scripts/verify_memory.py

# --- Stack lifecycle -----------------------------------------------------

.PHONY: down
down: ## Stop the stack (keeps data)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop and DESTROY all data (postgres volume + mattermost files)
	$(COMPOSE) down -v
	docker run --rm -v "$$PWD:/w" alpine:3.20 rm -rf /w/mattermost/volumes /w/ai-core/logs

.PHONY: logs
logs: ## Tail all logs
	$(COMPOSE) logs -f

.PHONY: logs-ai
logs-ai: ## Tail ai-core logs
	$(COMPOSE) logs -f ai-core

.PHONY: restart-ai
restart-ai: ## Recreate ai-core (picks up .env changes; re-applies migrations on start)
	$(COMPOSE) up -d --force-recreate --no-deps ai-core

.PHONY: branding
branding: ## Rasterise branding/logo.svg into the PNGs Mattermost accepts
	./scripts/prepare_branding.sh

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps
