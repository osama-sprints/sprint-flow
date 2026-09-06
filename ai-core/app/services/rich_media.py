"""Staging for rich replies: the one place tools put visual output.

The design rule is that a tool never posts. It stages an artifact onto the
turn's envelope and returns a sentence; ``conversation._deliver`` publishes the
single reply with the threading rules that already exist. That keeps one post
per turn, keeps threading correct without every tool having to know a post id,
and — because staging writes nothing to Mattermost — keeps these tools
non-mutating, so they can be shared with specialists that must not gain any
business permission.

Identity travels the same way the requester does: bound to a ContextVar before
the graph runs, never passed as a tool argument the model could fill in. A
model chooses a title; it does not choose a channel.

Every staged artifact is written to ``rich_artifacts`` as it is staged, so the
reply's content survives a restart, a graph replay and the gap between
publishing a post and recording that we published it. The in-memory context
keeps only the turn's identity and ordering; the database is the authority, and
a read failure degrades to the in-memory copy rather than dropping the visual.
"""

import uuid
from contextvars import ContextVar
from dataclasses import (
    dataclass,
    field,
)
from typing import (
    Any,
    Dict,
    List,
    Optional,
)

from app.core.config import settings
from app.core.logging import logger
from app.models import RichArtifact
from app.services.domain import rich_artifacts as store
from app.schemas.rich_media import (
    MAX_ARTIFACTS_PER_REPLY,
    Artifact,
    ArtifactKind,
    ArtifactStatus,
    ChartContent,
    ImageContent,
    MermaidContent,
    ReactContent,
    ReplyEnvelope,
)


class RichMediaUnavailable(RuntimeError):
    """Raised when a tool is called outside a turn that can publish."""


@dataclass
class RichMediaContext:
    """The trusted facts about the turn currently being answered.

    Attributes:
        turn_id: New for every turn. An artifact staged under an earlier turn
            is never published by a later one, which is what stops a resumed
            conversation from re-posting old output.
        session_id: LangGraph thread id, stored with each artifact for tracing.
        channel_id: Where the reply will land.
        root_id: Thread root, when the reply belongs to one.
        requester_user_id: ``users.id`` of the person being answered.
        mattermost_user_id: Their Mattermost id.
        artifacts: Staged so far, in order.
        images_requested: Counts against the per-turn image budget.
    """

    turn_id: str
    channel_id: str
    session_id: str = ""
    root_id: str = ""
    requester_user_id: Optional[int] = None
    mattermost_user_id: str = ""
    artifacts: List[Artifact] = field(default_factory=list)
    images_requested: int = 0


current_rich_media: ContextVar[Optional[RichMediaContext]] = ContextVar("current_rich_media", default=None)


def begin_turn(
    *,
    channel_id: str,
    root_id: str = "",
    requester_user_id: Optional[int] = None,
    mattermost_user_id: str = "",
    session_id: str = "",
) -> RichMediaContext:
    """Open a staging context for one turn and bind it.

    Args:
        channel_id: Channel the reply will be published to.
        root_id: Thread root the reply belongs to, if any.
        requester_user_id: ``users.id`` of the person being answered.
        mattermost_user_id: Their Mattermost user id.
        session_id: LangGraph thread id for this conversation.

    Returns:
        RichMediaContext: The bound context.
    """
    context = RichMediaContext(
        turn_id=str(uuid.uuid4()),
        channel_id=channel_id,
        root_id=root_id,
        requester_user_id=requester_user_id,
        mattermost_user_id=mattermost_user_id,
        session_id=session_id,
    )
    current_rich_media.set(context)
    return context


def end_turn() -> None:
    """Unbind the staging context so nothing leaks into the next turn."""
    current_rich_media.set(None)


def _require_context() -> RichMediaContext:
    """Return the bound context.

    Returns:
        RichMediaContext: The current turn's context.

    Raises:
        RichMediaUnavailable: When no turn is bound, which means the caller is
            not on the reply path and has nowhere to publish.
    """
    context = current_rich_media.get()
    if context is None:
        raise RichMediaUnavailable("no rich-media context is bound to this turn")
    return context


async def _stage(
    kind: ArtifactKind,
    *,
    title: str,
    description: str,
    content: Any,
    status: ArtifactStatus = ArtifactStatus.READY,
    operation_id: Optional[str] = None,
) -> Artifact:
    """Add one artifact to the current turn's envelope and persist it.

    Args:
        kind: Artifact kind.
        title: Card heading.
        description: Card caption.
        content: The kind-specific content model.
        status: Lifecycle state to publish with.
        operation_id: Stable key for an external side effect, so a replayed
            turn reuses the result instead of paying for it twice.

    Returns:
        Artifact: The staged artifact.

    Raises:
        RichMediaUnavailable: When no turn is bound, or the turn is full.
    """
    context = _require_context()
    if len(context.artifacts) >= MAX_ARTIFACTS_PER_REPLY:
        raise RichMediaUnavailable(f"a reply carries at most {MAX_ARTIFACTS_PER_REPLY} artifacts")

    artifact = Artifact(
        id=str(uuid.uuid4()),
        kind=kind,
        status=status,
        title=title.strip()[:200],
        description=description.strip()[:500],
        content=content,
        turn_id=context.turn_id,
        requester_user_id=context.requester_user_id,
        channel_id=context.channel_id,
        root_id=context.root_id,
    )
    context.artifacts.append(artifact)

    # Persisted as it is staged. A crash after this point can still recover the
    # reply's content; a crash before it loses nothing that was ever shown.
    try:
        await store.create_artifact(
            RichArtifact(
                id=artifact.id,
                turn_id=context.turn_id,
                session_id=context.session_id,
                kind=kind.value,
                schema_version=artifact.schema_version,
                revision=artifact.revision,
                status=status.value,
                title=artifact.title,
                description=artifact.description,
                content=content.model_dump(exclude_none=True) if content is not None else {},
                requester_user_id=context.requester_user_id,
                mattermost_user_id=context.mattermost_user_id,
                channel_id=context.channel_id,
                root_id=context.root_id,
                operation_id=operation_id,
            )
        )
    except Exception as e:
        # The visual still publishes from the in-memory copy; what is lost is
        # durability, and that is worth a loud log rather than a failed reply.
        logger.exception("rich_media_persist_failed", artifact_id=artifact.id, kind=kind.value, error=str(e))

    logger.info(
        "rich_media_artifact_staged",
        artifact_id=artifact.id,
        kind=kind.value,
        status=status.value,
        turn_id=context.turn_id,
        channel_id=context.channel_id,
        position=len(context.artifacts),
    )
    return artifact


async def stage_mermaid(*, definition: str, title: str = "", description: str = "") -> Artifact:
    """Stage a Mermaid diagram.

    Args:
        definition: Mermaid source.
        title: Card heading.
        description: Card caption.

    Returns:
        Artifact: The staged diagram.
    """
    return await _stage(
        ArtifactKind.MERMAID,
        title=title,
        description=description,
        content=MermaidContent(definition=definition.strip()),
    )


async def stage_chart(
    *,
    spec: Dict[str, Any],
    data: List[Dict[str, Any]],
    title: str = "",
    description: str = "",
) -> Artifact:
    """Stage a Vega-Lite chart.

    Args:
        spec: Validated Vega-Lite specification.
        data: The rows to plot — real, authorised values only.
        title: Card heading.
        description: Card caption.

    Returns:
        Artifact: The staged chart.
    """
    return await _stage(
        ArtifactKind.CHART,
        title=title,
        description=description,
        content=ChartContent(spec=spec, data=data),
    )


async def stage_react(
    *,
    source: str,
    data: Optional[Dict[str, Any]] = None,
    title: str = "",
    description: str = "",
) -> Artifact:
    """Stage a generated React component.

    Args:
        source: Component source, compiled and run only inside the sandbox.
        data: JSON handed to the component as its input.
        title: Card heading.
        description: Card caption.

    Returns:
        Artifact: The staged component.
    """
    return await _stage(
        ArtifactKind.REACT,
        title=title,
        description=description,
        content=ReactContent(source=source, data=data or {}),
    )


async def stage_image(
    *,
    prompt: str,
    alt_text: str = "",
    aspect_ratio: str = "1:1",
    title: str = "",
    description: str = "",
) -> Artifact:
    """Stage a pending generated image.

    The artifact is published immediately in ``PENDING`` so the person sees the
    reply at once; a worker fills it in and updates the same post.

    Args:
        prompt: What to generate.
        alt_text: Accessible description.
        aspect_ratio: Requested shape.
        title: Card heading.
        description: Card caption.

    Returns:
        Artifact: The staged, not-yet-generated image.

    Raises:
        RichMediaUnavailable: When the per-turn image budget is spent.
    """
    context = _require_context()
    if context.images_requested >= settings.RICH_MEDIA_MAX_IMAGES_PER_TURN:
        raise RichMediaUnavailable(f"at most {settings.RICH_MEDIA_MAX_IMAGES_PER_TURN} generated images per reply")
    context.images_requested += 1

    return await _stage(
        ArtifactKind.IMAGE,
        title=title,
        description=description,
        content=ImageContent(prompt=prompt.strip(), alt_text=alt_text.strip(), aspect_ratio=aspect_ratio),
        status=ArtifactStatus.PENDING,
        # One generation per (turn, position): a replayed graph reuses the row
        # instead of starting a second paid request.
        operation_id=f"image:{context.turn_id}:{context.images_requested}",
    )


def _to_artifact(row: RichArtifact) -> Optional[Artifact]:
    """Rebuild the envelope model from a stored row.

    Args:
        row: The stored artifact.

    Returns:
        Artifact | None: The model, or None when the stored kind or content no
        longer parses — a row written by a newer version, which this one must
        skip rather than render wrongly.
    """
    content_models = {
        ArtifactKind.MERMAID: MermaidContent,
        ArtifactKind.CHART: ChartContent,
        ArtifactKind.REACT: ReactContent,
        ArtifactKind.IMAGE: ImageContent,
    }
    try:
        kind = ArtifactKind(row.kind)
        model = content_models.get(kind)
        content = model.model_validate(row.content) if model and row.content else None
        return Artifact(
            id=row.id,
            kind=kind,
            schema_version=row.schema_version,
            revision=row.revision,
            status=ArtifactStatus(row.status),
            title=row.title,
            description=row.description,
            error=row.error,
            content=content,
            turn_id=row.turn_id,
            requester_user_id=row.requester_user_id,
            channel_id=row.channel_id,
            root_id=row.root_id,
            post_id=row.post_id,
            file_ids=list(row.file_ids or []),
        )
    except Exception as e:
        logger.warning("rich_media_row_unreadable", artifact_id=row.id, kind=row.kind, error=str(e))
        return None


async def collect(turn_id: Optional[str] = None) -> ReplyEnvelope:
    """Return the envelope for one turn, from durable storage.

    Args:
        turn_id: The turn to publish. Artifacts staged by any other turn are
            excluded, so a resumed conversation cannot republish earlier output.

    Returns:
        ReplyEnvelope: The staged artifacts, in order. Empty when nothing was
        staged.
    """
    context = current_rich_media.get()
    key = turn_id or (context.turn_id if context else None)
    if key is None:
        return ReplyEnvelope()

    artifacts: List[Artifact] = []
    try:
        rows = await store.list_for_turn(key)
        artifacts = [a for a in (_to_artifact(row) for row in rows) if a is not None]
    except Exception as e:
        logger.exception("rich_media_collect_failed", turn_id=key, error=str(e))

    if not artifacts and context is not None:
        # Storage was unavailable when these were staged. Publishing the
        # in-memory copy is strictly better than dropping the visual; the
        # artifact simply has no durable record to be fetched by id later.
        artifacts = [a for a in context.artifacts if a.turn_id == key]

    file_ids = [fid for a in artifacts for fid in a.file_ids]
    return ReplyEnvelope(turn_id=key, artifacts=artifacts[:MAX_ARTIFACTS_PER_REPLY], file_ids=file_ids)


async def already_published(turn_id: str) -> Optional[str]:
    """Return the post this turn already published, if any.

    This is the reconciliation read that closes the crash window between
    creating a post and recording it: the turn id travels in the post's props,
    so a retry can adopt the existing reply instead of posting a second one.

    Args:
        turn_id: The turn.

    Returns:
        str | None: The published post id, or None.
    """
    try:
        return await store.find_turn_publication(turn_id)
    except Exception as e:
        logger.exception("rich_media_publication_lookup_failed", turn_id=turn_id, error=str(e))
        return None


async def record_publication(turn_id: str, post_id: str, file_ids: Optional[List[str]] = None) -> None:
    """Record which post published a turn's artifacts.

    Args:
        turn_id: The turn.
        post_id: The created post.
        file_ids: Attachments the post carries.
    """
    try:
        await store.mark_published(turn_id, post_id, file_ids=file_ids)
    except Exception as e:
        logger.exception("rich_media_publication_record_failed", turn_id=turn_id, post_id=post_id, error=str(e))
