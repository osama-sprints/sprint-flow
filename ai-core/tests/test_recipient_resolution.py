import pytest
from unittest.mock import AsyncMock, patch
from app.models.user import User
from app.services.authorisation import ValidationFailed
from app.services.announcements import resolve_recipients_by_role, resolve_recipients_by_usernames

@pytest.fixture
def anyio_backend():
    return 'asyncio'

# --- Tests for Role Path ---

@pytest.mark.anyio
async def test_resolve_role_path_zero_members():
    """Role path returning 0 members"""
    session = AsyncMock()
    with patch("app.services.announcements.sprint_repo.get_sprint", new_callable=AsyncMock) as mock_cohort, \
         patch("app.services.announcements.sprint_repo.get_sprint_members_by_role", new_callable=AsyncMock) as mock_members:
        
        mock_cohort.return_value = AsyncMock(id=10)
        mock_members.return_value = []
        
        result = await resolve_recipients_by_role(session, 10, "mentor")
        assert result == []


@pytest.mark.anyio
async def test_resolve_role_path_one_member():
    """Role path returning 1 member"""
    session = AsyncMock()
    user = User(id=1, username="mentor_john")
    
    with patch("app.services.announcements.sprint_repo.get_sprint", new_callable=AsyncMock) as mock_cohort, \
         patch("app.services.announcements.sprint_repo.get_sprint_members_by_role", new_callable=AsyncMock) as mock_members:
        
        mock_cohort.return_value = AsyncMock(id=10)
        mock_members.return_value = [user]
        
        result = await resolve_recipients_by_role(session, 10, "mentor")
        assert len(result) == 1
        assert result[0]["username"] == "mentor_john"


@pytest.mark.anyio
async def test_resolve_role_path_many_members():
    """Role path returning multiple members"""
    session = AsyncMock()
    users = [User(id=1, username="student_1"), User(id=2, username="student_2")]
    
    with patch("app.services.announcements.sprint_repo.get_sprint", new_callable=AsyncMock) as mock_cohort, \
         patch("app.services.announcements.sprint_repo.get_sprint_members_by_role", new_callable=AsyncMock) as mock_members:
        
        mock_cohort.return_value = AsyncMock(id=10)
        mock_members.return_value = users
        
        result = await resolve_recipients_by_role(session, 10, "student")
        assert len(result) == 2


# --- Tests for Username Path ---

@pytest.mark.anyio
async def test_resolve_username_path_valid_member():
    """Username path with a valid cohort member"""
    session = AsyncMock()
    user = User(id=1, username="alice")
    
    with patch("app.services.announcements.sprint_repo.get_sprint", new_callable=AsyncMock) as mock_cohort, \
         patch("app.services.announcements.identity_repo.get_user_by_username", new_callable=AsyncMock) as mock_user, \
         patch("app.services.announcements.sprint_repo.is_user_in_sprint", new_callable=AsyncMock) as mock_member:
        
        mock_cohort.return_value = AsyncMock(id=10)
        mock_user.return_value = user
        mock_member.return_value = True
        
        result = await resolve_recipients_by_usernames(session, 10, ["alice"])
        assert len(result) == 1
        assert result[0]["username"] == "alice"


@pytest.mark.anyio
async def test_resolve_username_path_non_member_raises():
    """Username path with a real user who is NOT a cohort member -> raises ValidationFailed"""
    session = AsyncMock()
    user = User(id=2, username="bob_outside")
    
    with patch("app.services.announcements.sprint_repo.get_sprint", new_callable=AsyncMock) as mock_cohort, \
         patch("app.services.announcements.identity_repo.get_user_by_username", new_callable=AsyncMock) as mock_user, \
         patch("app.services.announcements.sprint_repo.is_user_in_sprint", new_callable=AsyncMock) as mock_member:
        
        mock_cohort.return_value = AsyncMock(id=10)
        mock_user.return_value = user
        mock_member.return_value = False
        
        with pytest.raises(ValidationFailed):
            await resolve_recipients_by_usernames(session, 10, ["bob_outside"])


@pytest.mark.anyio
async def test_resolve_username_path_nonexistent_user_raises():
    """Username path with non-existent username -> raises ValidationFailed"""
    session = AsyncMock()
    
    with patch("app.services.announcements.sprint_repo.get_sprint", new_callable=AsyncMock) as mock_cohort, \
         patch("app.services.announcements.identity_repo.get_user_by_username", new_callable=AsyncMock) as mock_user:
        
        mock_cohort.return_value = AsyncMock(id=10)
        mock_user.return_value = None
        
        with pytest.raises(ValidationFailed):
            await resolve_recipients_by_usernames(session, 10, ["ghost_user"])