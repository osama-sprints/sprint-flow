"""Application configuration management.

This module handles environment-specific configuration loading, parsing, and management
for the application. It includes environment detection, .env file loading, and
configuration value parsing.
"""

import os
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv


WEAK_JWT_SECRET_KEYS = {
    "",
    "changeme",
    "change_me",
    "change_me_to_a_random_32_plus_char_string",
    "supersecretkeythatshouldbechangedforproduction",
    "your-jwt-secret-key",
}


# Define environment types
class Environment(str, Enum):
    """Application environment types.

    Defines the possible environments the application can run in:
    development, staging, production, and test.
    """

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


# Determine environment
def get_environment() -> Environment:
    """Get the current environment.

    Returns:
        Environment: The current environment (development, staging, production, or test)
    """
    match os.getenv("APP_ENV", "development").lower():
        case "production" | "prod":
            return Environment.PRODUCTION
        case "staging" | "stage":
            return Environment.STAGING
        case "test":
            return Environment.TEST
        case _:
            return Environment.DEVELOPMENT


# Load appropriate .env file based on environment
def load_env_file():
    """Load environment-specific .env file."""
    env = get_environment()
    print(f"Loading environment: {env}")
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

    # Define env files in priority order
    env_files = [
        os.path.join(base_dir, f".env.{env.value}.local"),
        os.path.join(base_dir, f".env.{env.value}"),
        os.path.join(base_dir, ".env.local"),
        os.path.join(base_dir, ".env"),
    ]

    # Load the first env file that exists
    for env_file in env_files:
        if os.path.isfile(env_file):
            load_dotenv(dotenv_path=env_file)
            print(f"Loaded environment from {env_file}")
            return env_file

    # Fall back to default if no env file found
    return None


ENV_FILE = load_env_file()


# Parse list values from environment variables
def parse_list_from_env(env_key, default=None):
    """Parse a comma-separated list from an environment variable."""
    value = os.getenv(env_key)
    if not value:
        return default or []

    # Remove quotes if they exist
    value = value.strip("\"'")
    # Handle single value case
    if "," not in value:
        return [value]
    # Split comma-separated values
    return [item.strip() for item in value.split(",") if item.strip()]


# Parse dict of lists from environment variables with prefix
def parse_dict_of_lists_from_env(prefix, default_dict=None):
    """Parse dictionary of lists from environment variables with a common prefix."""
    result = default_dict or {}

    # Look for all env vars with the given prefix
    for key, value in os.environ.items():
        if key.startswith(prefix):
            endpoint = key[len(prefix) :].lower()  # Extract endpoint name
            # Parse the values for this endpoint
            if value:
                value = value.strip("\"'")
                if "," in value:
                    result[endpoint] = [item.strip() for item in value.split(",") if item.strip()]
                else:
                    result[endpoint] = [value]

    return result


class Settings:
    """Application settings without using pydantic."""

    def __init__(self):
        """Initialize application settings from environment variables.

        Loads and sets all configuration values from environment variables,
        with appropriate defaults for each setting. Also applies
        environment-specific overrides based on the current environment.
        """
        # Set the environment
        self.ENVIRONMENT = get_environment()

        # Application Settings
        self.PROJECT_NAME = os.getenv("PROJECT_NAME", "FastAPI LangGraph Template")
        self.VERSION = os.getenv("VERSION", "1.0.0")
        self.DESCRIPTION = os.getenv(
            "DESCRIPTION", "A production-ready FastAPI template with LangGraph and Langfuse integration"
        )
        self.API_V1_STR = os.getenv("API_V1_STR", "/api/v1")
        self.DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "t", "yes")

        # CORS Settings
        self.ALLOWED_ORIGINS = parse_list_from_env("ALLOWED_ORIGINS", ["*"])

        # Langfuse Configuration
        self.LANGFUSE_TRACING_ENABLED = os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in (
            "true",
            "1",
            "t",
            "yes",
        )
        self.LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self.LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
        # Support both LANGFUSE_HOST and LANGFUSE_BASE_URL for backwards compatibility
        self.LANGFUSE_HOST = os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        self.LANGFUSE_DEBUG = os.getenv("LANGFUSE_DEBUG", "false").lower() in ("true", "1", "t", "yes")
        # Sample rate for traces (1.0 = 100%, reduce for high-volume apps to lower costs)
        try:
            self.LANGFUSE_SAMPLE_RATE = float(os.getenv("LANGFUSE_SAMPLE_RATE", "1.0"))
        except ValueError:
            self.LANGFUSE_SAMPLE_RATE = 1.0

        # LLM Configuration — ALL traffic goes through the LiteLLM proxy.
        # There are no direct provider SDKs or provider keys in this stack:
        # OPENAI_API_KEY holds the LiteLLM virtual key and OPENAI_BASE_URL the
        # proxy's OpenAI-compatible endpoint. The names keep the OpenAI SDK and
        # mem0 working unmodified; the values are LiteLLM's.
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
        self.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
        # LiteLLM model-group names, e.g. "gemini/gemini-3.5-flash".
        self.DEFAULT_LLM_MODEL = os.getenv("DEFAULT_LLM_MODEL", "gemini/gemini-3.5-flash")
        # Circular fallback chain used when the default model errors out.
        self.LLM_FALLBACK_MODELS = parse_list_from_env("LLM_FALLBACK_MODELS", [])
        self.SESSION_NAMING_ENABLED = os.getenv("SESSION_NAMING_ENABLED", "true").lower() == "true"
        self.DEFAULT_LLM_TEMPERATURE = float(os.getenv("DEFAULT_LLM_TEMPERATURE", "0.2"))
        # Cap on the model's OUTPUT for a single reply.
        self.MAX_TOKENS = int(os.getenv("MAX_TOKENS", "2000"))
        # Budget for how much prior conversation is replayed to the model.
        # Kept separate from MAX_TOKENS: these were one setting, so raising the
        # context window also silently raised the maximum reply length.
        self.MAX_HISTORY_TOKENS = int(os.getenv("MAX_HISTORY_TOKENS", "6000"))
        self.MAX_LLM_CALL_RETRIES = int(os.getenv("MAX_LLM_CALL_RETRIES", "3"))
        self.LLM_TOTAL_TIMEOUT = int(os.getenv("LLM_TOTAL_TIMEOUT", "60"))

        # Long term memory Configuration
        self.LONG_TERM_MEMORY_MODEL = os.getenv("LONG_TERM_MEMORY_MODEL", "gemini/gemini-3.5-flash")
        self.LONG_TERM_MEMORY_EMBEDDER_MODEL = os.getenv(
            "LONG_TERM_MEMORY_EMBEDDER_MODEL", "gemini/gemini-embedding-001"
        )
        self.LONG_TERM_MEMORY_COLLECTION_NAME = os.getenv("LONG_TERM_MEMORY_COLLECTION_NAME", "longterm_memory")

        # Qdrant holds the long-term memory vectors. It is deliberately external
        # and not part of docker-compose: keeping the vector store out of the
        # Postgres cluster is what stops a vector-side problem from ever
        # touching Mattermost's database.
        #
        # QDRANT_URL is required for memory to run. mem0's Qdrant config falls
        # back to a LOCAL on-disk store at /tmp/qdrant when no url/host is given,
        # which would silently write memories into the container's temp dir and
        # lose them on restart — so memory.py refuses to start without it rather
        # than appearing to work.
        self.QDRANT_URL = os.getenv("QDRANT_URL", "")
        self.QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
        # Dimensions of the embedding model, and therefore of the collection.
        # Must match what the embedder returns; mem0 sends this as `dimensions`
        # on every embedding request.
        self.QDRANT_EMBEDDING_DIMS = int(os.getenv("QDRANT_EMBEDDING_DIMS", "1536"))
        # JWT Configuration
        self.JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
        self.JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
        self.JWT_ACCESS_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_DAYS", "30"))
        self.validate_jwt_secret_key()

        # Logging Configuration
        self.LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
        self.LOG_FORMAT = os.getenv("LOG_FORMAT", "json")  # "json" or "console"

        # Profiling Configuration (DEBUG only)
        self.PROFILING_DIR = Path(os.getenv("PROFILING_DIR", "/tmp/fastapi_profiles"))
        self.PROFILING_THRESHOLD_SECONDS = float(os.getenv("PROFILING_THRESHOLD_SECONDS", "2.0"))

        # Postgres Configuration
        self.POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
        self.POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
        self.POSTGRES_DB = os.getenv("POSTGRES_DB", "food_order_db")
        self.POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
        self.POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
        self.POSTGRES_POOL_SIZE = int(os.getenv("POSTGRES_POOL_SIZE", "20"))
        self.POSTGRES_MAX_OVERFLOW = int(os.getenv("POSTGRES_MAX_OVERFLOW", "10"))
        self.CHECKPOINT_TABLES = ["checkpoint_blobs", "checkpoint_writes", "checkpoints"]

        # Valkey/Redis Cache Configuration (optional — if host is set, caching is enabled)
        self.VALKEY_HOST = os.getenv("VALKEY_HOST", "")
        self.VALKEY_PORT = int(os.getenv("VALKEY_PORT", "6379"))
        self.VALKEY_DB = int(os.getenv("VALKEY_DB", "0"))
        self.VALKEY_PASSWORD = os.getenv("VALKEY_PASSWORD", "")
        self.VALKEY_MAX_CONNECTIONS = int(os.getenv("VALKEY_MAX_CONNECTIONS", "20"))
        self.CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

        # Rate Limiting Configuration
        self.RATE_LIMIT_DEFAULT = parse_list_from_env("RATE_LIMIT_DEFAULT", ["200 per day", "50 per hour"])

        # Rate limit endpoints defaults
        default_endpoints = {
            "chat": ["30 per minute"],
            "chat_stream": ["20 per minute"],
            "messages": ["50 per minute"],
            "register": ["10 per hour"],
            "login": ["20 per minute"],
            "root": ["10 per minute"],
            "health": ["20 per minute"],
            # Mattermost outgoing webhook: one call per triggering message, so
            # this bounds a busy channel rather than a single user.
            "mattermost_webhook": ["120 per minute"],
        }

        # Update rate limit endpoints from environment variables
        self.RATE_LIMIT_ENDPOINTS = default_endpoints.copy()
        for endpoint in default_endpoints:
            env_key = f"RATE_LIMIT_{endpoint.upper()}"
            value = parse_list_from_env(env_key)
            if value:
                self.RATE_LIMIT_ENDPOINTS[endpoint] = value

        # Evaluation Configuration
        self.EVALUATION_LLM = os.getenv("EVALUATION_LLM", "gemini/gemini-3.5-flash")
        # Never api.openai.com — evaluations go through the proxy like everything else.
        self.EVALUATION_BASE_URL = os.getenv("EVALUATION_BASE_URL", self.OPENAI_BASE_URL)
        self.EVALUATION_API_KEY = os.getenv("EVALUATION_API_KEY", self.OPENAI_API_KEY)
        self.EVALUATION_SLEEP_TIME = int(os.getenv("EVALUATION_SLEEP_TIME", "10"))

        # Mattermost Integration
        # Reached over the internal Docker network, so the default is the
        # compose service name rather than localhost.
        self.MATTERMOST_URL = os.getenv("MATTERMOST_URL", "http://mattermost:8065")
        # Personal Access Token of the bot account; used for every REST call.
        self.MATTERMOST_BOT_TOKEN = os.getenv("MATTERMOST_BOT_TOKEN", "")
        # Shared secret Mattermost puts in the outgoing-webhook request body.
        self.MATTERMOST_OUTGOING_WEBHOOK_TOKEN = os.getenv("MATTERMOST_OUTGOING_WEBHOOK_TOKEN", "")
        self.MATTERMOST_BOT_USERNAME = os.getenv("MATTERMOST_BOT_USERNAME", "sprintflow-assistant")
        self.MATTERMOST_HTTP_TIMEOUT = float(os.getenv("MATTERMOST_HTTP_TIMEOUT", "30"))

        # WebSocket listener — the only way to receive direct messages, since
        # Mattermost never fires outgoing webhooks outside public channels.
        self.MATTERMOST_WS_ENABLED = os.getenv("MATTERMOST_WS_ENABLED", "true").lower() in (
            "true",
            "1",
            "t",
            "yes",
        )
        # Mattermost channel types the listener accepts: D = direct,
        # G = group message, P = private channel, O = public channel.
        #
        # "O" is included because thread continuity needs it — a follow-up
        # inside a thread carries no trigger word, so the outgoing webhook
        # never fires for it. Double replies are prevented by routing, not by
        # this filter: the listener skips any public post whose first word is a
        # webhook trigger word, because the webhook already owns that message.
        self.MATTERMOST_WS_CHANNEL_TYPES = parse_list_from_env("MATTERMOST_WS_CHANNEL_TYPES", ["D", "O"])

        # The trigger words configured on the Mattermost outgoing webhook. This
        # is the single source of truth: scripts/bootstrap_mattermost.sh reads
        # the same variable when creating the webhook, so the two cannot drift.
        # Mattermost matches trigger words against the FIRST WORD of a message
        # only, which is exactly the rule the listener mirrors when deciding
        # whether the webhook owns a given post.
        self.MATTERMOST_TRIGGER_WORDS = parse_list_from_env(
            "MATTERMOST_TRIGGER_WORDS", [f"@{self.MATTERMOST_BOT_USERNAME}", "!ask"]
        )

        # How long to remember that the bot participates in a thread. Only
        # positive results are cached, and participation never becomes false,
        # so this can be generous.
        self.MATTERMOST_THREAD_CACHE_TTL = int(os.getenv("MATTERMOST_THREAD_CACHE_TTL", "900"))

        # Automatic onboarding. Mattermost cannot auto-join a new account to a
        # named team by configuration — no setting does it — so the bot listens
        # for the server-wide `new_user` event and adds them over the REST API.
        # Channels then come free: ExperimentalDefaultChannels applies on join.
        self.MATTERMOST_AUTO_ONBOARD = os.getenv("MATTERMOST_AUTO_ONBOARD", "true").lower() in (
            "true",
            "1",
            "t",
            "yes",
        )
        self.MATTERMOST_DEFAULT_TEAM = os.getenv("MATTERMOST_DEFAULT_TEAM", "sprints-community")

        # Admin agent authorisation. An explicit allowlist rather than the
        # Mattermost system_admin role: privileged tool calls originate from
        # chat text, so the gate is deliberately held outside the workspace and
        # outside anything the model or a Mattermost misconfiguration can reach.
        self.ADMIN_EMAILS = [e.strip().lower() for e in parse_list_from_env("ADMIN_EMAILS", []) if e.strip()]

        # Apply environment-specific settings
        self.apply_environment_settings()

    def apply_environment_settings(self):
        """Apply environment-specific settings based on the current environment."""
        env_settings = {
            Environment.DEVELOPMENT: {
                "DEBUG": True,
                "LOG_LEVEL": "DEBUG",
                "LOG_FORMAT": "console",
                "RATE_LIMIT_DEFAULT": ["1000 per day", "200 per hour"],
            },
            Environment.STAGING: {
                "DEBUG": False,
                "LOG_LEVEL": "INFO",
                "RATE_LIMIT_DEFAULT": ["500 per day", "100 per hour"],
            },
            Environment.PRODUCTION: {
                "DEBUG": False,
                "LOG_LEVEL": "WARNING",
                "RATE_LIMIT_DEFAULT": ["200 per day", "50 per hour"],
            },
            Environment.TEST: {
                "DEBUG": True,
                "LOG_LEVEL": "DEBUG",
                "LOG_FORMAT": "console",
                "RATE_LIMIT_DEFAULT": ["1000 per day", "1000 per hour"],  # Relaxed for testing
            },
        }

        # Get settings for current environment
        current_env_settings = env_settings.get(self.ENVIRONMENT, {})

        # Apply settings if not explicitly set in environment variables
        for key, value in current_env_settings.items():
            env_var_name = key.upper()
            # Only override if environment variable wasn't explicitly set
            if env_var_name not in os.environ:
                setattr(self, key, value)

    def validate_jwt_secret_key(self):
        """Reject empty, short, or placeholder JWT secrets outside test runs."""
        if self.ENVIRONMENT == Environment.TEST:
            return

        secret = self.JWT_SECRET_KEY.strip()
        if len(secret) < 32:
            raise RuntimeError("JWT_SECRET_KEY must be at least 32 characters long")

        if secret.lower() in WEAK_JWT_SECRET_KEYS:
            raise RuntimeError("JWT_SECRET_KEY must be a strong random value, not a placeholder or default")


# Create settings instance
settings = Settings()
