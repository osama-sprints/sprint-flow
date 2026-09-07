import pytest
from app.core.langgraph.tools.mattermost_admin import current_requester
from app.core.langgraph.tools.ceremony_scheduler import schedule_ceremony, _get_requester

print("Direct _get_requester:", _get_requester())

token = current_requester.set({"channel_id": "chan_123", "team_id": "team_123"})
print("After set _get_requester:", _get_requester())

try:
    print("Calling invoke...")
    res = schedule_ceremony.invoke({
        "ceremony_type": "standup",
        "raw_time": "tomorrow at 10 AM UTC",
        "organizer_id": "admin_user",
        "agenda": "Weekly sync",
    })
    print("RES:", res)
except Exception as e:
    import traceback
    traceback.print_exc()
