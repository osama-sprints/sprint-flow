# Ceremony Scheduling: Technical Notes

Here is a quick breakdown of how I approached the ceremony scheduling tool and the reasoning behind the code.

### 1. Handling Time and Preventing Silent Guessing
The biggest issue with letting an LLM handle scheduling is that it likes to guess. If a user says "tomorrow at 2", the LLM might silently assume they mean 2:00 PM UTC and just book it. 

To fix this, I completely removed the time validation from the LLM's prompt and moved it to a strict Python function (`is_time_correct`). I used the `dateparser` library to read the time, but the tool will immediately reject the input if it doesn't clearly contain two things:
1. A proper timezone.
2. An explicit AM/PM indicator or a 24-hour format (13-23).

If the input is ambiguous, the Python tool throws a `SYSTEM REJECTION` string back to the LLM. This forces the agent to stop and explicitly ask the user for clarification before doing anything else.

### 2. Database Storage
For storage, once the time passes the validation checks, the code converts it to a UTC datetime object and formats it to a standard ISO string. This makes sure PostgreSQL gets a clean, timezone-aware timestamp, which will prevent a lot of bugs when we eventually build the reminder and reporting features.

---

### Future Improvements
While the current setup works well for preventing hallucinations, I was thinking about a couple of ways we could make the architecture a bit more robust for production:

* **Smart Slot Suggestions:** Right now, if a user picks a time that is already booked, the system just rejects it and waits for them to guess another time. We could update the tool to query the database for the day's existing meetings, figure out what 30-minute slots are still open, and pass those back to the LLM. That way, the bot can say something like, "2 PM is booked, but I can do 2:30 or 4:00."

* **Human-In-The-Loop** Relying on the LLM to understand when a user finally says "Yes, book it" still feels a bit risky. A safer approach would be using LangGraph's `interrupt()` feature. We could freeze the graph right before saving the data and wait for the user to click a physical "Approve" button in the Mattermost UI. It takes the final database transaction entirely out of the LLM's control.