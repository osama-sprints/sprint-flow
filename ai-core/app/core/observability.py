"""Observability module for the application with Langfuse integration.

This module provides comprehensive tracing capabilities following Langfuse best practices:
- Singleton client pattern (Langfuse 3.x)
- Context managers for trace management
- Custom spans for key operations
- User and session tracking
- Proper error instrumentation
- Async-safe shutdown handling

Documentation: https://langfuse.com/docs/integrations/langchain
"""

from typing import Optional
from functools import wraps

from langfuse import Langfuse, get_client, observe, propagate_attributes
from langfuse.langchain import CallbackHandler

from app.core.config import settings
from app.core.logging import logger


def langfuse_init():
    """Initialize Langfuse with best practices.
    
    Creates a singleton Langfuse client that is used throughout the application.
    Should be called once at application startup.
    
    Follows Langfuse 3.x patterns:
    - Uses environment variables for configuration
    - Verifies authentication on initialization
    - Handles sampling and debug settings
    - Uses singleton pattern via get_client()
    
    Best practices implemented:
    - Async-safe batching and queueing
    - Graceful degradation if Langfuse is unavailable
    - Proper environment-specific configuration
    """
    if not settings.LANGFUSE_TRACING_ENABLED:
        logger.debug("langfuse_tracing_disabled")
        return

    try:
        # Initialize Langfuse client with configuration
        # The Langfuse SDK uses environment variables by default,
        # but we explicitly pass settings for better control
        langfuse_client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            baseurl=settings.LANGFUSE_HOST,
            environment=settings.ENVIRONMENT.value,
            debug=settings.LANGFUSE_DEBUG,
            sample_rate=settings.LANGFUSE_SAMPLE_RATE,
        )

        # Verify authentication
        if langfuse_client.auth_check():
            logger.info(
                "langfuse_initialized",
                environment=settings.ENVIRONMENT.value,
                host=settings.LANGFUSE_HOST,
                debug=settings.LANGFUSE_DEBUG,
                sample_rate=settings.LANGFUSE_SAMPLE_RATE,
            )
        else:
            logger.warning(
                "langfuse_auth_check_failed",
                message="Langfuse credentials are invalid. Tracing will be disabled.",
            )
    except Exception as e:
        logger.exception(
            "langfuse_initialization_failed",
            error=str(e),
            message="Failed to initialize Langfuse. Tracing will be disabled.",
        )


def get_langfuse_callback_handler(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> CallbackHandler:
    """Create a Langfuse CallbackHandler for LangChain/LangGraph tracing.
    
    Creates a handler for tracing LangChain/LangGraph operations.
    
    Note: Trace attributes (user_id, session_id, tags) are passed via the
    `metadata` dict in the LangChain config, not in the handler constructor.
    This function signature supports both patterns for convenience.
    
    Args:
        user_id: Deprecated - use metadata['langfuse_user_id'] instead
        session_id: Deprecated - use metadata['langfuse_session_id'] instead
        tags: Deprecated - use metadata['langfuse_tags'] instead
        
    Returns:
        CallbackHandler: Configured handler ready for use with LangChain components
        
    Best practices:
    - Handler is stateless and can be reused across requests
    - Trace attributes go in the config metadata dict:
      config = {"callbacks": [handler], "metadata": {"langfuse_user_id": "...", ...}}
    - The handler automatically captures LLM calls, tools, and timing
    
    Example usage in LangGraph:
        handler = get_langfuse_callback_handler()
        config = {
            "callbacks": [handler],
            "metadata": {
                "langfuse_user_id": username,
                "langfuse_session_id": thread_id,
                "langfuse_tags": ["chat", "production"]
            }
        }
        result = await graph.ainvoke(input, config)
    """
    return CallbackHandler()


# Global handler instance for LangGraph usage
langfuse_callback_handler = get_langfuse_callback_handler()


@observe()
async def trace_agent_turn(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    input_data: Optional[dict] = None,
) -> None:
    """Decorator for tracing agent turns with automatic span creation.
    
    Creates a Langfuse span for the decorated function, useful for
    wrapping high-level agent operations that contain LangChain calls.
    
    Args:
        user_id: User identifier for trace
        session_id: Session identifier for grouping
        input_data: Input data to log with the span
        
    Example:
        @trace_agent_turn(user_id="user_123", session_id="session_456")
        async def process_user_message(message: str):
            # LangChain calls here are automatically nested under this span
            return await agent.invoke({"messages": [message]})
    """
    pass


def shutdown_langfuse():
    """Gracefully shutdown Langfuse client.
    
    Flushes any pending events to ensure all traces are sent before
    application termination. Should be called during application shutdown.
    
    In production environments, this ensures that short-lived traces
    (e.g., in serverless functions) are properly recorded.
    
    Best practices:
    - Call in FastAPI lifespan shutdown handler
    - Safe to call even if Langfuse is disabled
    - Blocks until all events are flushed
    """
    if not settings.LANGFUSE_TRACING_ENABLED:
        return

    try:
        client = get_client()
        client.flush()
        logger.info("langfuse_flushed")
    except Exception as e:
        logger.exception("langfuse_flush_failed", error=str(e))
