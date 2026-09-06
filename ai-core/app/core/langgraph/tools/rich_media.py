"""Shared visual-output tools, available to every specialist.

These are the only way an agent produces a diagram, a chart, an interface or an
image. They are shared on purpose: a learner asking for a flowchart and a tech
lead asking for a burndown want the same renderer, and rendering is not a
business permission. Sharing them is safe precisely because they *stage* onto
the turn's envelope rather than writing anything — no Mattermost call, no
database write, no authority needed. Publication stays with
``conversation._deliver``, which already knows the threading rules.

Every argument here is content the model authors. Identity — who is asking,
which channel, which thread — is never an argument; it comes from the turn
context, so a model cannot redirect output at someone else's conversation.
"""

import json
from typing import (
    Any,
    Dict,
    List,
)

from langchain_core.tools import tool

from app.core.config import settings
from app.core.langgraph.tools.results import (
    ResultCode,
    guarded_tool,
    tool_result,
)
from app.core.logging import logger
from app.services import rich_media
from app.services.rich_media import RichMediaUnavailable

# Mermaid's own diagram keywords. A model that answers with prose instead of a
# definition fails here with a correctable message rather than shipping a post
# whose only content is a red parse error.
_MERMAID_KEYWORDS = (
    "graph",
    "flowchart",
    "sequencediagram",
    "classdiagram",
    "statediagram",
    "statediagram-v2",
    "erdiagram",
    "journey",
    "gantt",
    "pie",
    "gitgraph",
    "mindmap",
    "timeline",
    "quadrantchart",
    "requirementdiagram",
    "sankey-beta",
    "xychart-beta",
    "block-beta",
    "c4context",
)

# Vega-Lite marks we accept. Restricting the vocabulary keeps the renderer's
# behaviour predictable and rules out specs that pull remote resources.
_ALLOWED_MARKS = ("bar", "line", "point", "area", "arc", "circle", "rule", "text")

# Bare specifiers a generated component may import. Anything else — a URL, a
# relative path, an unpinned package — is refused before it reaches the sandbox.
_ALLOWED_IMPORTS = ("react", "react-dom", "react/jsx-runtime", "sprintflow/ui")

_UNAVAILABLE = "The visual could not be attached to this reply, so I answered in text instead."


def _looks_like_mermaid(definition: str) -> bool:
    """Whether a string opens with a Mermaid diagram declaration.

    Args:
        definition: The candidate definition.

    Returns:
        bool: True when the first word names a diagram type.
    """
    first = definition.strip().split(None, 1)
    if not first:
        return False
    head = first[0].strip().lower().rstrip(";:")
    return head in _MERMAID_KEYWORDS or any(head.startswith(k) for k in _MERMAID_KEYWORDS)


@tool
@guarded_tool
async def send_mermaid_diagram(definition: str, title: str = "", caption: str = "") -> str:
    r"""Attach an interactive Mermaid diagram to the reply you are about to send.

    Use this when the person ASKS for a diagram, flow, chart of steps or map, or
    when the answer is a multi-step process, sequence, hierarchy or state machine
    that words alone would make hard to follow — a registration flow, a sprint
    lifecycle, a decision tree. Do NOT add a diagram to an ordinary explanation,
    definition or comparison that reads fine as text; a picture nobody asked for
    is clutter. It works for any language: Arabic labels are rendered
    right-to-left correctly.

    Write the diagram in Mermaid syntax, starting with a diagram type such as
    ``graph TD``, ``flowchart LR``, ``sequenceDiagram`` or ``gantt``. Do not
    wrap it in a markdown code fence.

    After calling this, write ONE short sentence of text. The diagram appears
    with your reply automatically — never paste the definition into the message
    as well.

    Args:
        definition: The Mermaid source, e.g. "graph TD;\\n  A[Start] --> B[End];".
        title: Short heading shown above the diagram.
        caption: Optional line shown underneath it.

    Returns:
        str: Whether the diagram was attached.
    """
    if not settings.RICH_MEDIA_ENABLED:
        return tool_result(
            ResultCode.RENDERING_UNAVAILABLE,
            "Rich replies are switched off here, so answer with a mermaid code block instead.",
        )

    cleaned = definition.strip().strip("`")
    if cleaned.lower().startswith("mermaid"):
        cleaned = cleaned[len("mermaid") :].strip()

    if not cleaned:
        return tool_result(ResultCode.VALIDATION_ERROR, "The diagram definition was empty; nothing was attached.")
    if not _looks_like_mermaid(cleaned):
        return tool_result(
            ResultCode.VALIDATION_ERROR,
            "That is not Mermaid syntax. Start with a diagram type such as 'graph TD' or 'sequenceDiagram'.",
        )

    try:
        artifact = await rich_media.stage_mermaid(definition=cleaned, title=title, description=caption)
    except RichMediaUnavailable as e:
        logger.warning("rich_media_stage_refused", tool="send_mermaid_diagram", reason=str(e))
        return tool_result(ResultCode.RENDERING_UNAVAILABLE, _UNAVAILABLE)

    return tool_result(
        ResultCode.DIAGRAM_ATTACHED,
        f"The diagram is attached to your reply ({artifact.id}). Now write one short sentence introducing it.",
    )


def _validate_chart(spec: Dict[str, Any], data: List[Dict[str, Any]]) -> str:
    """Check a chart specification and its rows.

    Args:
        spec: The Vega-Lite specification.
        data: The rows to plot.

    Returns:
        str: An empty string when valid, otherwise the reason it was refused.
    """
    if not isinstance(spec, dict) or not spec:
        return "The chart specification must be a JSON object."
    if "data" in spec:
        return "Do not put data in the specification; pass the rows in the data argument."

    mark = spec.get("mark")
    mark_type = mark.get("type") if isinstance(mark, dict) else mark
    if not isinstance(mark_type, str) or mark_type not in _ALLOWED_MARKS:
        return f"Chart mark must be one of: {', '.join(_ALLOWED_MARKS)}."
    if not isinstance(spec.get("encoding"), dict):
        return "The specification needs an 'encoding' object saying which fields map to which axes."

    if not data:
        return "A chart needs rows. Read the real values with your data tools first; never invent numbers."
    if len(data) > settings.RICH_MEDIA_MAX_CHART_ROWS:
        return f"Too many rows for one chart (limit {settings.RICH_MEDIA_MAX_CHART_ROWS})."
    for row in data:
        if not isinstance(row, dict):
            return "Every row must be a flat JSON object."
        for value in row.values():
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                return "Row values must be strings, numbers, booleans or null."
    return ""


@tool
@guarded_tool
async def send_chart(
    spec: Dict[str, Any],
    data: List[Dict[str, Any]],
    title: str = "",
    caption: str = "",
) -> str:
    """Attach an interactive chart (bar, line or pie) to the reply.

    Use this to show measurements you have ALREADY READ with your data tools —
    sprint progress, attendance, ticket counts. Never invent or estimate the
    numbers: pass exactly the values the data tool returned.

    ``spec`` is a Vega-Lite specification WITHOUT its data, for example
    ``{"mark": "bar", "encoding": {"x": {"field": "sprint", "type": "nominal"},
    "y": {"field": "completed", "type": "quantitative"}}}``. Use mark "arc" for
    a pie chart. ``data`` is the list of rows those fields refer to.

    After calling this, write ONE short sentence. The chart appears with your
    reply automatically.

    Args:
        spec: Vega-Lite specification, without a data property.
        data: The rows to plot, as flat JSON objects.
        title: Short heading shown above the chart.
        caption: Optional line shown underneath it.

    Returns:
        str: Whether the chart was attached.
    """
    if not settings.RICH_MEDIA_ENABLED:
        return tool_result(ResultCode.RENDERING_UNAVAILABLE, "Rich replies are switched off here.")

    problem = _validate_chart(spec, data)
    if problem:
        return tool_result(ResultCode.VALIDATION_ERROR, problem)

    try:
        artifact = await rich_media.stage_chart(spec=spec, data=data, title=title, description=caption)
    except RichMediaUnavailable as e:
        logger.warning("rich_media_stage_refused", tool="send_chart", reason=str(e))
        return tool_result(ResultCode.RENDERING_UNAVAILABLE, _UNAVAILABLE)

    return tool_result(
        ResultCode.CHART_ATTACHED,
        f"The chart is attached to your reply ({artifact.id}) with {len(data)} rows. Now write one short sentence.",
    )


def _validate_react(source: str) -> str:
    """Check generated component source against the runtime contract.

    This is defence in depth, not the security boundary — that is the sandboxed
    iframe. It catches the cases the sandbox should never be asked to handle:
    an unpinned dependency, a remote import, a component with no entry point.

    Args:
        source: The component source.

    Returns:
        str: An empty string when acceptable, otherwise the reason.
    """
    if not source.strip():
        return "The component source was empty."
    if len(source) > settings.RICH_MEDIA_MAX_REACT_CHARS:
        return f"The component is too long (limit {settings.RICH_MEDIA_MAX_REACT_CHARS} characters)."
    if "export default" not in source:
        return "The component must have a default export — that is the entry point the runtime mounts."

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("import "):
            continue
        if "'" not in stripped and '"' not in stripped:
            continue
        quote = "'" if "'" in stripped else '"'
        specifier = stripped.split(quote)[1]
        if specifier not in _ALLOWED_IMPORTS:
            return (
                f"Import of '{specifier}' is not allowed. Only {', '.join(_ALLOWED_IMPORTS)} are available, "
                "and remote or relative imports are refused."
            )
    return ""


@tool
@guarded_tool
async def send_react_artifact(
    source: str,
    data: Dict[str, Any],
    title: str = "",
    description: str = "",
) -> str:
    """Attach a small interactive React interface to the reply.

    Use this when the person needs to explore something rather than look at it:
    a cost calculator with sliders, a what-if estimator, a filterable table.
    The component runs in an isolated sandbox in the reader's browser; its state
    is local to each reader and nothing it does changes any stored data.

    Write ONE self-contained component with a default export. Build the UI from
    the design system — ``import {Card, Stack, Grid, Slider, NumberInput, Select,
    Segmented, Stat, Alert, Empty, Table, Button, format} from 'sprintflow/ui'``
    — so it matches the reader's theme and direction. Do not write CSS, style
    attributes or Tailwind classes; do not import anything else, fetch, or read
    the page. Read inputs from the ``data`` prop instead of hard-coding numbers.

    Layout rules: a ``Card`` with a title, inputs first in a ``Stack``, results
    after them in a ``Grid`` of ``Stat`` (mark the main figure ``primary``), and
    an ``Alert`` or ``Empty`` for invalid or empty states. Every ``Slider`` and
    ``NumberInput`` needs a label, min, max and step; format money with
    ``format.currency(n, "USD")`` and rates with ``format.percent``. Labels in
    the person's language.

    Example:
        import React, {useState} from 'react';
        import {Card, Stack, Grid, Slider, Stat, Alert, format} from 'sprintflow/ui';
        export default function Estimator({data}) {
          const [devs, setDevs] = useState(data.devs ?? 3);
          const [weeks, setWeeks] = useState(data.weeks ?? 8);
          const cost = devs * weeks * (data.weeklyRate ?? 1200);
          return (
            <Card title="تقدير التكلفة" subtitle="حرّك المؤشرات لتغيير التقدير">
              <Stack>
                <Slider label="عدد المطورين" value={devs} min={1} max={20} onChange={setDevs}/>
                <Slider label="عدد الأسابيع" value={weeks} min={1} max={52} onChange={setWeeks}/>
                <Grid>
                  <Stat primary label="التكلفة الإجمالية" value={format.currency(cost, "USD")}/>
                  <Stat label="أسابيع العمل" value={format.number(devs * weeks)} hint="مطور × أسبوع"/>
                </Grid>
                {cost > 100000 ? <Alert tone="warning">التقدير يتجاوز الميزانية المعتادة.</Alert> : null}
              </Stack>
            </Card>
          );
        }

    Args:
        source: The component source, with a default export.
        data: JSON passed to the component as its ``data`` prop.
        title: Short heading shown above the interface.
        description: Optional line shown underneath it.

    Returns:
        str: Whether the interface was attached.
    """
    if not settings.RICH_MEDIA_ENABLED:
        return tool_result(ResultCode.RENDERING_UNAVAILABLE, "Rich replies are switched off here.")

    problem = _validate_react(source)
    if problem:
        return tool_result(ResultCode.VALIDATION_ERROR, problem)

    try:
        json.dumps(data)
    except (TypeError, ValueError):
        return tool_result(ResultCode.VALIDATION_ERROR, "The data argument must be JSON-serialisable.")

    try:
        artifact = await rich_media.stage_react(source=source, data=data, title=title, description=description)
    except RichMediaUnavailable as e:
        logger.warning("rich_media_stage_refused", tool="send_react_artifact", reason=str(e))
        return tool_result(ResultCode.RENDERING_UNAVAILABLE, _UNAVAILABLE)

    return tool_result(
        ResultCode.INTERFACE_ATTACHED,
        f"The interface is attached to your reply ({artifact.id}). Now write one short sentence.",
    )


@tool
@guarded_tool
async def generate_and_send_image(prompt: str, alt_text: str = "", aspect_ratio: str = "1:1") -> str:
    """Generate an illustration and attach it to the reply.

    Use this only when a picture is genuinely what was asked for — an
    illustration, a poster, a concept image. It is NOT for diagrams, flows or
    charts: use send_mermaid_diagram or send_chart for those, which are sharper
    and readable on every client.

    The image is generated after your reply is posted; the person sees a loading
    card that fills in by itself. Describe what you want in the prompt, in
    English or Arabic.

    Args:
        prompt: What to draw.
        alt_text: A short description for people using a screen reader.
        aspect_ratio: "1:1", "16:9", "4:3" or "9:16".

    Returns:
        str: Whether the image was queued.
    """
    if not (settings.RICH_MEDIA_ENABLED and settings.IMAGE_GENERATION_ENABLED):
        return tool_result(
            ResultCode.RENDERING_UNAVAILABLE,
            "Image generation is not switched on here. Say so plainly and answer without a picture.",
        )

    if not prompt.strip():
        return tool_result(ResultCode.VALIDATION_ERROR, "The image prompt was empty.")
    if aspect_ratio not in ("1:1", "16:9", "4:3", "9:16"):
        return tool_result(ResultCode.VALIDATION_ERROR, "Aspect ratio must be one of 1:1, 16:9, 4:3, 9:16.")

    try:
        artifact = await rich_media.stage_image(prompt=prompt, alt_text=alt_text, aspect_ratio=aspect_ratio)
    except RichMediaUnavailable as e:
        logger.warning("rich_media_stage_refused", tool="generate_and_send_image", reason=str(e))
        return tool_result(ResultCode.RENDERING_UNAVAILABLE, str(e))

    return tool_result(
        ResultCode.IMAGE_QUEUED,
        f"The image is being generated and will appear in your reply ({artifact.id}). "
        "Say that it is on its way, in one short sentence.",
    )


# Every specialist gets these. They stage output; they change nothing.
RICH_MEDIA_TOOLS = [
    send_mermaid_diagram,
    send_chart,
    send_react_artifact,
    generate_and_send_image,
]

__all__ = [
    "RICH_MEDIA_TOOLS",
    "generate_and_send_image",
    "send_chart",
    "send_mermaid_diagram",
    "send_react_artifact",
]
