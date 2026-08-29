"""The shared LangGraph agent instance.

One instance for the whole process: it owns a Postgres connection pool and the
compiled graph, both of which are pre-warmed at startup. Constructing a second
one would open a second pool and duplicate the checkpointer setup.

This lived in the REST chatbot router until that router was removed. SprintFlow
reaches the agent through the Mattermost transports, not over HTTP, so the
singleton belongs in the service layer rather than in an API module.
"""

from app.core.langgraph.graph import LangGraphAgent

agent = LangGraphAgent()
