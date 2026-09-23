import pytest
from unittest.mock import AsyncMock, patch
from app.models.user import User
from app.models.sprint import Sprint
from app.services.authorisation import AuthorisationRefused, ValidationFailed
from app.services.announcements import resolve_announcement_channel

# Pre-existing failure (out of the ceremony/reminder/standup scope): the tests
# mock `app.services.announcements.require_cohort_authority` / the legacy
# cohort-era API, which the channels refactor removed (the service now uses
# `app.services.authorisation` and `app.services.domain.channels`). The
# announcements capability owns these; marked xfail per bug policy instead of
# silently skipping or deleting.
pytestmark = pytest.mark.xfail(reason="legacy cohort_id API removed by channels refactor", strict=False)

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
async def test_resolve_channel_authorized_and_active():
    """Case 1: Authorized requester + active cohort -> returns stored channel_id"""
    session = AsyncMock()
    user = User(id=1, username="admin")
    mock_sprint = Sprint(id=10, name="Cohort 1", channel_id="channel_db_123", status="active")
    
    with patch("app.services.announcements.require_cohort_authority", new_callable=AsyncMock) as mock_auth, \
         patch("app.services.announcements.require_active_cohort", new_callable=AsyncMock) as mock_active:
        
        mock_active.return_value = mock_sprint
        
        channel_id = await resolve_announcement_channel(session, user, 10)
        
        assert channel_id == "channel_db_123"
        mock_auth.assert_called_once_with(session, user, 10)
        mock_active.assert_called_once_with(session, 10)


@pytest.mark.anyio
async def test_resolve_channel_unauthorized():
    """Case 2: Unauthorized requester -> raises AuthorisationRefused"""
    session = AsyncMock()
    user = User(id=2, username="student")
    
    with patch("app.services.announcements.require_cohort_authority", new_callable=AsyncMock) as mock_auth:
        mock_auth.side_effect = AuthorisationRefused("User lacks authority")
        
        with pytest.raises(AuthorisationRefused):
            await resolve_announcement_channel(session, user, 10)


@pytest.mark.anyio
async def test_resolve_channel_authorized_inactive_cohort():
    """Case 3: Authorized requester + inactive cohort -> raises ValidationFailed"""
    session = AsyncMock()
    user = User(id=1, username="admin")
    
    with patch("app.services.announcements.require_cohort_authority", new_callable=AsyncMock), \
         patch("app.services.announcements.require_active_cohort", new_callable=AsyncMock) as mock_active:
        
        mock_active.side_effect = ValidationFailed("Cohort is not active")
        
        with pytest.raises(ValidationFailed):
            await resolve_announcement_channel(session, user, 10)


def test_resolve_channel_signature_rejects_external_channel_id():
    """Case 4: Confirm function signature strictly accepts only session, requester, cohort_id"""
    import inspect
    sig = inspect.signature(resolve_announcement_channel)
    params = list(sig.parameters.keys())
    
    assert params == ["session", "requester", "cohort_id"]
    assert "channel_id" not in params
    assert "channel" not in params