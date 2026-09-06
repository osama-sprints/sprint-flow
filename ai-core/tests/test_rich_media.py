"""Direct tests for rich-media staging, validation and the reply envelope.

These exercise the parts a browser cannot: what a tool refuses, what the
envelope puts in post props, and what happens when a tool is called outside a
turn. Staging persists through the data-access layer, which logs and continues
when no database is reachable — so these run without one, and ``collect`` falls
back to the in-memory copy exactly as it does in production during an outage.
"""

import json

import pytest

from app.core.langgraph.tools.results import result_code_of
from app.core.langgraph.tools.rich_media import (
    generate_and_send_image,
    send_chart,
    send_mermaid_diagram,
    send_react_artifact,
)
from app.schemas.rich_media import (
    MAX_ARTIFACTS_PER_REPLY,
    MAX_INLINE_BYTES,
    RICH_MEDIA_POST_TYPE,
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    MermaidContent,
    ReplyEnvelope,
)
from app.services import rich_media

VALID_DIAGRAM = "graph TD;\n  A[Start] --> B[End];"

VALID_SPEC = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "sprint", "type": "nominal"},
        "y": {"field": "done", "type": "quantitative"},
    },
}
VALID_ROWS = [{"sprint": "S1", "done": 3}, {"sprint": "S2", "done": 5}]

VALID_COMPONENT = "import React from 'react';\nexport default function C() { return <b>hi</b>; }\n"


@pytest.fixture
def turn():
    """Bind a staging context for one turn and unbind it afterwards."""
    context = rich_media.begin_turn(
        channel_id="channel-1",
        root_id="root-1",
        requester_user_id=7,
        mattermost_user_id="mm-user-1",
        session_id="session-1",
    )
    yield context
    rich_media.end_turn()


# --- trusted context -----------------------------------------------------------


async def test_tool_outside_a_turn_reports_unavailable_and_stages_nothing():
    """A tool called with no bound turn has nowhere to publish and says so."""
    rich_media.end_turn()
    result = await send_mermaid_diagram.ainvoke({"definition": VALID_DIAGRAM})
    assert result_code_of(result) == "RENDERING_UNAVAILABLE"


async def test_staged_artifact_takes_identity_from_the_context_not_arguments(turn):
    """Channel, thread and requester come from the turn, never from the model."""
    await send_mermaid_diagram.ainvoke({"definition": VALID_DIAGRAM, "title": "T"})
    artifact = turn.artifacts[0]
    assert artifact.channel_id == "channel-1"
    assert artifact.root_id == "root-1"
    assert artifact.requester_user_id == 7
    assert artifact.turn_id == turn.turn_id


async def test_collect_excludes_artifacts_from_another_turn(turn):
    """A resumed conversation must not republish an earlier turn's output."""
    await send_mermaid_diagram.ainvoke({"definition": VALID_DIAGRAM})
    envelope = await rich_media.collect("some-other-turn")
    assert envelope.artifacts == []


# --- validation ----------------------------------------------------------------


async def test_prose_instead_of_a_diagram_is_refused(turn):
    """A model that answers in words gets a correctable error, not a red post."""
    result = await send_mermaid_diagram.ainvoke({"definition": "First the user registers, then..."})
    assert result_code_of(result) == "VALIDATION_ERROR"
    assert turn.artifacts == []


async def test_fenced_diagram_is_unwrapped_rather_than_refused(turn):
    """Models fence their answers; that is not worth failing a turn over."""
    result = await send_mermaid_diagram.ainvoke({"definition": f"```mermaid\n{VALID_DIAGRAM}\n```"})
    assert result_code_of(result) == "DIAGRAM_ATTACHED"
    assert turn.artifacts[0].content.definition.startswith("graph TD")


async def test_chart_without_rows_is_refused_rather_than_invented(turn):
    """The agent must read real values; an empty chart is a prompt to go and look."""
    result = await send_chart.ainvoke({"spec": VALID_SPEC, "data": []})
    assert result_code_of(result) == "VALIDATION_ERROR"
    assert turn.artifacts == []


async def test_chart_specification_may_not_carry_its_own_data(turn):
    """Data in the spec is how a chart would reach out to a URL."""
    result = await send_chart.ainvoke(
        {"spec": {**VALID_SPEC, "data": {"url": "https://x/y.json"}}, "data": VALID_ROWS}
    )
    assert result_code_of(result) == "VALIDATION_ERROR"


async def test_chart_row_count_is_bounded(turn):
    """A chart is not a data export."""
    rows = [{"sprint": str(i), "done": i} for i in range(10_000)]
    result = await send_chart.ainvoke({"spec": VALID_SPEC, "data": rows})
    assert result_code_of(result) == "VALIDATION_ERROR"


async def test_chart_is_staged_with_its_rows(turn):
    """The happy path keeps the rows the tool was given, unchanged."""
    result = await send_chart.ainvoke({"spec": VALID_SPEC, "data": VALID_ROWS, "title": "Sprint"})
    assert result_code_of(result) == "CHART_ATTACHED"
    assert turn.artifacts[0].content.data == VALID_ROWS


async def test_component_importing_anything_else_is_refused(turn):
    """Only the pinned runtime packages resolve in the sandbox."""
    source = "import axios from 'axios';\nexport default function C() { return null; }"
    result = await send_react_artifact.ainvoke({"source": source, "data": {}})
    assert result_code_of(result) == "VALIDATION_ERROR"
    assert turn.artifacts == []


async def test_design_system_import_is_allowed(turn):
    """'sprintflow/ui' is the one extra module the sandbox resolves."""
    source = (
        "import React from 'react';\nimport {Card, Slider, Stat} from 'sprintflow/ui';\n"
        "export default function C() { return <Card title='t'><Stat label='l' value='1'/></Card>; }\n"
    )
    result = await send_react_artifact.ainvoke({"source": source, "data": {}})
    assert result_code_of(result) == "INTERFACE_ATTACHED"


async def test_component_without_a_default_export_is_refused(turn):
    """The runtime mounts the default export; without one there is nothing to run."""
    result = await send_react_artifact.ainvoke({"source": "function C() { return null; }", "data": {}})
    assert result_code_of(result) == "VALIDATION_ERROR"


async def test_valid_component_is_staged(turn):
    """A component that meets the contract is attached."""
    result = await send_react_artifact.ainvoke({"source": VALID_COMPONENT, "data": {"rate": 5}})
    assert result_code_of(result) == "INTERFACE_ATTACHED"
    assert turn.artifacts[0].kind == ArtifactKind.REACT


# --- limits --------------------------------------------------------------------


async def test_a_reply_cannot_carry_unbounded_artifacts(turn):
    """Past the cap the tool refuses instead of growing post props without end."""
    for _ in range(MAX_ARTIFACTS_PER_REPLY):
        assert result_code_of(await send_mermaid_diagram.ainvoke({"definition": VALID_DIAGRAM})) == "DIAGRAM_ATTACHED"

    result = await send_mermaid_diagram.ainvoke({"definition": VALID_DIAGRAM})
    assert result_code_of(result) == "RENDERING_UNAVAILABLE"
    assert len(turn.artifacts) == MAX_ARTIFACTS_PER_REPLY


async def test_image_budget_is_per_turn(turn, monkeypatch):
    """Generated images are metered, so one turn cannot queue a dozen."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "IMAGE_GENERATION_ENABLED", True)
    monkeypatch.setattr(settings, "RICH_MEDIA_MAX_IMAGES_PER_TURN", 1)

    assert result_code_of(await generate_and_send_image.ainvoke({"prompt": "a poster"})) == "IMAGE_QUEUED"
    second = await generate_and_send_image.ainvoke({"prompt": "another poster"})
    assert result_code_of(second) == "RENDERING_UNAVAILABLE"


# --- disabled capabilities -----------------------------------------------------


async def test_disabled_image_generation_is_reported_not_claimed(turn, monkeypatch):
    """An unavailable capability must never answer as though it worked."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "IMAGE_GENERATION_ENABLED", False)
    result = await generate_and_send_image.ainvoke({"prompt": "a poster"})
    assert result_code_of(result) == "RENDERING_UNAVAILABLE"
    assert "not switched on" in result
    assert turn.artifacts == []


async def test_disabled_rich_media_falls_back_to_a_code_block(turn, monkeypatch):
    """With rendering off the agent is told what to do instead."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RICH_MEDIA_ENABLED", False)
    result = await send_mermaid_diagram.ainvoke({"definition": VALID_DIAGRAM})
    assert result_code_of(result) == "RENDERING_UNAVAILABLE"
    assert "code block" in result


# --- the envelope --------------------------------------------------------------


def _artifact(**overrides) -> Artifact:
    """Build an artifact for envelope tests."""
    defaults = dict(
        id="a1",
        kind=ArtifactKind.MERMAID,
        status=ArtifactStatus.READY,
        title="T",
        content=MermaidContent(definition=VALID_DIAGRAM),
        turn_id="turn-1",
        channel_id="channel-1",
    )
    defaults.update(overrides)
    return Artifact(**defaults)


def test_props_carry_the_turn_id_for_reconciliation():
    """The turn id in props is how a retry finds a reply it already published."""
    props = ReplyEnvelope(turn_id="turn-1", artifacts=[_artifact()]).to_props()
    assert props["sf_turn_id"] == "turn-1"
    assert props["sf_envelope_version"] == 1
    assert props["sf_artifacts"][0]["id"] == "a1"


def test_small_content_travels_inline():
    """A short diagram needs no second request to render."""
    reference = _artifact().to_reference()
    assert reference["inline"]["definition"].startswith("graph TD")


def test_large_content_is_left_out_of_props():
    """Post props stay small; the browser fetches the rest by id."""
    huge = _artifact(content=MermaidContent(definition="graph TD;\n" + ("  A --> B;\n" * 800)))
    reference = huge.to_reference()
    assert "inline" not in reference
    assert len(json.dumps(reference).encode()) < MAX_INLINE_BYTES


def test_post_type_is_within_the_servers_column_limit():
    """Mattermost stores Posts.Type in varchar(26) and rejects anything longer."""
    assert len(RICH_MEDIA_POST_TYPE) <= 26
