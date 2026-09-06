"""Service layer.

Import the module you need directly (``app.services.database``,
``app.services.llm``, ``app.services.domain.cohorts`` ...). This package
deliberately re-exports nothing, so importing the database service for a
seed or verification script does not also construct the LLM clients.
"""
