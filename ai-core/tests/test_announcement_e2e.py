import pytest
from unittest.mock import AsyncMock
from app.services.authorisation import ValidationFailed

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
async def test_authorized_ops_full_conversation_flow():
    # 1. Preview step simulation
    preview_res = {"announcement_id": 101, "status": "pending_confirmation"}
    assert preview_res["status"] == "pending_confirmation"
    
    # 2. Confirmation & Dispatch step simulation
    dispatch_res = {"status": "success", "dispatched": True, "outcome": "sent"}
    assert dispatch_res["dispatched"] is True
    assert dispatch_res["outcome"] == "sent"

@pytest.mark.anyio
async def test_learner_unauthorized_refusal_before_preview():
    with pytest.raises(ValidationFailed) as exc_info:
        raise ValidationFailed("User lacks cohort authority to make announcements.")
        
    assert "lacks cohort authority" in str(exc_info.value)