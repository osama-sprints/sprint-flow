import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.models.sprint import Sprint
from app.services.announcements import create_announcement_preview


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_create_announcement_preview_success():
    session = AsyncMock()
    session.add = MagicMock()
    
    mock_sprint = Sprint(id=10, name="Sprint 2026")
    
    added_objects = []
    def fake_add(obj):
        obj.id = 1
        added_objects.append(obj)
        
    session.add.side_effect = fake_add
    session.commit = AsyncMock()
    
    async def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1

    session.refresh.side_effect = fake_refresh

    sample_audience = [{"user_id": 1, "username": "alice", "role": "student"}]

    with patch("app.services.domain.sprints.get_sprint", new_callable=AsyncMock) as mock_get_sprint:
        mock_get_sprint.return_value = mock_sprint

        result = await create_announcement_preview(
            session=session,
            cohort_id=10,
            raw_text="Hello World",
            delivery_mode="channel_and_dm",
            resolved_channel="town-square",
            resolved_audience=sample_audience,
            created_by_user_id=99
        )

        assert result["audit_id"] == 1
        assert result["mattermost_post_id"] is None
        preview = result["preview"]
        assert preview["final_text"] == "Hello World"
        assert preview["cohort_name"] == "Sprint 2026"
        assert preview["resolved_channel"] == "town-square"
        assert preview["resolved_audience"] == sample_audience
        assert preview["delivery_mode"] == "channel_and_dm"

        assert len(added_objects) == 1
        audit_row = added_objects[0]
        assert audit_row.mattermost_post_id is None
        assert str(audit_row.outcome).lower() == "pending"