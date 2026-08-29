"""LLM model registry, driven entirely by the LiteLLM proxy.

SprintFlow routes 100% of LLM traffic through a single LiteLLM proxy — there are
no direct provider SDKs and no provider-specific credentials anywhere in the
stack. ``ChatOpenAI`` is used purely as an OpenAI-*compatible* HTTP client that
happens to be pointed at the proxy; the model ids are LiteLLM model-group names
(e.g. ``gemini/gemini-3.5-flash``), not OpenAI model ids.

The upstream template hard-coded a list of model names and passed a
``reasoning`` parameter. Both were removed: the model list is now built from
configuration so the proxy's catalogue can change without a code edit, and
``reasoning`` is rejected by most non-OpenAI upstreams behind the proxy.
"""

from typing import (
    Any,
    Dict,
    List,
    cast,
)

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.core.config import settings
from app.core.logging import logger

_API_KEY = SecretStr(settings.OPENAI_API_KEY)


def _build_llm(model_name: str, **overrides: Any) -> ChatOpenAI:
    """Construct a chat model bound to the LiteLLM proxy.

    Args:
        model_name: LiteLLM model-group name, e.g. ``gemini/gemini-3.5-flash``.
        **overrides: Extra kwargs forwarded to ``ChatOpenAI``.

    Returns:
        A configured ``ChatOpenAI`` instance.
    """
    params: Dict[str, Any] = {
        "model": model_name,
        "api_key": _API_KEY,
        "max_completion_tokens": settings.MAX_TOKENS,
        "temperature": settings.DEFAULT_LLM_TEMPERATURE,
    }
    # Explicit base_url beats relying on the OpenAI SDK's OPENAI_BASE_URL
    # fallback, so a missing env var fails loudly instead of silently calling
    # api.openai.com with a LiteLLM key.
    if settings.OPENAI_BASE_URL:
        params["base_url"] = settings.OPENAI_BASE_URL
    params.update(overrides)
    return ChatOpenAI(**params)


def _model_chain() -> List[str]:
    """Return the ordered model chain: default first, then fallbacks.

    Duplicates are removed while preserving order, so a fallback that repeats
    the default doesn't waste a retry slot in the circular fallback loop.
    """
    chain = [settings.DEFAULT_LLM_MODEL, *settings.LLM_FALLBACK_MODELS]
    seen: set[str] = set()
    ordered = [m for m in chain if m and not (m in seen or seen.add(m))]
    return ordered or [settings.DEFAULT_LLM_MODEL]


class LLMRegistry:
    """Registry of available LLM models with pre-initialized instances.

    Entries are built once at import time from configuration. Index 0 is the
    default and the head of the circular fallback chain used by ``LLMService``.
    """

    LLMS: List[Dict[str, Any]] = [{"name": name, "llm": _build_llm(name)} for name in _model_chain()]

    @classmethod
    def get(cls, model_name: str, **kwargs) -> BaseChatModel:
        """Get an LLM by name with optional argument overrides.

        When kwargs are provided a fresh instance is returned with those
        overrides applied, leaving the shared registry entry untouched.

        Args:
            model_name: Name of the model to retrieve.
            **kwargs: Optional arguments to override default model configuration.

        Returns:
            BaseChatModel instance.

        Raises:
            ValueError: If model_name is not found in LLMS.
        """
        model_entry = next((e for e in cls.LLMS if e["name"] == model_name), None)

        if not model_entry:
            available = ", ".join(e["name"] for e in cls.LLMS)
            raise ValueError(f"model '{model_name}' not found in registry. available models: {available}")

        if kwargs:
            base_llm = cast(ChatOpenAI, model_entry["llm"])
            logger.debug(
                "creating_llm_with_custom_args",
                model_name=model_name,
                model=base_llm.model_name,
                custom_args=list(kwargs.keys()),
            )
            return _build_llm(base_llm.model_name, **kwargs)

        return cast(BaseChatModel, model_entry["llm"])

    @classmethod
    def get_all_names(cls) -> List[str]:
        """Return all registered model names in order.

        Returns:
            List of model name strings.
        """
        return [e["name"] for e in cls.LLMS]

    @classmethod
    def get_model_at_index(cls, index: int) -> Dict[str, Any]:
        """Return the model entry at a specific index, wrapping to 0 if out of range.

        Args:
            index: Index into LLMS.

        Returns:
            Model entry dict.
        """
        if 0 <= index < len(cls.LLMS):
            return cls.LLMS[index]
        return cls.LLMS[0]
