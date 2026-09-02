# SprintFlow — common operations.
.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: prepare
prepare: ## Create bind-mount dirs with the uids the containers need
	./scripts/prepare_volumes.sh

.PHONY: up
up: prepare ## Build and start the whole stack
	$(COMPOSE) up -d --build

.PHONY: bootstrap
bootstrap: ## Create the admin, team, bot, token and outgoing webhook
	./scripts/bootstrap_mattermost.sh

.PHONY: smoke
smoke: ## End-to-end test: post a message and wait for the bot's reply
	./scripts/smoke_test.sh

.PHONY: verify
verify: ## Run every verification suite (takes several minutes)
	@set -a; . ./.env; set +a; \
	./scripts/smoke_test.sh && \
	python3 scripts/verify_routing.py && \
	python3 scripts/verify_threading.py && \
	python3 scripts/verify_isolation.py && \
	python3 scripts/verify_onboarding.py && \
	python3 scripts/verify_admin_agent.py && \
	python3 scripts/verify_memory.py

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
restart-ai: ## Recreate ai-core (picks up .env changes)
	$(COMPOSE) up -d --force-recreate --no-deps ai-core

.PHONY: psql
psql: ## psql shell on the ai-core database (vector store + checkpointer)
	$(COMPOSE) exec postgres-vector psql -U $${POSTGRES_USER:-sprintflow} -d $${POSTGRES_DB:-sprintflow}

.PHONY: psql-mm
psql-mm: ## psql shell on the Mattermost database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-sprintflow} -d $${MATTERMOST_DB:-mattermost}

.PHONY: branding
branding: ## Rasterise branding/logo.svg into the PNGs Mattermost accepts
	./scripts/prepare_branding.sh

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: verify format check

verify:
	docker exec -it -e PYTHONPATH=. sprintflow-ai-core uv run pytest tests/test_authorisation_sprint1.py -v

format:
	docker exec -it sprintflow-ai-core uv run ruff format .
	docker exec -it sprintflow-ai-core uv run ruff check --fix .