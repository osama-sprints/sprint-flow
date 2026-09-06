"""The rich-media reply envelope: what a tool stages and what a post carries.

One reply is one Mattermost post. Everything a turn wants to show — a diagram,
a chart, a generated image — is staged as an *artifact* and published together
by ``app.services.conversation._deliver``. Tools never post.

Two size rules are structural rather than stylistic:

* ``post.props`` stays small. A reference may carry its content inline only
  while that content serialises under ``MAX_INLINE_BYTES``; anything larger is
  published as a reference alone and fetched from ai-core by id.
* the ``message`` field is always a complete answer on its own, because every
  client without the plugin — mobile, search, notification emails — shows only
  that.

The envelope is versioned. ``ENVELOPE_VERSION`` is the contract the webapp
plugin validates against, and a plugin that sees a higher version says so
instead of rendering a post it does not understand.
"""

import json
from enum import StrEnum
from typing import (
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Union,
)

from pydantic import (
    BaseModel,
    Field,
)

# Bumped only for a breaking change to the props shape. The plugin refuses
# anything higher than the version it was built against.
ENVELOPE_VERSION = 1

# Mattermost's Posts.Type column is varchar(26); a longer type is rejected at
# insert with a 500, which is why this is not "custom_sprintflow_rich_media".
RICH_MEDIA_POST_TYPE = "custom_sf_rich_media"

# The pre-existing single-diagram type. Posts of this type are already in
# channel history, so its shape is frozen.
LEGACY_MERMAID_POST_TYPE = "custom_interactive_mermaid"

# Largest inline payload, in bytes of JSON. Mirrors MAX_INLINE_CHARS in the
# plugin; keep the two in step.
MAX_INLINE_BYTES = 4096

# Most artifacts one reply may carry. The plugin drops the rest.
MAX_ARTIFACTS_PER_REPLY = 8

MAX_DEFINITION_CHARS = 20000
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 500


class ArtifactKind(StrEnum):
    """What an artifact is, which decides how the plugin renders it."""

    MERMAID = "mermaid"
    CHART = "chart"
    REACT = "react"
    IMAGE = "image"


class ArtifactStatus(StrEnum):
    """Where an artifact is in its lifecycle.

    ``PENDING`` and ``RUNNING`` are published states, not internal ones: an
    image is posted with a loading card and the same post is updated when the
    worker finishes, so the person sees the reply immediately.
    """

    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class MermaidContent(BaseModel):
    """A Mermaid diagram's source."""

    definition: str = Field(..., max_length=MAX_DEFINITION_CHARS)


class ChartContent(BaseModel):
    """A Vega-Lite specification and the rows it renders."""

    spec: Dict[str, Any]
    data: List[Dict[str, Any]] = Field(default_factory=list)


class ReactContent(BaseModel):
    """Generated React source and the JSON data handed to it."""

    source: str
    data: Dict[str, Any] = Field(default_factory=dict)


class ImageContent(BaseModel):
    """A generated image: the prompt that made it and where it landed."""

    prompt: str
    alt_text: str = ""
    aspect_ratio: str = "1:1"
    file_id: Optional[str] = None


ArtifactContent = Union[MermaidContent, ChartContent, ReactContent, ImageContent]


class Artifact(BaseModel):
    """One artifact, as ai-core holds it.

    Identity and ownership come from the turn's trusted context, never from a
    tool argument: a model can choose a title, not a channel.

    Attributes:
        id: Server-generated identifier.
        kind: Which renderer displays it.
        schema_version: Envelope version this artifact was written for.
        revision: Bumped whenever content changes after publication.
        status: Lifecycle state.
        title: Heading shown on the card.
        description: Caption under the card.
        error: Why it failed, when status is FAILED.
        content: The kind-specific payload.
        turn_id: The turn that staged it — the guard against a resumed
            conversation republishing an earlier turn's artifacts.
        requester_user_id: ``users.id`` of the person the turn is for.
        channel_id: Where the reply will be published.
        root_id: Thread root of the reply, when it has one.
        post_id: The published post, once it exists.
        file_ids: Mattermost file ids attached to the reply.
    """

    id: str
    kind: ArtifactKind
    schema_version: int = ENVELOPE_VERSION
    revision: int = 1
    status: ArtifactStatus = ArtifactStatus.READY
    title: str = Field(default="", max_length=MAX_TITLE_CHARS)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    error: Optional[str] = None
    content: Optional[ArtifactContent] = None

    turn_id: str = ""
    requester_user_id: Optional[int] = None
    channel_id: str = ""
    root_id: str = ""
    post_id: Optional[str] = None
    file_ids: List[str] = Field(default_factory=list)

    def inline_payload(self) -> Optional[Dict[str, Any]]:
        """Return the content to embed in props, or None when it is too large.

        Returns:
            dict | None: The serialised content when it fits within
            ``MAX_INLINE_BYTES``, otherwise None so the plugin fetches it by id.
        """
        if self.content is None:
            return None

        payload = self.content.model_dump(exclude_none=True)
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_INLINE_BYTES:
            return None
        return payload

    def to_reference(self) -> Dict[str, Any]:
        """Render this artifact as the reference that travels in post props.

        Returns:
            dict: The reference, small by construction.
        """
        reference: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "status": self.status.value,
        }
        if self.title:
            reference["title"] = self.title
        if self.description:
            reference["description"] = self.description
        if self.error:
            reference["error"] = self.error

        inline = self.inline_payload()
        if inline is not None:
            reference["inline"] = inline
        return reference


class ReplyEnvelope(BaseModel):
    """Everything one reply publishes, beyond its text.

    Attributes:
        version: Envelope schema version.
        turn_id: The turn that produced this reply. It travels in the post's
            props so a retry after a crash can find the reply it already
            published instead of posting a second one.
        artifacts: Ordered; the plugin renders them in this order.
        file_ids: Native Mattermost attachments (generated images).
    """

    version: Literal[1] = ENVELOPE_VERSION
    turn_id: str = ""
    artifacts: List[Artifact] = Field(default_factory=list)
    file_ids: List[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """Whether this reply is plain text after all.

        Returns:
            bool: True when nothing was staged.
        """
        return not (self.artifacts or self.file_ids)

    def to_props(self) -> Dict[str, Any]:
        """Build the post props for this envelope.

        Returns:
            dict: Props carrying the version and the ordered references.
        """
        props: Dict[str, Any] = {
            "sf_envelope_version": self.version,
            "sf_artifacts": [a.to_reference() for a in self.artifacts[:MAX_ARTIFACTS_PER_REPLY]],
        }
        if self.turn_id:
            props["sf_turn_id"] = self.turn_id
        return props
