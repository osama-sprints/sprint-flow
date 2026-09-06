"""Internal artifact reads for the Mattermost server plugin.

The browser cannot reach ai-core: it is on the internal Docker network and the
page has no credential for it. So the plugin's Go component is the only client
of this endpoint, and the split of responsibility is deliberate:

* **this endpoint** authenticates the CALLER (the plugin, by shared secret) and
  returns the artifact together with the channel and post it belongs to;
* **the plugin** authenticates the VIEWER (Mattermost's own session) and checks
  that this viewer may read that channel and post before returning anything.

Neither half is sufficient alone, and the bot's own visibility is never used as
proof of a viewer's access — the bot is a member of channels its readers may not
be. Artifact ids are identifiers, not capabilities: knowing one gets you
nothing without a session that can read the post it was published in.
"""

import hmac
from typing import (
    Any,
    Dict,
)

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    status,
)

from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.services.domain import rich_artifacts as store

router = APIRouter()


def _verify_plugin(token: str | None) -> None:
    """Check the shared secret the server plugin presents.

    Args:
        token: Value of the ``X-SprintFlow-Plugin-Token`` header.

    Raises:
        HTTPException: 503 when no secret is configured — refusing is safer than
            serving artifacts to anything that can reach the port — and 401 when
            the secret does not match.
    """
    expected = settings.RICH_MEDIA_PLUGIN_TOKEN
    if not expected:
        logger.error("artifact_plugin_token_not_configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="artifact access not configured")

    if not token or not hmac.compare_digest(token, expected):
        logger.warning("artifact_plugin_token_mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid plugin token")


@router.get("/{artifact_id}")
@limiter.limit(settings.RATE_LIMIT_ENDPOINTS["artifacts"][0])
async def read_artifact(
    request: Request,
    artifact_id: str,
    x_sprintflow_plugin_token: str | None = Header(default=None),
) -> Dict[str, Any]:
    """Return one artifact, with the channel and post that authorise it.

    Args:
        request: The incoming request, used for rate limiting.
        artifact_id: The artifact to read.
        x_sprintflow_plugin_token: Shared secret proving the caller is the plugin.

    Returns:
        dict: The artifact, including ``channel_id`` and ``post_id`` so the
        caller can authorise the viewer against them.

    Raises:
        HTTPException: 401 on a bad token, 404 when the artifact is unknown.
    """
    _verify_plugin(x_sprintflow_plugin_token)

    artifact = await store.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")

    logger.info("artifact_read", artifact_id=artifact_id, kind=artifact.kind, status=artifact.status)
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "schema_version": artifact.schema_version,
        "revision": artifact.revision,
        "status": artifact.status,
        "title": artifact.title,
        "description": artifact.description,
        "error": artifact.error,
        "content": artifact.content,
        # The authorisation anchors. The caller must check the viewer against
        # these before returning the content to a browser.
        "channel_id": artifact.channel_id,
        "post_id": artifact.post_id,
        "root_id": artifact.root_id,
    }
