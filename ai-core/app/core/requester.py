"""Who is asking — carried out-of-band so the model can never supply or alter it.

The conversation layer resolves the requester from the Mattermost event's
``user_id`` (never from message text), syncs the profile into ``users``, and
binds a ``RequesterContext`` to ``current_requester`` before the graph runs.
Tools read it from the ContextVar; it is never a tool argument.

``channel_roles`` is a snapshot for cheap routing decisions only. Authorisation
re-reads the database at decision time (see ``app.services.authorisation``).
"""

from contextvars import ContextVar
from dataclasses import (
    dataclass,
    field,
)
from types import MappingProxyType
from typing import Mapping

from app.models.enums import CHANNEL_ADMIN_ROLES


@dataclass(frozen=True)
class RequesterContext:
    """Identity and situation of the person behind the current turn.

    Attributes:
        mattermost_user_id: The id on the Mattermost event. Always present.
        username: Mattermost handle, when the transport supplied it.
        email: Email read back from the Mattermost profile, lower-cased.
        channel_id: Where the message arrived.
        team_id: Where the message arrived.
        channel_type: O public, P private, D direct, G group.
        user_id: ``users.id`` after the profile sync; None if the sync failed.
        is_superadmin: Whether the email is on the ``ADMIN_EMAILS`` allowlist.
        timezone: IANA zone from the Mattermost profile, or None.
        channel_roles: ``{channel_id: role_key}`` for active roles (routing hint).
    """

    mattermost_user_id: str
    username: str = ""
    email: str | None = None
    channel_id: str = ""
    team_id: str = ""
    channel_type: str = ""
    user_id: int | None = None
    is_superadmin: bool = False
    timezone: str | None = None
    channel_roles: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def is_admin(self) -> bool:
        """Legacy name kept for the workspace-administration tools.

        Returns:
            bool: Same as ``is_superadmin``.
        """
        return self.is_superadmin

    @property
    def is_direct_message(self) -> bool:
        """Whether the turn arrived by direct message.

        Returns:
            bool: True for channel type ``D``.
        """
        return self.channel_type == "D"

    def has_channel_authority(self, channel_id: str) -> bool:
        """Routing hint: may this person administer the channel, per the snapshot?

        Args:
            channel_id: The channel.

        Returns:
            bool: True for superadmins and holders of a ``CHANNEL_ADMIN_ROLES`` role there.
        """
        if self.is_superadmin:
            return True
        return self.channel_roles.get(channel_id) in CHANNEL_ADMIN_ROLES

    def has_any_channel_authority(self) -> bool:
        """Routing hint: may this person administer at least one channel, per the snapshot?

        Returns:
            bool: True for superadmins and anyone holding an admin role somewhere.
        """
        if self.is_superadmin:
            return True
        return any(role in CHANNEL_ADMIN_ROLES for role in self.channel_roles.values())

    def describe(self) -> str:
        """One-line description for logs and prompts (no secrets).

        Returns:
            str: e.g. ``@alice (superadmin) roles={'town-square': 'scrum_master'}``.
        """
        flags = " (superadmin)" if self.is_superadmin else ""
        return f"@{self.username or self.mattermost_user_id}{flags} roles={dict(self.channel_roles)}"


# Set per turn by the conversation layer. Never populated from model output.
current_requester: ContextVar[RequesterContext | None] = ContextVar("current_requester", default=None)
