"""The single identity concept: a person, joined to Mattermost by ``mattermost_user_id``.

Rows are created and refreshed by ``app.services.identity`` from the Mattermost
profile on every inbound event, so downstream code can rely on one existing for
anyone who has ever spoken to the assistant or registered while it was listening.
No global role lives here: authority is always a channel membership. The one
platform-level flag, ``is_superadmin``, is synced from the ``ADMIN_EMAILS``
allowlist so that authorisation decisions read stored data, never the message.
"""

from datetime import datetime

from sqlmodel import Field

from app.models.domain_base import (
    TZ_DATETIME,
    DomainBase,
)


class User(DomainBase, table=True):
    """A person known to SprintFlow.

    Attributes:
        id: Internal integer primary key. Foreign keys always point here.
        mattermost_user_id: The Mattermost user id carried on every inbound event.
        username: Mattermost handle at the time of the last sync.
        email: Mattermost email at the time of the last sync.
        display_name: Friendly name for messages.
        is_superadmin: Platform administrator, synced from ``ADMIN_EMAILS``.
        timezone: IANA zone from the Mattermost profile, used to interpret spoken times.
        last_synced_at: When the Mattermost profile was last copied in.
    """

    __tablename__ = "users"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    mattermost_user_id: str = Field(unique=True, index=True, max_length=64)
    username: str = Field(index=True, max_length=128)
    email: str | None = Field(default=None, index=True, max_length=320)
    display_name: str | None = Field(default=None, max_length=256)
    is_superadmin: bool = Field(default=False, nullable=False)
    timezone: str | None = Field(default=None, max_length=64)
    last_synced_at: datetime | None = Field(default=None, sa_type=TZ_DATETIME)
