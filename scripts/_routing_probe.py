"""In-container orchestration probe: routing corpus, latency, tool-group containment, graph shape.

Driven by the host-side ``scripts/verify_orchestration.py`` (which pipes this
file over stdin into the ai-core container), or run directly on a developer
machine from ``ai-core/`` with the app's environment variables set:

    docker compose exec -T ai-core /app/.venv/bin/python - probe < scripts/_routing_probe.py
    cd ai-core && .venv/bin/python ../scripts/_routing_probe.py probe

Sub-commands (first argument):

    probe                                   PASS/FAIL lines, latency distribution, exit 1 on any FAIL
    setup-cohort NAME MM_USER_ID USERNAME EMAIL
                                            create NAME (idempotent) and make that person its scrum master
    check-sprint COHORT SPRINT              print JSON {"exists": bool, ...}
    check-ceremonies COHORT                 print JSON list of upcoming ceremonies for the cohort

The database sub-commands print one JSON document on stdout and nothing else.
"""

import asyncio
import json
import os
import statistics
import sys
import time
from typing import (
    Any,
    List,
    Sequence,
)

sys.path.insert(0, os.getcwd())
sys.path.insert(0, "/app")

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.langgraph.routing_examples import (  # noqa: E402
    REQUESTERS,
    ROUTING_EXAMPLES,
)
from app.core.langgraph.routing_rules import detect_intents  # noqa: E402
from app.core.langgraph.specialists import SPECIALISTS  # noqa: E402
from app.services.agent import agent  # noqa: E402
from app.core.langgraph.tools import TOOL_GROUPS  # noqa: E402
from app.core.metrics import routing_model_calls_total  # noqa: E402
from app.models.enums import (  # noqa: E402
    CeremonyTypeKey,
    RoleKey,
)
from app.services.domain import ceremonies as ceremony_repo  # noqa: E402
from app.services.domain import cohorts as cohort_repo  # noqa: E402
from app.services.domain import identity as identity_repo  # noqa: E402
from app.services.domain import sprints as sprint_repo  # noqa: E402
from app.services.domain.reference_data import seed_reference_data  # noqa: E402

MUTATING_TOOL_NAMES = {"create_cohort", "assign_role", "open_sprint", "schedule_ceremony", "amend_ceremony"}
EXPECTED_NODES = {
    "supervisor",
    "chat",
    "tool_call",
    "learner_support",
    "learner_support_tools",
    "back_office",
    "back_office_tools",
}
LATENCY_MIN_SAMPLES = 1000
LATENCY_P95_BUDGET_MS = 2.0

results: List[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """Print one PASS/FAIL line and remember the outcome."""
    results.append(bool(ok))
    suffix = f"  ({detail})" if detail and not ok else ""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")


def _percentile(sorted_samples: Sequence[float], fraction: float) -> float:
    index = min(len(sorted_samples) - 1, int(len(sorted_samples) * fraction))
    return sorted_samples[index]


def probe_routing() -> None:
    """Assert every labelled sentence and time the router."""
    print("==> routing corpus")
    failures = 0
    for example in ROUTING_EXAMPLES:
        got = detect_intents(example.text, REQUESTERS[example.requester])
        routes = tuple(r.route.value for r in got)
        ok = routes == example.routes and (example.rule is None or got[0].matched_rule == example.rule)
        if not ok:
            failures += 1
            print(
                f"        mismatch: {example.text!r} as {example.requester}: expected {example.routes}/{example.rule}, "
                f"got {routes}/{got[0].matched_rule}"
            )
    hard = sum(1 for ex in ROUTING_EXAMPLES if ex.hard)
    multi = sum(1 for ex in ROUTING_EXAMPLES if len(ex.routes) > 1)
    check(f"routing corpus: {len(ROUTING_EXAMPLES)} labelled sentences all route as expected", failures == 0)
    check(
        f"routing corpus includes deliberately ambiguous cases ({hard}) and multi-intent cases ({multi})",
        hard and multi,
    )
    check(
        "learner saying 'create a cohort' -> learner_support / back_office_cohort_denied_role",
        (detect_intents("create a cohort", REQUESTERS["learner"])[0].matched_rule == "back_office_cohort_denied_role"),
    )

    print("==> routing latency (regex only, no model)")
    samples_ms: List[float] = []
    while len(samples_ms) < LATENCY_MIN_SAMPLES:
        for example in ROUTING_EXAMPLES:
            requester = REQUESTERS[example.requester]
            started = time.perf_counter()
            detect_intents(example.text, requester)
            samples_ms.append((time.perf_counter() - started) * 1000.0)
    samples_ms.sort()
    p50 = statistics.median(samples_ms)
    p95 = _percentile(samples_ms, 0.95)
    p99 = _percentile(samples_ms, 0.99)
    print(
        f"        n={len(samples_ms)}  p50={p50:.4f} ms  p95={p95:.4f} ms  p99={p99:.4f} ms  "
        f"max={samples_ms[-1]:.4f} ms  mean={statistics.fmean(samples_ms):.4f} ms"
    )
    check(
        f"routing p95 latency {p95:.4f} ms < {LATENCY_P95_BUDGET_MS} ms over {len(samples_ms)} calls",
        p95 < LATENCY_P95_BUDGET_MS,
    )
    model_calls = sum(
        s.value for m in routing_model_calls_total.collect() for s in m.samples if s.name.endswith("_total")
    )
    check(
        "router consulted no model while classifying the corpus (sprintflow_routing_model_calls_total == 0)",
        model_calls == 0,
    )


def probe_tool_groups() -> None:
    """Assert the tool groups genuinely differ and contain no forbidden names."""
    print("==> tool groups")
    names = {group: {t.name for t in members} for group, members in TOOL_GROUPS.items()}
    for group in ("general", "learner_support", "back_office"):
        check(f"tool group '{group}' exists", group in names)
    learner = names.get("learner_support", set())
    back_office = names.get("back_office", set())
    general = names.get("general", set())
    print(f"        learner_support = {sorted(learner)}")
    print(f"        back_office     = {sorted(back_office)}")
    print(f"        general         = {sorted(general)}")
    leaked = sorted(n for n in learner if n in MUTATING_TOOL_NAMES or n.startswith("mattermost_"))
    check("learner_support holds no mutating or workspace tool", not leaked, f"leaked: {leaked}")
    leaked = sorted(n for n in back_office if n == "duckduckgo_search" or n.startswith("mattermost_"))
    check("back_office holds no web-search or workspace tool", not leaked, f"leaked: {leaked}")
    check("learner_support and back_office are different capability sets", learner != back_office)
    check("every specialist maps to an existing tool group", all(s.tool_group in names for s in SPECIALISTS.values()))
    check(
        f"ROUTING_MAX_ROUTES_PER_TURN = {settings.ROUTING_MAX_ROUTES_PER_TURN} (>= 2 for multi-intent)",
        (settings.ROUTING_MAX_ROUTES_PER_TURN >= 2),
    )


def probe_graph_shape() -> None:
    """Build the real graph on an in-memory checkpointer and assert its node names."""
    print("==> graph shape")
    graph = agent.build_graph(MemorySaver())
    nodes = set(graph.nodes.keys())
    print(f"        nodes = {sorted(nodes)}")
    check(
        "compiled graph contains supervisor + three specialists + three executors",
        EXPECTED_NODES <= nodes,
        (f"missing: {sorted(EXPECTED_NODES - nodes)}"),
    )
    check(
        "pre-Sprint-1 node names 'chat' and 'tool_call' still exist (old checkpoints resume)",
        ({"chat", "tool_call"} <= nodes),
    )


async def setup_cohort(name: str, mattermost_user_id: str, username: str, email: str) -> dict[str, Any]:
    """Create the cohort if missing and make the given person its scrum master."""
    await seed_reference_data()
    user = await identity_repo.upsert_mattermost_user(
        mattermost_user_id=mattermost_user_id,
        username=username,
        email=email.lower(),
        display_name=username,
        timezone=None,
        is_superadmin=email.lower() in settings.ADMIN_EMAILS,
    )
    cohort = await cohort_repo.get_cohort_by_name(name)
    created = cohort is None
    if cohort is None:
        cohort = await cohort_repo.create_cohort(name, created_by_id=user.id)
    role = await cohort_repo.get_role_by_key(RoleKey.SCRUM_MASTER)
    if role is None:
        raise SystemExit("roles are not seeded")
    assert cohort.id is not None and user.id is not None and role.id is not None
    change = await cohort_repo.upsert_membership(
        user_id=user.id, cohort_id=cohort.id, role_id=role.id, assigned_by_id=user.id
    )
    return {
        "cohort_id": cohort.id,
        "cohort_name": cohort.name,
        "cohort_created": created,
        "user_id": user.id,
        "is_superadmin": user.is_superadmin,
        "membership_created": change.created,
        "role": RoleKey.SCRUM_MASTER.value,
    }


async def check_sprint(cohort_name: str, sprint_name: str) -> dict[str, Any]:
    """Report whether a sprint exists for the cohort."""
    cohort = await cohort_repo.get_cohort_by_name(cohort_name)
    if cohort is None or cohort.id is None:
        return {"exists": False, "reason": "cohort not found"}
    sprint = await sprint_repo.get_sprint_by_name(cohort.id, sprint_name)
    if sprint is None:
        names = [s.name for s in await sprint_repo.list_sprints(cohort.id)]
        return {"exists": False, "reason": "sprint not found", "sprints": names}
    return {
        "exists": True,
        "sprint_id": sprint.id,
        "name": sprint.name,
        "status": sprint.status,
        "start_date": sprint.start_date.isoformat(),
        "end_date": sprint.end_date.isoformat(),
    }


async def check_ceremonies(cohort_name: str) -> dict[str, Any]:
    """List the cohort's upcoming (non-cancelled) ceremonies."""
    cohort = await cohort_repo.get_cohort_by_name(cohort_name)
    if cohort is None or cohort.id is None:
        return {"cohort_found": False, "ceremonies": []}
    types = {t.id: t.key for t in await ceremony_repo.list_ceremony_types()}
    rows = await ceremony_repo.list_ceremonies(cohort.id)
    return {
        "cohort_found": True,
        "ceremonies": [
            {
                "id": row.id,
                "type": types.get(row.ceremony_type_id, str(row.ceremony_type_id)),
                "scheduled_at": row.scheduled_at.isoformat(),
                "tz_aware": row.scheduled_at.tzinfo is not None,
                "status": row.status,
                "duration_minutes": row.duration_minutes,
            }
            for row in rows
        ],
        "standup_key": CeremonyTypeKey.DAILY_STANDUP.value,
    }


def main(argv: List[str]) -> int:
    """Dispatch on the sub-command."""
    command = argv[0] if argv else "probe"
    if command == "probe":
        probe_routing()
        probe_tool_groups()
        probe_graph_shape()
        failed = results.count(False)
        print(f"==> probe: {len(results) - failed}/{len(results)} checks passed")
        return 1 if failed else 0
    if command == "setup-cohort" and len(argv) == 5:
        print(json.dumps(asyncio.run(setup_cohort(argv[1], argv[2], argv[3], argv[4]))))
        return 0
    if command == "check-sprint" and len(argv) == 3:
        print(json.dumps(asyncio.run(check_sprint(argv[1], argv[2]))))
        return 0
    if command == "check-ceremonies" and len(argv) == 2:
        print(json.dumps(asyncio.run(check_ceremonies(argv[1]))))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
