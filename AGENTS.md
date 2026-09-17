# Agent Prompt Rules

## Environment & Path Resolution

- Always execute Python scripts inside `ai-core` with `uv run python` or `/app/.venv/bin/python`.
- Never call the system `python` directly on the host machine or inside the container without virtual-environment context.
- Reference script paths relative to the container root (`/app`).
- For scripts inside the `ai-core` subfolder, use:

  ```bash
  docker compose exec ai-core uv run python ai-core/scripts/<script_name>.py
  ```

- For scripts directly in the repository root, use:

  ```bash
  docker compose exec ai-core uv run python scripts/<script_name>.py
  ```

## Domain Refactoring & Imports

- The `cohorts` domain has been refactored to `channels`.
- Never import `cohorts` from `app.services.domain`. Use:

  ```python
  from app.services.domain import channels as channel_repo
  ```

- Treat all legacy `cohort_id` references as `channel_id` across models, services, and test probes.

## Verification Commands

Use these commands for the standard verification probes:

```powershell
# Ingestion Pipeline
docker compose exec ai-core uv run python ai-core/scripts/verify_ingestion.py

# Escalation Workflow
docker compose exec ai-core uv run python scripts/verify_escalation.py

# Scheduling & Reminders
docker compose exec ai-core uv run python scripts/verify_scheduling.py
```

- **Container Execution Command Format**: Always pass `-e PYTHONPATH=.` when running scripts inside `ai-core`:
  `docker compose exec -e PYTHONPATH=. ai-core uv run python scripts/<script_name>.py`

## Testing & Verification Enforcement

The primary directive is to verify and regression-test every engineering domain before declaring a task complete.

### Pre-Run Diagnostics

- Before running a verification probe, perform a static check and compilation check for syntax errors, stale imports, and mismatched domain methods.
- Never execute Python scripts directly with host or system Python.
- For probe scripts in the root `scripts` folder that are outside the container context, pipe standard input with TTY disabled:

  ```powershell
  Get-Content scripts\_<probe_name>.py | docker compose exec -T -i -e PYTHONPATH=. ai-core uv run python -
  ```

### Architecture & Domain Integrity

- Never allow or generate `from app.services.domain import cohorts`.
- Always use `from app.services.domain import channels as channel_repo`.
- Align legacy database references, variables, and probe fixtures from `cohort_id` and `cohorts` to `channel_id` and `channels`.

### Mandatory Verification Checklist

#### Ingestion Pipeline

Run:

```powershell
docker compose exec -e PYTHONPATH=. ai-core uv run python ai-core/scripts/verify_ingestion.py
```

Pass criteria: pgvector initialization, chunking, embedding generation, role-based isolation, and document pruning complete successfully; embedding endpoints return HTTP 200.

#### Escalation Pipeline

Run:

```powershell
Get-Content scripts\_escalation_probe.py | docker compose exec -T -i -e PYTHONPATH=. ai-core uv run python -
```

Pass criteria: clean execution with no import errors or missing domain methods, and SQL cleanup targets the current channels schema.

#### Scheduling & Reminders

Run:

```powershell
Get-Content scripts\_scheduling_probe.py | docker compose exec -T -i -e PYTHONPATH=. ai-core uv run python -
```

Pass criteria: event loops complete cleanly without `NameError`, missing channel references, or unhandled async exceptions.

#### Mattermost Integration

Run the REST API integration test inside the running `ai-core` container:

```powershell
Get-Content scripts\test_mattermost.py | docker compose exec -T -i -e PYTHONPATH=. ai-core uv run python -
```

The environment must define `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`, and `MATTERMOST_TEST_CHANNEL_ID`. `MATTERMOST_WEBHOOK_URL` is optional; when configured, the test also sends a webhook payload.

Pass criteria:

- `GET /api/v4/users/me` returns HTTP 200.
- `GET /api/v4/channels/{MATTERMOST_TEST_CHANNEL_ID}` returns HTTP 200 for the requested channel.
- `POST /api/v4/posts` returns HTTP 201 and a non-empty post ID.
- An optional webhook returns HTTP 200 or 201.
- The script exits with status 0 and prints `Mattermost integration test passed`.

## Mattermost Service & Ingestion Protocol

### Architecture Rules

1. Always run incoming Mattermost posts through `claim_mattermost_event(post_id)` before dispatching them.
2. Discard events where `user_id == bot_user_id` or `props.from_bot is True`.
3. When `root_id` is present, fetch thread history from `GET /api/v4/posts/{post_id}/thread` and pass the structured `thread_history` into the conversation graph.
4. Do not append raw `[Source: doc_name, §Section, p.X]` citation strings to user-facing output.
5. For admin document ingestion, require non-empty `post.file_ids` and an ingestion keyword such as `study`, `ingest`, `add document`, `new file`, `pdf`, or `doc`.
6. Validate the sender through `GET /api/v4/users/{user_id}`. Require `system_admin`, `channel_admin`, or an approved admin allowlist entry.
7. Download attachments with `GET /api/v4/files/{file_id}` and invoke `run_ingestion_pipeline()` for each supported PDF or DOCX file.

### Verification Reporting

When reporting verification status, include:

1. A pass/fail table for ingestion, escalation, and scheduling.
2. Exact traceback details with file locations and line references for failures.
3. A specific action plan naming the required source or schema fixes.
