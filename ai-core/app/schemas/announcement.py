from pydantic import BaseModel
from typing import List


class RecipientAudience(BaseModel):
    resolution_type: str
    cohort_id: int
    resolved_channel_id: str
    target_identifier: str
    recipient_user_ids: List[int]
    recipient_count: int
